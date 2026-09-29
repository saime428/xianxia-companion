"""离线检查群聊活跃：真实存储/开关，替身 Telegram，禁止网络。"""
import asyncio
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from telethon.errors import FloodWaitError, SlowModeWaitError
from tg_game.services import group_activity as ga
from tg_game.services.automation_switch import pause_automation, resume_automation
from tg_game.services.profile_schedules import stop_current_profile_schedules
from tg_game.storage import Storage


async def noop(*args, **kwargs):
    pass


async def main():
    def deny(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
            raise AssertionError("network forbidden")
    sys.addaudithook(deny)
    with tempfile.TemporaryDirectory() as folder:
        storage = Storage(Path(folder) / "game.db")
        storage.init_schema()
        profile = storage.create_profile("group-activity-test")
        chat = -100000000042
        storage.create_chat_binding(profile.id, chat, thread_id=42, bot_username="fanrenxiuxian_bot")
        clock = [datetime(2026, 9, 28, 10, tzinfo=ga.BEIJING).timestamp()]
        sent, deleted = [], []
        messages = {}
        fault = [None]

        async def send(chat_id, text, **kwargs):
            if fault[0]:
                raise fault[0]
            msg = SimpleNamespace(id=len(sent) + 1, sender_id=123)
            messages[msg.id] = msg
            sent.append((chat_id, text))
            return msg

        async def get_messages(chat_id, ids):
            return messages.get(ids)

        async def delete(chat_id, ids, **kwargs):
            deleted.extend(ids)

        client = SimpleNamespace(send_message=send, get_messages=get_messages, delete_messages=delete)
        def due():
            task = ga.get_task(storage, profile.id)
            storage.update_companion_auto_task(task["id"], next_run_at=clock[0] - 1)

        with patch.object(ga.time, "time", lambda: clock[0]), patch.object(
            ga.send_utils, "_throttle_outgoing_send", noop
        ), patch.object(ga.send_utils, "_resolve_binding_thread_id", return_value=None), patch.object(
            ga.random, "random", return_value=0.2
        ):
            await ga.tick(client, storage, profile.id, 123)
            assert not sent and ga.get_task(storage, profile.id) is None, "默认关闭"
            task = ga.toggle(storage, profile.id)
            assert 2700 <= task["next_run_at"] - clock[0] <= 4500
            due()
            await ga.tick(client, storage, profile.id, 123)
            assert len(sent) == 1 and sent[0][0] == chat
            state = ga.load_state(storage, profile.id)
            assert state["pending"][0]["delete_at"] == clock[0] + 90
            ga.toggle(storage, profile.id)
            clock[0] += 91
            # 新 Storage 模拟进程重启；关闭发言后也会撤回已登记消息。
            await ga.tick(client, Storage(Path(storage.path)), profile.id, 123)
            assert deleted == [1] and len(sent) == 1
            saved = ga.load_state(Storage(Path(storage.path)), profile.id)
            assert saved["last_sent_at"] == clock[0] - 91 and saved["last_text"] == sent[0][1], "关闭、重启、撤回后保留最近发言"

            # 数据中即使出现别人的消息或其他账号遗留，也绝不删除。
            messages[99] = SimpleNamespace(id=99, sender_id=999)
            state = ga.load_state(storage, profile.id)
            state["pending"] = [dict(chat_id=chat, message_id=99, owner_id=123, delete_at=clock[0]),
                                dict(chat_id=chat, message_id=1, owner_id=999, delete_at=clock[0])]
            ga.save_state(storage, profile.id, state)
            await ga.tick(client, storage, profile.id, 123)
            assert deleted == [1]

            # 未抽中撤回的消息保持原样；网络恢复不立刻补发。
            ga.toggle(storage, profile.id)
            due()
            with patch.object(ga.random, "random", return_value=0.9):
                await ga.tick(client, storage, profile.id, 123)
            assert not ga.load_state(storage, profile.id)["pending"]
            due()
            with patch.object(ga, "is_network_paused", return_value=True):
                await ga.tick(client, storage, profile.id, 123)
            assert ga.get_task(storage, profile.id)["next_run_at"] > clock[0]
            ga.toggle(storage, profile.id)

            ga.toggle(storage, profile.id)
            for i in range(15):
                due()
                await ga.tick(client, storage, profile.id, 123)
                assert sent[-1][1] not in [text for _, text in sent[max(0, len(sent)-11):-1]]
            assert len(ga.load_state(storage, profile.id)["history"]) == 10
            count = len(sent)
            due()
            clock[0] = datetime(2026, 9, 28, 23, tzinfo=ga.BEIJING).timestamp()
            await ga.tick(client, storage, profile.id, 123)
            assert len(sent) == count
            morning = datetime.fromtimestamp(ga.get_task(storage, profile.id)["next_run_at"], ga.BEIJING)
            assert morning.day == 29 and morning.hour == 8
            clock[0] = datetime(2026, 9, 29, 10, tzinfo=ga.BEIJING).timestamp()
            due()
            pause_automation(storage, now=clock[0])
            await ga.tick(client, storage, profile.id, 123)
            assert len(sent) == count
            resume_automation(storage)
            other = storage.create_profile("other-player")
            heart = storage.upsert_companion_heart_tribulation_task(
                profile_id=other.id, chat_id=chat, enabled=True, workflow_state="await_round1_edit",
            )
            await ga.tick(client, storage, profile.id, 123)
            assert len(sent) == count, "另一个账号的心劫也应优先"
            # 库里遗留的 failed_stopped 心劫（调度器跳过它）不能永远挡住闲聊；
            # 别的号最近说过的也避开，免得两个号先后说同一句
            storage.update_companion_heart_tribulation_task(heart["id"], workflow_state="failed_stopped")
            ga.save_state(storage, other.id, {"history": list(ga.PHRASES[:5])})
            offered, real_choice = [], ga.random.choice

            def spy_choice(options):
                offered.append(list(options))
                return real_choice(options)

            with patch.object(ga.random, "choice", spy_choice):
                await ga.tick(client, storage, profile.id, 123)
            assert len(sent) == count + 1 and not set(offered[-1]) & set(ga.PHRASES[:5])
            count += 1
            storage.update_companion_heart_tribulation_task(heart["id"], enabled=0)

            for exc, seconds in [(FloodWaitError(None, 120), 120), (SlowModeWaitError(None, 240), 240), (RuntimeError("offline"), 600)]:
                fault[0] = exc
                due()
                await ga.tick(client, storage, profile.id, 123)
                assert ga.load_state(storage, profile.id)["blocked_until"] == clock[0] + seconds
                clock[0] += seconds - 1
                await ga.tick(client, storage, profile.id, 123)
                assert len(sent) == count
                clock[0] += 2
            fault[0] = None
            # 限流等待期间关闭：最终发送检查阻止消息出群。
            async def turn_off():
                ga.toggle(storage, profile.id)
            due()
            with patch.object(ga.send_utils, "_throttle_outgoing_send", turn_off):
                await ga.tick(client, storage, profile.id, 123)
            assert len(sent) == count

            ga.toggle(storage, profile.id)
            stop_current_profile_schedules(storage, profile.id)
            assert not ga.get_task(storage, profile.id)["enabled"]
            ga.toggle(storage, profile.id)
            due()
            client._tg_game_profile_id = profile.id
            async def me():
                return SimpleNamespace(id=123)
            client.get_me = me
            async def end_loop(*args):
                raise asyncio.CancelledError()
            with patch.object(ga.asyncio, "sleep", end_loop):
                try:
                    await ga.run(client, storage)
                except asyncio.CancelledError:
                    pass
            assert len(sent) == count and ga.get_task(storage, profile.id)["next_run_at"] > clock[0], "离线不补发"

            # 页面开关复用现有卡片样式，关闭/开启文案及不可用状态均能渲染。
            from jinja2 import Environment
            page = (Path(__file__).resolve().parent.parent / "app/assets/templates/modules/other.html").read_text(encoding="utf-8")
            card = page[page.index('    <div class="detail-card">'):page.index('    <div class="detail-card">', page.index('    <div class="detail-card">') + 1)]
            template = Environment(autoescape=True).from_string(card)
            html = template.render(group_activity_task={}, group_activity_state={}, command_chat_ready=False, format_timestamp=str)
            assert '开启自动群聊骚话' in html and 'disabled' in html
            assert '尚未发言' in html and '<span>发言内容</span><strong>-</strong>' in html
            html = template.render(group_activity_task={'enabled':1,'next_run_at':clock[0]}, group_activity_state=saved, command_chat_ready=True, format_timestamp=lambda ts: datetime.fromtimestamp(ts, ga.BEIJING).strftime('%Y-%m-%d %H:%M:%S'))
            assert '关闭自动群聊骚话' in html and 'disabled' not in html
            assert '2026-09-28 10:00:00' in html and sent[0][1] in html
            html = template.render(group_activity_task={}, group_activity_state={'last_sent_at': saved['last_sent_at'], 'last_text': '<b>道友 & 我</b>'}, command_chat_ready=True, format_timestamp=str)
            assert str(saved['last_sent_at']) in html and '&lt;b&gt;道友 &amp; 我&lt;/b&gt;' in html, "关闭后仍显示记录，正文按纯文本转义"
    print("group activity: ok")


asyncio.run(main())
