"""Exercise real HTTP controls, account isolation, and concurrent worker writes."""
import asyncio
import importlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/src'))
from tg_game.config import get_settings
from tg_game.storage import Storage, CompatDb, ASC_EXTERNAL_PROVIDER
from tg_game.web import profile_automation_settings as ui
from tg_game.features import biz_ldc_red_packet as ldc
from tg_game.features.tianxing import biz_tianxing_runtime as tianxing
from tg_game.runtime import executors as ex
import biz_fanren_game
import biz_sect_game


class ControlsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.storage = Storage(Path(folder.name) / 'test.db')
        self.storage.init_schema()
        db = CompatDb(self.storage)
        try:
            biz_fanren_game.ensure_tables(db)
            biz_sect_game.ensure_tables(db)
        finally:
            db.close()
        self.profile = self.storage.create_profile('测试主号')
        self.other = self.storage.create_profile('测试小号')
        self.pid = self.profile.id
        self.storage.update_profile_sect_info(self.pid, sect_name='天星宗')
        self.chat = -100000000042
        for profile, uid in ((self.profile, '123'), (self.other, '456')):
            self.storage.bind_profile_telegram_account(profile.id, telegram_user_id=uid, telegram_username='test')
            self.storage.create_chat_binding(profile.id, self.chat, thread_id=42, bot_username='fanrenxiuxian_bot')
            self.storage.upsert_external_account(profile.id, ASC_EXTERNAL_PROVIDER, uid, 'test', 'connected', 'session=offline', {}, '')
        settings = get_settings()
        mocked = patch.multiple(settings, database_path=self.storage.path, authorized_user_id='123', bound_chat_id=None)
        mocked.start(); self.addCleanup(mocked.stop)
        self.web = importlib.import_module('tg_game.web.app')

    def state(self, key):
        return json.loads(self.storage.get_runtime_state(f'{key}:{self.pid}') or '{}')

    def save(self, feature, **fields):
        ui.save_settings(self.storage, self.pid, feature, fields)

    def test_http_controls_isolate_profiles_preserve_results_and_validate(self):
        token = self.storage.create_app_session(self.pid)
        self.storage.set_runtime_state(f'fate_cards:{self.pid}', json.dumps({'history': [1], 'date': '2026-10-02', 'status': 'settled'}))
        self.storage.set_runtime_state(f'wild_experience_report:{self.pid}', '{"sent":"2026-10-02","failed_at":123}')
        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.web.create_app()), base_url='http://test') as client:
                response = await client.post('/runtime/automation-settings/fate-cards', data={'enabled': '1'})
                self.assertIn(response.status_code, (303, 401, 403))
                self.assertNotIn('enabled', self.state('fate_cards'))
                client.cookies.set(self.web.APP_SESSION_COOKIE, token)
                for feature, fields in (
                    ('fate-cards', {'choice': 'accept', 'question': 'cultivation'}),
                    ('ldc-red-packet', {'min_total': '300', 'delay_low': '2', 'delay_high': '4'}),
                    ('wild-report', {}), ('stock-schedule', {'interval_minutes': '45'}),
                ):
                    response = await client.post('/runtime/automation-settings/' + feature,
                                                 data={**fields, 'enabled': '1', 'profile_id': str(self.other.id)})
                    self.assertEqual(response.status_code, 303)
                    self.assertIn('settings_saved=1', response.headers['location'])
                view = ui.build_view(self.storage, self.pid)
                self.assertTrue(all(view[k]['enabled'] for k in ('fate', 'ldc', 'wild', 'stock')))
                self.assertEqual(view['fate']['history'], [1])
                self.assertEqual(view['wild']['sent'], '2026-10-02')
                self.assertEqual(view['stock']['interval_minutes'], 45)
                other = ui.build_view(self.storage, self.other.id)
                self.assertFalse(any(other[k].get('enabled') for k in ('fate', 'ldc', 'wild', 'stock')))
                for page, labels in (('other', ('天机命脉', 'LDC 自动抢红包', '停止批量卜筮')),
                                     ('stock', ('股市定时分析',)), ('sect', ('普通闭关前推命',))):
                    response = await client.get('/modules/' + page)
                    self.assertEqual(response.status_code, 200, page)
                    for label in labels: self.assertIn(label, response.text)
                before = self.storage.get_runtime_state(f'ldc_red_packet:{self.pid}')
                response = await client.post('/runtime/automation-settings/ldc-red-packet', data={'enabled': '1', 'min_total': 'nan'})
                self.assertIn('settings_error=', response.headers['location'])
                self.assertEqual(self.storage.get_runtime_state(f'ldc_red_packet:{self.pid}'), before)
                for feature in ('fate-cards', 'ldc-red-packet', 'wild-report', 'stock-schedule'):
                    response = await client.post('/runtime/automation-settings/' + feature, data={})
                    self.assertIn('settings_saved=1', response.headers['location'])
                view = ui.build_view(self.storage, self.pid)
                self.assertFalse(any(view[k].get('enabled') for k in ('fate', 'ldc', 'wild', 'stock')))
                self.assertEqual(self.state('wild_experience_report')['sent'], '2026-10-02')
                self.assertEqual(self.storage.list_outgoing_commands(self.pid), [])
                self.assertEqual((await client.post('/runtime/automation-settings/unknown')).status_code, 404)
        asyncio.run(check())

    def test_automatic_brake_cannot_be_undone_by_stale_form(self):
        self.save('ldc-red-packet', enabled='1')
        ldc._brake(self.storage, self.pid, '未绑定')
        with self.assertRaises(ValueError): self.save('ldc-red-packet', enabled='1')
        state = self.state('ldc_red_packet')
        self.assertFalse(state['enabled'])
        self.save('ldc-red-packet', enabled='1', braked_at=str(state['braked_at']))
        self.assertTrue(self.state('ldc_red_packet')['enabled'])
        self.assertEqual(self.state('ldc_red_packet')['braked'], '未绑定')

    def test_retreat_checkbox_can_disable_without_changing_deep_retreat(self):
        token = self.storage.create_app_session(self.pid)
        tianxing.set_profile_config(self.storage, self.pid, {'deep_retreat_consume_enabled': True})
        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.web.create_app()), base_url='http://test',
                                         cookies={self.web.APP_SESSION_COOKIE: token}) as client:
                fields = {'profile_id': str(self.pid), 'scope': 'risk', 'deep_retreat_consume_enabled': '1'}
                for enabled in (True, False):
                    response = await client.post('/modules/tianxing/config', data={**fields, **({'retreat_farm_enabled': '1'} if enabled else {})})
                    self.assertEqual(response.status_code, 303)
                    config = tianxing.get_profile_record(self.storage, self.pid)['config']
                    self.assertEqual(config['retreat_farm_enabled'], enabled)
                    self.assertTrue(config['deep_retreat_consume_enabled'])
        asyncio.run(check())

    def test_invalid_settings_do_not_change_runtime(self):
        for feature, fields in (
            ('fate-cards', {'choice': 'invalid'}), ('fate-cards', {'question': 'invented'}),
            ('ldc-red-packet', {'delay_low': '5', 'delay_high': '1'}),
            ('ldc-red-packet', {'min_total': '199'}),
            ('stock-schedule', {'interval_minutes': 'inf'}),
            ('stock-schedule', {'interval_minutes': '0'}),
        ):
            with self.assertRaises(ValueError): self.save(feature, enabled='1', **fields)
        self.assertFalse(ui.build_view(self.storage, self.pid)['stock']['enabled'])

    def test_fate_result_does_not_restore_old_settings(self):
        self.save('fate-cards', enabled='1')
        async def launch(*args): return {'ok': True, 'token': 'test', 'init_data': 'test'}
        def run(**kwargs):
            self.save('fate-cards', choice='accept')
            return {'ok': True, 'status': 'settled', 'reward': {'tianjiTrace': 3}}
        with patch.object(ex, 'FATE_CARDS_EARLIEST_TIME', '00:00'), \
             patch.object(ex.fate_cards_miniapp, 'resolve_fate_cards_launch', launch), \
             patch.object(ex.fate_cards_miniapp, 'run_fate_cards_flow', run):
            asyncio.run(ex._run_pending_fate_cards(SimpleNamespace(_tg_game_profile_id=self.pid), self.storage, self.pid))
        state = self.state('fate_cards')
        self.assertFalse(state['enabled'])
        self.assertEqual(state['choice'], 'accept')
        self.assertEqual(state['status'], 'settled')
        self.assertEqual(len(state['history']), 1)

    def test_wild_send_completion_preserves_disabled_switch(self):
        self.save('wild-report', enabled='1')
        async def send(*args): self.save('wild-report')
        with patch.object(ex.wild_experience_miniapp, 'is_completed_today', return_value=True), \
             patch.object(ex.wild_experience_miniapp, 'build_daily_report', return_value='test'):
            asyncio.run(ex._run_wild_experience_report(SimpleNamespace(send_message=send), self.storage, self.pid, {}))
        state = self.state('wild_experience_report')
        self.assertFalse(state['enabled'])
        self.assertTrue(state['sent'])

    def test_batch_controls_start_once_and_cancel_pending(self):
        token = self.storage.create_app_session(self.pid)
        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.web.create_app()), base_url='http://test',
                                         cookies={self.web.APP_SESSION_COOKIE: token}) as client:
                fields = {'chat_id': str(self.chat), 'thread_id': '42', 'target_count': '3'}
                for _ in range(2):
                    response = await client.post('/runtime/commands/divination-batch', data=fields)
                    self.assertEqual(response.status_code, 303)
                self.assertIsNotNone(self.storage.get_active_divination_batch(self.pid, self.chat))
                self.assertEqual(len(self.storage.list_outgoing_commands(self.pid)), 1)
                historical = self.storage.enqueue_outgoing_command(profile_id=self.pid, chat_id=self.chat, text='.卜筮问天', thread_id=42)
                with self.storage.connect() as db:
                    db.execute("UPDATE outgoing_commands SET status='needs_manual_confirm' WHERE id=?", (historical,))
                await client.post('/runtime/commands/divination-batch/cancel', data={'chat_id': str(self.chat)})
                self.assertIsNone(self.storage.get_active_divination_batch(self.pid, self.chat))
                commands = self.storage.list_outgoing_commands(self.pid)
                self.assertTrue(any(row['status'] == 'failed' and row['error_text'] == 'Cancelled by user' for row in commands))
                self.assertTrue(any(row['id'] == historical and row['status'] == 'needs_manual_confirm' for row in commands))
        asyncio.run(check())


if __name__ == '__main__': unittest.main()
