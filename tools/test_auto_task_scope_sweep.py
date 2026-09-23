"""A sect-specific auto task left on after changing sect gets disabled instead of aborting every pass.

run: PYTHONPATH=app/src python tools/test_auto_task_scope_sweep.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app" / "src"))

from tg_game.runtime.executors import _disable_other_sect_auto_tasks  # noqa: E402
from tg_game.storage import Storage  # noqa: E402

CHAT = -1001000000001


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "t.db")
        storage.init_schema()
        profile = storage.create_profile("t")
        storage.update_profile_sect_info(profile.id, sect_name="天星宗")
        for feature_key, strategy in (("taiyi_yindao", "水"), ("artifact_touch", "7200|.抚摸法宝 x")):
            storage.upsert_companion_auto_task(profile_id=profile.id, chat_id=CHAT, feature_key=feature_key, enabled=True, strategy=strategy, bot_username="fanrenxiuxian_bot")

        disabled = _disable_other_sect_auto_tasks(storage, profile.id)
        assert disabled == ["taiyi_yindao"], disabled
        remaining = {t["feature_key"] for t in storage.list_active_companion_auto_tasks(profile.id)}
        assert remaining == {"artifact_touch"}, remaining

        # nothing left to disable: second sweep is a no-op, so the scheduler keeps its old backoff path
        assert _disable_other_sect_auto_tasks(storage, profile.id) == []
    print("test_auto_task_scope_sweep: ok")


if __name__ == "__main__":
    main()
