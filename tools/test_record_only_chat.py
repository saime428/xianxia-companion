"""只记录群：老群消息由大号 client 原样存进 recorded_messages，不进 bound_messages、不走任何自动化。

bound_messages 里有不按群过滤的查询（股市行情），所以老群必须分表；这里用临时库锁住
「存得进、编辑覆盖、只有大号写、名单外不记、跟着 48 小时清理」这几条。

运行：PYTHONPATH=app/src .venv/bin/python tools/test_record_only_chat.py
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.config import get_settings  # noqa: E402
from tg_game.runtime.router import record_unbound_message  # noqa: E402
from tg_game.storage import Storage  # noqa: E402

OLD_GROUP = -1002083016447


class _Profile:
    def __init__(self, uid):
        self.id = 2
        self.telegram_user_id = uid


class _Sender:
    username = "hantianzun34_bot"
    bot = True


class _Event:
    sender = _Sender()


class _Context:
    def __init__(self, profile, *, chat_id=OLD_GROUP, message_id=1111554, text="【魔魂降临】"):
        self.profile = profile
        self.chat_id = chat_id
        self.message_id = message_id
        self.text = text
        self.event = _Event()
        self.thread_id = None
        self.reply_to_msg_id = 1111553
        self.sender_id = 8918867302
        self.is_bot_sender = True
        self.is_outgoing = False
        self.chat_binding = None


admin = _Profile(get_settings().authorized_user_id)
alt = _Profile("1000000013")
assert admin.telegram_user_id, "需要 .env 里的 AUTHORIZED_USER_ID（在 VPS 上跑）"

with tempfile.TemporaryDirectory() as tmp:
    storage = Storage(Path(tmp) / "record_only.db")
    storage.init_schema()

    assert record_unbound_message(storage, _Context(admin), (OLD_GROUP,)) is True
    # 同一条被编辑：原地覆盖正文，首次写入的发送者保留
    assert record_unbound_message(storage, _Context(admin, text="【魔魂降临】编辑后"), (OLD_GROUP,)) is True
    with storage.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM recorded_messages")]
        bound_count = conn.execute("SELECT COUNT(*) FROM bound_messages").fetchone()[0]
    assert len(rows) == 1, rows
    assert rows[0]["text"] == "【魔魂降临】编辑后" and rows[0]["sender_username"] == "hantianzun34_bot", rows
    assert rows[0]["is_bot"] == 1 and rows[0]["direction"] == "incoming", rows
    assert bound_count == 0, "只记录群不能写进 bound_messages（股市等查询不按群过滤）"

    assert record_unbound_message(storage, _Context(alt, message_id=2), (OLD_GROUP,)) is False, "只让大号 client 写"
    assert record_unbound_message(storage, _Context(admin, chat_id=-100123, message_id=3), (OLD_GROUP,)) is False, "名单外的群不记"
    assert record_unbound_message(storage, _Context(admin, message_id=None), (OLD_GROUP,)) is False, "没有消息号不记"
    assert record_unbound_message(storage, _Context(admin, message_id=4), ()) is False, "名单为空等于关闭"

    # 跟 bound_messages 同一保留期一起清
    storage.delete_bound_messages_older_than(max_age_seconds=3600, now=time.time() + 7200)
    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM recorded_messages").fetchone()[0] == 0, "过期没清掉"

print("ok")
