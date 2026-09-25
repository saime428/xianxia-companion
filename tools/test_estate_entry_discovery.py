"""洞府公共入口（09-25）：新装的机器也要找得到入口。

新登录的 session 还没缓存入口群 → get_entity 先失败，拉一遍会话列表再取；
入口是管理员置顶的老消息 → 最近 200 条和关键词搜索够不着时，再看置顶。

run: PYTHONPATH=app/src python tools/test_estate_entry_discovery.py
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))

from tg_game.features.estate import biz_estate_miniapp as estate  # noqa: E402

ENTRY_URL = "https://t.me/hantianzun16_bot?startapp=df_abcdefgh1234"


def msg(message_id, text="", url=None):
    buttons = [[SimpleNamespace(text="进入洞府", url=url)]] if url else None
    return SimpleNamespace(id=message_id, message=text, buttons=buttons)


class FakeClient:
    """recent 从新到旧；pinned 是置顶消息。cached=False 模拟新 session 还不认识这个群。"""

    def __init__(self, recent, pinned=(), cached=True, member=True):
        self.recent, self.pinned = list(recent), list(pinned)
        self.cached, self.member = cached, member
        self.dialogs_loaded = False

    async def get_entity(self, chat_id):
        if not self.member or (not self.cached and not self.dialogs_loaded):
            raise ValueError(f"Could not find the input entity for PeerChannel(channel_id={chat_id})")
        return SimpleNamespace(id=chat_id)

    async def get_dialogs(self):
        self.dialogs_loaded = True
        return []

    async def get_messages(self, channel, ids=None, limit=None):
        if ids is not None:
            return next((m for m in self.recent + self.pinned if m.id == ids), None)
        return self.recent[:limit]

    async def iter_messages(self, channel, limit=None, search=None, filter=None):
        if filter is not None:
            source = self.pinned
        elif search:
            source = [m for m in self.recent if search in (m.message or "")]
        else:
            source = self.recent
        for message in source[:limit]:
            yield message


def discover(client):
    return asyncio.run(estate.discover_estate_public_miniapp_launch(client))


def main() -> None:
    chatter = [msg(2000 - i, text=f"闲聊 {i}") for i in range(250)]  # 入口早被刷出最近 200 条

    # 新装：session 不认识入口群、入口只在置顶里
    fresh = FakeClient(chatter, pinned=[msg(1170, text="韩天尊洞府", url=ENTRY_URL)], cached=False)
    result = discover(fresh)
    assert fresh.dialogs_loaded and result["ok"], result
    assert result["launch"]["token"] == "df_abcdefgh1234" and result["launch"]["bot_username"] == "hantianzun16_bot"
    assert result["state"]["current_message_id"] == 1170 and result["state"]["last_scan_status"] == "ok", result["state"]

    # 老路径不变：入口在最近的消息里
    recent = FakeClient([msg(10, url=ENTRY_URL)] + chatter[:5])
    result = discover(recent)
    assert result["ok"] and result["state"]["current_message_id"] == 10 and not recent.dialogs_loaded, result

    # 哪儿都没有：照旧报找不到（调用方再去读 .env 的备用入口）
    result = discover(FakeClient(chatter))
    assert not result["ok"] and result["error"] == "洞府公共入口未找到", result

    # 账号不在入口群：拉完会话列表还是拿不到，照样抛出去（调用方记成 error）
    try:
        discover(FakeClient(chatter, member=False))
    except ValueError:
        pass
    else:
        raise AssertionError("账号不在群里应该报错")
    print("estate entry discovery: ok")


if __name__ == "__main__":
    main()
