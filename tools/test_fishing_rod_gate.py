import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app' / 'src'))
from tg_game.web.biz_web_display_formatting import profile_has_fishing_rod

def payload(name):
 return {'inventory': {'items': [{'name': name, 'item_id': 'x', 'quantity': 1}]}}

assert profile_has_fishing_rod(payload('青竹钓竿'))
assert profile_has_fishing_rod(payload('银竹钓竿'))
assert profile_has_fishing_rod(payload('金雷竹钓竿'))
assert not profile_has_fishing_rod(payload('青竹蜂云剑'))
assert not profile_has_fishing_rod({})
print('ok')
