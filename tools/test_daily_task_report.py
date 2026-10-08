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
        self.assertIn("核对 .查询（结果未确认", text)

    def test_actionable_digest_rewards_and_rift_results(self):
        stamp = self.end.timestamp()-1
        payload = {"tianji_trial": {"miniapp_run": {"status": "settled", "updated_at": stamp, "reward_trace": 4}},
                   "beast_merge": {"run": {"status": "completed", "updated_at": stamp,
                   "total_trace": 6, "trace_balance": 999}},
                   "pagoda_miniapp": {"run": {"status": "settled", "updated_at": stamp,
                   "replay": {"rewardLines": ["修为 +100"]}}}}
        self.storage.update_external_account_payload(self.pid, ASC_EXTERNAL_PROVIDER, lambda _: payload)
        self.storage.set_runtime_state(f'fate_cards:{self.pid}', json.dumps({
            'status': 'settled', 'updated_at': stamp,
            'last': {'reward': {'tianjiTrace': 2, 'kunwuPass': 1, 'balance': 999}}}))
        with self.storage.connect() as db:
            for command, status in ((".查询", "confirmed"), (".自动重试", "pending"), (".签到", "failed")):
                db.execute("INSERT INTO outgoing_commands(profile_id,chat_id,text,status,created_at,updated_at) VALUES(?,1,?,?,?,?)", (self.pid,command,status,stamp,stamp))
            for event, when, message, value in (
                ('bot_reply_received', stamp, 1, '探寻裂缝失败，损失修为 50'),
                ('success', stamp, 1, '探寻裂缝失败，损失修为 50'),
                ('success', self.start.timestamp(), 2, '探寻裂缝获得修为 +200'),
                ('success', self.start.timestamp()-1, 3, '旧探缝结果'),
                ('success', self.end.timestamp(), 4, '下一时段探缝结果')):
                db.execute("INSERT INTO rift_execution_logs(profile_id,chat_id,event_type,message_id,text,created_at) VALUES(?,1,?,?,?,?)", (self.pid,event,message,value,when))
        text = report.build_report(self.storage, self.start, self.end)
        self.assertIn('核对 .签到（发送失败）', text)
        self.assertIn('天机残痕 +6（最近批次）', text)
        self.assertIn('天机残痕 +4（最近批次）', text)
        self.assertIn('修为 +100', text)
        self.assertIn('天机残痕 +2、昆吾通行令 +1', text)
        self.assertEqual(text.count('损失修为 50'), 1)
        self.assertIn('探寻裂缝获得修为 +200', text)
        for absent in ('群指令', '已确认', '.自动重试', '.查询', '999', '旧探缝结果', '下一时段探缝结果'):
            self.assertNotIn(absent, text)

    def test_rare_drops_are_flagged_at_top_and_inline(self):
        stamp = self.end.timestamp() - 1
        payload = {"pagoda_miniapp": {"run": {"status": "settled", "updated_at": stamp, "state": {"todayHighest": 9},
                   "replay": {"rewardLines": ["获得了【大衍灵傀图谱】x1", "获得了【庚金砂】x2"]}}},
                   "wild_experience_miniapp": {"run": {"status": "completed", "updated_at": stamp,
                   "attempts": [{"cultivation_delta": 1, "loot": [{"name": "庚金", "quantity": 1}]}]}}}
        self.storage.update_external_account_payload(self.pid, ASC_EXTERNAL_PROVIDER, lambda _: payload)
        with self.storage.connect() as db:
            for message, value in ((1, "【激战得胜】你从其残骸中，获得了【四级妖丹】x5，以及一件至宝：【庚金】！"),
                                   (2, "【探寻成功】你的元婴满载而归，为你带来了：【法则碎片·土】, 一份意外之喜 【九天神雷木】！")):
                db.execute("INSERT INTO rift_execution_logs(profile_id,chat_id,event_type,message_id,text,created_at) VALUES(?,1,'success',?,?,?)", (self.pid, message, value, stamp))
        lines = report.build_report(self.storage, self.start, self.end).splitlines()
        self.assertEqual(lines[2], "🎁 稀有掉落：测试号 【大衍灵傀图谱】、【庚金】")
        for start in ("问心塔", "野外", "探缝 "):
            self.assertTrue(any(l.startswith("🎁 " + start) for l in lines), start)
        self.assertTrue(next(l for l in lines if "九天神雷木" in l).startswith("探缝"))  # game says 意外之喜, data says common

    def test_world_boss_reward_comes_from_result_notice(self):
        start = self.end.timestamp() - 3600
        self.storage.upsert_external_account(self.pid, ASC_EXTERNAL_PROVIDER, "", "me", "connected", "", {}, "")
        identity = {"settlement_confirmed": True, "hit_count": 16, "perfect_count": 12}
        event = {"status": "completed", "started_at": start, "completed_at": start + 160, "updated_at": start + 160,
                 "identity_results": [identity]}
        self.storage.set_runtime_state(f"world_boss_state:{self.pid}", json.dumps({"world_boss_events": [event]}))
        notice = "\n".join(["【世界通告｜真仙试锋功成】", "战果", "- 结果：伐仙功成", "贡献榜", "1. @me - 3760 分",
                            "称号授予", "- @me 获得称号徽章 【斩青元者】", "奖励结算",
                            "- @me：伐仙功 +19，修为 +12520，新称号 【斩青元者】", "- @me2：修为 +1",
                            "珍稀掉落", "- @me 获得 【衍神玉简】x1", "- @me 获得 【四级妖丹】x3"])
        with self.storage.connect() as db:
            for profile in (self.pid, self.pid + 1):  # each profile keeps its own copy of the notice
                db.execute("INSERT INTO bound_messages(profile_id,chat_id,message_id,direction,is_bot,text,created_at,updated_at) VALUES(?,1,9,'incoming',1,?,?,?)", (profile, notice, start + 240, start + 240))
        text = report.build_report(self.storage, self.start, self.end)
        self.assertIn("命中16、完美12（伐仙功成）；奖励：伐仙功 +19，修为 +12520，新称号 【斩青元者】；获得 【衍神玉简】x1", text)
        self.assertIn("🎁 稀有掉落：测试号 【衍神玉简】\n", text)
        self.assertNotIn("称号徽章", text)
        self.assertNotIn("修为 +1\n", text + "\n")
        self.storage.upsert_external_account(self.pid, ASC_EXTERNAL_PROVIDER, "", "other", "connected", "", {}, "")
        self.assertIn("本号未列出", report.build_report(self.storage, self.start, self.end))
        with self.storage.connect() as db:
            db.execute("DELETE FROM bound_messages")
        self.assertIn("奖励：未见结果公告", report.build_report(self.storage, self.start, self.end))

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

    def test_rift_uses_edited_settlement_and_excludes_other_players(self):
        stamp = self.end.timestamp()-1
        with self.storage.connect() as db:
            db.execute("INSERT INTO rift_execution_logs(profile_id,chat_id,event_type,message_id,text,created_at) VALUES(?,1,'success',11,?,?)", (self.pid,'将元婴送入其中探寻机缘...',stamp-10))
            for message, reply, direction, bot, value in (
                (10, None, 'outgoing', 0, '.探寻裂缝'),
                (11, 10, 'incoming', 1, '【探寻成功】获得了：【测试碎片】！'),
                (20, None, 'incoming', 0, '.探寻裂缝'),
                (21, 20, 'incoming', 1, '【探寻成功】别人的奖励'),
                (30, None, 'outgoing', 0, '.探寻裂缝'),
                (31, 30, 'incoming', 1, '【遭遇风暴】修为倒退了 50 点！')):
                db.execute("INSERT INTO bound_messages(profile_id,chat_id,message_id,reply_to_msg_id,direction,is_bot,text,created_at,updated_at) VALUES(?,1,?,?,?,?,?,?,?)", (self.pid,message,reply,direction,bot,value,stamp-10,stamp))
        text = report.build_report(self.storage,self.start,self.end)
        self.assertEqual(text.count('测试碎片'),1)
        self.assertIn('修为倒退了 50 点',text)
        self.assertNotIn('别人的奖励',text)
        self.assertNotIn('送入其中',text)

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
