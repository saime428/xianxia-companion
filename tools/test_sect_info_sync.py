"""天机阁同步把宗门写进 profiles 的自检；临时数据库，不连接游戏。

运行：uv run --with-requirements requirements.txt python -X utf8 -B tools/test_sect_info_sync.py
"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.services import external_sync
from tg_game.storage import Storage


def check():
    with tempfile.TemporaryDirectory() as folder:
        storage = Storage(Path(folder) / "sect.db")
        storage.init_schema()
        pid = storage.create_profile("sect-test").id
        storage.update_profile_sect_info(pid, sect_name="散修")

        def refresh(payload):
            def fetch(*args, **kwargs):
                return payload, "sect-test", "", ""
            with patch.object(external_sync, "fetch_cultivator_payload", fetch):
                external_sync.sync_external_account(storage, pid, cookie_text="session=offline")

        # 后台同步（不经过网页刷新）也要把新宗门写进档案
        refresh({"sect_name": "星宫", "is_sect_elder": True, "sect_contribution": 3345})
        profile = storage.get_profile(pid)
        assert profile.sect_name == "星宫", profile.sect_name
        assert profile.sect_position == "长老", profile.sect_position
        assert profile.sect_contribution_text == "3345", profile.sect_contribution_text
        # 残缺 payload（没有 sect_name 字段）不能把宗门冲成空
        refresh({"active_buffs": "{}"})
        assert storage.get_profile(pid).sect_name == "星宫"
        # 退宗照常写
        refresh({"sect_name": "散修", "is_sect_elder": False})
        profile = storage.get_profile(pid)
        assert (profile.sect_name, profile.sect_position) == ("散修", ""), profile


check()
print("ok")
