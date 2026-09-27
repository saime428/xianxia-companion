"""野外历练日报（09-27）：当天打满才发、一天只发一次、没开就不发；战果按场次从今天的 history 拼。

run: PYTHONPATH=app/src python tools/test_wild_experience_report.py
"""

import asyncio
import logging
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))

from tg_game.features.tianxing import biz_tianxing_runtime as tx  # noqa: E402
from tg_game.features.wild_experience import biz_wild_experience_miniapp as wild  # noqa: E402
from tg_game.runtime import executors as ex  # noqa: E402
from tg_game.storage import ASC_EXTERNAL_PROVIDER, Storage  # noqa: E402

NOW = time.time()
TODAY = wild._day_key(NOW)
HIT = "【推命命中】司命演算吻合，天机值 +1，宗门贡献 +30"
# 09-27 大号实况：5 场改命脱险、2 胜、1 平安
FIGHTS = [
    ("fate_escape", 0, True, "养魂木", 1), ("victory", 45000, False, "灵石", 639),
    ("victory", 45000, False, "养魂木", 3), ("fate_escape", 0, True, "三级妖丹", 1),
    ("fate_escape", 0, True, "灵石", 674), ("safe_event", 45000, False, "三级妖丹", 2),
    ("fate_escape", 0, True, "天凤之翎", 1), ("fate_escape", 0, True, "灵石", 404),
]


def attempt(count, outcome, delta, protected, name, quantity):
    return {"daily_count": count, "outcome": outcome, "cultivation_delta": delta, "fate_protected": protected,
            "strategy": "深入", "loot": [{"name": name, "quantity": quantity}], "notes": [HIT]}


def payload(fights):
    # 昨天的第 7、8 场（败）还留在 history 里，不能算进今天
    history = [{"day_key": "2000-01-01", "attempts": [attempt(7, "defeat", -144011, False, "灵石", 1), attempt(8, "defeat", -144008, False, "灵石", 1)]}]
    attempts = []
    for count, fight in enumerate(fights, 1):
        attempts = (attempts + [attempt(count, *fight)])[-2:]  # 和 finish_request 一样每轮只留最近两场
        history.append({"day_key": TODAY, "status": "retry_wait", "attempts": list(attempts)})
    run = {**history[-1], "status": "completed", "daily_count": len(fights), "daily_limit": 8, "daily_remaining": 8 - len(fights)}
    return {wild.STATE_KEY: {"run": run, "history": history[-wild.HISTORY_LIMIT:]}}


def check_report():
    text = wild.build_daily_report(payload(FIGHTS), tianji_value=111, tianji_checked_at=NOW, now=NOW)
    lines = text.splitlines()
    assert lines[0] == f"【野外历练日报 {TODAY[5:]}】深入，8/8 场", lines[0]
    assert lines[1] == "改命脱险 5 · 胜 2 · 平安 1", lines[1]
    assert lines[2] == "修为 +135,000", lines[2]
    assert "推命命中 +8" in lines[3] and "改命挡下 5 场" in lines[3] and "显示 111" in lines[3], lines[3]
    assert "灵石×1,717" in lines[4] and "天凤之翎×1" in lines[4], lines[4]
    assert lines[5] == "1. 改命脱险 +0，养魂木×1" and lines[12] == "8. 改命脱险 +0，灵石×404", lines[5:]
    assert "144" not in text and "只记到" not in text, text  # 昨天的败局没混进来
    partial = wild.build_daily_report(payload(FIGHTS[:3]), now=NOW)
    assert "3/8 场" in partial and "只记到 3 场" in partial and "显示" not in partial, partial


class Client:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send_message(self, peer, text):
        if self.fail:
            raise ConnectionError("offline")
        self.sent.append((peer, text))


def check_runner():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "t.db")
        storage.init_schema()
        pid = storage.create_profile("t").id
        tx.save_profile_record(storage, pid, state={"tianji_value": 111, "last_panel_checked_at": NOW})
        full, half = payload(FIGHTS), payload(FIGHTS[:4])
        key = f"wild_experience_report:{pid}"

        def tick(client, data):
            return asyncio.run(ex._run_wild_experience_report(client, storage, pid, data))

        client = Client()
        tick(client, full)
        assert client.sent == [], "没开就不发（每个号的 worker 都跑这段）"
        storage.set_runtime_state(key, '{"enabled": true}')
        tick(client, half)
        assert client.sent == [], "没打满不发"
        failing = Client(fail=True)
        tick(failing, full)
        tick(client, full)
        assert client.sent == [], "发失败后 10 分钟内不重试"
        storage.set_runtime_state(key, '{"enabled": true, "failed_at": 1}')
        tick(client, full)
        tick(client, full)
        assert len(client.sent) == 1 and client.sent[0][0] == "me" and "8/8 场" in client.sent[0][1], client.sent
        # payload 没传时自己去读缓存
        storage.upsert_external_account(pid, ASC_EXTERNAL_PROVIDER, "", "", "connected", "", full, "")
        storage.set_runtime_state(key, '{"enabled": true}')
        tick(client, None)
        assert len(client.sent) == 2, client.sent


def main() -> None:
    logging.disable(logging.CRITICAL)
    check_report()
    check_runner()
    print("wild experience report: ok")


if __name__ == "__main__":
    main()
