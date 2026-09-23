import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app' / 'src'))
from biz_fishing_game import fishing_rod_from_inventory
from tg_game.features.fishing.biz_fishing_view_model import build_fishing_view

payload = {'inventory': {'items': [{'name': '银竹钓竿', 'item_id': 'item_fishing_rod_silver', 'description': '持有后每日可垂钓 10 竿。'}]}}
session = {'daily_count': 1, 'daily_limit': 5, 'rod_text': '青竹钓竿（每日 5 竿）', 'state': 'idle'}
view = build_fishing_view(session, payload=payload)
assert view['daily_limit'] == 10, view
assert '银竹' in view['rod_text'], view
view2 = build_fishing_view(session)
assert view2['daily_limit'] == 5
assert fishing_rod_from_inventory(payload)['daily_limit'] == 10
print('ok')
