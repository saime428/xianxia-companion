"""Offline money limits, owner association, edits, expiry and daily report checks."""
import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app/src'))
from tg_game.features.market import stone_gambling as s
from tg_game.storage import Storage,ASC_EXTERNAL_PROVIDER
from tg_game.services import daily_task_report as report
from tg_game.telegram import runtime,send_utils

CHAT=-100000001
PANEL='【赌石坊 · 选石】\n'+ '\n\n'.join(f'{n}号原石 (售价: {p} 灵石)\n📝 *描述{n}*' for n,p in [(1,4500),(2,3500),(3,4000)])+'\n\n请在 2分钟 内回复 .切 <编号>'

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.storage=Storage(Path(self.tmp.name)/'test.db');self.storage.init_schema()
        self.pid=self.storage.create_profile('test').id
        self.now=datetime(2026,10,13,3,10,tzinfo=s.TZ).timestamp()
        self.db=s.connect(Path(self.tmp.name)/'test.db')
        with self.db:
            self.db.execute('UPDATE profiles SET telegram_user_id=?,telegram_verified_at=1 WHERE id=?',('100',self.pid))
        s.ensure_schema(self.db)
        self.storage.set_runtime_state(f'telegram_worker_heartbeat:{self.pid}',str(self.now))
        with self.db:
            self.db.execute('INSERT INTO stone_auto_config VALUES(?,?,NULL,?,1,5000,5500,?)',(self.pid,CHAT,'03:10','2026-10-13'))
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def msg(self,mid,text,uid=100,user='player',reply=None,at=None,thread=None):
        at=self.now if at is None else at
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO recorded_messages(chat_id,message_id,thread_id,reply_to_msg_id,sender_id,sender_username,is_bot,text,created_at,updated_at) VALUES(?,?,?,?,?,?,0,?,?,?)',(CHAT,mid,thread,reply,uid,user,text,at,at))
    def collect(self,at=None):
        with self.db:s.sync_messages(self.db,at or self.now+1);s.rebuild_rounds(self.db)
    def tick(self,at=None):
        with self.db:self.db.execute('BEGIN IMMEDIATE');s.tick_auto(self.db,at or self.now)
    def commands(self):return self.db.execute('SELECT * FROM outgoing_commands ORDER BY id').fetchall()
    def sent(self,cid,mid):
        with self.db:self.db.execute("UPDATE outgoing_commands SET sent_message_id=?,status='awaiting_confirm' WHERE id=?",(mid,cid))
    def entry_panel(self):
        self.tick();self.sent(self.commands()[0]['id'],10)
        self.msg(10,'.赌石');self.msg(11,'💰 支付成功 (灵石 -500)',900,'hantianzun1_bot',10,self.now+1)
        self.msg(12,PANEL,900,'hantianzun1_bot',10,self.now+2)
        self.collect(self.now+3);self.tick(self.now+3);self.tick(self.now+4)
    def test_parse_and_refuse_malformed_panel(self):
        self.assertEqual([x['price'] for x in s.parse_panel(PANEL)],[4500,3500,4000])
        self.assertFalse(s.parse_panel(PANEL.replace('3号原石','2号原石')))
    def test_interleaved_players_results_edits_dedup_and_spoof(self):
        self.msg(10,'.赌石');self.msg(11,'.赌石',200,'other')
        self.msg(12,PANEL,900,'hantianzun1_bot',10,self.now+1)
        self.msg(13,PANEL,901,'hantianzun2_bot',11,self.now+2)
        self.msg(14,'.切 1',at=self.now+3);self.msg(15,'.切 2',200,'other',at=self.now+3)
        self.msg(16,'【大涨！】\n【庚金】x1',999,'pretend',14)
        self.msg(17,'【小涨！】\n【百年铁木】x4',900,'hantianzun1_bot',14,self.now+3)
        self.msg(18,'【垮了！】',901,'hantianzun2_bot',15,self.now+4)
        self.collect();self.collect()
        rows=self.db.execute('SELECT * FROM stone_rounds ORDER BY entry_id').fetchall()
        self.assertEqual(len(rows),2);self.assertEqual([r['outcome'] for r in rows],['小涨','垮了'])
        self.msg(17,'【大涨！】\n【庚金】x1',900,'hantianzun1_bot',14,self.now+5)
        self.collect(self.now+6)
        self.assertEqual(self.db.execute('SELECT outcome FROM stone_rounds WHERE entry_id=10').fetchone()[0],'大涨')
    def test_new_entry_without_panel_does_not_match_previous(self):
        self.msg(10,'.赌石');self.msg(11,PANEL,900,'hantianzun1_bot',10)
        self.msg(12,'.赌石');self.msg(13,'.切 1');self.msg(14,'【垮了！】',900,'hantianzun1_bot',13)
        self.collect();self.assertEqual(self.db.execute("SELECT count(*) FROM stone_rounds WHERE link_status='matched'").fetchone()[0],0)
    def test_cheapest_once_per_day_atomic_restart_and_result(self):
        self.entry_panel()
        self.assertEqual([c['text'] for c in self.commands()],['.赌石','.切 2'])
        self.assertEqual(self.commands()[1]['reply_to_msg_id'],12)
        for _ in range(3):self.tick(self.now+5)
        self.assertEqual(len(self.commands()),2)
        self.sent(self.commands()[1]['id'],13);self.msg(13,'.切 2',reply=12,at=self.now+6)
        self.msg(14,'【大涨！】\n【三级妖丹】x1',900,'hantianzun1_bot',13,self.now+7)
        self.collect(self.now+8);self.tick(self.now+8);self.tick(self.now+9)
        self.assertEqual(self.db.execute('SELECT state,reserved FROM stone_auto_runs').fetchone()[:],('completed',4000))
        self.tick(self.now+10);self.assertEqual(len(self.commands()),2)
    def test_budget_guard_keeps_only_entry_fee(self):
        with self.db:self.db.execute('UPDATE stone_auto_config SET daily_budget=3000')
        self.entry_panel();self.assertEqual(len(self.commands()),1)
        row=self.db.execute('SELECT state,reserved FROM stone_auto_runs').fetchone()
        self.assertEqual(row[:],('stopped',500))
    def test_timeout_disables_and_never_retries(self):
        self.tick();self.tick(self.now+61)
        self.assertEqual(self.commands()[0]['status'],'failed')
        self.assertEqual(self.db.execute('SELECT enabled FROM stone_auto_config').fetchone()[0],0)
        self.tick(self.now+86400);self.assertEqual(len(self.commands()),1)
    def test_send_guard_expiry_even_without_worker(self):
        self.tick()
        with self.db:self.db.execute("UPDATE outgoing_commands SET status='sending'")
        s.guard_command(self.storage,self.commands()[0]['id'],self.now+5)
        with self.assertRaises(RuntimeError):s.guard_command(self.storage,self.commands()[0]['id'],self.now+61)
    def test_send_guard_manual_override(self):
        self.entry_panel()
        cid=self.commands()[1]['id']
        with self.db:self.db.execute("UPDATE outgoing_commands SET status='sending' WHERE id=?",(cid,))
        s.guard_command(self.storage,cid,self.now+5)
        self.msg(13,'.赌石',at=self.now+6)
        with self.assertRaises(RuntimeError):s.guard_command(self.storage,cid,self.now+7)
    def test_pause_offline_and_missed_window(self):
        self.storage.set_runtime_state('automation_paused_at',str(self.now))
        self.tick();self.assertFalse(self.commands())
        self.storage.set_runtime_state('automation_paused_at','0')
        self.tick(self.now+1000);self.assertFalse(self.commands())
    def test_manual_gamble_counts_as_daily_attempt(self):
        self.msg(10,'.赌石',at=self.now-60);self.collect()
        self.tick();self.assertFalse(self.commands())
        self.assertEqual(self.db.execute('SELECT state FROM stone_auto_runs').fetchone()[0],'skipped')
    def test_daily_report_cost_items_and_unknown_without_profit_claim(self):
        self.storage.upsert_external_account(self.pid,ASC_EXTERNAL_PROVIDER,'','','connected','',{},'')
        self.msg(10,'.赌石');self.msg(11,'💰 支付成功 (灵石 -500)',900,'hantianzun1_bot',10)
        self.msg(12,PANEL,900,'hantianzun1_bot',10)
        self.msg(13,'.切 1');self.msg(14,'【小涨！】\n【百年铁木】x4',900,'hantianzun1_bot',13)
        self.collect()
        start=datetime.fromtimestamp(self.now-60,s.TZ);end=start+timedelta(days=1)
        text=report.build_report(self.storage,start,end)
        self.assertIn('【百年铁木】×4',text);self.assertIn('花费5000灵石',text)
        self.assertNotIn('盈利',text)
    def test_long_term_records_survive_source_retention_cleanup(self):
        self.msg(10,'.赌石');self.msg(11,PANEL,900,'hantianzun1_bot',10)
        self.msg(12,'.切 1');self.msg(13,'【垮了！】',900,'hantianzun1_bot',12)
        self.collect()
        with self.db:self.db.execute('DELETE FROM recorded_messages')
        self.collect(self.now+3*86400)
        self.assertEqual(self.db.execute('SELECT outcome FROM stone_rounds').fetchone()[0],'垮了')
    def test_cut_acknowledgment_does_not_stop_before_edited_result(self):
        self.entry_panel();self.sent(self.commands()[1]['id'],13)
        self.msg(13,'.切 2',reply=12,at=self.now+5)
        self.msg(14,'正在切石，请稍候...',900,'hantianzun1_bot',13,self.now+6)
        self.collect(self.now+7);self.tick(self.now+7);self.tick(self.now+8)
        self.assertEqual(self.db.execute('SELECT state FROM stone_auto_runs').fetchone()[0],'await_result')
        self.msg(14,'【垮了！】',900,'hantianzun1_bot',13,self.now+9)
        self.collect(self.now+10);self.tick(self.now+10)
        self.assertEqual(self.db.execute('SELECT state FROM stone_auto_runs').fetchone()[0],'completed')
    def test_manual_entry_before_automatic_send_cancels(self):
        self.tick();self.msg(10,'.赌石',at=self.now+1)
        with self.assertRaises(RuntimeError):s.guard_command(self.storage,self.commands()[0]['id'],self.now+2)
    def test_changed_offered_price_is_rejected_before_send(self):
        self.entry_panel()
        self.msg(12,PANEL.replace('3500','6000'),900,'hantianzun1_bot',10,self.now+5)
        with self.assertRaises(RuntimeError):s.guard_command(self.storage,self.commands()[1]['id'],self.now+6)
    def test_missing_bot_metadata_is_linked_by_known_bot_id(self):
        self.msg(10,'.赌石');self.msg(11,'💰 支付成功 (灵石 -500)',900,'hantianzun1_bot',10)
        self.msg(12,PANEL,900,'',10);self.msg(13,'.切 1')
        self.msg(14,'【垮了！】',900,'',13);self.collect()
        self.assertEqual(self.db.execute('SELECT outcome FROM stone_rounds').fetchone()[0],'垮了')

    def test_stone_guard_is_called_at_final_queue_send_check(self):
        self.tick();cid=self.commands()[0]['id']
        with self.db:self.db.execute("UPDATE outgoing_commands SET status='sending'")
        with patch.object(s,'guard_command',side_effect=RuntimeError('expired')):
            with self.assertRaisesRegex(RuntimeError,'expired'):
                send_utils._ensure_send_allowed(self.storage,self.pid,'.赌石',guard_network_pause=False,outgoing_command_id=cid)

    def test_silent_cut_retries_after_30_seconds_once_even_after_restart(self):
        self.entry_panel();first=self.commands()[1]['id'];self.sent(first,13)
        self.msg(13,'.切 2',reply=12,at=self.now+5)
        self.collect(self.now+6);self.tick(self.now+6)
        self.storage.set_runtime_state(f'telegram_worker_heartbeat:{self.pid}',str(self.now+35))
        self.tick(self.now+34);self.assertEqual(len(self.commands()),2)
        self.tick(self.now+35)
        self.assertEqual([c['text'] for c in self.commands()],['.赌石','.切 2','.切 2'])
        retry=self.commands()[2]['id']
        with self.db:self.db.execute("UPDATE outgoing_commands SET status='sending' WHERE id=?",(retry,))
        s.guard_command(self.storage,retry,self.now+35)
        self.sent(retry,14);self.msg(14,'.切 2',reply=12,at=self.now+36)
        self.collect(self.now+37);self.tick(self.now+37)
        self.db.close();self.db=s.connect(Path(self.tmp.name)/'test.db')
        self.storage.set_runtime_state(f'telegram_worker_heartbeat:{self.pid}',str(self.now+70))
        self.tick(self.now+70);self.assertEqual(len(self.commands()),3)
        self.msg(15,'【垮了！】',900,'hantianzun1_bot',14,self.now+71)
        self.collect(self.now+72);self.tick(self.now+72)
        self.assertEqual(self.db.execute('SELECT state,reserved FROM stone_auto_runs').fetchone()[:],('completed',4000))

    def test_manual_same_stone_retry_is_linked_and_prevents_automatic_retry(self):
        self.entry_panel();self.sent(self.commands()[1]['id'],13)
        self.msg(13,'.切 2',reply=12,at=self.now+5)
        self.collect(self.now+6);self.tick(self.now+6)
        self.msg(14,'.切 2',reply=12,at=self.now+34)
        self.collect(self.now+35);self.tick(self.now+35)
        self.assertEqual(len(self.commands()),2)
        self.assertEqual(self.db.execute('SELECT state FROM stone_auto_runs').fetchone()[0],'await_result')
        self.msg(15,'【垮了！】',900,'hantianzun1_bot',14,self.now+36)
        self.collect(self.now+37);self.tick(self.now+37)
        row=self.db.execute('SELECT link_status,selected,price,result_id FROM stone_rounds').fetchone()
        self.assertEqual(row[:],('matched',2,3500,15))
        self.assertEqual(self.db.execute('SELECT state FROM stone_auto_runs').fetchone()[0],'completed')

    def test_cut_acknowledgment_prevents_retry_after_30_seconds(self):
        self.entry_panel();self.sent(self.commands()[1]['id'],13)
        self.msg(13,'.切 2',reply=12,at=self.now+5)
        self.msg(14,'正在切石，请稍候...',900,'hantianzun1_bot',13,self.now+6)
        self.collect(self.now+7);self.tick(self.now+7)
        self.storage.set_runtime_state(f'telegram_worker_heartbeat:{self.pid}',str(self.now+40))
        self.tick(self.now+40)
        self.assertEqual(len(self.commands()),2)
        self.assertEqual(self.db.execute('SELECT state FROM stone_auto_runs').fetchone()[0],'await_result')

    def test_late_reply_cancels_queued_retry_at_final_send_guard(self):
        self.entry_panel();self.sent(self.commands()[1]['id'],13)
        self.msg(13,'.切 2',reply=12,at=self.now+5)
        self.collect(self.now+6);self.tick(self.now+6)
        self.storage.set_runtime_state(f'telegram_worker_heartbeat:{self.pid}',str(self.now+35))
        self.tick(self.now+35);retry=self.commands()[-1]['id']
        self.assertEqual(len(self.commands()),3)
        self.msg(14,'正在切石，请稍候...',900,'hantianzun1_bot',13,self.now+36)
        with self.db:self.db.execute("UPDATE outgoing_commands SET status='sending' WHERE id=?",(retry,))
        with self.assertRaisesRegex(RuntimeError,'已有切石回包'):
            s.guard_command(self.storage,retry,self.now+36)
        self.collect(self.now+37);self.tick(self.now+37)
        self.assertEqual(self.commands()[-1]['status'],'failed')
        self.assertEqual(self.db.execute('SELECT state FROM stone_auto_runs').fetchone()[0],'await_result')
        self.msg(14,'【垮了！】',900,'hantianzun1_bot',13,self.now+38)
        self.collect(self.now+39);self.tick(self.now+39)
        self.assertEqual(self.db.execute('SELECT state FROM stone_auto_runs').fetchone()[0],'completed')

    def test_retry_respects_pause_and_two_minute_window(self):
        self.entry_panel();self.sent(self.commands()[1]['id'],13)
        self.msg(13,'.切 2',reply=12,at=self.now+5)
        self.collect(self.now+6);self.tick(self.now+6)
        self.storage.set_runtime_state(f'telegram_worker_heartbeat:{self.pid}',str(self.now+35))
        self.storage.set_runtime_state('automation_paused_at',str(self.now+35))
        self.tick(self.now+35);self.assertEqual(len(self.commands()),2)
        self.storage.set_runtime_state('automation_paused_at','0')
        self.tick(self.now+36);retry=self.commands()[-1]['id']
        with self.db:self.db.execute('UPDATE stone_auto_commands SET deadline=? WHERE command_id=?',(self.now+999,retry))
        with self.assertRaisesRegex(RuntimeError,'两分钟'):s.guard_command(self.storage,retry,self.now+122)

    def test_manual_retry_cancels_queued_retry_before_dispatch(self):
        self.entry_panel();self.sent(self.commands()[1]['id'],13)
        self.msg(13,'.切 2',reply=12,at=self.now+5)
        self.collect(self.now+6);self.tick(self.now+6)
        self.storage.set_runtime_state(f'telegram_worker_heartbeat:{self.pid}',str(self.now+35))
        self.tick(self.now+35);retry=self.commands()[-1]['id']
        self.msg(14,'.切 2',reply=12,at=self.now+36)
        with self.assertRaisesRegex(RuntimeError,'人工补发'):s.guard_command(self.storage,retry,self.now+36)
        self.collect(self.now+37);self.tick(self.now+37)
        self.assertEqual(self.commands()[-1]['status'],'failed')
        self.msg(15,'【垮了！】',900,'hantianzun1_bot',14,self.now+38)
        self.collect(self.now+39);self.tick(self.now+39)
        self.assertEqual(self.db.execute('SELECT state FROM stone_auto_runs').fetchone()[0],'completed')

    def test_no_retry_is_queued_after_the_two_minute_window(self):
        self.entry_panel();self.sent(self.commands()[1]['id'],13)
        self.msg(13,'.切 2',reply=12,at=self.now+5)
        self.collect(self.now+6);self.tick(self.now+6)
        with self.db:self.db.execute('UPDATE stone_auto_runs SET deadline=?',(self.now+200,))
        self.storage.set_runtime_state(f'telegram_worker_heartbeat:{self.pid}',str(self.now+123))
        self.tick(self.now+123);self.assertEqual(len(self.commands()),2)

    def test_different_manual_selection_remains_ambiguous(self):
        self.msg(10,'.赌石');self.msg(11,PANEL,900,'hantianzun1_bot',10,self.now+1)
        self.msg(12,'.切 1',reply=11,at=self.now+2)
        self.msg(13,'.切 2',reply=11,at=self.now+33)
        self.msg(14,'【垮了！】',900,'hantianzun1_bot',13,self.now+34)
        self.collect(self.now+35)
        self.assertEqual(self.db.execute('SELECT link_status FROM stone_rounds').fetchone()[0],'ambiguous')

    def test_two_distinct_cut_results_remain_ambiguous_even_if_identical(self):
        self.msg(10,'.赌石');self.msg(11,PANEL,900,'hantianzun1_bot',10,self.now+1)
        self.msg(12,'.切 1',reply=11,at=self.now+2)
        self.msg(13,'.切 1',reply=11,at=self.now+33)
        self.msg(14,'【垮了！】',900,'hantianzun1_bot',12,self.now+34)
        self.msg(15,'【垮了！】',900,'hantianzun1_bot',13,self.now+35)
        self.collect(self.now+36)
        self.assertEqual(self.db.execute('SELECT link_status FROM stone_rounds').fetchone()[0],'ambiguous')

    def test_dispatch_does_not_merge_an_intentional_cut_retry(self):
        self.entry_panel();clock=[self.now+5];slept=[False]
        sender=AsyncMock(side_effect=[SimpleNamespace(id=13),SimpleNamespace(id=14)])
        async def next_pass(seconds):
            if slept[0]:raise asyncio.CancelledError
            slept[0]=True
            self.msg(13,'.切 2',reply=12,at=clock[0])
            self.collect(self.now+6);self.tick(self.now+6)
            clock[0]=self.now+35
            self.storage.set_runtime_state(f'telegram_worker_heartbeat:{self.pid}',str(clock[0]))
            self.tick(clock[0])
        async def dispatch():
            try:await runtime._dispatch_outgoing_commands(object(),self.storage,self.pid)
            except asyncio.CancelledError:pass
        with patch.object(runtime.time,'time',side_effect=lambda:clock[0]),patch.object(runtime.time,'monotonic',return_value=1000),patch.object(runtime,'TELEGRAM_RESUME_OFFLINE_GAP_SECONDS',3600),patch.object(runtime.asyncio,'sleep',next_pass),patch.object(runtime,'send_message_with_thread_fallback',sender):
            asyncio.run(dispatch())
        self.assertEqual(sender.await_count,2)
        self.assertEqual(self.commands()[-1]['sent_message_id'],14)
        self.assertEqual(self.commands()[-1]['status'],'awaiting_confirm')

    def test_dispatch_still_merges_ordinary_duplicate_commands(self):
        sender=AsyncMock(return_value=SimpleNamespace(id=13))
        async def idle(seconds):raise asyncio.CancelledError
        async def dispatch():
            try:await runtime._dispatch_outgoing_commands(object(),self.storage,self.pid)
            except asyncio.CancelledError:pass
        with patch.object(runtime.time,'time',return_value=self.now+5),patch.object(runtime.time,'monotonic',return_value=1000),patch.object(runtime.asyncio,'sleep',idle),patch.object(runtime,'send_message_with_thread_fallback',sender):
            self.storage.enqueue_outgoing_command(self.pid,CHAT,'.查询')
            self.storage.enqueue_outgoing_command(self.pid,CHAT,'.查询')
            asyncio.run(dispatch())
        self.assertEqual(sender.await_count,1)
        self.assertEqual(self.commands()[-1]['status'],'confirmed')

    def test_pass_only_takes_the_write_lock_when_there_is_work(self):
        # 记录器原来每 2 秒先 BEGIN IMMEDIATE 再扫两张消息表，一天 4 万多次，绝大多数轮次没东西可写
        quiet=self.now+3600  # 04:10：窗口外，也没有进行中的 run
        beat=s.watch_pass(self.db,quiet,True,0);self.assertEqual(beat,quiet)
        before=self.db.total_changes
        self.assertEqual(s.watch_pass(self.db,quiet+2,True,beat),beat)  # 空转：不写库
        self.assertEqual(self.db.total_changes,before);self.assertFalse(self.db.in_transaction)
        beat=s.watch_pass(self.db,quiet+31,True,beat);self.assertEqual(beat,quiet+31)  # 30 秒记一次心跳
        self.assertEqual(self.db.execute("SELECT value FROM stone_meta WHERE key='worker_heartbeat'").fetchone()[0],str(quiet+31))
        self.msg(10,'.赌石',200,'other',at=quiet+32)
        self.assertEqual(s.watch_pass(self.db,quiet+33,True,beat),quiet+33)  # 有赌石消息：归档
        self.assertEqual(self.db.execute('SELECT count(*) FROM stone_messages').fetchone()[0],1)
    def test_pass_drives_the_auto_run_inside_its_window(self):
        self.assertEqual(s.watch_pass(self.db,self.now+1,True,self.now),self.now+1)  # 窗口内每轮都推进
        self.assertEqual([c['text'] for c in self.commands()],['.赌石'])
        self.assertTrue(s.auto_due(self.db,self.now+3600))  # 窗口过了，这一轮还没结束，仍要推进

if __name__=='__main__':unittest.main()
