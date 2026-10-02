"""Daily digest time boundaries, factual results, and durable private delivery."""
import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/src"))
from tg_game.game_clock import GAME_TZ
from tg_game.services import daily_task_report as report
from tg_game.storage import Storage, ASC_EXTERNAL_PROVIDER


class DailyReportTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="daily-report-")
        self.storage = Storage(Path(self.folder.name) / "test.db")
        self.storage.init_schema()
        self.pid = self.storage.create_profile("测试号").id
        with self.storage.connect() as db:
            db.execute("UPDATE profiles SET telegram_verified_at=1 WHERE id=?", (self.pid,))
        self.end = datetime(2026, 10, 2, 23, 50, tzinfo=GAME_TZ)
        self.start = self.end - timedelta(days=1)
        self.config = {"enabled": True, "sender_profile_id": self.pid, "time": "23:50", "start_day": "2026-10-02"}
        self.storage.set_runtime_state(report.CONFIG_KEY, json.dumps(self.config))
        self.storage.upsert_external_account(self.pid, ASC_EXTERNAL_PROVIDER, "", "", "connected", "", {}, "")

    def tearDown(self):
        self.folder.cleanup()

    def test_due_time_is_game_timezone_and_does_not_send_historical_on_enable(self):
        self.assertIsNone(report.due_window(self.config, self.end.timestamp() - 1))
        self.assertEqual(report.due_window(self.config, self.end.timestamp()), (self.start, self.end))
        self.assertEqual(report.due_window(self.config, (self.end + timedelta(hours=1)).timestamp()), (self.start, self.end))
        self.assertIsNone(report.due_window({**self.config, "enabled": False}, self.end.timestamp()))

    def test_summary_ignores_stale_payload_and_preserves_unknown_results(self):
        payload = {"dao_name": "测试道友", "tianji_trial": {"miniapp_run": {"status": "settled", "completed_today": 3,
                   "daily_limit": 3, "updated_at": (self.start-timedelta(days=1)).timestamp()}},
                   "luoyun_spirit_tree": {"miniapp_run": {"status": "settlement_unknown", "error": "response timeout",
                   "updated_at": self.end.timestamp()-1}}}
        self.storage.update_external_account_payload(self.pid, ASC_EXTERNAL_PROVIDER, lambda _: payload)
        with self.storage.connect() as db:
            for stamp in (self.start.timestamp()-1, self.start.timestamp(), self.end.timestamp()-1, self.end.timestamp()):
                db.execute("INSERT INTO fishing_casts(profile_id,caught,created_at) VALUES(?,?,?)", (self.pid, 1, stamp))
            db.execute("INSERT INTO outgoing_commands(profile_id,chat_id,text,status,created_at,updated_at) VALUES(?,1,'.查询','needs_manual_confirm',?,?)", (self.pid,self.end.timestamp()-1,self.end.timestamp()-1))
        text = report.build_report(self.storage,self.start,self.end)
        self.assertIn("垂钓 2竿／钓获2", text)
        self.assertNotIn("试炼 3/3", text)
        self.assertIn("灵树：response timeout", text)
        self.assertIn(".查询未确认", text)

    def test_delivery_retry_reuses_message_and_random_id_then_stops(self):
        requests = []
        async def client(request):
            requests.append(request)
            if len(requests)==1:
                raise TimeoutError("lost acknowledgement")
        now = self.end.timestamp()
        self.assertFalse(asyncio.run(report.send_due_report(client,self.storage,self.pid,now=now)))
        self.assertFalse(asyncio.run(report.send_due_report(client,self.storage,self.pid,now=now+10)))
        self.assertTrue(asyncio.run(report.send_due_report(client,self.storage,self.pid,now=now+301)))
        self.assertFalse(asyncio.run(report.send_due_report(client,self.storage,self.pid,now=now+601)))
        self.assertEqual(len(requests),2)
        self.assertEqual(requests[0].random_id,requests[1].random_id)
        self.assertEqual(requests[0].message,requests[1].message)
        self.assertEqual(type(requests[0].peer).__name__,"InputPeerSelf")

    def test_concurrent_delivery_claims_once(self):
        requests = []
        async def client(request):
            requests.append(request)
            await asyncio.sleep(0.02)
        async def run():
            await asyncio.gather(*(report.send_due_report(client,self.storage,self.pid,now=self.end.timestamp()) for _ in range(2)))
        asyncio.run(run())
        self.assertEqual(len(requests),1)

    def test_pending_report_survives_next_daily_boundary(self):
        requests = []
        async def client(request):
            requests.append(request)
            if len(requests) == 1:
                raise TimeoutError("lost acknowledgement")
        now = self.end.timestamp()
        self.assertFalse(asyncio.run(report.send_due_report(client,self.storage,self.pid,now=now)))
        self.assertTrue(asyncio.run(report.send_due_report(client,self.storage,self.pid,now=now+86400)))
        self.assertEqual(requests[0].random_id,requests[1].random_id)
        self.assertEqual(requests[0].message,requests[1].message)
        self.assertTrue(asyncio.run(report.send_due_report(client,self.storage,self.pid,now=now+86401)))
        self.assertNotEqual(requests[1].random_id,requests[2].random_id)

    def test_skipped_and_cancelled_results_are_not_running(self):
        payload = {"pagoda_miniapp": {"run": {"status": "skipped", "updated_at": self.end.timestamp()-1}},
                   "beast_merge": {"run": {"status": "cancelled", "updated_at": self.end.timestamp()-1}}}
        self.storage.update_external_account_payload(self.pid, ASC_EXTERNAL_PROVIDER, lambda _:payload)
        text = report.build_report(self.storage,self.start,self.end)
        self.assertIn("问心塔已跳过",text)
        self.assertIn("虫群已取消",text)
        self.assertNotIn("处理中",text)

    def test_other_profile_and_deployment_drain_never_send(self):
        async def client(request):
            self.fail("unexpected message")
        self.assertFalse(asyncio.run(report.send_due_report(client,self.storage,self.pid+1,now=self.end.timestamp())))
        self.storage.set_runtime_state("deployment_drain","maintenance")
        self.assertFalse(asyncio.run(report.send_due_report(client,self.storage,self.pid,now=self.end.timestamp())))

    def test_error_credentials_are_not_in_digest(self):
        for error in ('Authorization: Bearer private-value', '{"initData": "private-value"}',
                      "{'runToken': 'private-value'}", 'cookie=private-value'):
            self.assertNotIn('private-value',report._short(error))
        self.assertEqual(report._short('The read operation timed out'),'The read operation timed out')

    def test_fate_cards_are_reported_from_runtime_record(self):
        self.storage.set_runtime_state(f'fate_cards:{self.pid}',json.dumps({'status':'settled','updated_at':self.end.timestamp()-1}))
        self.assertIn('命运卡已结算',report.build_report(self.storage,self.start,self.end))

    def test_fate_error_and_recovery_history_survive_later_success(self):
        self.storage.set_runtime_state(f'fate_cards:{self.pid}',json.dumps({
            'status':'failed','updated_at':self.end.timestamp()-1,'last':{'error':'启牌失败，次数未返回'}}))
        payload = {'recovery_history': [
            {'updated_at':self.end.timestamp()-1,'summary':'虫群第4局中断，已消耗但未结算'},
            {'updated_at':self.start.timestamp()-1,'summary':'旧日异常'}]}
        self.storage.update_external_account_payload(self.pid, ASC_EXTERNAL_PROVIDER, lambda _:payload)
        self.storage.upsert_external_account(self.pid, ASC_EXTERNAL_PROVIDER, "", "", "connected", "", {}, "")
        text = report.build_report(self.storage,self.start,self.end)
        self.assertIn('命运卡：启牌失败，次数未返回',text)
        self.assertIn('虫群第4局中断，已消耗但未结算',text)
        self.assertNotIn('旧日异常',text)


if __name__ == "__main__":
    unittest.main()
