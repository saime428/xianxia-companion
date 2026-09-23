"""Daily Tianxing duel run: rotate targets, same target at least GAP minutes apart, stop when
神念 runs out or every target hit its 24h cap.

usage: PYTHONPATH=app/src python tools/tianxing_duel_daily.py PROFILE_ID GAP_MINUTES MAX_ROUNDS t1 t2 ...
A target is anything the game resolves: @username, bare username, or bare Telegram user id
(`.斗法 1000000022` resolved to @rival_beta). Use the bare user id for our own 小号 — 天机阁
reports a `username` that is the game handle, not always a live Telegram username (乙真人
demo_alt_old→demo_alt, 丁真人 demo_alt2), and an @ that no longer resolves burns 神念 for nothing.
installed in the xianxia crontab on the VPS (system TZ is UTC, so 19:20 UTC = 03:20 Beijing):
  20 19 * * * cd /opt/xianxia-companion && TZ=Asia/Shanghai PYTHONPATH=app/src .venv/bin/python tools/tianxing_duel_daily.py 2 15 10 1000000013 1000000014 >> data/tianxing_duel_daily.log 2>&1
  攻方 甲真人 (profile 2)；目标 1000000013 = 乙真人 (profile 3)、1000000014 = 丁真人 (profile 4)。
  不再打 @RivalAlpha。同一目标每天最多 5 场；只有两个小号时，有一个被跳过就凑不满 10 点神念。
A target that is one of our own profiles is skipped while it is 夺舍-locked or its 天机阁 status
is not normal (残魂 ESCAPED_SOUL, 虚弱…): these duels only exist for the 推命 天机点, and a 残魂
beaten again can 陨落 for good. It is re-checked every round, so a reborn 小号 rejoins the run.

ponytail: in-memory last-hit table, one process per run; the attacker cooldown and the
other-route 推命 check live in tianxing_group.run_group.
"""

import re
import sys
import time

from tianxing_group import run_group
from tg_game.config import get_settings
from tg_game.services.external_sync import read_cached_external_payload
from tg_game.services.profile_rebirth import is_profile_rebirth_locked
from tg_game.storage import Storage

SPIRIT = re.compile(r"今日神念[:：]\s*(\d+)\s*/\s*10")
REMAINING = re.compile(r"对此人剩余交锋[:：]\s*(\d+)")


def unavailable_reason(storage, target):
    """Why an own-profile target must not be hit right now; "" for outsiders and healthy 小号."""
    key = target.lstrip("@").lower()
    for profile in storage.list_profiles():
        if key not in {str(profile.telegram_user_id), (profile.telegram_username or "").lower()}:
            continue
        name = profile.display_name or profile.name
        if is_profile_rebirth_locked(storage, profile.id):
            return f"{name} 夺舍重生中"
        # ponytail: payload is at most one keepalive (~15 min) old; stale "normal" can still slip through
        status = str(read_cached_external_payload(storage, profile.id).get("status") or "normal")
        return "" if status.lower() == "normal" else f"{name} 状态 {status}"
    return ""


def main(argv):
    profile_id = int(argv[1])
    gap = int(argv[2]) * 60
    max_rounds = int(argv[3])
    targets = argv[4:]
    last = {t: 0.0 for t in targets}
    done = set()
    rounds = 0
    storage = Storage(get_settings().database_path)
    shown_blocked = {}
    print(f"===== daily duel run {time.strftime('%F %T')} targets={targets} gap={gap}s", flush=True)
    while rounds < max_rounds and len(done) < len(targets):
        now = time.time()
        open_targets = [t for t in targets if t not in done]
        blocked = {t: why for t in open_targets if (why := unavailable_reason(storage, t))}
        if blocked != shown_blocked:
            print(f"  .. skipping {blocked}" if blocked else "  .. all targets available again", flush=True)
            shown_blocked = blocked
        ready = [t for t in open_targets if t not in blocked]
        if not ready:
            print("every remaining target is unavailable, ending run", flush=True)
            break
        eligible = [t for t in ready if now - last[t] >= gap]
        if not eligible:
            wait = min(last[t] + gap - now for t in ready)
            print(f"  .. all targets inside the {gap // 60} min gap, sleeping {int(wait)}s", flush=True)
            time.sleep(wait)
            continue
        target = min(eligible, key=lambda t: last[t])
        print(f"----- round {rounds + 1} vs {target} {time.strftime('%H:%M:%S')}", flush=True)
        rc, replies = run_group(profile_id, [".推命 斗法", f".斗法 {target}"])
        last[target] = time.time()
        report = replies[-1] if replies else ""
        if rc == 4:
            if "天道有则" in report:
                done.add(target)
                continue
            print("hard stop, ending run", flush=True)
            break
        if rc == 5:
            print("another route pending, ending run", flush=True)
            break
        if rc != 0:
            continue
        rounds += 1
        spirit = SPIRIT.search(report)
        if spirit and int(spirit.group(1)) <= 0:
            print("神念 exhausted", flush=True)
            break
        remaining = REMAINING.search(report)
        if remaining and int(remaining.group(1)) <= 0:
            done.add(target)
    run_group(profile_id, [".天机盘"])
    print(f"===== done rounds={rounds} capped={sorted(done)} {time.strftime('%H:%M:%S')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
