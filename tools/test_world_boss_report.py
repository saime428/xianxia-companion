"""After-battle reports: announcement timing, account scope and durable delivery."""
import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/src"))
from tg_game.game_clock import GAME_TZ
from tg_game.services import daily_task_report as daily
from tg_game.services import world_boss_report as report
from tg_game.storage import Storage, ASC_EXTERNAL_PROVIDER


class WorldBossReportTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="boss-report-")
        self.storage = Storage(Path(self.folder.name) / "test.db")
        self.storage.init_schema()
        self.start = datetime(2026, 10, 5, 13, 40, tzinfo=GAME_TZ).timestamp()
        self.pids = []
        for name in ("one", "two", "three"):
            pid = self.storage.create_profile(name).id
            self.pids.append(pid)
            self.storage.upsert_external_account(pid, ASC_EXTERNAL_PROVIDER, "", name, "connected", "", {}, "")
            with self.storage.connect() as db:
                db.execute("UPDATE profiles SET telegram_verified_at=1 WHERE id=?", (pid,))
                db.execute("INSERT INTO companion_auto_tasks(profile_id,chat_id,feature_key,enabled,created_at,updated_at) VALUES(?,10,'world_boss',1,?,?)", (pid,self.start,self.start))
                db.execute("INSERT INTO bound_messages(profile_id,chat_id,message_id,direction,is_bot,text,created_at,updated_at) VALUES(?,10,100,'incoming',1,'【世界通告｜真仙试锋开启】',?,?)", (pid,self.start,self.start))
            event = {"message_id": 100, "fingerprint": "a" * 64, "status": "completed", "started_at": self.start,
                     "updated_at": self.start+170, "identity_results": [{"settlement_confirmed": True,
                     "hit_count": 16, "perfect_count": 11, "damage_yi_total": 638362624}]}
            self.storage.set_runtime_state(f"world_boss_state:{pid}", json.dumps({"world_boss_events": [event]}))
        self.config = {"enabled": True, "sender_profile_id": self.pids[0], "time": "23:50",
                       "start_day": "2026-10-02", "world_boss_after_battle": True,
                       "world_boss_start_day": "2026-10-05"}
        self.storage.set_runtime_state(daily.CONFIG_KEY, json.dumps(self.config))
        self.notice = "\n".join(["【世界通告｜真仙试锋败退】", "战果", "- 结果：天道败退", "- 参战：51 人",
                                 "贡献榜", "3. @one - 3670 分｜强攻 16｜伤害 6.67亿亿", "保底结算",
                                 "- @one：伐仙功 +7，修为 +5670", "- @one2：修为 +999"])

    def tearDown(self):
        self.folder.cleanup()

    def notice_at(self, stamp=None, chat=10, message=200):
        stamp = self.start+420 if stamp is None else stamp
        with self.storage.connect() as db:
            for pid in self.pids:
                db.execute("INSERT INTO bound_messages(profile_id,chat_id,message_id,direction,is_bot,text,created_at,updated_at) VALUES(?,?,?,'incoming',1,?,?,?)", (pid,chat,message,self.notice,stamp,stamp))

    def test_waits_for_world_result_not_personal_finish(self):
        self.assertIsNone(report.next_report(self.storage, self.pids[0], self.start+180))
        self.notice_at()
        candidate = report.next_report(self.storage, self.pids[0], self.start+421)
        text = candidate[2]["text"]
        self.assertIn("2026-10-05 13:40", text)
        self.assertIn("天道败退", text)
        self.assertIn("第3名", text)
        self.assertIn("修为 +5670", text)
        self.assertIn("命中16、完美11", text)
        self.assertIn("@two", text)
        self.assertIn("@three", text)
        self.assertIn("本号未列出", text)
        self.assertNotIn("999", text)
        self.assertLess(len(text.encode("utf-16-le")), 8192)

    def test_missing_notice_reports_unknown_after_deadline(self):
        self.notice_at(chat=11)
        self.assertIsNone(report.next_report(self.storage, self.pids[0], self.start+899))
        text = report.next_report(self.storage, self.pids[0], self.start+901)[2]["text"]
        self.assertIn("未见战果公告", text)
        self.assertNotIn("天道败退", text)
        self.assertNotIn("未进前十", text)

    def test_old_notice_and_disabled_or_wrong_sender_do_not_send(self):
        self.notice_at(stamp=self.start-86400, message=199)
        self.assertIsNone(report.next_report(self.storage, self.pids[0], self.start+500))
        self.notice_at()
        self.assertIsNone(report.next_report(self.storage, self.pids[1], self.start+500))
        for change in ({"enabled": False}, {"world_boss_after_battle": False}, {"world_boss_start_day": "2026-10-06"}):
            self.storage.set_runtime_state(daily.CONFIG_KEY, json.dumps({**self.config, **change}))
            self.assertIsNone(report.next_report(self.storage, self.pids[0], self.start+500))

    def test_immediate_mode_removes_boss_from_evening_only(self):
        end = datetime.fromtimestamp(self.start, GAME_TZ).replace(hour=23, minute=50)
        self.notice_at()
        self.assertNotIn("青元子", daily.build_report(self.storage, end-timedelta(days=1), end))
        self.storage.set_runtime_state(daily.CONFIG_KEY, json.dumps({**self.config, "world_boss_after_battle": False}))
        self.assertIn("青元子已结算", daily.build_report(self.storage, end-timedelta(days=1), end))

    def test_retry_freezes_report_across_day_and_concurrent_workers(self):
        self.notice_at()
        now = self.start+500
        candidate = report.next_report(self.storage, self.pids[0], now)
        requests = []
        async def client(request):
            requests.append(request)
            if len(requests) == 1:
                raise TimeoutError("lost ack")
            await asyncio.sleep(0.01)
        self.assertFalse(asyncio.run(report.send_report(client, self.storage, self.pids[0], candidate, now=now)))
        self.assertIsNone(report.next_report(self.storage, self.pids[0], now+1))
        self.storage.set_runtime_state(f"world_boss_state:{self.pids[0]}", "{}")
        later = now+86400
        retry = report.next_report(self.storage, self.pids[0], later)
        async def concurrent():
            await asyncio.gather(*(report.send_report(client, self.storage, self.pids[0], retry, now=later) for _ in range(2)))
        asyncio.run(concurrent())
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].random_id, requests[1].random_id)
        self.assertEqual(requests[0].message, requests[1].message)
        self.assertEqual(type(requests[0].peer).__name__, "InputPeerSelf")
        self.assertIsNone(report.next_report(self.storage, self.pids[0], later+301))

    def test_drain_and_config_change_prevent_delivery(self):
        self.notice_at()
        candidate = report.next_report(self.storage, self.pids[0], self.start+500)
        async def client(request):
            self.fail("unexpected delivery")
        self.storage.set_runtime_state("deployment_drain", "maintenance")
        self.assertFalse(asyncio.run(report.send_report(client,self.storage,self.pids[0],candidate,now=self.start+500)))
        self.storage.set_runtime_state("deployment_drain", "0")
        self.storage.set_runtime_state(daily.CONFIG_KEY, json.dumps({**self.config, "world_boss_after_battle": False}))
        self.assertFalse(asyncio.run(report.send_report(client,self.storage,self.pids[0],candidate,now=self.start+502)))

    def test_missing_account_and_sensitive_error_are_not_invented_as_success(self):
        self.notice_at()
        self.storage.set_runtime_state(f"world_boss_state:{self.pids[1]}", "{}")
        state = json.loads(self.storage.get_runtime_state(f"world_boss_state:{self.pids[2]}"))
        state["world_boss_events"][0].update(status="failed", error="qyz_private_value")
        self.storage.set_runtime_state(f"world_boss_state:{self.pids[2]}", json.dumps(state))
        text = report.next_report(self.storage, self.pids[0], self.start+500)[2]["text"]
        self.assertEqual(text.count("个人已结算"), 1)
        self.assertIn("未记录本号参战结果", text)
        self.assertNotIn("private_value", text)

    def test_rare_drop_is_flagged_under_the_header(self):
        self.notice += "\n珍稀掉落\n- @one 获得 【衍神玉简】x1\n- @two 获得 【四级妖丹】x3"
        self.notice_at()
        lines = report.next_report(self.storage, self.pids[0], self.start+421)[2]["text"].splitlines()
        self.assertEqual(lines[3], "🎁 稀有掉落：@one 【衍神玉简】")
        self.assertTrue(next(l for l in lines if "衍神玉简" in l and "奖励：" in l).startswith("🎁 （天道败退）；奖励："))
        self.assertTrue(next(l for l in lines if "四级妖丹" in l).startswith("（天道败退）；奖励："))

    def test_late_world_announcement_still_includes_rewards(self):
        self.notice_at(stamp=self.start+1000)
        text = report.next_report(self.storage,self.pids[0],self.start+1001)[2]["text"]
        self.assertIn("修为 +5670", text)

    def test_duplicate_random_id_is_delivery_confirmation(self):
        self.notice_at()
        candidate = report.next_report(self.storage, self.pids[0], self.start+500)
        class RandomIdDuplicateError(Exception):
            pass
        async def client(request):
            raise RandomIdDuplicateError()
        self.assertTrue(asyncio.run(report.send_report(client,self.storage,self.pids[0],candidate,now=self.start+500)))
        self.assertIsNone(report.next_report(self.storage,self.pids[0],self.start+900))

    def test_changing_task_room_cannot_resend_old_event(self):
        self.notice_at()
        candidate = report.next_report(self.storage,self.pids[0],self.start+500)
        async def client(request):
            pass
        self.assertTrue(asyncio.run(report.send_report(client,self.storage,self.pids[0],candidate,now=self.start+500)))
        with self.storage.connect() as db:
            db.execute("UPDATE companion_auto_tasks SET chat_id=11")
        self.notice = self.notice.replace("天道败退", "别群胜利").replace("+5670", "+999")
        self.notice_at(chat=11)
        self.assertIsNone(report.next_report(self.storage,self.pids[0],self.start+501))

    def test_new_event_room_survives_message_cleanup_and_legacy_never_guesses(self):
        with self.storage.connect() as db:
            db.execute("DELETE FROM bound_messages")
            db.execute("UPDATE companion_auto_tasks SET chat_id=11")
        self.assertIsNone(report.next_report(self.storage,self.pids[0],self.start+901))
        for pid in self.pids:
            state=json.loads(self.storage.get_runtime_state(f"world_boss_state:{pid}"))
            state["world_boss_events"][0]["chat_id"]=10
            self.storage.set_runtime_state(f"world_boss_state:{pid}",json.dumps(state))
        self.notice_at()
        self.notice = self.notice.replace("天道败退", "别群胜利").replace("+5670", "+999")
        self.notice_at(chat=11)
        text = report.next_report(self.storage,self.pids[0],self.start+500)[2]["text"]
        self.assertIn("修为 +5670",text)
        self.assertNotIn("别群胜利",text)

    def test_legacy_missing_own_notice_does_not_borrow_unrelated_room_message(self):
        with self.storage.connect() as db:
            db.execute("DELETE FROM bound_messages WHERE profile_id=?", (self.pids[0],))
            db.execute("UPDATE bound_messages SET chat_id=11 WHERE message_id=100")
        for pid in self.pids[1:]:
            self.storage.set_runtime_state(f"world_boss_state:{pid}", "{}")
        self.notice_at(chat=11)
        self.assertIsNone(report.next_report(self.storage,self.pids[0],self.start+901))

    def test_same_entry_fingerprint_recovers_other_profiles_without_own_notice(self):
        with self.storage.connect() as db:
            db.execute("DELETE FROM bound_messages WHERE profile_id!=?", (self.pids[0],))
        self.notice_at()
        text=report.next_report(self.storage,self.pids[0],self.start+500)[2]["text"]
        self.assertEqual(text.count("个人已结算"),3)


if __name__ == "__main__":
    unittest.main()
