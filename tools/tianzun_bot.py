"""小天尊：在自己人的小群里按天机阁数据回和韩天尊一样的面板。

  `.查灵根 A`   → 天命玉牒（同 `.我的灵根`）。天机阁没有第二元神，也没有祭出容量（「当前祭出」行末的 13/15），这两处省掉。
  `.查储物袋 A` → 储物袋（同 `.储物袋`）。材料那段天机阁按编号排，和韩天尊（按入袋先后）顺序不同，条目一样。
  `.查状态 A`   → 身上没到期的负面状态（带说明）、九转还魂丹几颗、增益，深度闭关和侍妾远航还剩多久；
                 每条状态附来源：斗法挂上的（神魂动荡、道心破碎）从战报找「几点输给谁」，其余按 STATUS_SOURCES。
  `.查观星`     → 最近几次观星（下一次天道演化、天命归谁）和最近 8 次整点结算。
  `.查冷却 A`   → 副本、闭关（含搜寻节点）、斗法、侍妾、宗门、小世界、日常的冷却还剩多久；只列 10-15 对过回包或结算时间的项目。
                 黄龙夺灯天机阁不记，按主服务存的副本记录（dungeon_messages）看今天出过手没有；
                 玄骨遗府的每日通关奖励天机阁也不记，只列门票古府寻踪珠几颗。
  `.帮助`       → 上面这些指令的说明，指令点一下就能复制。
  另外主动往群里报元磁山核·乙：有人观星看到下一次是星辰异象就预告，整点结算出了乙再报一条。

A 是 TG 用户名（带不带 @、大小写随意）或完整道号；道号重名就列出候选让人用用户名再查。
不写 A、回复某人的消息发 `.查状态` 等，就查被回复的那个人。TG 昵称天机阁里没有，查不了。
数据取天机阁 /api/cultivator，借主服务库里存的天机阁登录（只读）。

配置写在 /opt/xianxia-companion/.env：
  TIANZUN_BOT_TOKEN=<BotFather 给的 token>
  TIANZUN_BOT_CHAT_IDS=<只在这些群回，逗号分隔；私聊和别的群一概不理>
BotFather 里要 /setprivacy → Disable（或把 bot 设成群管理员），否则群里收不到这些指令。

运行（VPS 上以 xianxia 身份，在 /opt/xianxia-companion 下）：.venv/bin/python tools/tianzun_bot.py
  常驻用 systemd 单元 tools/xianxia-tianzun-bot.service
只读查一次（同样以 xianxia 身份，root 开库会让主服务写不进去）：.venv/bin/python tools/tianzun_bot.py --check '.查储物袋 A'
自检：python tools/tianzun_bot.py --self-check
"""
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app" / "src"))
from tg_game.clients import asc_client  # noqa: E402

ASC_PROVIDER = "asc_aiopenai"  # 同 external_sync.ASC_PROVIDER；不为一个常量拖进主服务整条依赖链
DB = ROOT / "data" / "tg_game.db"
COMMAND = re.compile(r"^[.。]查(灵根|储物袋|状态|冷却)(?:\s+@?(\S{1,64}))?\s*$")
BEIJING = timezone(timedelta(hours=8))
MESSAGE_LIMIT = 4096  # Telegram 单条上限
SEPARATOR = "─" * 16
CACHE_SECONDS = 1800  # 物品名、境界门槛、道号名单半小时取一次
STALE_SECONDS = 120  # 停机期间攒下的旧指令不补答
# 斗法输了挂上的负面状态：持续秒数、战报余波里认哪句（10-15 各对过一个样本）
DUEL_DEBUFFS = {"heavy_injury": (600, "神魂重创"), "broken_dao_heart": (86400, "道心已碎")}
# 九转还魂丹：神魂动荡（败后 10 分钟虚弱）期间再输一场，碎一颗免陨落，换 24 小时道心破碎
REVIVE_PILL = "elixir_901_revive"
# .查冷却 只列 10-15 对过游戏回包或结算时间的（没对上的不列，免得报错时间）：
# 这几个副本天机阁存的直接是冷却结束时刻（小极宫 72h、秘圃 48h、苍坤 2.5h、其余 2h）
DUNGEON_UNTIL_FIELDS = (
    ("last_xiaoji_time", "北冥小极宫"),
    ("last_luoyun_mipu_time", "落云秘圃"),
    ("last_zhuimo_time", "坠魔谷"),
    ("last_cangkun_time", "苍坤洞府"),
    ("last_dungeon_time", "虚天殿"),
    ("last_huanglong_time", "黄龙山大战"),
)
# 侍妾：上次时间 + 时长（面板剩余分钟反推）
COMPANION_COOLDOWNS = (
    ("last_dream_map_seek_time", "入梦寻图", 8),
    ("last_companion_heart_tribulation_time", "共历心劫", 10),
    ("last_divination_chain_time", "天机代卜", 12),
)
WIND_WINGS = "treasure_203"  # 祭出风雷翅：探寻裂缝冷却 12h → 9h
# 玄骨遗府（10-15 新副本）的门票。每日通关奖励、每日掉票上限天机阁都不记，暂时只能看身上有几颗
XUANGU_PEARL = "item_xuangu_seeking_pearl"
# 其余状态的来路：10-15 在群里找到发放这些状态的原消息才写，没见过的不猜（风之祝福、南陇侯的注视等）
STATUS_SOURCES = {
    "sect_hunt": "叛出元婴宗，7 日内被元婴宗长老追杀",
    "heart_vow_backlash": "主动毁掉掩月心契的誓约",
    "star_formation": "星宫周天星斗大阵成阵，参与者每人 6 小时",
    "jiyin_blessing": "极阴祖师的赏赐",
    "spirit_domain_buff": "自己施展 .法相天地",
    "dageng_sword_formation": "自己花 2000 修为布下，御使神雷版飞剑时战力大增",
    "badge_shihuang_outfits_kiss": "佩戴始皇的新衣 .亲吻徽章",
    "badge_shihuang_outfits_kiss_cooldown": "上次 .亲吻徽章 后的冷却",
    "companion_divination_chain": "侍妾天机代卜（花 180 修为焚香推演得卦）",
    "companion_divination_guard": "侍妾的机缘（.我的侍妾「第二期机缘」里列着）",
    "companion_zhuimo_trial": "侍妾 .坠魔心劫 所得，进坠魔谷时队伍生效",
}
# 状态条目里的数值怎么读：坠魔心劫护持的三个数就是进坠魔谷开局显示的「魔染护持 +12，封印起始 +8，士气起始 +12」
# （10-15 对过副本记录，另一档是 +18/+12/+20）
STATUS_DETAILS = {
    "companion_zhuimo_trial": "魔染护持 +{taint_shield}，封印起始 +{seal_bonus}，士气起始 +{morale_bonus}",
}
OBSERVE = re.compile(r"^[.。]查观星\s*$")
HELP_COMMAND = re.compile(r"^[.。]帮助\s*$")
# 指令包在 <code> 里，TG 上点一下就复制；新加指令记得写进来（自检会核对这里的每条指令都认得）
# 排版照韩天尊的「.苍坤洞府帮助」：指令那段是 code，参数不包进去
HELP = """<b>【小天尊查询指令】</b>
- <code>.查灵根</code> 名字：天命玉牒
- <code>.查储物袋</code> 名字：储物袋
- <code>.查状态</code> 名字：负面状态（带说明和来源）、九转还魂丹、增益、闭关和远航剩余
- <code>.查冷却</code> 名字：副本、闭关、斗法、侍妾、宗门、小世界、日常的冷却
- <code>.查观星</code>：最近几次观星和整点天道演化结算
- <code>.帮助</code>：这条说明

<b>【说明】</b>
- 点一下指令就复制，后面接名字发出去
- 名字 = TG 用户名（带不带 @、大小写都行）或完整道号；道号重名会列出候选
- 不写名字、回复某人的消息发指令 = 查被回复的那个人
- 有人观星看到星辰异象、或整点结算出了元磁山核·乙，小天尊会主动在群里报"""
READING = re.compile(r"@(\w+) 闭目凝神.*?下一次天道演化将是: 【([^】]+)】.*?当前天命所归: @?(\S+)", re.S)
SETTLEMENT = re.compile(r"天机阁快报 - ([^】]+)】")
JADE = "元磁山核·乙"  # 只有「星辰异象」结算出它；观星能提前看到下一个整点是不是星辰异象
POLL_SECONDS = 10  # getUpdates 长轮询改短，顺便每 10 秒看一次有没有新的观星/结算
log = logging.getLogger("tianzun")
_cache: dict[str, tuple[float, object]] = {}


def _asc_get(path: str):
    # 每次现读主服务维护的登录：主服务刷新了 cookie，这边自然跟着用新的
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT cookie_text, api_token FROM external_accounts"
            " WHERE provider = ? AND cookie_text != '' ORDER BY profile_id LIMIT 1",
            (ASC_PROVIDER,),
        ).fetchone()
    finally:
        con.close()
    if not row:
        raise asc_client.AscAuthError("库里没有天机阁登录")
    return asc_client._perform_json_get(path, row[0], row[1])[0]


def _cached(path: str):
    hit = _cache.get(path)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = _asc_get(path)
    _cache[path] = (time.time(), value)
    return value


def _cultivator(username: str) -> dict:
    return _asc_get("/api/cultivator/" + urllib.parse.quote(username, safe=""))


def _json(value):
    # 天机阁有的字段是 JSON 字符串、有的已经是对象
    return json.loads(value) if isinstance(value, str) and value else value


def _ids(value) -> list:
    return list(_json(value) or [])


def _active(d: dict, field: str, now: datetime):
    """身上没到期的状态：(编号, 原始条目, 名字, 到期时间或 None)。游戏不清过期状态，几个月前的新手护道气运还挂着。"""
    for key, e in (_json(d.get(field)) or {}).items():
        if not isinstance(e, dict):
            continue
        # 星力加持那条没有 name，名字在说明的冒号前；慕兰军功之类纯记录既没名字也没说明，不算状态
        name = e.get("name") or (e.get("description") or "").split("：")[0]
        raw = e.get("expiry_time") or e.get("next_available_at")
        until = _until(raw)
        if name and not (raw and (until is None or until <= now)):
            yield key, e, name, until


def _remain(until: datetime, now: datetime) -> str:
    # 韩天尊玉牒的写法：151小时19分钟50秒、6分钟33秒
    hours, rest = divmod(max(0, int((until - now).total_seconds())), 3600)
    minutes, seconds = divmod(rest, 60)
    return (f"{hours}小时" if hours else "") + (f"{minutes}分钟" if hours or minutes else "") + f"{seconds}秒"


def format_panel(d: dict, boot: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    items = boot.get("game_items") or {}

    def names(ids) -> str:
        return ", ".join(f"【{(items.get(i) or {}).get('name') or i}】" for i in ids)

    badges = [
        it.get("item_id") or ""
        for it in (d.get("inventory") or {}).get("items") or []
        if it.get("type") == "badge"
    ]
    # ponytail: 荣誉称号目前只见过青元子讨伐那三枚（badge_qyz_*）；游戏加了别的荣誉徽章这里会漏，见到样本再补
    honors = list(dict.fromkeys(b for b in badges if b.startswith("badge_qyz_")))
    natal = _ids(d.get("natal_treasure_ids"))
    equipped = _ids(d.get("equipped_treasure_id"))
    refined = _ids(d.get("refined_treasure_ids"))
    threshold = (boot.get("level_thresholds") or {}).get(d.get("cultivation_level"), "?")

    lines = [f"@{d.get('username')} 的天命玉牒", SEPARATOR]
    if d.get("active_badge"):
        lines.append(f"称号: {names([d['active_badge']])}")
    if honors:
        lines.append(f"荣誉称号: {names(honors)}")
    if natal:
        lines.append(f"本命法宝: {names(natal)}")
    if equipped:
        lines.append(f"当前祭出: {names(equipped)}")
    if refined:
        lines.append(f"已祭炼法宝: {len(refined)} 件")
    sect = d.get("sect_name")
    if sect:
        lines.append("宗门: 散修" if sect == "散修" else f"宗门: 【{sect}】")  # 韩天尊散修不加括号
    lines += [
        f"灵根: {d.get('spirit_root') or '-'}",
        f"修为: {d.get('cultivation_points') or 0} / {threshold}",
        f"丹毒: {d.get('drug_poison_points') or 0} 点",
        f"杀戮: {d.get('kill_count') or 0} 人",
        "",
    ]
    # 韩天尊只列有期限的负面状态（南陇侯的注视这种不列）；ponytail: 同时挂两个时的分隔没见过样本，先用逗号
    timed = [f"【{name}】(剩余: {_remain(until, now)})" for _, _, name, until in _active(d, "debuffs", now) if until]
    if timed:
        lines += [f"负面状态: {', '.join(timed)}", ""]
    lines += ["【新手秘籍】", "若有疑惑，可查阅《天道总纲》。"]
    return "\n".join(lines)


def format_bag(d: dict, boot: dict) -> str:
    names = boot.get("game_items") or {}
    inventory = d.get("inventory") or {}
    goods = []
    for it in inventory.get("items") or []:
        if not it.get("quantity"):
            continue
        line = f"- {it.get('name') or it.get('item_id')} x {it['quantity']}"
        if it.get("max_durability"):
            line += f" (耐久 {it.get('durability') or 0}/{it['max_durability']})"
        goods.append(line)
    materials = [
        f"- {(names.get(k) or {}).get('name') or k} x {v}"
        for k, v in (inventory.get("materials") or {}).items()
        if v
    ]
    lines = [f"@{d.get('username')} 的储物袋", ""]
    if goods:
        lines += ["法宝/丹药/杂物:", *goods, ""]
    if materials:
        lines += ["材料:", *materials]
    if not goods and not materials:
        lines.append("空空如也")
    return "\n".join(lines).rstrip()


def _until(value) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None
    if moment and moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)  # 天机阁的时间都是 UTC
    return moment


def _left(until: datetime, now: datetime) -> str:
    minutes = int((until - now).total_seconds() // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    if days:
        left = f"{days} 天 {hours} 小时"
    elif hours:
        left = f"{hours} 小时 {minutes} 分钟"
    else:
        left = f"{minutes} 分钟" if minutes else "不到 1 分钟"
    local = until.astimezone(BEIJING)
    same_day = local.date() == now.astimezone(BEIJING).date()
    return f"剩 {left}，至 {local:%H:%M}" if same_day else f"剩 {left}，至 {local:%m-%d %H:%M}"


def duel_source(username: str, at: float, marker: str) -> str | None:
    """找 username 在 at 前后 5 分钟输掉、余波里带 marker 的那份战报，回「几点输给谁」。

    战报取主服务存的游戏群消息 recorded_messages（约 48 小时）；占位的「正在整理天道战报」没有胜负，自然跳过。
    """
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT text, created_at FROM recorded_messages WHERE created_at BETWEEN ? AND ?"
            " AND text LIKE ? AND text LIKE ? ORDER BY created_at DESC",
            (at - 300, at + 300, f"%败者：@{username}%", f"%{marker}%"),
        ).fetchall()
    finally:
        con.close()
    loser = re.compile(rf"败者：@{re.escape(username)}(?!\w)", re.IGNORECASE)
    for text, created_at in rows:
        winner = re.search(r"胜者：@(\w+)", text)
        if not loser.search(text) or not winner:
            continue
        dao = re.search(rf"@{re.escape(winner.group(1))} · ([^·\n]+?) ·", text)
        when = datetime.fromtimestamp(created_at, BEIJING)
        return f"{when:%m-%d %H:%M} 斗法输给 @{winner.group(1)}" + (f"（{dao.group(1).strip()}）" if dao else "")
    return None


def format_status(d: dict, boot: dict, now: datetime | None = None, find_source=duel_source) -> str:
    now = now or datetime.now(timezone.utc)

    def effects(field: str, explain: bool = False) -> list[str]:
        lines = []
        for key, e, name, until in _active(d, field, now):
            line = f"- {name}（{_left(until, now)}）" if until else f"- {name}"
            try:
                detail = STATUS_DETAILS[key].format_map(e) if key in STATUS_DETAILS else ""
            except (KeyError, ValueError):
                detail = ""  # 游戏改了字段名就不写数值，别整条报错
            if detail:
                line += f"：{detail}"
            elif explain and e.get("name") and e.get("description"):
                line += f"：{e['description']}"
            lines.append(line)
            if key in DUEL_DEBUFFS and until:
                seconds, marker = DUEL_DEBUFFS[key]
                source = find_source(d.get("username") or "", until.timestamp() - seconds, marker)
                lines.append(f"  来源：{source or '群里近两天的战报里没找到'}")
            elif key in STATUS_SOURCES:
                lines.append(f"  来源：{STATUS_SOURCES[key]}")
        return lines

    pills = sum(
        it.get("quantity") or 0
        for it in (d.get("inventory") or {}).get("items") or []
        if it.get("item_id") == REVIVE_PILL
    )
    debuffs, buffs = effects("debuffs", explain=True), effects("active_buffs")
    lines = [f"@{d.get('username')} 的状态", SEPARATOR]
    if debuffs:
        lines += ["负面状态:", *debuffs]
    lines.append(f"九转还魂丹: {pills} 颗" if pills else "九转还魂丹: 没有")
    if buffs:
        lines += ["增益:", *buffs]
    seclusion = _until(d.get("deep_seclusion_end_time"))
    if seclusion and seclusion > now:
        lines.append(f"深度闭关: {_left(seclusion, now)}")
    companion = d.get("companion") or {}
    voyage = companion.get("voyage") or {}
    voyage_end = _until(voyage.get("end_time"))
    if voyage.get("status") == "sailing" and voyage_end and voyage_end > now:
        lines.append(f"侍妾远航: {companion.get('name') or '-'} {_left(voyage_end, now)}")
    return "\n".join(lines)


def duodeng_today(username: str, now: datetime) -> bool:
    """黄龙夺灯每天一次、北京零点重置，天机阁档案里没有字段；按主服务存的副本记录看这个人今天出过手没有。"""
    if not username:
        return False  # 没设用户名的人记录里发送人是空的，对不上
    start = now.astimezone(BEIJING).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return bool(_rows(
        "SELECT 1 FROM dungeon_messages WHERE dungeon = 'duodeng' AND created_at >= ? AND is_bot = 0"
        " AND lower(sender_username) = lower(?) AND (text LIKE '.夺灯行动%' OR text LIKE '.进入夺灯%') LIMIT 1",
        (start, username),
    ))


def format_cooldowns(d: dict, boot: dict, now: datetime | None = None, ran_duodeng=duodeng_today) -> str:
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(BEIJING).date().isoformat()

    def after(value, hours: float) -> datetime | None:
        start = _until(value)
        return start + timedelta(hours=hours) if start else None

    def cd(label: str, until: datetime | None, idle: str = "无冷却") -> str:
        return f"- {label}：{_left(until, now)}" if until and until > now else f"- {label}：{idle}"

    lines = [f"@{d.get('username')} 的冷却", SEPARATOR, "副本:"]
    lines += [cd(name, _until(d.get(field))) for field, name in DUNGEON_UNTIL_FIELDS]
    ran = ran_duodeng(d.get("username") or "", now)
    lines.append("- 黄龙夺灯：今日已出征" if ran else "- 黄龙夺灯：今日还没出征（按群里副本记录）")
    pearls = sum(
        it.get("quantity") or 0
        for it in (d.get("inventory") or {}).get("items") or []
        if it.get("item_id") == XUANGU_PEARL
    )
    lines.append(f"- 玄骨遗府：古府寻踪珠 {pearls} 颗")

    lines += ["闭关:", cd("闭关修炼", _until(d.get("cultivation_cooldown_until")))]
    seclusion = _until(d.get("deep_seclusion_end_time"))
    if seclusion and seclusion > now:
        lines.append(cd("深度闭关", seclusion))
    rift_hours = 9 if WIND_WINGS in _ids(d.get("equipped_treasure_id")) else 12
    lines.append(cd("探寻裂缝", after(d.get("last_rift_explore_time"), rift_hours)))
    # 搜寻节点：上次 + 12 小时。10-15 大号 21:23 搜过，21:36 再发回「11小时46分钟29秒后再行搜寻」，正好对上；
    # 被冷却挡回的那次不刷新 last_node_search_time。ponytail: 只有这一个样本，神识/大衍诀会不会缩短没见过
    lines.append(cd("搜寻节点", after(d.get("last_node_search_time"), 12)))
    lines.append(cd("双修", after(d.get("last_dual_cultivation_time"), 1)))
    if d.get("last_wen_dao_time"):
        lines.append(cd("元婴宗问道", after(d.get("last_wen_dao_time"), 12)))

    stamina = d.get("daily_battle_stamina") if d.get("last_battle_date") == today else 10
    lines += ["斗法:", f"- 今日神念：剩 {stamina if stamina is not None else 10}/10"]

    residence = _json((d.get("dongfu") or {}).get("companion_residence")) or []
    companions = [(c, "随行") for c in [d.get("companion")] if isinstance(c, dict) and c.get("name")]
    companions += [(c, "藏娇阁") for c in residence if isinstance(c, dict) and c.get("name")]
    if companions:
        lines.append("侍妾:")
    for c, where in companions:
        parts = []
        for field, name, hours in COMPANION_COOLDOWNS:
            until = after(c.get(field), hours)
            parts.append(f"{name} {_left(until, now)}" if until and until > now else f"{name} 可用")
        voyage = c.get("voyage") or {}
        back = _until(voyage.get("end_time"))
        if voyage.get("status") == "sailing" and back and back > now:
            parts.append(f"远航 {_left(back, now)}")
        lines.append(f"- {c['name']}（{where}）：" + "；".join(parts))

    sect = d.get("sect_name") or ""
    sect_lines = []
    if sect == "星宫":
        observed = str(d.get("last_divination_time") or "")[:10] == today
        sect_lines.append(f"- 观星：{'今日已观' if observed else '今日未观'}")
        sect_lines.append(cd("周天星斗大阵", after(d.get("last_star_formation_time"), 12)))
    leave = _until(d.get("sect_leave_cooldown_until"))
    if leave and leave > now:
        sect_lines.append(cd("退宗后不能拜入", leave))
    if sect_lines:
        lines += ["宗门:", *sect_lines]

    world = d.get("small_world")
    if isinstance(world, dict) and world:
        lines += [
            "小世界:",
            cd("祈愿", after(world.get("last_prayer_time"), 6)),
            cd("神谕", after(world.get("last_edict_time"), 3)),
        ]

    wild = d.get("wild_experience_daily_count") if d.get("wild_experience_last_date") == today else 0
    pagoda = _json(d.get("pagoda_progress")) or {}
    lines += [
        "日常:",
        f"- 野外历练：今日 {wild or 0}/8",
        f"- 闯塔：{'今日已闯' if isinstance(pagoda, dict) and pagoda.get('last_attempt_date') == today else '今日未闯'}",
    ]
    return "\n".join(lines)


FORMATTERS = {"灵根": format_panel, "储物袋": format_bag, "状态": format_status, "冷却": format_cooldowns}


def find(name: str) -> dict | list[dict]:
    """查到就回档案；否则回全服名单里的候选（空 = 查无此人，多个 = 道号重名）。"""
    try:
        return _cultivator(name)
    except asc_client.AscNotFoundError:
        pass
    # 天机阁只认用户名且区分大小写；不是用户名就当道号（或大小写不对的用户名）去全服名单里找
    wanted = name.lower()
    matches = [
        c
        for c in _cached("/api/all_cultivators")
        if c.get("username") and (c.get("dao_name") == name or c["username"].lower() == wanted)
    ]
    return _cultivator(matches[0]["username"]) if len(matches) == 1 else matches


def lookup(kind: str, name: str) -> str:
    data = find(name)
    if not data:
        return f"天机阁查无此人：{name}"
    if isinstance(data, list):
        # 全服 307 个道号重名（10-15）：列出来让人用用户名再查，不替人挑
        ranked = sorted(data, key=lambda c: -(c.get("cultivation_points") or 0))
        lines = [f"道号「{name}」有 {len(data)} 人，用用户名再查："]
        lines += [
            f"- {c['username']}（{c.get('cultivation_level') or '?'}，修为 {c.get('cultivation_points') or 0}）"
            for c in ranked[:20]
        ]
        return "\n".join(lines + (["…"] if len(data) > 20 else []))
    text = FORMATTERS[kind](data, _cached("/api/bootstrap"))
    # ponytail: 超长直接截断；目前最大的储物袋约 2000 字，真有人超了再改成分条发
    return text if len(text) <= MESSAGE_LIMIT else text[: MESSAGE_LIMIT - 2] + "\n…"


def _rows(sql: str, args: tuple = ()) -> list:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _slot(ts: float) -> datetime:
    """观星看到的是下一个整点（北京 0/3/6/…/21 点）的天道演化。"""
    t = datetime.fromtimestamp(ts, BEIJING)
    return t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=3 - t.hour % 3)


def _hour(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, BEIJING).replace(minute=0, second=0, microsecond=0)


def _settled(text: str) -> str:
    """结算快报 → 「星辰异象（元磁山核·乙）」；结果里没写物品就只给事件名。"""
    event = SETTLEMENT.search(text)
    items = re.findall(r"【([^】]+)】", text.split("天机演化结果", 1)[1]) if "天机演化结果" in text else []
    return (event[1] if event else "?") + (f"（{items[-1]}）" if items else "")


def format_observations(now: datetime | None = None) -> str:
    """观星和整点结算都取主服务存的 fate_evolution_logs（群里谁观星、每次结算都记着）。"""
    now = now or datetime.now(timezone.utc)
    lines = ["天道演化（每 3 小时整点结算，观星能提前看到下一次）", SEPARATOR, "最近观星:"]
    for text, ts in _rows(
        "SELECT text, created_at FROM fate_evolution_logs WHERE kind = 'reading' ORDER BY id DESC LIMIT 3"
    ):
        m = READING.search(text)
        if m:
            pending = "，还没到" if _slot(ts) > now else ""
            lines.append(
                f"- {datetime.fromtimestamp(ts, BEIJING):%m-%d %H:%M} {m[1]} 观得 "
                f"{_slot(ts):%H:%M}【{m[2]}】，天命所归 {m[3]}{pending}"
            )
    if len(lines) == 3:
        lines.append("- 近来没人观星")
    lines.append("最近结算:")
    for text, ts in _rows(
        "SELECT text, created_at FROM fate_evolution_logs WHERE kind = 'settlement' ORDER BY id DESC LIMIT 8"
    ):
        lines.append(f"- {_hour(ts):%m-%d %H:%M} {_settled(text)}")
    return "\n".join(lines)


def jade_alerts(after_id: int, predicted: dict) -> tuple[int, list[str]]:
    """after_id 之后新记下的观星/结算：星辰异象预告（同一整点只报一次）、出了元磁山核·乙的结算。

    predicted 记着每个整点观星时的天命所归，结算时拿来说给了谁。
    """
    alerts = []
    for row_id, kind, text, ts in _rows(
        "SELECT id, kind, text, created_at FROM fate_evolution_logs"
        " WHERE id > ? AND kind IN ('reading', 'settlement') ORDER BY id",
        (after_id,),
    ):
        after_id = row_id
        m = READING.search(text) if kind == "reading" else None
        if m and "星辰异象" in m[2] and _slot(ts) not in predicted:
            predicted[_slot(ts)] = m[3]
            alerts.append(
                f"🔮 {_slot(ts):%H:%M} 的天道演化是【星辰异象】，会出一枚{JADE}\n"
                f"当前天命所归：{m[3]}（{m[1]} 观星看到的，观星的人 5 分钟内能 .改换星移）"
            )
        elif kind == "settlement" and JADE in text:
            who = predicted.get(_hour(ts))
            alerts.append(
                f"🌟 {_hour(ts):%H:%M} 天道演化【星辰异象】已结算，出了一枚{JADE}"
                + (f"（观星时天命归 {who}，之后有人改命就不是了）" if who else "（这次没人观星，不知道给了谁）")
            )
    return after_id, alerts


def answer(text: str, replied: str | None = None) -> str | None:
    """replied：这条指令回复的那个人的 TG 用户名（"" = 对方没设用户名，None = 不是回复）。"""
    if HELP_COMMAND.match(text):
        return HELP
    if OBSERVE.match(text):
        return format_observations()
    match = COMMAND.match(text)
    if not match:
        return None
    kind, name = match.groups()
    name = name or replied
    if name:
        return lookup(kind, name)
    if replied == "":
        return "对方没设 TG 用户名，天机阁查不到，用道号查吧"
    return f"用法：.查{kind} 用户名或道号，或者回复某人的消息发 .查{kind}"


def _bot(token: str, method: str, **params):
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=params.get("timeout", 0) + 20) as response:
        return json.load(response)["result"]


def handle(token: str, message: dict, chat_ids: set[int]) -> None:
    asked = message.get("text") or ""
    if not (HELP_COMMAND.match(asked) or OBSERVE.match(asked) or COMMAND.match(asked)) or message["chat"]["id"] not in chat_ids:
        return
    if time.time() - message.get("date", 0) > STALE_SECONDS:
        return
    replied = None
    parent = message.get("reply_to_message")
    if parent and not parent.get("forum_topic_created"):  # 话题群里不点回复也挂着建话题那条
        replied = (parent.get("from") or {}).get("username") or ""
    try:
        text = answer(asked, replied)
    except asc_client.AscAuthError:
        log.exception("天机阁登录失效")
        text = "天机阁登录失效，稍后再试"
    except Exception:
        log.exception("%s 失败", asked)
        text = "暂时查不到，稍后再试"
    params = {
        "chat_id": message["chat"]["id"],
        "text": text,
        "reply_parameters": {
            "message_id": message["message_id"],
            "allow_sending_without_reply": True,
        },
    }
    if text == HELP:  # 只有帮助走 HTML，面板里的 < & 照原样发
        params["parse_mode"] = "HTML"
    if message.get("is_topic_message"):
        params["message_thread_id"] = message["message_thread_id"]
    _bot(token, "sendMessage", **params)


def run() -> None:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    token = os.environ.get("TIANZUN_BOT_TOKEN", "").strip()
    chat_ids = {
        int(x) for x in os.environ.get("TIANZUN_BOT_CHAT_IDS", "").split(",") if x.strip()
    }
    if not token or not chat_ids:
        sys.exit(".env 里要配 TIANZUN_BOT_TOKEN 和 TIANZUN_BOT_CHAT_IDS")
    log.info("小天尊启动，只在 %s 回", sorted(chat_ids))
    offset = None
    # 从现在起的新观星/结算才提醒，重启不把历史再报一遍
    fate_after, predicted = _rows("SELECT coalesce(max(id), 0) FROM fate_evolution_logs")[0][0], {}
    while True:
        try:
            fate_after, alerts = jade_alerts(fate_after, predicted)
            for text in alerts:
                for chat_id in chat_ids:
                    _bot(token, "sendMessage", chat_id=chat_id, text=text)
        except Exception:
            log.exception("元磁山核·乙提醒失败")
        try:
            updates = _bot(
                token, "getUpdates", offset=offset, timeout=POLL_SECONDS, allowed_updates=["message"]
            )
        except Exception:
            log.exception("getUpdates 失败，10 秒后重试")
            time.sleep(10)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            if update.get("message"):
                try:
                    handle(token, update["message"], chat_ids)
                except Exception:
                    log.exception("回复失败")


def self_check() -> None:
    boot = {
        "game_items": {
            "badge_a": {"name": "始皇的新衣"},
            "badge_qyz_trial": {"name": "真仙试锋"},
            "badge_qyz_alliance": {"name": "伐仙同盟"},
            "t1": {"name": "金光砖"},
            "t2": {"name": "青竹蜂云剑（神雷版）"},
        },
        "level_thresholds": {"化神中期": 8000000},
    }
    data = {
        "username": "daoyou_test",
        "active_badge": "badge_a",
        "inventory": {"items": [
            {"item_id": "badge_qyz_trial", "type": "badge"},
            {"item_id": "badge_a", "type": "badge"},
            {"item_id": "badge_qyz_alliance", "type": "badge"},
            {"item_id": "badge_qyz_trial", "type": "badge"},
            {"item_id": "t1", "type": "treasure"},
        ]},
        "natal_treasure_ids": '["t2"]',
        "equipped_treasure_id": '["t1", "t2"]',
        "refined_treasure_ids": '["t1", "t2", "t3"]',
        "sect_name": "天星宗",
        "spirit_root": "天灵根(火)",
        "cultivation_level": "化神中期",
        "cultivation_points": 5294899,
        "drug_poison_points": -10,
        "kill_count": 138,
    }
    expected = """@daoyou_test 的天命玉牒
────────────────
称号: 【始皇的新衣】
荣誉称号: 【真仙试锋】, 【伐仙同盟】
本命法宝: 【青竹蜂云剑（神雷版）】
当前祭出: 【金光砖】, 【青竹蜂云剑（神雷版）】
已祭炼法宝: 3 件
宗门: 【天星宗】
灵根: 天灵根(火)
修为: 5294899 / 8000000
丹毒: -10 点
杀戮: 138 人

【新手秘籍】
若有疑惑，可查阅《天道总纲》。"""
    assert format_panel(data, boot) == expected, format_panel(data, boot)

    bare = {"username": "xin_ren", "spirit_root": "真灵根(水木)", "cultivation_level": "结丹后期",
            "cultivation_points": 114036, "natal_treasure_ids": "[]", "equipped_treasure_id": "[]"}
    lines = format_panel(bare, boot).splitlines()
    assert lines[2:4] == ["灵根: 真灵根(水木)", "修为: 114036 / ?"], lines

    now = datetime(2026, 10, 15, 6, 16, 30, tzinfo=timezone.utc)  # 北京 14:16:30
    assert _remain(now + timedelta(seconds=33), now) == "33秒"
    assert _remain(now + timedelta(minutes=6, seconds=33), now) == "6分钟33秒"
    assert _remain(now + timedelta(hours=151, minutes=19, seconds=50), now) == "151小时19分钟50秒"
    # 散修不加括号；玉牒只列有期限的负面状态，空一行放在新手秘籍前
    loner = dict(bare, sect_name="散修", debuffs={
        "sect_hunt": {"name": "元神烙印", "expiry_time": "2026-10-21T13:35:00+00:00"},
        "nangonghou_gaze": {"name": "南陇侯的注视"},
        "heavy_injury": {"name": "神魂动荡", "expiry_time": "2026-10-15T06:00:00+00:00"},
    })
    assert format_panel(loner, boot, now).splitlines()[2:] == [
        "宗门: 散修", "灵根: 真灵根(水木)", "修为: 114036 / ?", "丹毒: 0 点", "杀戮: 0 人", "",
        "负面状态: 【元神烙印】(剩余: 151小时18分钟30秒)", "",
        "【新手秘籍】", "若有疑惑，可查阅《天道总纲》。",
    ], format_panel(loner, boot, now)

    bag = {
        "username": "daoyou_test",
        "inventory": {
            "items": [
                {"item_id": "e1", "name": "增元丹", "quantity": 2},
                {"item_id": "t1", "name": "金光砖", "quantity": 1, "durability": 90, "max_durability": 100},
                {"item_id": "e2", "name": "用光了", "quantity": 0},
            ],
            "materials": {"mat_001": 405, "mat_x": 3, "mat_y": 0},
        },
    }
    boot["game_items"]["mat_001"] = {"name": "灵石"}
    assert format_bag(bag, boot) == """@daoyou_test 的储物袋

法宝/丹药/杂物:
- 增元丹 x 2
- 金光砖 x 1 (耐久 90/100)

材料:
- 灵石 x 405
- mat_x x 3""", format_bag(bag, boot)
    assert format_bag({"username": "kong"}, boot).splitlines()[-1] == "空空如也"

    status = {
        "username": "daoyou_test",
        "debuffs": json.dumps({
            "heavy_injury": {"name": "神魂动荡", "expiry_time": "2026-10-15T06:23:46.752514+00:00"},
            "broken_dao_heart": {"name": "道心破碎", "expiry_time": "2026-10-15T11:33:37+00:00", "description": "所有收益减半"},
            "nangonghou_gaze": {"name": "南陇侯的注视", "description": "高风险PVE失败惩罚加倍"},
        }),
        "active_buffs": {
            "newbie_guardian_luck": {"name": "新手护道气运", "expiry_time": "2026-05-12T20:19:24+00:00"},
            "star_formation": {"expiry_time": "2026-10-15T09:01:21+00:00", "description": "星力加持：闭关成功率提升至98%"},
            "kiss_cooldown": {"name": "始皇的新衣冷却", "next_available_at": "2026-10-15T06:16:50+00:00"},
            "mulan_smoke": {"total_merit": 36},
            "companion_zhuimo_trial": {"name": "坠魔心劫护持", "expiry_time": "2026-10-16T03:47:48+00:00",
                                       "taint_shield": 12, "seal_bonus": 8, "morale_bonus": 12},
        },
        "deep_seclusion_end_time": "2026-10-19T00:20:37+00:00",
        "companion": {"name": "莎儿", "voyage": {"status": "sailing", "end_time": "2026-10-15T12:29:10"}},
        "inventory": {"items": [{"item_id": REVIVE_PILL, "quantity": 21}]},
    }
    calls = []

    def fake_source(username, at, marker):
        calls.append((username, round(at), marker))
        return "10-15 14:13 斗法输给 @shou_xia（雷火叟）" if marker == "神魂重创" else None

    assert format_status(status, boot, now, fake_source) == """@daoyou_test 的状态
────────────────
负面状态:
- 神魂动荡（剩 7 分钟，至 14:23）
  来源：10-15 14:13 斗法输给 @shou_xia（雷火叟）
- 道心破碎（剩 5 小时 17 分钟，至 19:33）：所有收益减半
  来源：群里近两天的战报里没找到
- 南陇侯的注视：高风险PVE失败惩罚加倍
九转还魂丹: 21 颗
增益:
- 星力加持（剩 2 小时 44 分钟，至 17:01）
  来源：星宫周天星斗大阵成阵，参与者每人 6 小时
- 始皇的新衣冷却（剩 不到 1 分钟，至 14:16）
- 坠魔心劫护持（剩 21 小时 31 分钟，至 10-16 11:47）：魔染护持 +12，封印起始 +8，士气起始 +12
  来源：侍妾 .坠魔心劫 所得，进坠魔谷时队伍生效
深度闭关: 剩 3 天 18 小时，至 10-19 08:20
侍妾远航: 莎儿 剩 6 小时 12 分钟，至 20:29""", format_status(status, boot, now, fake_source)
    # 神魂动荡按到期前 10 分钟、道心破碎按到期前 24 小时去找战报
    assert calls == [
        ("daoyou_test", round(datetime(2026, 10, 15, 6, 13, 47, tzinfo=timezone.utc).timestamp()), "神魂重创"),
        ("daoyou_test", round(datetime(2026, 10, 14, 11, 33, 37, tzinfo=timezone.utc).timestamp()), "道心已碎"),
    ], calls
    assert format_status({"username": "kong", "debuffs": "{}"}, boot, now).splitlines()[2:] == ["九转还魂丹: 没有"]

    # duel_source：只认这个人输的那份（@xu_a 不能匹配 @xu_ab），胜者道号从对阵双方里取
    global DB
    real_db, at = DB, now.timestamp()
    with tempfile.TemporaryDirectory() as tmp:
        DB = Path(tmp) / "t.db"
        con = sqlite3.connect(DB)
        con.execute("CREATE TABLE recorded_messages (text TEXT, created_at REAL)")
        report = "攻方：@{w} · 雷火叟 · 化神中期\n守方：@{l} · 木讷生 · 化神初期\n胜者：@{w} | 余血\n败者：@{l} | 余血 0\n💔 【神魂重创】"
        con.execute("INSERT INTO recorded_messages VALUES (?, ?)", (report.format(w="shou_xia", l="xu_ab"), at))
        con.execute("INSERT INTO recorded_messages VALUES (?, ?)", ("战斗结束，正在整理天道战报...", at))
        con.execute("INSERT INTO recorded_messages VALUES (?, ?)", (report.format(w="shou_xia", l="xu_a"), at - 60))

        def bj(hour, minute, second=0):
            return datetime(2026, 10, 15, hour, minute, second, tzinfo=BEIJING).timestamp()

        def star(observer, event, target):
            return (f"【星盘显化】\n@{observer} 闭目凝神，推演天机...星盘之上，天机已然显现！\n\n"
                    f"下一次天道演化将是: 【{event}】\n当前天命所归: @{target}\n\n你有 5分钟 时间")

        con.execute("CREATE TABLE fate_evolution_logs (id INTEGER PRIMARY KEY, kind TEXT, text TEXT, created_at REAL)")
        con.executemany("INSERT INTO fate_evolution_logs VALUES (?, ?, ?, ?)", [
            (1, "reading", star("guan_xing", "Good - 星辰异象", "ling_ke"), bj(8, 38, 28)),
            (2, "reading", star("zhan_bu", "Good - 星辰异象", "ling_ke"), bj(8, 40)),
            (3, "settlement", "【天机阁快报 - 星辰异象】\n\n天机演化结果: 星光散去，坑底躺着一枚【元磁山核·乙】！", bj(9, 1, 3)),
            (4, "read_cmd", ".观星", bj(11, 59, 3)),
            (5, "reading", star("guan_xing", "Good - 地心火脉现世", "ming_a"), bj(11, 59, 7)),
        ])
        con.execute("CREATE TABLE dungeon_messages (dungeon TEXT, sender_username TEXT, is_bot INTEGER, text TEXT, created_at REAL)")
        con.executemany("INSERT INTO dungeon_messages VALUES (?, ?, ?, ?, ?)", [
            ("duodeng", "Lamp_Mate", 0, ".夺灯行动 1 潜行", bj(11, 20)),       # 今天出手
            ("duodeng", "", 0, ".夺灯行动 2 破阵 北", bj(11, 21)),             # 没用户名的人
            ("duodeng", "old_mate", 0, ".夺灯行动 1 防守", bj(11, 20) - 86400),  # 昨天
            ("duodeng", "bot_echo", 1, ".夺灯行动 1 防守", bj(11, 22)),
        ])
        con.commit()
        con.close()
        lamp = [duodeng_today(u, datetime.fromtimestamp(bj(15, 0), timezone.utc))
                for u in ("lamp_mate", "old_mate", "", "bot_echo", "nobody")]
        found = duel_source("xu_a", at, "神魂重创")
        missing = duel_source("xu_a", at - 3600, "神魂重创")
        observed = format_observations(datetime.fromtimestamp(bj(11, 59, 30), timezone.utc))
        predicted = {}
        first = jade_alerts(0, predicted)
        again = jade_alerts(first[0], predicted)
        blind = jade_alerts(2, {})
        DB = real_db
    assert found == f"{datetime.fromtimestamp(at - 60, BEIJING):%m-%d %H:%M} 斗法输给 @shou_xia（雷火叟）", found
    assert missing is None
    # 夺灯：用户名不分大小写；昨天的、机器人的、没用户名的都不算
    assert lamp == [True, False, False, False, False], lamp
    assert observed == """天道演化（每 3 小时整点结算，观星能提前看到下一次）
────────────────
最近观星:
- 10-15 11:59 guan_xing 观得 12:00【Good - 地心火脉现世】，天命所归 ming_a，还没到
- 10-15 08:40 zhan_bu 观得 09:00【Good - 星辰异象】，天命所归 ling_ke
- 10-15 08:38 guan_xing 观得 09:00【Good - 星辰异象】，天命所归 ling_ke
最近结算:
- 10-15 09:00 星辰异象（元磁山核·乙）""", observed
    # 同一整点两个人观到星辰异象只预告一次；结算时说出观星时的天命所归
    assert first == (5, [
        "🔮 09:00 的天道演化是【星辰异象】，会出一枚元磁山核·乙\n"
        "当前天命所归：ling_ke（guan_xing 观星看到的，观星的人 5 分钟内能 .改换星移）",
        "🌟 09:00 天道演化【星辰异象】已结算，出了一枚元磁山核·乙（观星时天命归 ling_ke，之后有人改命就不是了）",
    ]), first
    assert again == (5, []), again
    assert blind == (5, ["🌟 09:00 天道演化【星辰异象】已结算，出了一枚元磁山核·乙（这次没人观星，不知道给了谁）"]), blind
    assert _settled("【天机阁快报 - 心魔大劫】\n\n天机演化结果: 心魔肆虐。") == "心魔大劫"
    assert _slot(bj(23, 8)) == datetime(2026, 10, 16, 0, 0, tzinfo=BEIJING)
    assert _slot(bj(4, 59, 43)).hour == 6 and _slot(bj(12, 0, 30)).hour == 15

    busy = {
        "username": "daoyou_test",
        "last_xiaoji_time": "2026-10-16T08:28:54+00:00",  # 副本字段存的是冷却结束时刻
        "last_zhuimo_time": "2026-10-15T01:40:50+00:00",
        "cultivation_cooldown_until": "2026-10-15T06:20:00+00:00",
        "equipped_treasure_id": json.dumps([WIND_WINGS]),  # 祭出风雷翅：裂缝 9 小时
        "last_rift_explore_time": "2026-10-15T00:00:00+00:00",
        "last_node_search_time": "2026-10-15T00:00:00+00:00",
        "last_dual_cultivation_time": "2026-10-15T05:30:00+00:00",
        "last_battle_date": "2026-10-15",
        "daily_battle_stamina": 6,
        "companion": {
            "name": "莎儿",
            "last_dream_map_seek_time": "2026-10-15T04:26:16+00:00",
            "last_companion_heart_tribulation_time": "2026-10-14T19:02:46+00:00",
            "voyage": {"status": "sailing", "end_time": "2026-10-15T12:29:10+00:00"},
        },
        "dongfu": {"companion_residence": json.dumps(
            [{"name": "南宫婉·月影", "last_divination_chain_time": "2026-10-14T21:38:26+00:00"}])},
        "sect_name": "星宫",
        "last_divination_time": "2026-10-15",
        "last_star_formation_time": "2026-10-15T03:01:21+00:00",
        "sect_leave_cooldown_until": "2026-10-11T02:48:42+00:00",
        "small_world": {"last_prayer_time": "2026-10-14T20:47:06+00:00", "last_edict_time": "2026-10-15T05:00:00+00:00"},
        "wild_experience_last_date": "2026-10-15",
        "wild_experience_daily_count": 8,
        "pagoda_progress": json.dumps({"last_attempt_date": "2026-10-14"}),
        "inventory": {"items": [{"item_id": XUANGU_PEARL, "quantity": 2}]},
    }
    assert format_cooldowns(busy, boot, now, lambda username, at: username == "daoyou_test") == """@daoyou_test 的冷却
────────────────
副本:
- 北冥小极宫：剩 1 天 2 小时，至 10-16 16:28
- 落云秘圃：无冷却
- 坠魔谷：无冷却
- 苍坤洞府：无冷却
- 虚天殿：无冷却
- 黄龙山大战：无冷却
- 黄龙夺灯：今日已出征
- 玄骨遗府：古府寻踪珠 2 颗
闭关:
- 闭关修炼：剩 3 分钟，至 14:20
- 探寻裂缝：剩 2 小时 43 分钟，至 17:00
- 搜寻节点：剩 5 小时 43 分钟，至 20:00
- 双修：剩 13 分钟，至 14:30
斗法:
- 今日神念：剩 6/10
侍妾:
- 莎儿（随行）：入梦寻图 剩 6 小时 9 分钟，至 20:26；共历心劫 可用；天机代卜 可用；远航 剩 6 小时 12 分钟，至 20:29
- 南宫婉·月影（藏娇阁）：入梦寻图 可用；共历心劫 可用；天机代卜 剩 3 小时 21 分钟，至 17:38
宗门:
- 观星：今日已观
- 周天星斗大阵：剩 8 小时 44 分钟，至 23:01
小世界:
- 祈愿：无冷却
- 神谕：剩 1 小时 43 分钟，至 16:00
日常:
- 野外历练：今日 8/8
- 闯塔：今日未闯""", format_cooldowns(busy, boot, now, lambda username, at: True)
    # 什么都没做过的号：昨天的神念不算今天，侍妾/宗门/小世界整段不出
    kong = format_cooldowns({"username": "kong", "last_battle_date": "2026-10-14", "daily_battle_stamina": 2},
                            boot, now, lambda username, at: False).splitlines()
    assert kong[9:11] == ["- 黄龙夺灯：今日还没出征（按群里副本记录）", "- 玄骨遗府：古府寻踪珠 0 颗"], kong
    assert kong[16:] == ["斗法:", "- 今日神念：剩 10/10", "日常:", "- 野外历练：今日 0/8", "- 闯塔：今日未闯"], kong

    assert COMMAND.match(".查冷却 @daoyou_test").groups() == ("冷却", "daoyou_test")
    assert COMMAND.match(".查灵根 @daoyou_test").groups() == ("灵根", "daoyou_test")
    assert COMMAND.match(".查状态 daoyou_test").groups() == ("状态", "daoyou_test")
    assert COMMAND.match("。查储物袋 云游子 ").groups() == ("储物袋", "云游子")
    assert COMMAND.match(".查灵根").groups() == ("灵根", None)
    assert not COMMAND.match(".查灵根 a b")

    # 名字解析：天机阁区分大小写，查不到再按道号/不分大小写的用户名找；道号重名列候选
    people = [
        {"username": "ling_a", "dao_name": "霸天子", "cultivation_level": "结丹后期", "cultivation_points": 900},
        {"username": "ling_b", "dao_name": "霸天子", "cultivation_level": "化神初期", "cultivation_points": 5000},
        {"username": "Ling_C", "dao_name": "独行子", "cultivation_level": "元婴初期", "cultivation_points": 300},
    ]

    def fake_cultivator(username):
        hit = [p for p in people if p["username"] == username]
        if not hit:
            raise asc_client.AscNotFoundError("未找到该修士")
        return dict(hit[0])

    real = _cultivator, _cached
    globals().update(
        _cultivator=fake_cultivator,
        _cached=lambda path: people if path.endswith("all_cultivators") else boot,
    )
    try:
        assert lookup("灵根", "霸天子") == (
            "道号「霸天子」有 2 人，用用户名再查：\n- ling_b（化神初期，修为 5000）\n- ling_a（结丹后期，修为 900）"
        ), lookup("灵根", "霸天子")
        assert lookup("灵根", "独行子").startswith("@Ling_C 的天命玉牒")
        assert lookup("灵根", "ling_c").startswith("@Ling_C 的天命玉牒")
        assert lookup("灵根", "无此人") == "天机阁查无此人：无此人"
        assert answer(".查灵根", "ling_b").startswith("@ling_b 的天命玉牒")  # 回复消息查被回复的人
        assert answer(".查灵根 ling_a", "ling_b").startswith("@ling_a 的天命玉牒")  # 写了名字就按名字
        assert answer(".查状态", "") == "对方没设 TG 用户名，天机阁查不到，用道号查吧"
        assert answer(".查状态") == "用法：.查状态 用户名或道号，或者回复某人的消息发 .查状态"
    finally:
        globals().update(_cultivator=real[0], _cached=real[1])
    assert not COMMAND.match(".查宗门 a")
    assert OBSERVE.match(".查观星") and OBSERVE.match("。查观星 ") and not OBSERVE.match(".查观星 a")
    # 帮助：每条可复制的指令都真认得、每种查询都写进去了；去掉 <code> 后不能再有 HTML 特殊字符
    assert answer(".帮助") == HELP and answer("。帮助 ") == HELP and answer(".帮助 a") is None
    listed = re.findall(r"<code>(.*?)</code>", HELP)
    assert all(HELP_COMMAND.match(c) or OBSERVE.match(c) or COMMAND.match(c) for c in listed), listed
    assert {f".查{kind}" for kind in FORMATTERS} <= set(listed), listed
    assert not re.search(r"[<>&]", re.sub(r"</?(code|b)>", "", HELP))
    print("self-check ok")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if sys.argv[1:2] == ["--self-check"]:
        self_check()
    elif sys.argv[1:2] == ["--check"] and len(sys.argv) == 3:
        print(answer(sys.argv[2]) or "不是小天尊的指令")
    else:
        run()
