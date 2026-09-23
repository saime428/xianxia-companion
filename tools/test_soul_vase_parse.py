from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app" / "src"))

from tg_game.features.soul.biz_soul_cultivation import (
    parse_cultivation_reply,
    parse_status_reply,
)
from tg_game.features.vase.biz_vase_condense import parse_bottle_status, parse_condense_reply


def main() -> None:
    assert parse_cultivation_reply("你的第二元神正在(修炼中)，无法分心修炼。") == (
        "need_status",
        0,
    )
    kind, seconds = parse_status_reply(
        "【你的第二元神：火之元神】\n状态: 修炼中 (剩余: 8小时24分钟25秒)"
    )
    assert kind == "cooldown", kind
    assert 8 * 3600 + 24 * 60 + 20 <= seconds <= 8 * 3600 + 24 * 60 + 30, seconds

    assert parse_condense_reply("【掌天瓶·凝液】\n当前绿液：1/1") == ("success", 0)
    kind, seconds = parse_condense_reply("请在 11小时59分钟48秒 后再试")
    assert kind == "cooldown", kind
    assert seconds > 11 * 3600

    assert parse_bottle_status("【掌天瓶】\n凝液状态：此刻可凝液。") == ("idle", 0)
    assert parse_bottle_status("【掌天瓶】\n当前绿液：1/1") == ("success", 0)
    print("ok")


if __name__ == "__main__":
    main()
