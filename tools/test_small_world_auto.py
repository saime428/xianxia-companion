"""小世界自动决策自检。

关键事实（都来自群里真实回包统计）：
- `.神迹 赈灾` / `.神迹 布道` / `.安抚信徒` 共用一个 3 小时神谕冷却
- 赈灾 1000 灵石，额外恢复约 900 人口；布道只涨信仰/稳定
- `.显灵` 走自己的 6 小时祈愿冷却，和神迹不冲突

运行：.venv/bin/python tools/test_small_world_auto.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
import biz_small_world_game as sw

NOW = 1_800_000_000.0

PANEL = """【测试的小世界】

⛩️ 神庙: Lv.1【草创神龛】
👥 人口: {pop} 人
🏙️ 承载上限: 100000 人
🙏 信仰: {faith} / 100
⚖️ 稳定: {stab} / 100
☁️ 待收香火: {incense}
🏺 香火库存: 10
🔥 预计产出: 15.98 香火/小时
🛡️ 护界禁制: 未开启
🧠 神识强度: 0
{prayer}
下一阶【乡土神庙】消耗：香火x3000、灵石x10000
"""

PRAYER_OPEN = """🔥 凡人祈愿：瘟疫
📝 一场恶疾在凡人城池中蔓延。
⚡ 显灵消耗: 清灵丹x2
请使用 .显灵 响应祈愿，或忽略之。
"""
PRAYER_NONE = "暂无祈愿，凡间风调雨顺。\n(下一次祈愿感应需等待: 4小时34分钟33秒)\n"

ALL_ON = sw.pack_auto_strategy(
    collect_enabled=True,
    collect_interval_hours=24,
    manifest_enabled=True,
    relief_enabled=True,
    preach_enabled=True,
)
# 神迹二选一：灵石烧不起就只留布道，反过来只留赈灾
PREACH_ONLY = sw.pack_auto_strategy(manifest_enabled=True, preach_enabled=True)
RELIEF_ONLY = sw.pack_auto_strategy(manifest_enabled=True, relief_enabled=True)
NO_MIRACLE = sw.pack_auto_strategy(
    collect_enabled=True, collect_interval_hours=24, manifest_enabled=True
)


def panel(pop=99000, faith=100, stab=100, incense="0.00", prayer=PRAYER_NONE):
    return sw.parse_small_world_reply(
        PANEL.format(pop=pop, faith=faith, stab=stab, incense=incense, prayer=prayer),
        created_at=NOW,
    )


def commands(state, strategy=ALL_ON, **kw):
    kw.setdefault("now", NOW)
    return sw.build_auto_commands(state, strategy, **kw)


# --- 面板解析没跑偏 ---
p = panel(pop=99000, faith=63, stab=82, incense="107.63", prayer=PRAYER_OPEN)
assert p["opened"] and p["population_value"] == 99000 and p["capacity_value"] == 100000
assert p["faith"].startswith("63") and p["pending_incense_value"] == 107.63
assert p["prayer_title"] == "瘟疫" and p["prayer_cooldown_seconds"] == 0

# --- 优先级：显灵排在神迹前，赈灾排在布道前，但一轮只发第一条 ---
assert commands(p) == [sw.SMALL_WORLD_COLLECT_COMMAND, sw.SMALL_WORLD_MANIFEST_COMMAND,
                       sw.SMALL_WORLD_RELIEF_COMMAND], commands(p)

# --- 神迹是二选一：两个都开时赈灾优先，布道绝不出现 ---
q = panel(faith=50, stab=50)
assert commands(q) == [sw.SMALL_WORLD_RELIEF_COMMAND]

# --- 赈灾被"国库空虚"挡住时，退回布道 ---
assert commands(q, relief_blocked_until=NOW + 600) == [sw.SMALL_WORLD_PREACH_COMMAND]

# --- 共用冷却没过：赈灾和布道都不发 ---
assert commands(q, miracle_cooldown_until=NOW + 600) == []

# --- 2026-09-09：发哪个只看开关，不再看信仰/稳定/人口 ---
# 旧逻辑要求"信仰或稳定没满"才布道。可这游戏里两者顶到 100 就不会回落，
# 布道那条分支等于永久死掉：09-08 14:22 双百之后 9 个多小时一条神迹都没发，
# 每 90 分钟只换回一句"无需操作"。
full = panel(pop=100000, faith=100, stab=100)
assert commands(full, PREACH_ONLY) == [sw.SMALL_WORLD_PREACH_COMMAND]
assert commands(full, RELIEF_ONLY) == [sw.SMALL_WORLD_RELIEF_COMMAND]
assert commands(full) == [sw.SMALL_WORLD_RELIEF_COMMAND]  # 都开还是赈灾优先
assert commands(full, relief_blocked_until=NOW + 600) == [sw.SMALL_WORLD_PREACH_COMMAND]

# --- 灵石不够就只开布道：面板长什么样都只出布道，绝不出赈灾 ---
for state in (full, q, panel(pop=6000, faith=76, stab=87)):
    assert commands(state, PREACH_ONLY) == [sw.SMALL_WORLD_PREACH_COMMAND], state["raw_text"]

# --- 布道自己被"法力不足"挡住时，这一轮就什么都不发 ---
assert commands(full, PREACH_ONLY, preach_blocked_until=NOW + 600) == []

# --- 两个开关都关 -> 根本不碰神迹 ---
assert commands(full, NO_MIRACLE) == []

# --- 收割每天一次 ---（用不带神迹的策略，免得神迹混进来）
rich = panel(pop=100000, incense="500.00")
assert commands(rich, NO_MIRACLE) == [sw.SMALL_WORLD_COLLECT_COMMAND]
assert commands(rich, NO_MIRACLE, last_collect_at=NOW - 23 * 3600) == []
assert commands(rich, NO_MIRACLE, last_collect_at=NOW - 25 * 3600) == [
    sw.SMALL_WORLD_COLLECT_COMMAND
]
assert commands(panel(pop=100000, incense="0.30"), NO_MIRACLE) == []  # 三毛香火不值一条指令

# --- 显灵资源不够被拒后要歇一阵，而且不能把神迹名额一直占着 ---
# 实测：9/5 18:44 ~ 9/6 02:07 之间「显灵所需香火不足」重复了 28 次，
# 每半小时一条，并且因为显灵排在神迹前面，整整 8 小时一次赈灾都没做成。
poor = panel(pop=6000, faith=76, stab=87, prayer=PRAYER_OPEN)
assert commands(poor)[0] == sw.SMALL_WORLD_MANIFEST_COMMAND
blocked = sw.resolve_manifest_blocked_until(
    {"text": "显灵所需香火不足 (需要 250，拥有 1)。", "created_at": NOW}
)
assert blocked == NOW + sw.SMALL_WORLD_MANIFEST_RETRY_SECONDS, blocked
after = commands(poor, manifest_blocked_until=blocked)
assert sw.SMALL_WORLD_MANIFEST_COMMAND not in after, after
assert after == [sw.SMALL_WORLD_RELIEF_COMMAND], after  # 名额让给赈灾
# 歇够了要能自己回来
assert commands(poor, manifest_blocked_until=NOW - 1)[0] == sw.SMALL_WORLD_MANIFEST_COMMAND

# 其他资源和"压根没祈愿"同样算被挡；成功/失败结算不算（它们已经吃了 6 小时祈愿冷却）
for text in ("显灵所需【清灵丹】不足 (需要 2)。", "显灵所需【灵石】不足 (需要 500)。",
             "当前没有凡人祈愿需要处理。"):
    assert sw.resolve_manifest_blocked_until({"text": text, "created_at": NOW}) > NOW, text
for text in ("✅ 显灵成功！\n(信仰 +10, 稳定 +8, 人口 +0)\n下一次凡人祈愿感应需等待 360 分钟。",
             "❌ 显灵失败...\n(信仰 -8, 稳定 -10, 人口 -80)"):
    assert sw.resolve_manifest_blocked_until({"text": text, "created_at": NOW}) == 0, text
assert sw.resolve_manifest_blocked_until(None) == 0

# --- 祈愿冷却中不显灵 ---
cooling = sw.parse_small_world_reply(
    PANEL.format(
        pop=99000, faith=50, stab=50, incense="0.00",
        prayer="🔥 凡人祈愿：大旱\n📝 赤地千里。\n⚡ 显灵消耗: 灵石x500\n"
               "(下一次祈愿感应需等待: 2小时)\n",
    ),
    created_at=NOW,
)
assert sw.SMALL_WORLD_MANIFEST_COMMAND not in commands(cooling)

# --- 神迹回包解析：占冷却 vs 只是资源不够 ---
shared = sw.parse_miracle_reply("凡间方才承受神谕，需再等待 2小时59分钟44秒。")
assert shared == {"cooldown_seconds": 10784, "shared": True}, shared
ok_relief = sw.parse_miracle_reply(
    "【天降甘霖】\n你消耗了 1000 灵石，化作无边甘霖滋润凡间！\n"
    "凡人感念神恩，人口恢复了 944 人，信仰提升至 100，稳定提升至 100！"
)
assert ok_relief == {"cooldown_seconds": sw.MIRACLE_COOLDOWN_SECONDS, "shared": True}
ok_preach = sw.parse_miracle_reply(
    "【神音浩荡】\n你消耗 8000 点修为，在天穹之上显化法相，传颂大道！\n"
    "凡人狂热膜拜，信仰提升至 100，稳定提升至 100！"
)
assert ok_preach["shared"] is True
broke = sw.parse_miracle_reply("国库空虚！赈灾需要 1000 灵石。")
assert broke == {"cooldown_seconds": sw.MIRACLE_COOLDOWN_SECONDS, "shared": False}
assert sw.parse_miracle_reply("法力不足！显化神迹布道需要 8000 点修为。")["shared"] is False
assert sw.parse_miracle_reply("")["cooldown_seconds"] == 0

# 只有真占冷却的回包才计入倒计时
assert sw.resolve_miracle_cooldown_until({"text": "国库空虚！赈灾需要 1000 灵石。",
                                          "created_at": NOW}) == 0
assert sw.resolve_miracle_cooldown_until(
    {"text": "凡间方才承受神谕，需再等待 1小时。", "created_at": NOW}
) == NOW + 3600
assert sw.resolve_miracle_cooldown_until(None) == 0

# 两条命令的回包取新的那条
older = {"text": "a", "created_at": NOW - 10}
newer = {"text": "b", "created_at": NOW}
assert sw.pick_latest_miracle_reply(older, newer) is newer
assert sw.pick_latest_miracle_reply(None, older) is older
assert sw.pick_latest_miracle_reply(None, None) is None

# --- 旧策略行（只有 t/p，没有 h/r）还能读，不会崩 ---
legacy = sw.unpack_auto_strategy('{"c":1,"t":100.0,"q":1,"m":1,"p":1,"i":1800}')
assert legacy["collect_enabled"] and legacy["preach_enabled"]
assert legacy["relief_enabled"] is False
assert legacy["collect_interval_hours"] == sw.SMALL_WORLD_DEFAULT_COLLECT_INTERVAL_HOURS

# --- 没开辟 / 关掉开关就什么都不发 ---
assert commands(sw.parse_small_world_reply("尚未开辟小世界")) == []

# --- 冷却期里不刷面板：全靠 payload 算下一个醒来时间 ---
# 实测 9/7 下午：每 30 分钟一条 .小世界 换一句"无需操作"，2 小时里 5 条全是空转。
EDICT = "2026-09-07T13:56:46.325640+00:00"   # +3h = 16:56:46
PRAYER = "2026-09-07T10:40:54.833147+00:00"  # +6h = 16:40:54
T1500 = 1789052400.0  # 仅作相对基准，下面只比较大小关系


def payload(**kw):
    base = {"faith": 58, "stability": 66, "population": 2950, "active_prayer": None,
            "last_edict_time": EDICT, "last_prayer_time": PRAYER}
    base.update(kw)
    return {"small_world": base}


edict_ready = sw._parse_iso_ts(EDICT) + sw.MIRACLE_COOLDOWN_SECONDS
prayer_ready = sw._parse_iso_ts(PRAYER) + sw.PRAYER_COOLDOWN_SECONDS
assert edict_ready > prayer_ready  # 这组数据里祈愿先到

early = prayer_ready - 3600


def wake(pl, strategy=ALL_ON, now=None, last_collect_at=None):
    return sw.resolve_next_wakeup_from_payload(
        pl, strategy, now=early if now is None else now,
        last_collect_at=early if last_collect_at is None else last_collect_at)


# 两个冷却都没到 -> 睡到最早的那个（祈愿），而不是每 30 分钟刷一次
assert wake(payload()) == prayer_ready, wake(payload())
# 冷却都过了 -> 立刻去刷面板
assert wake(payload(), now=edict_ready + 1) == 0
# 有祈愿挂着就别睡
assert wake(payload(active_prayer={"name": "瘟疫"})) == 0
# 信仰稳定双满也照睡：神迹不再看这两个值，冷却才是唯一的门
# （旧逻辑在这里返回 0，双百时每 90 分钟空刷一次面板）
assert wake(payload(faith=100, stability=100)) == prayer_ready
# 收割到点了也要醒
assert wake(payload(), last_collect_at=early - 25 * 3600) == 0
# payload 缺字段 / 没同步上 -> 一律照旧刷面板，别把功能锁死
assert wake({"small_world": {}}) == 0
assert wake({"small_world": {"faith": 58}}) == 0
assert wake({}) == 0
assert wake(None) == 0
assert wake(payload(last_edict_time=None)) == 0
# 关掉的功能不参与计算：只开收割时，睡到下次收割
only_collect = sw.pack_auto_strategy(collect_enabled=True, collect_interval_hours=24)
assert wake(payload(), strategy=only_collect, last_collect_at=early) == early + 24 * 3600
# 全关 -> 没有候选时间，照旧走老路
assert wake(payload(), strategy=sw.pack_auto_strategy()) == 0

# --- 天机阁 payload 就能看出还没开辟，不用发指令去问 ---
assert sw.is_small_world_unopened({"small_world": None}) is True
assert sw.is_small_world_unopened({"small_world": {}}) is True
assert sw.is_small_world_unopened({"small_world": {"temple_level": 1}}) is False
# 字段缺失 = payload 没同步上，这时候宁可照旧发指令问，别把功能锁死
assert sw.is_small_world_unopened({"dao_name": "甲真人"}) is False
assert sw.is_small_world_unopened({}) is False
assert sw.is_small_world_unopened(None) is False
assert commands(q, sw.pack_auto_strategy()) == []

print("ok")
