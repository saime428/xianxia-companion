"""远航前置在等共历心劫、心劫调度器又以为侍妾在远航：两边互等，每 2 分钟刷一次 .我的侍妾。

09-19 实况：大号 04:00~07:16 刷了 59 次（归航后心劫冷却正好就绪），乙真人 15:42 起刷个不停
（侍妾董萱儿远航途中被南陇侯掳走，那条「均衡航线进行中」再也不会被新消息顶掉）。
_resolve_active_companion_voyage_target 只要最新远航回复是「远航中」、面板里没有归航时间，
就恒返回 now+60。修复后：更新的面板明确空闲才放行，其余情况照旧挡住。

run: PYTHONPATH=app/src python tools/test_voyage_heart_deadlock.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))

from tg_game.runtime import executors as ex  # noqa: E402

NOW = time.time()
# 03:58 董萱儿的面板（被掳前最后一次）、18:04 新侍妾凌玉灵的面板：线上全文（节选会被解析成 unknown）
PANEL_TAIL = (
    "\n\n---\n\n常住洞府与随行均保留既有神通，沿用原情缘、消耗、冷却与远航限制。\n"
    "你已有一位侍妾随行；如需再寻第二位，请先使用 .安置侍妾 将她安置到藏娇阁。"
)
OLD_PANEL_VOYAGING = (
    "1. 你的红尘道侣: 【董萱儿】 (状态: 随行中)\n\n她安静地陪伴着你，虽不通星宫秘法，却也可为你牵引第二期机缘。\n\n"
    "【掩月心契】\n- 当前誓约: 守秘 (闭关奇遇率提高8%)\n- 立誓时间: 2026-09-05 02:47 (Asia/Shanghai)\n"
    "命令: .立誓 护道/守秘/共修、.毁誓\n\n【第二期机缘】\n- 天机代卜链: 残图引（剩余 710分钟）\n"
    "- 坠魔谷护持: 可用（剩余 1433分钟）\n- 入梦寻图冷却: 可施展\n- 共历心劫冷却: 591分钟\n"
    "- 天机代卜冷却: 710分钟\n- 梦图拼片: 虚天 4/4 | 苍坤 4/4\n"
    "命令: .入梦寻图、.残图、.拼图、.共历心劫、.坠魔心劫、.天机代卜\n\n"
    "远航状态: 均衡航线进行中，剩余约 474 分钟。" + PANEL_TAIL
)
NEW_PANEL_IDLE = (
    "1. 你的红尘道侣: 【凌玉灵】 (状态: 随行中)\n\n她安静地陪伴着你，虽不通星宫秘法，却也可为你牵引第二期机缘。\n\n"
    "【掩月心契】\n- 当前誓约: 无\n命令: .立誓 护道/守秘/共修、.毁誓\n\n【第二期机缘】\n- 天机代卜链: 无\n"
    "- 坠魔谷护持: 可用（剩余 1301分钟）\n- 入梦寻图冷却: 339分钟\n- 共历心劫冷却: 可施展\n"
    "- 天机代卜冷却: 578分钟\n- 梦图拼片: 虚天 0/4 | 苍坤 0/4\n"
    "命令: .入梦寻图、.残图、.拼图、.共历心劫、.坠魔心劫、.天机代卜" + PANEL_TAIL
)
STATUS_VOYAGING = "侍妾【南宫婉·月影】正在执行【月殿寻痕】远航。\n预计归航还需 5小时59分钟50秒。"
PANEL_RETURNED = "1. 你的红尘道侣: 【董萱儿】 (状态: 随行中)\n\n远航状态: 侍妾已远航归来，待结算。"


def resolve(voyage_reply, panel_reply):
    with patch.object(ex, "_get_latest_companion_voyage_reply", return_value=voyage_reply), \
            patch.object(ex, "_get_latest_companion_panel_message", return_value=panel_reply):
        return ex._resolve_active_companion_voyage_target(None, profile_id=3, chat_id=-1, thread_id=None, now=NOW)


def msg(text, minutes_ago):
    return {"text": text, "created_at": NOW - minutes_ago * 60}


def main() -> None:
    stale = msg(OLD_PANEL_VOYAGING, 14 * 60)  # 474 分钟的航程，14 小时前：归航时间早过了
    blocked = NOW + ex.COMPANION_VOYAGE_RECHECK_SECONDS
    # fixtures must parse like production did (live check: 18:14 panel -> idle, 03:58 panel -> voyaging)
    assert ex._build_companion_voyage_state_from_reply(msg(NEW_PANEL_IDLE, 1))["status"] == "idle"
    assert ex._build_companion_voyage_state_from_reply(stale)["status"] == "voyaging"

    # the deadlock: newer panel says idle -> not voyaging any more
    assert resolve(stale, msg(NEW_PANEL_IDLE, 1)) == 0.0

    # everything else keeps the old, conservative answer
    assert resolve(stale, msg(NEW_PANEL_IDLE, 15 * 60)) == blocked  # idle panel is older than the voyage
    assert resolve(stale, msg(PANEL_RETURNED, 1)) == blocked  # 归航待结算：远航任务先去 .远航归来
    assert resolve(stale, None) == blocked
    active = resolve(msg(STATUS_VOYAGING, 1), msg(NEW_PANEL_IDLE, 2))
    assert NOW + 5.9 * 3600 < active < NOW + 6 * 3600, active  # real voyage in progress wins
    print("voyage/heart deadlock: ok")


if __name__ == "__main__":
    main()
