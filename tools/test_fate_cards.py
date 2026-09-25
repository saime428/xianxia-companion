"""天机命脉（09-25）：外府换出 fate_ token → start/draw/interpret/choose/settle；每天一次，接着手动停下的地方往下走。

run: PYTHONPATH=app/src python tools/test_fate_cards.py
"""

import asyncio
import json
import logging
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))

from tg_game.features.fate_cards import biz_fate_cards_miniapp as fate  # noqa: E402
from tg_game.runtime import executors as ex  # noqa: E402
from tg_game.storage import Storage  # noqa: E402

TOKEN = "fate_AbCdEf0123456789xyzw"
CHOICES = [{"key": "accept", "name": "顺势承命"}, {"key": "defy", "name": "逆势改命"}, {"key": "hide", "name": "藏锋避劫"}]
QUESTIONS = [{"key": "cultivation", "name": "修行"}, {"key": "opportunity", "name": "机缘"}]
CARDS = [{"positionName": "前因", "title": "掌天瓶", "orientation": "正位"}, {"positionName": "今时", "title": "天机阁", "orientation": "逆位"}]


def server(*, start, settle=None, choose_quest=None):
    """假服务器：按接口名回包，记下每次调用的 (接口, 请求体)。"""
    calls = []

    def transport(request):
        endpoint = request["url"].rsplit("/", 1)[-1]
        calls.append((endpoint, dict(request["payload"])))
        assert request["url"] == f"https://asc.aiopenai.app/api/miniapp/xianxia-fate-cards/{endpoint}", request["url"]
        assert request["payload"]["token"] == TOKEN and request["payload"]["initData"] == "init"
        if endpoint == "start":
            return 200, json.dumps({"ok": True, **start})
        if endpoint == "draw":
            return 200, json.dumps({"ok": True, "record": {"challengeDate": "2026-09-25", "questionKey": request["payload"]["questionKey"], "cards": CARDS}, "reward": {"balance": 133}})
        if endpoint == "interpret":
            return 500, json.dumps({"ok": False, "error": "ai_down"})  # AI 挂了也要能选命择
        if endpoint == "choose":
            quest = choose_quest or {"metric": "wait_seconds", "target": 180, "progress": 0, "status": "active", "canSettle": False, "title": "避劫·藏锋"}
            return 200, json.dumps({"ok": True, "record": {"challengeDate": "2026-09-25", "cards": CARDS, "choiceKey": request["payload"]["choiceKey"], "quest": quest}})
        if endpoint == "settle":
            record = {"challengeDate": "2026-09-25", "cards": CARDS, "choiceKey": "hide", "quest": {"status": "settled"}}
            return settle or (200, json.dumps({"ok": True, "record": record, "reward": {"tianjiTrace": 3, "kunwuPass": 0, "balance": 136}}))
        raise AssertionError(endpoint)

    return transport, calls


def flow(transport, **kwargs):
    slept = []
    result = fate.run_fate_cards_flow(token=TOKEN, init_data="init", transport=transport, sleeper=slept.append, **kwargs)
    return result, slept


def check_launch_and_request():
    def data(url):
        return {"account": {"externalApps": {"groups": [{"apps": [{"key": "fate_cards", "url": url}]}]}}}

    assert fate.extract_fate_cards_launch(data(f"/miniapp/xianxia-fate-cards?startapp={TOKEN}")) == {"token": TOKEN}
    assert fate.extract_fate_cards_launch(data(f"https://asc.aiopenai.app/miniapp/xianxia-fate-cards/?startapp={TOKEN}")) == {"token": TOKEN}
    assert fate.extract_fate_cards_launch(data(f"https://evil.example/miniapp/xianxia-fate-cards?startapp={TOKEN}")) == {}
    assert fate.extract_fate_cards_launch(data(f"/miniapp/xianxia-beast-merge?startapp={TOKEN}")) == {}
    assert fate.extract_fate_cards_launch(data("/miniapp/xianxia-fate-cards?startapp=beastmerge_abcdef")) == {}
    assert fate.extract_fate_cards_launch(data("")) == {} and fate.extract_fate_cards_launch({}) == {}
    try:
        fate.build_fate_cards_request("start", token="df_notours", init_data="x")
    except ValueError:
        pass
    else:
        raise AssertionError("非 fate_ token 不该放行")


def check_flow():
    # 新的一天：启牌 → AI 解读失败也往下走 → 藏锋避劫 → 等三分钟 → 验命
    transport, calls = server(start={"challengeDate": "2026-09-25", "traceBalance": 132, "questions": QUESTIONS, "choices": CHOICES, "hasDrawn": False})
    result, slept = flow(transport)
    assert [c[0] for c in calls] == ["start", "draw", "interpret", "choose", "settle"], calls
    assert calls[1][1]["questionKey"] == "cultivation" and calls[3][1]["choiceKey"] == "hide", calls
    assert slept == [185], slept
    assert result["status"] == "settled" and result["reward"] == {"tianjiTrace": 3, "kunwuPass": 0, "balance": 136}, result
    assert result["cards"] == ["前因·掌天瓶（正位）", "今时·天机阁（逆位）"], result["cards"]
    assert "token" not in json.dumps(result) and TOKEN not in json.dumps(result)

    # 主题不在今天的列表里：退回第一个，别拿不存在的 key 去启牌
    transport, calls = server(start={"questions": [{"key": "fortune"}], "choices": CHOICES, "hasDrawn": False})
    flow(transport, question_key="cultivation")
    assert calls[1][1]["questionKey"] == "fortune", calls

    # 今天手动做完了：只看一眼
    done = {"hasDrawn": True, "record": {"choiceKey": "hide", "aiReading": {"overview": "x"}, "quest": {"status": "settled"}}}
    transport, calls = server(start=done)
    result, _ = flow(transport)
    assert [c[0] for c in calls] == ["start"] and result["status"] == "settled", (calls, result)

    # 手动选了顺势承命、修为还没攒够：不改命择、不去验，半小时后再来
    accept = {"hasDrawn": True, "record": {"choiceKey": "accept", "quest": {"metric": "cultivation", "target": 30, "progress": 10, "status": "active", "canSettle": False}}}
    transport, calls = server(start=accept)
    result, slept = flow(transport)
    assert [c[0] for c in calls] == ["start"] and result["status"] == "waiting" and slept == [], (calls, result)
    assert result["next_check_seconds"] == fate.WAITING_RECHECK_SECONDS

    # 手动启了牌、没选：接着选，AI 已解读过就不再调
    drawn = {"hasDrawn": True, "choices": CHOICES, "record": {"cards": CARDS, "aiReading": {"overview": "已解读"}}}
    transport, calls = server(start=drawn)
    result, _ = flow(transport)
    assert [c[0] for c in calls] == ["start", "choose", "settle"] and result["status"] == "settled", calls

    # 认不出的命择：选了不能改，宁可不选
    transport, calls = server(start={"hasDrawn": True, "choices": CHOICES[:2], "record": {"cards": CARDS}})
    result, _ = flow(transport)
    assert result["status"] == "failed" and "choose" not in [c[0] for c in calls], (calls, result)

    # 验命说没做完：等下一轮
    transport, calls = server(start={"hasDrawn": False, "choices": CHOICES}, settle=(400, json.dumps({"ok": False, "error": "quest_incomplete"})))
    result, _ = flow(transport)
    assert result["status"] == "waiting", result

    # start 就失败
    result, _ = flow(lambda request: (500, json.dumps({"ok": False, "error": "boom"})))
    assert result["status"] == "failed" and result["error"] == "boom", result


async def check_daily_runner():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "t.db")
        storage.init_schema()
        pid = storage.create_profile("甲真人").id
        client = SimpleNamespace(_tg_game_profile_id=pid)
        key = f"fate_cards:{pid}"
        outcomes = []

        async def launch(*_args, **_kwargs):
            return {"ok": True, "token": TOKEN, "init_data": "init"}

        def fake_flow(**kwargs):
            outcomes.append(kwargs["choice_key"])
            return {"ok": True, "status": "settled", "choice": kwargs["choice_key"], "reward": {"tianjiTrace": 3}}

        def state():
            return json.loads(storage.get_runtime_state(key) or "{}")

        with patch.object(ex, "FATE_CARDS_EARLIEST_TIME", "00:00"), \
             patch.object(fate, "resolve_fate_cards_launch", launch), \
             patch.object(fate, "run_fate_cards_flow", fake_flow):
            assert await ex._run_pending_fate_cards(client, storage, pid) is False  # 没开
            storage.set_runtime_state(key, json.dumps({"enabled": True}))
            assert await ex._run_pending_fate_cards(client, storage, pid) is True
            assert await ex._run_pending_fate_cards(client, storage, pid) is False  # 今天领过了
            assert outcomes == ["hide"] and state()["status"] == "settled" and len(state()["history"]) == 1, state()

            # 换了一天：再跑一次
            storage.set_runtime_state(key, json.dumps({**state(), "date": "2026-09-24"}))
            assert await ex._run_pending_fate_cards(client, storage, pid) is True and len(state()["history"]) == 2

        # 逆势改命只认选完之后打的那局噬金虫：虫巢今天还没打才选它，打过了/没开自动就退回藏锋避劫
        async def choice_for(beast):
            with tempfile.TemporaryDirectory() as tmp2:
                storage2 = Storage(Path(tmp2) / "t.db")
                storage2.init_schema()
                pid2 = storage2.create_profile("丁真人").id
                storage2.create_chat_binding(pid2, -1001000000001, bot_username="fanrenxiuxian_bot")
                if beast is not None:
                    storage2.upsert_companion_auto_task(
                        profile_id=pid2, chat_id=-1001000000001, feature_key="beast_merge_daily", enabled=True,
                        strategy="00:05", bot_username="fanrenxiuxian_bot", last_run_at=beast,
                    )
                storage2.set_runtime_state(f"fate_cards:{pid2}", json.dumps({"enabled": True, "choice": "defy"}))
                outcomes.clear()
                with patch.object(ex, "FATE_CARDS_EARLIEST_TIME", "00:00"), \
                     patch.object(fate, "resolve_fate_cards_launch", launch), \
                     patch.object(fate, "run_fate_cards_flow", fake_flow):
                    await ex._run_pending_fate_cards(SimpleNamespace(_tg_game_profile_id=pid2), storage2, pid2)
                return outcomes[0]

        assert await choice_for(time.time() - 3 * 86400) == "defy"  # 虫巢今天还没打
        assert await choice_for(time.time()) == "hide"  # 今天已经打过
        assert await choice_for(None) == "hide"  # 没开噬金虫自动

        # 一直失败：半小时一次，五次就放弃到明天
        async def broken(*_args, **_kwargs):
            return {"ok": False, "error": "外府目录没有返回天机命脉入口"}

        storage.set_runtime_state(key, json.dumps({"enabled": True, "choice": "accept"}))
        with patch.object(ex, "FATE_CARDS_EARLIEST_TIME", "00:00"), patch.object(fate, "resolve_fate_cards_launch", broken):
            for attempt in range(5):
                assert await ex._run_pending_fate_cards(client, storage, pid) is True
                assert (state()["status"] == "gave_up") == (attempt == 4), (attempt, state())
                assert state()["next_at"] > time.time() + 1700
                storage.set_runtime_state(key, json.dumps({**state(), "next_at": 0}))
            assert await ex._run_pending_fate_cards(client, storage, pid) is False
            assert state()["choice"] == "accept" and state()["history"] == []


def main() -> None:
    logging.disable(logging.CRITICAL)
    check_launch_and_request()
    check_flow()
    asyncio.run(check_daily_runner())
    print("fate cards: ok")


if __name__ == "__main__":
    main()
