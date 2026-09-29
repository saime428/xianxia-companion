"""天机更新直接刷新已提供字段，不发群查询；空库存、零军功和旧缓存不会混淆。"""
import asyncio
from contextlib import ExitStack
from datetime import datetime
import importlib
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/src'))
from tg_game.storage import Storage, ASC_EXTERNAL_PROVIDER
from tg_game.features.fishing.biz_fishing_view_model import build_fishing_view
from tg_game.web.biz_mulan_view_model import build_mulan_state
from tg_game.web.biz_web_display_formatting import SHANGHAI_TZ
from tg_game.web.module_detail_state import build_fishing_module_state

loop = asyncio.new_event_loop()
def deny(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.sendto'}:
        raise AssertionError('network forbidden')
sys.addaudithook(deny)

with tempfile.TemporaryDirectory() as folder:
    storage = Storage(Path(folder) / 'test.db')
    storage.init_schema()
    profile = storage.create_profile('payload-test')
    storage.bind_profile_telegram_account(profile.id, telegram_user_id='123', telegram_username='self')
    chat = -100000000042
    storage.create_chat_binding(profile.id, chat, thread_id=42, bot_username='fanrenxiuxian_bot')
    binding = storage.get_primary_chat_binding(profile.id)
    now = time.time()
    storage.upsert_game_items([{'id':'mat_fish_qinglin_ji','name':'青鳞小鲫'}, {'id':'item_fishing_bait_plain','name':'凡饵'}])
    session = storage.upsert_fishing_session(profile_id=profile.id, chat_id=chat, enabled=False, state='finished', catches={'旧鱼获':99}, baits={'旧鱼饵':99})
    payload = {'inventory': json.dumps({'materials':{'mat_fish_qinglin_ji':5,'mat_fish_meat':10,'item_fishing_bait_plain':3}}),
               'active_buffs': json.dumps({'mulan_smoke':{'total_merit':0,'streak':0,'matched_orders':0,'danger_wins':0,'last_date':datetime.now(SHANGHAI_TZ).date().isoformat()}})}
    with patch('time.time', return_value=now+10):
        storage.upsert_external_account(profile.id, ASC_EXTERNAL_PROVIDER, '123', 'self', 'connected', 'session=offline', payload, '')
    view = build_fishing_module_state(storage, enabled=True, active_profile=profile, command_chat=binding, build_fishing_view=build_fishing_view, payload=payload)['fishing_state']
    assert view['catches'] == [('青鳞小鲫',5)] and view['baits'] == [('凡饵',3)]
    assert view['inventory_source'] == '天机阁' and view['daily_count'] == session['daily_count']
    empty = build_fishing_view(session, payload={'inventory':{'materials':{}}}, payload_updated_at=now+10)
    assert empty['catches'] == empty['baits'] == [], '天机阁明确空库存要清掉旧记录'
    missing = build_fishing_view(session, payload={'inventory':{}}, payload_updated_at=now+10)
    assert missing['catches'] == [('旧鱼获',99)], '接口缺失不等于空库存'
    stale = build_fishing_view(session, payload=payload, payload_updated_at=now-10)
    assert stale['catches'] == [('旧鱼获',99)], '旧天机数据不能覆盖新钓鱼记录'
    mulan = build_mulan_state(storage, profile, binding)
    assert mulan['military_merit'] == '0' and mulan['streak'] == '0 天' and mulan['status'] == '已支援'
    assert mulan['data_source'] == '天机阁' and not mulan['daily_council'], '不捏造接口没有的军议'
    assert not storage.list_outgoing_commands(profile.id), '读取页面不发指令'

    # 经过实际 HTTP 入口；仅替换外部同步边界，验证单号和全部刷新都不触发宗门查询。
    from tg_game.config import get_settings
    settings = get_settings()
    settings.database_path = storage.path
    settings.authorized_user_id = '123'
    settings.bound_chat_id = None
    storage.set_external_cookie_override('session=offline')
    token = storage.create_app_session(profile.id)
    web = importlib.import_module('tg_game.web.app')
    import httpx
    with ExitStack() as mocks:
        for name in ('_sync_bootstrap_if_needed','_sync_all_items_if_needed','_sync_shop_items_if_needed','_sync_marketplace_listings_if_needed','_sync_profile_from_cultivator','sync_cultivation_session'):
            mocks.enter_context(patch.object(web,name,return_value=None))
        sync = mocks.enter_context(patch.object(web,'sync_external_account',return_value=payload))
        sect = mocks.enter_context(patch.object(Storage,'request_sect_refresh'))
        async def check_http():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.create_app()),base_url='http://test',cookies={web.APP_SESSION_COOKIE:token}) as client:
                assert (await client.post(f'/profiles/{profile.id}/refresh-info')).status_code == 303
                assert (await client.post('/profiles/refresh-all-info')).status_code == 303
                assert sync.call_count == 2 and sect.call_count == 0
                assert not storage.list_outgoing_commands(profile.id)
        loop.run_until_complete(check_http())
loop.close()
print('tianji payload refresh: ok')
