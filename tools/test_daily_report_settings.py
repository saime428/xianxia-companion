"""Exercise authenticated settings routes and stop stale sends after a toggle."""
import asyncio
from datetime import datetime
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/src'))
from tg_game.config import get_settings
from tg_game.game_clock import GAME_TZ
from tg_game.services import daily_task_report as report
from tg_game.services import world_boss_report as boss_report
from tg_game.storage import Storage
from tg_game.web import daily_report_settings as settings_ui


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = Storage(Path(self.temp.name)/'test.db')
        self.storage.init_schema()
        self.admin = self.storage.create_profile('主号')
        self.other = self.storage.create_profile('小号')
        for profile, uid in ((self.admin,'123'),(self.other,'456')):
            self.storage.bind_profile_telegram_account(profile.id, telegram_user_id=uid, telegram_username='test')
        settings = get_settings()
        self.patch = patch.multiple(settings, database_path=self.storage.path, authorized_user_id='123', bound_chat_id=None)
        self.patch.start();self.addCleanup(self.patch.stop)
        self.web = importlib.import_module('tg_game.web.app')

    def save(self, **extra):
        settings_ui.save_settings(self.storage, **{'enabled':True,'run_time':'23:50','sender_profile_id':self.admin.id,**extra})

    def test_http_controls_persist_and_require_admin(self):
        admin_token=self.storage.create_app_session(self.admin.id)
        other_token=self.storage.create_app_session(self.other.id)
        async def run():
            app=self.web.create_app()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                denied=await client.post('/admin/daily-task-report',data={'enabled':'1'})
                self.assertIn(denied.status_code,(303,401,403))
                self.assertIsNone(self.storage.get_runtime_state(report.CONFIG_KEY))
                client.cookies.set(self.web.APP_SESSION_COOKIE,other_token)
                self.assertEqual((await client.post('/admin/daily-task-report')).status_code,403)
                client.cookies.set(self.web.APP_SESSION_COOKIE,admin_token)
                fields={'enabled':'1','run_time':'22:30','sender_profile_id':str(self.other.id)}
                self.assertEqual((await client.post('/admin/daily-task-report',data=fields)).status_code,303)
                config=json.loads(self.storage.get_runtime_state(report.CONFIG_KEY))
                self.assertTrue(config['enabled']);self.assertEqual(config['sender_profile_id'],self.other.id)
                page=await client.get('/admin/global-execution')
                self.assertEqual(page.status_code,200)
                self.assertIn('value="22:30"',page.text)
                self.assertIn('任务结果日报',page.text)
                self.assertIn('role="switch" checked',page.text)
                fields.pop('enabled')
                await client.post('/admin/daily-task-report',data=fields)
                self.assertFalse(json.loads(self.storage.get_runtime_state(report.CONFIG_KEY))['enabled'])
                before=self.storage.get_runtime_state(report.CONFIG_KEY)
                for invalid in ({**fields,'enabled':'1','sender_profile_id':'9999'},{**fields,'run_time':'25:61'}):
                    response=await client.post('/admin/daily-task-report',data=invalid)
                    self.assertIn('report_error=',response.headers['location'])
                    self.assertEqual(self.storage.get_runtime_state(report.CONFIG_KEY),before)
                self.assertEqual(self.storage.list_outgoing_commands(self.admin.id),[])
        asyncio.run(run())

    def test_frozen_delivery_blocks_account_change_but_can_disable(self):
        self.save()
        key=report.STATE_PREFIX+'2026-10-02'
        record={'status':'retry_pending','profile_id':self.admin.id,'text':'frozen','random_id':123}
        self.storage.set_runtime_state(key,json.dumps(record))
        with self.assertRaises(ValueError):self.save(sender_profile_id=self.other.id)
        self.save(enabled=False)
        self.assertEqual(json.loads(self.storage.get_runtime_state(key)),record)
        record['status']='sent';self.storage.set_runtime_state(key,json.dumps(record))
        self.save(sender_profile_id=self.other.id)
        self.assertEqual(json.loads(self.storage.get_runtime_state(key)),record)

    def test_disabling_while_report_is_built_prevents_delivery(self):
        self.save()
        now=datetime.now(GAME_TZ).replace(hour=23,minute=51,second=0).timestamp()
        def build(*args):
            self.save(enabled=False)
            return 'must not send'
        async def client(request):self.fail('disabled report sent')
        with patch.object(report,'build_report',side_effect=build):
            self.assertFalse(asyncio.run(report.send_due_report(client,self.storage,self.admin.id,now=now)))
        with self.storage.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM app_runtime_state WHERE key GLOB 'daily_task_report:*'").fetchone()[0],0)

    def test_pending_battle_report_blocks_account_change_until_sent(self):
        self.save()
        key = boss_report.STATE_PREFIX + '10:100'
        for status in ('prepared', 'sending', 'retry_pending'):
            with self.subTest(status=status):
                record = {
                    'status': status, 'profile_id': self.admin.id,
                    'text': 'frozen battle report', 'random_id': 123,
                }
                self.storage.set_runtime_state(key, json.dumps(record))
                before = self.storage.get_runtime_state(report.CONFIG_KEY)
                with self.assertRaisesRegex(ValueError, '待确认'):
                    self.save(sender_profile_id=self.other.id)
                self.assertEqual(self.storage.get_runtime_state(report.CONFIG_KEY), before)
                self.save(enabled=False)
                self.assertFalse(json.loads(self.storage.get_runtime_state(report.CONFIG_KEY))['enabled'])
                self.assertEqual(json.loads(self.storage.get_runtime_state(key)), record)
                self.save()
        record['status'] = 'sent'
        self.storage.set_runtime_state(key, json.dumps(record))
        self.save(sender_profile_id=self.other.id)
        self.assertEqual(json.loads(self.storage.get_runtime_state(report.CONFIG_KEY))['sender_profile_id'], self.other.id)
        self.assertEqual(json.loads(self.storage.get_runtime_state(key)), record)


if __name__=='__main__':unittest.main()
