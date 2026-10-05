"""Missing companion recovery through the real scheduler, without Telegram/network."""
import asyncio
import json
import importlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/src'))
from tg_game.runtime import executors as ex
from tg_game.storage import Storage, ASC_EXTERNAL_PROVIDER
from tg_game.features.companion import biz_companion_replenish as recovery

CHAT = -1001000000001
FEATURE = 'companion_replenish'


class RecoveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = Storage(Path(self.tmp.name) / 'test.db')
        self.s.init_schema()
        self.pid = self.s.create_profile('demo_alt2_new').id
        self.s.create_chat_binding(self.pid, CHAT, bot_username='fanrenxiuxian_bot')
        self.s.upsert_companion_auto_task(profile_id=self.pid, chat_id=CHAT,
            feature_key=FEATURE, enabled=True, bot_username='fanrenxiuxian_bot')
        self.client = SimpleNamespace(_tg_game_profile_id=self.pid, _tg_game_storage=self.s)
        self.payload = {'companion': {'name': '月婵'}, 'companion_status': '随行中',
                        'dongfu': {'companion_residence': None}}
        self.panel('月婵', True)

    def panel(self, name, attending, second=''):
        text = f'1. 你的红尘道侣: 【{name}】 (状态: {"随行中" if attending else "居于藏娇阁"})\n【第二期机缘】\n- 共历心劫冷却: 可施展\n'
        if second:
            text += f'2. 你的红尘道侣: 【{second}】 (状态: 居于藏娇阁)\n'
        self.reply = {'text': text, 'created_at': time.time() + 1}

    def task(self):
        return self.s.get_companion_auto_task(self.pid, CHAT, FEATURE)

    def commands(self):
        with self.s.connect() as c:
            return [r[0] for r in c.execute('select text from outgoing_commands order by id')]

    async def tick(self):
        self.s.upsert_external_account(self.pid, ASC_EXTERNAL_PROVIDER, '', '', 'connected', '', self.payload, '')
        self.s.update_companion_auto_task(self.task()['id'], next_run_at=0)
        with patch.object(ex, '_refresh_companion_payload', return_value=self.payload), \
             patch.object(ex, '_get_latest_companion_panel_message', return_value=self.reply):
            await ex._run_companion_auto_scheduler(self.client, self.s, run_once=True, include_tianxing=False)

    async def test_one_attending_places_then_searches_once(self):
        await self.tick()
        self.assertEqual(self.commands(), ['.安置侍妾'])
        with self.s.connect() as c:
            c.execute("update outgoing_commands set status='confirmed',updated_at=?", (time.time()-40,))
        self.payload['companion_status'] = '居于藏娇阁'
        self.payload['dongfu']['companion_residence'] = json.dumps([{'name': '月婵'}])
        self.panel('月婵', False)
        await self.tick()
        self.assertEqual(self.commands(), ['.安置侍妾', '.红尘寻缘'])
        await self.tick()
        self.assertEqual(self.commands(), ['.安置侍妾', '.红尘寻缘'])
        self.payload['companion'] = {'name': '董萱儿'}
        self.payload['companion_status'] = '随行中'
        await self.tick()
        self.assertEqual(self.task()['workflow_state'], '')
        await self.tick()
        self.assertEqual(self.commands(), ['.安置侍妾', '.红尘寻缘'])

    async def test_one_resident_searches_without_placing(self):
        self.payload['companion_status'] = '居于藏娇阁'
        self.payload['dongfu']['companion_residence'] = json.dumps([{'name': '月婵'}])
        self.panel('月婵', False)
        await self.tick()
        self.assertEqual(self.commands(), ['.红尘寻缘'])

    async def test_manual_replenish_wins_over_stale_payload(self):
        self.panel('董萱儿', True, '月婵')
        await self.tick()
        self.assertEqual(self.commands(), [])

    async def test_missing_payload_never_means_missing_companion(self):
        self.payload = {}
        self.reply = None
        await self.tick()
        self.assertEqual(self.commands(), [])

    async def test_manual_replenish_cancels_our_pending_command_only(self):
        await self.tick()
        self.s.enqueue_outgoing_command(self.pid, CHAT, '.签到')
        self.panel('董萱儿', True, '月婵')
        await self.tick()
        with self.s.connect() as c:
            rows = [tuple(r) for r in c.execute('select text,status from outgoing_commands order by id')]
        self.assertEqual(rows, [('.安置侍妾', 'failed'), ('.签到', 'pending')])

    async def test_manual_replenish_cancels_command_waiting_in_throttle(self):
        await self.tick()
        with self.s.connect() as c:
            c.execute("update outgoing_commands set status='sending'")
        self.panel('董萱儿', True, '月婵')
        await self.tick()
        with self.s.connect() as c:
            self.assertEqual(c.execute('select status from outgoing_commands').fetchone()[0], 'failed')

    async def test_missing_refresh_releases_lock_and_cancels_unsent_change(self):
        await self.tick()
        self.payload = {}
        await self.tick()
        self.assertFalse(recovery.is_changing_roster(self.s, self.pid))
        with self.s.connect() as c:
            self.assertEqual(c.execute('select status from outgoing_commands').fetchone()[0], 'failed')

    async def test_other_tasks_continue_while_replenishing(self):
        await self.tick()
        self.s.upsert_companion_auto_task(profile_id=self.pid, chat_id=CHAT,
            feature_key='artifact_touch', enabled=True, strategy='7200|.抚摸法宝 青竹蜂云剑')
        await self.tick()
        self.assertIn('.抚摸法宝 青竹蜂云剑', self.commands())

    async def test_unanswered_search_backs_off_without_duplicate(self):
        self.payload['companion_status'] = '居于藏娇阁'
        self.payload['dongfu']['companion_residence'] = json.dumps([{'name': '月婵'}])
        self.panel('月婵', False)
        await self.tick()
        with self.s.connect() as c:
            c.execute("update outgoing_commands set status='needs_manual_confirm',updated_at=?", (time.time()-400,))
        self.s.update_companion_auto_task(self.task()['id'], last_run_at=time.time()-400)
        await self.tick()
        self.assertEqual(self.commands(), ['.红尘寻缘'])
        self.assertGreater(self.task()['next_run_at'], time.time()+3500)
        self.assertFalse(recovery.is_changing_roster(self.s, self.pid))

    async def test_active_heart_prevents_placement(self):
        heart = self.s.upsert_companion_heart_tribulation_task(profile_id=self.pid, chat_id=CHAT, enabled=True)
        self.s.update_companion_heart_tribulation_task(heart['id'], workflow_state='await_tribulation_reply')
        await self.tick()
        self.assertEqual(self.commands(), [])

    async def test_heart_rechecks_roster_lock_after_refresh(self):
        self.s.upsert_companion_heart_tribulation_task(profile_id=self.pid, chat_id=CHAT, enabled=True)
        self.payload['companion']['last_companion_heart_tribulation_time'] = '2026-01-01T00:00:00+00:00'
        def lock_during_refresh(storage, pid):
            storage.update_companion_auto_task(self.task()['id'], workflow_state='replenish_place_wait')
            return self.payload
        with patch.object(ex, '_refresh_companion_payload', side_effect=lock_during_refresh), \
             patch.object(ex, '_resolve_active_companion_voyage_target', return_value=0), \
             patch.object(ex, '_send_companion_heart_tribulation_command', new_callable=AsyncMock) as send, \
             patch.object(ex.asyncio, 'sleep', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await ex._run_companion_heart_tribulation_scheduler(self.client, self.s)
        send.assert_not_called()

    async def test_disabled_during_refresh_never_queues(self):
        def disable(storage, pid):
            storage.disable_companion_auto_task(pid, CHAT, FEATURE)
            return self.payload
        with patch.object(ex, '_refresh_companion_payload', side_effect=disable):
            await recovery.tick(self.s, self.task(), refresh_payload=ex._refresh_companion_payload,
                                get_panel=lambda *a, **kw: self.reply)
        self.assertEqual(self.commands(), [])

    async def test_malformed_residence_is_unknown(self):
        for residence in ('not-json', [None], {'broken': 1}):
            self.payload['dongfu']['companion_residence'] = residence
            await self.tick()
        self.assertEqual(self.commands(), [])

    async def test_disabled_and_pause_are_respected(self):
        self.s.set_runtime_state('automation_paused_at', str(time.time()))
        await self.tick()
        self.assertEqual(self.commands(), [])

    async def test_residence_json_counts_for_command_scope(self):
        self.payload['companion'] = None
        self.payload['dongfu']['companion_residence'] = json.dumps([{'name': '月婵'}])
        self.s.upsert_external_account(self.pid, ASC_EXTERNAL_PROVIDER, '', '', 'connected', '', self.payload, '')
        self.assertTrue(self.s.profile_has_companion(self.pid))

    async def test_return_unsettled_defers_heart(self):
        self.s.upsert_companion_heart_tribulation_task(profile_id=self.pid, chat_id=CHAT, enabled=True)
        task = self.s.get_companion_heart_tribulation_task(self.pid, CHAT)
        text = self.reply['text'] + '\n远航状态: 稳妥航线已归航，待结算（.远航归来）。'
        self.assertTrue(ex._defer_companion_heart_tribulation_if_voyaging(
            self.s, task, text=text, now=time.time(), step='await_panel_reply'))

    async def test_http_enable_and_disable_does_not_search_immediately(self):
        import httpx
        from tg_game.config import get_settings
        self.s.bind_profile_telegram_account(self.pid, telegram_user_id='123', telegram_username='demo_alt2_new')
        self.s.upsert_external_account(self.pid, ASC_EXTERNAL_PROVIDER, '123', 'demo_alt2_new', 'connected', 'session=offline', self.payload, '')
        self.s.disable_companion_auto_task(self.pid, CHAT, FEATURE)
        settings = get_settings()
        with patch.multiple(settings, database_path=self.s.path, authorized_user_id='123', bound_chat_id=None):
            web = importlib.import_module('tg_game.web.app')
            token = self.s.create_app_session(self.pid)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.create_app()), base_url='http://test') as client:
                client.cookies.set(web.APP_SESSION_COOKIE, token)
                form = dict(chat_id=str(CHAT), feature_key=FEATURE, bot_username='fanrenxiuxian_bot')
                response = await client.post('/runtime/commands/companion-auto', data=form)
                self.assertEqual(response.status_code, 303, response.text)
                self.assertTrue(self.task()['enabled'])
                self.assertEqual(self.commands(), [])
                await self.tick()
                self.assertEqual(self.commands(), ['.安置侍妾'])
                response = await client.post('/runtime/commands/companion-auto', data=form)
                self.assertEqual(response.status_code, 303, response.text)
                self.assertFalse(self.task()['enabled'])
                with self.s.connect() as c:
                    self.assertEqual(c.execute('select status from outgoing_commands').fetchone()[0], 'failed')

    async def test_template_compiles_with_new_control(self):
        from jinja2 import Environment, FileSystemLoader
        root = Path(__file__).resolve().parents[1]
        env = Environment(loader=FileSystemLoader(root / 'app/assets/templates'))
        env.get_template('modules/other.html')


if __name__ == '__main__':
    unittest.main()
