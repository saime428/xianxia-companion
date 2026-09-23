"""Daily duel must skip our own 小号 while they are 残魂 / 夺舍-locked, and nobody else.

run: PYTHONPATH=app/src python tools/test_duel_target_guard.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app" / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from tg_game.services import profile_rebirth  # noqa: E402
from tg_game.services.external_sync import ASC_PROVIDER  # noqa: E402
from tg_game.storage import Storage  # noqa: E402
from tianxing_duel_daily import unavailable_reason  # noqa: E402


def alt(storage, name, uid, username, status=None):
    profile = storage.create_profile(name)
    storage.bind_profile_telegram_account(profile.id, telegram_user_id=uid, telegram_username=username)
    if status is not None:
        storage.upsert_external_account(profile.id, ASC_PROVIDER, uid, username, "connected", "", {"status": status}, "")
    return profile


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "t.db")
        storage.init_schema()
        alt(storage, "乙真人", "1000000013", "demo_alt", status="ESCAPED_SOUL")
        alt(storage, "丁真人", "1000000014", "demo_alt2", status="normal")
        alt(storage, "新号", "7000000001", "fresh01")  # no 天机阁 payload yet
        locked = alt(storage, "锁定号", "7000000002", "locked01", status="normal")
        profile_rebirth.start_profile_rebirth(
            storage, profile_id=locked.id, chat_id=-100, thread_id=None, chat_type="group", bot_username="b",
        )

        assert unavailable_reason(storage, "@RivalAlpha") == ""  # outsider: never our business
        assert unavailable_reason(storage, "1000000014") == ""  # healthy 小号 by bare uid
        assert unavailable_reason(storage, "@DEMO_ALT2") == ""  # username match is case-insensitive
        assert "ESCAPED_SOUL" in unavailable_reason(storage, "1000000013")
        assert "ESCAPED_SOUL" in unavailable_reason(storage, "@demo_alt")
        assert "夺舍重生中" in unavailable_reason(storage, "7000000002")  # lock wins over a stale "normal"
        assert unavailable_reason(storage, "7000000001") == ""  # no payload = assume normal
    print("ok")


if __name__ == "__main__":
    main()
