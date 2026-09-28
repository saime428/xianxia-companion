"""首次钓鱼、刷新期间寻宝请求、Bot 换号的离线回归。"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.features.estate import biz_estate_hunt_queue as hunt
from tg_game.runtime.router import Router
from tg_game.runtime.executors import _is_context_sender_allowed_bot
from tg_game.storage import ASC_EXTERNAL_PROVIDER, Storage


async def main():
    def deny(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
            raise AssertionError("network forbidden")
    sys.addaudithook(deny)
    with tempfile.TemporaryDirectory() as folder:
        storage = Storage(Path(folder) / "game.db")
        storage.init_schema()
        profile = storage.create_profile("regression-player")
        chat = -100000000042
        # 首次试钓显式传入来源；随后更新、旧调用默认值和非法输入均可保存。
        for source in ("craft_only", "buy", "craft"):
            row = storage.upsert_fishing_session(
                profile_id=profile.id, chat_id=chat, bait_source=source, enabled=True, state="miniapp_canary",
            )
            assert row["bait_source"] == source
            assert storage.get_fishing_session(profile.id, chat)["bait_source"] == source
        assert storage.upsert_fishing_session(profile_id=profile.id, chat_id=chat-1)["bait_source"] == "craft"
        assert storage.upsert_fishing_session(profile_id=profile.id, chat_id=chat, bait_source="invalid")["bait_source"] == "craft"

        def refresh(payload):
            storage.upsert_external_account(profile.id, ASC_EXTERNAL_PROVIDER, "101", "offline", "connected", "", payload, "")
            return json.loads(storage.get_external_account(profile.id, ASC_EXTERNAL_PROVIDER)["me_json"])

        refresh({"dongfu": {"level": 1}})
        for phase in ("queued", "resolving", "running"):
            payload = hunt.queue_estate_miniapp_hunt_request({"dongfu": {}}, chat_id=chat)
            if phase != "queued":
                payload = hunt.claim_estate_miniapp_hunt_request(payload, "worker-test")
            if phase == "running":
                payload = hunt.mark_estate_miniapp_hunt_request_status(payload, "running", execution_owner="worker-test")
            storage.update_external_account_payload(profile.id, ASC_EXTERNAL_PROVIDER, lambda _: payload)
            result = refresh({"dongfu": {"level": 2, "miniapp_hunt": {"status": "completed"},
                                          "miniapp_hunt_request": {"status": "queued", "request_id": "stale"}}})
            assert result["dongfu"]["level"] == 2
            assert result["dongfu"]["miniapp_hunt_request"] == payload["dongfu"]["miniapp_hunt_request"], phase
            assert hunt.get_pending_estate_miniapp_hunt_request(result)
            if phase != "queued":
                assert hunt.is_estate_miniapp_hunt_request_owned(result, "worker-test")
        # 真实结束函数移除请求；稍后旧快照回来不能把请求恢复。
        stale = result
        ended = hunt.mark_estate_miniapp_hunt_limit_reached(result)
        storage.update_external_account_payload(profile.id, ASC_EXTERNAL_PROVIDER, lambda _: ended)
        assert "miniapp_hunt_request" not in refresh(stale)["dongfu"]
        ended["dongfu"]["miniapp_hunt_request"] = {"status": "cancelled", "request_id": "cancelled"}
        storage.update_external_account_payload(profile.id, ASC_EXTERNAL_PROVIDER, lambda _: ended)
        assert refresh(stale)["dongfu"]["miniapp_hunt_request"]["status"] == "cancelled"

        storage.create_chat_binding(profile.id, chat, thread_id=42, bot_username="fanrenxiuxian_bot", bot_id=111)
        storage.create_chat_binding(profile.id, chat-1, thread_id=42, bot_username="fanrenxiuxian_bot", bot_id=222)
        seen = []
        class Observer:
            key = "test"
            async def handle(self, context, storage):
                seen.append(_is_context_sender_allowed_bot(context))
                return True
        router = Router(storage, [Observer()], runtime_profile_id=profile.id)
        def event(sender_id, username, bot=True, target=chat, first_name="", title=""):
            return SimpleNamespace(
                chat_id=target, sender_id=sender_id, id=None, raw_text="test", out=False,
                sender=SimpleNamespace(id=sender_id, bot=bot, username=username, first_name=first_name, title=title),
                message=SimpleNamespace(reply_to=SimpleNamespace(reply_to_top_id=42)),
            )
        with patch.object(storage, "add_chat_binding_bot_id", wraps=storage.add_chat_binding_bot_id) as add:
            await router.dispatch(SimpleNamespace(), event(999001, "hantianzun99_bot"))
            assert seen[-1], "新 ID 的第一条消息就应通过下游数字 ID 校验"
            assert 999001 in storage.get_chat_binding(profile.id, chat, thread_id=42).bot_ids
            assert 111 in storage.get_chat_binding(profile.id, chat, thread_id=42).bot_ids
            assert 999001 not in storage.get_chat_binding(profile.id, chat-1, thread_id=42).bot_ids
            await router.dispatch(SimpleNamespace(), event(999001, "hantianzun99_bot"))
            assert add.call_count == 1, "已绑定 ID 不应反复写库"
            await router.dispatch(SimpleNamespace(), event(999002, "hantianzun98_bot", bot=False))
            assert not seen[-1], "普通账号即使用户名匹配也不能自动信任"
            await router.dispatch(SimpleNamespace(), event(999003, "unknown_bot", first_name="韩天尊"))
            assert not seen[-1], "只有昵称不够"
            await router.dispatch(SimpleNamespace(), event(999004, "unknown_bot", first_name="韩天尊", title="天尊"))
            assert seen[-1], "保留现有头衔识别路径"
            await router.dispatch(SimpleNamespace(), event(999005, "hantianzun97_bot", target=chat-2))
            assert 999005 not in storage.get_chat_binding(profile.id, chat, thread_id=42).bot_ids
    print("fishing/hunt/bot regressions: ok")


asyncio.run(main())
