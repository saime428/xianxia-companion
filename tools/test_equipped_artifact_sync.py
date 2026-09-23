"""祭出法宝同步自检。

`.探寻裂缝` 的冷却按「有没有祭出风雷翅」算（9 小时 / 12 小时），
而这个判断读的是 profiles.artifact_text。以前只有网页刷新才写这一列，
后台 keepalive 不写 —— 换装备后要等到手动刷新才生效。

运行：.venv/bin/python tools/test_equipped_artifact_sync.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
import biz_fanren_game as fg
from tg_game.services.external_sync import _sync_equipped_artifacts
from tg_game.web.biz_web_display_formatting import format_external_artifacts


def payload(*names):
    return {
        "equipped_treasure_id": [f"t{i}" for i, _ in enumerate(names)],
        "inventory": {
            "items": [
                {"item_id": f"t{i}", "name": n, "durability": 100, "max_durability": 100}
                for i, n in enumerate(names)
            ]
        },
    }


class FakeStorage:
    def __init__(self, artifact_text=""):
        self.artifact_text = artifact_text
        self.writes = 0

    def get_profile(self, profile_id):
        return SimpleNamespace(artifact_text=self.artifact_text)

    def update_profile_game_info(self, profile_id, **fields):
        self.writes += 1
        if "artifact_text" in fields:
            self.artifact_text = fields["artifact_text"]


WINGS = fg.RIFT_WIND_THUNDER_WINGS_NAME
NORMAL = fg.RIFT_EXPLORE_COOLDOWN_SECONDS
FAST = fg.RIFT_WIND_THUNDER_WINGS_COOLDOWN_SECONDS
assert FAST < NORMAL, (FAST, NORMAL)

# 祭出风雷翅 -> 9 小时
st = FakeStorage()
_sync_equipped_artifacts(st, 2, payload(WINGS, "青竹蜂云剑"))
assert WINGS in st.artifact_text, st.artifact_text
assert fg.get_rift_cooldown_seconds(st, 2) == FAST

# 散念之后同一份同步要把它摘掉 -> 退回 12 小时
_sync_equipped_artifacts(st, 2, payload("青竹蜂云剑", "皇鳞甲"))
assert WINGS not in st.artifact_text, st.artifact_text
assert fg.get_rift_cooldown_seconds(st, 2) == NORMAL

# 什么都没祭出：合法的空列表，可以写空
_sync_equipped_artifacts(st, 2, {"equipped_treasure_id": [], "inventory": {"items": []}})
assert st.artifact_text == ""

# 关键保护：payload 里没有这个字段时绝不能写——写空会静默退回 12 小时
st = FakeStorage(artifact_text=f"- {WINGS}: 100/100")
before = st.writes
for bad in ({}, {"username": "demo_main"}, None, "", []):
    _sync_equipped_artifacts(st, 2, bad)
assert st.writes == before, "字段缺失时不该写库"
assert fg.get_rift_cooldown_seconds(st, 2) == FAST

# 没传 storage 时退回默认 12 小时，不炸
assert fg.get_rift_cooldown_seconds(None, None) == NORMAL
assert fg.get_rift_cooldown_seconds(FakeStorage(), 0) == NORMAL

# 展示格式本身：只列祭出的，未祭出的不算
text = format_external_artifacts(payload("金光砖"))
assert text == "- 金光砖: 100/100", text

print("ok")
