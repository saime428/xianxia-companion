"""Exploration gate as a command group: other-route lock, queue what is missing, release when set.

run: PYTHONPATH=app/src python tools/test_tianxing_gate.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app" / "src"))

from tg_game.features.tianxing import biz_tianxing_runtime as tx  # noqa: E402
from tg_game.storage import Storage  # noqa: E402

CHAT = -1001000000001
DAY = tx.get_day_key()


def queued(storage, profile_id):
    with storage.connect() as conn:
        return [r[0] for r in conn.execute("select text from outgoing_commands where profile_id=? order by id", (profile_id,))]


def gate(storage, profile_id, now):
    return tx.build_exploration_route_gate(storage, profile_id=profile_id, chat_id=CHAT, thread_id=1, chat_type="group", bot_username="fanrenxiuxian_bot", now=now)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "t.db")
        storage.init_schema()
        profile = storage.create_profile("t")
        storage.update_profile_sect_info(profile.id, sect_name="天星宗")
        now = time.time()
        tx.set_profile_config(storage, profile.id, {"auto_predict_enabled": True, "auto_change_fate_enabled": True})
        base = {"observed_stars": ["贪狼"], "observed_stars_day": DAY, "available_stars": ["贪狼"], "available_stars_source": "observe", "available_stars_day": DAY, "fixed_star": "贪狼", "fixed_star_day": DAY}

        # 1. a pending 斗法 推命 blocks exploration and queues nothing
        tx.save_profile_record(storage, profile.id, state={**base, "current_prediction": "斗法", "current_prediction_until": now + 3600})
        result = gate(storage, profile.id, now)
        assert result["allowed"] is False and "斗法" in result["reason"], result
        assert queued(storage, profile.id) == [], queued(storage, profile.id)

        # 2. nothing pending: both 推命 探索 and 改命 探索 go out as one group, gate stays closed
        tx.save_profile_record(storage, profile.id, state=base)
        result = gate(storage, profile.id, now)
        assert result["allowed"] is False, result
        assert queued(storage, profile.id) == [".推命 探索", ".改命 探索"], queued(storage, profile.id)

        # 3. both confirmed and the 天机盘 was checked recently: released without extra commands
        armed = {**base, "current_prediction": "探索", "current_prediction_until": now + 3600, "current_change": "探索", "current_change_until": now + 3600}
        tx.save_profile_record(storage, profile.id, state={**armed, "last_panel_checked_at": now - 60})
        result = gate(storage, profile.id, now)
        assert result["allowed"] is True, result
        assert len(queued(storage, profile.id)) == 2

        # 3b. 09-26: 本地记着改命还在，其实 09-25 12:47 裂缝败局「改命回天」已经用掉了（回包没认领上），
        # 零点 8 场深入全裸打。本地说在、但半小时内没对过天机盘 → 先查盘，不放行
        tx.save_profile_record(storage, profile.id, state={**armed, "last_panel_checked_at": now - 11 * 3600})
        result = gate(storage, profile.id, now)
        assert result["allowed"] is False and queued(storage, profile.id)[-1] == ".天机盘", (result, queued(storage, profile.id))
        # 盘上写「当前改命: 无」→ 本地清掉，下一轮补挂改命
        panel = "【天机盘】\n今日可选命星: 【贪狼】\n今日已定命星: 【贪狼】\n当前推命: 探索（剩余 7小时50分钟）\n当前改命: 无\n天机值: 121\n逆命劫: 0"
        record = tx.get_profile_record(storage, profile.id)
        after = tx.apply_parsed_to_state(record["state"], tx.parse_tianxing_text(panel, now=now), now=now)
        assert after["current_change"] == "" and after["last_panel_checked_at"] == now, after
        tx.save_profile_record(storage, profile.id, state=after)
        with storage.connect() as conn:  # 盘的回包到了 = 这条指令已确认
            conn.execute("update outgoing_commands set status='confirmed'")
        result = gate(storage, profile.id, now + 5)
        assert result["allowed"] is False and queued(storage, profile.id)[-1] == ".改命 探索", (result, queued(storage, profile.id))

        # 4. a duel settlement consumes the pending 斗法 推命 (not only 炼制 any more)
        state = tx.normalize_state({"current_prediction": "斗法", "current_prediction_until": now + 3600})
        parsed = tx.parse_tianxing_text("【天道战报·文字版】\n胜者：@me\n【推命命中】司命演算吻合，天机值 +1，宗门贡献 +30", now=now)
        after = tx.apply_parsed_to_state(state, parsed, now=now)
        assert after["current_prediction"] == "", after["current_prediction"]

        # 5. MiniApp 野外历练 notes reach the state the same way
        tx.save_profile_record(storage, profile.id, state={**base, "current_prediction": "探索", "current_prediction_until": now + 3600})
        tx.apply_settlement_text(storage, profile.id, "【野外历练】\n【推命命中】司命演算吻合，天机值 +1，宗门贡献 +30", now=now)
        assert tx.normalize_state(tx.get_profile_record(storage, profile.id)["state"])["current_prediction"] == ""

        # 6. the daily star falls back to 紫微 when neither 贪狼 nor 太阴 showed up
        assert tx._select_set_star(tx.normalize_config({}), ["天府", "紫微"]) == "紫微"

        # 7. occupied 改命 斗法: wait until it expires, do not keep sending 推命/改命 探索
        before = queued(storage, profile.id)
        change_until = now + 22 * 3600 + 17 * 60
        tx.save_profile_record(
            storage,
            profile.id,
            state={
                **base,
                "current_prediction": "探索",
                "current_prediction_until": now + 8 * 3600,
                "current_change": "斗法",
                "current_change_until": change_until,
                "current_change_until_source": "cooldown_reply",
            },
        )
        result = gate(storage, profile.id, now)
        assert result["allowed"] is False, result
        assert "斗法" in result["reason"], result
        assert abs(float(result["next_time"]) - change_until) < 1, result
        assert queued(storage, profile.id) == before, queued(storage, profile.id)

        # 8. the game cooldown reply is what stamps 改命 斗法 + remaining time
        parsed = tx.parse_tianxing_text(
            "你已有一道关于【斗法】的改命尚未耗尽，还可维持 22小时17分钟。",
            now=now,
        )
        assert parsed.get("action") == "改命", parsed
        assert parsed.get("result") == "cooldown", parsed
        assert parsed.get("current_change") == "斗法", parsed
        assert abs(float(parsed.get("current_change_until") or 0) - change_until) < 1, parsed
    print("test_tianxing_gate: ok")


if __name__ == "__main__":
    main()
