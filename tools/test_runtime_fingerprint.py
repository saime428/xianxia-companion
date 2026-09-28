"""运行版本指纹覆盖整个 app/src：以后新加的模块改了，指纹也得变。

09-28 审计 A3：原来的手工清单漏了世界 Boss、天机命脉、external_sync，只改这些文件忘了重启，
健康检查照样报 telegram_code_current=true。
运行：.venv/bin/python -B tools/test_runtime_fingerprint.py
"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game import runtime_status

files = runtime_status.runtime_source_files()
for missed in (
    "tg_game/services/external_sync.py",
    "tg_game/features/world_boss/world_boss_features.py",
    "tg_game/features/fate_cards/biz_fate_cards_miniapp.py",
):
    assert missed in files, missed
assert files == sorted(files) and not [name for name in files if not name.endswith(".py")]

with tempfile.TemporaryDirectory() as folder:
    base = Path(folder)
    (base / "pkg").mkdir()
    (base / "a.py").write_text("x = 1\n", encoding="utf-8")
    with patch.object(runtime_status, "BASE_DIR", base):
        before = runtime_status.compute_runtime_code_fingerprint()
        assert runtime_status.compute_runtime_code_fingerprint() == before
        (base / "pkg" / "new_feature.py").write_text("y = 1\n", encoding="utf-8")
        added = runtime_status.compute_runtime_code_fingerprint()
        assert added != before
        (base / "pkg" / "new_feature.py").write_text("y = 2\n", encoding="utf-8")
        edited = runtime_status.compute_runtime_code_fingerprint()
        assert edited != added
        (base / "notes.txt").write_text("不是代码", encoding="utf-8")
        assert runtime_status.compute_runtime_code_fingerprint() == edited

print("runtime fingerprint: ok")
