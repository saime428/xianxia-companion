"""三个 reply-driven 自动任务共用一段执行逻辑，这里守住 SPEC 契约与各自解析。

运行：.venv/bin/python tools/test_reply_driven_specs.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.features.harmony.biz_dual_cultivation import (
    SPEC as DUAL_SPEC,
    align_cooldown_seconds,
    resolve_partner_message_id,
)
from tg_game.features.soul.biz_soul_cultivation import SPEC as SOUL_SPEC
from tg_game.features.sect.biz_taiyi_yindao import SPEC as TAIYI_SPEC
from tg_game.features.sword.biz_sword_formation import SPEC as SWORD_SPEC
from tg_game.features.vase.biz_vase_condense import SPEC as VASE_SPEC
from tg_game.features.xinggong.biz_xinggong_divine import SPEC as DIVINE_SPEC
from tg_game.features.artifact.biz_xutian_cauldron import SPEC as XUTIAN_SPEC

REQUIRED = (
    "command",
    "await_state",
    "parse",
    "default_interval_seconds",
    "poll_seconds",
    "wait_seconds",
    "buffer_seconds",
    "labels",
)
LABEL_KEYS = ("sending", "pending", "success", "cooldown", "timeout", "unknown")

for name, spec in (
    ("vase", VASE_SPEC),
    ("sword", SWORD_SPEC),
    ("soul", SOUL_SPEC),
    ("dual", DUAL_SPEC),
    ("taiyi", TAIYI_SPEC),
    ("divine", DIVINE_SPEC),
    ("xutian", XUTIAN_SPEC),
):
    for key in REQUIRED:
        assert key in spec, (name, key)
    assert spec["command"].startswith("."), name
    assert callable(spec["parse"]), name
    for key in LABEL_KEYS:
        assert spec["labels"].get(key), (name, key)
    # 通用分支只认这几种 kind
    kind, seconds = spec["parse"]("完全无关的一段话")
    assert kind in {"success", "cooldown", "active", "unknown"}, (name, kind)

# 只有凝液带跟发钩子，且返回 (指令, 延迟)
assert "followup" not in SWORD_SPEC and "followup" not in SOUL_SPEC
assert "followup" not in DUAL_SPEC
# 只有双修需要"回复某人"，且缺了目标要有专门文案
assert "reply_target" not in VASE_SPEC and "reply_target" not in SWORD_SPEC
assert callable(DUAL_SPEC["reply_target"])
assert DUAL_SPEC["labels"].get("no_reply_target")
assert VASE_SPEC["followup"]({"strategy": "养树"}) == (".掌天瓶 养树", 30)
assert VASE_SPEC["followup"]({"strategy": ""})[0] == ""
# 只有观星带「按回包内容跟一条回复」和「只在窗口里发」这两个钩子；别的任务不该被它们影响
for other in (VASE_SPEC, SWORD_SPEC, SOUL_SPEC, DUAL_SPEC, TAIYI_SPEC):
    assert "reply_followup" not in other and "send_gate" not in other
assert callable(DIVINE_SPEC["reply_followup"]) and callable(DIVINE_SPEC["send_gate"])
assert "{followup}" in DIVINE_SPEC["labels"]["success_followup"]

# 元神修炼：成功回包带时长，冷却回包按剩余时长重排
parse = SOUL_SPEC["parse"]
assert parse("你的第二元神已开始闭关修炼，将在24小时后为你带来修为增长。") == (
    "success",
    24 * 3600,
)
kind, seconds = parse("你的第二元神正在闭关中，还需 3小时20分钟。")
assert (kind, seconds) == ("cooldown", 3 * 3600 + 20 * 60), (kind, seconds)
# 实测冷却回包不带时长。bot 放人比"24小时后"晚二十几分钟，这里必须小步重试：
# 一旦退回默认的 24 小时间隔，就会整整一天不修炼，而且第二天还会照样早到、继续错过。
kind, seconds = parse("你的第二元神正在(修炼中)，无法分心修炼。")
assert kind == "need_status" and seconds == 0, (kind, seconds)
from tg_game.features.soul.biz_soul_cultivation import parse_status_reply
assert parse_status_reply("【你的第二元神：火之元神】 状态: 修炼中 (剩余: 5小时24分钟47秒)") == ("cooldown", 5 * 3600 + 24 * 60 + 47)
# 面板说窍中温养时 .元神修炼 仍可能回"正在…无法分心修炼"，立刻重发会每 90 秒刷一对指令，改成 10 分钟后再试
assert parse_status_reply("【你的第二元神：水之元神】 状态: 窍中温养") == ("cooldown", 600)
assert parse_status_reply("【你的第二元神：金之元神】 状态: 修炼中") == ("cooldown", 600)
# 成功后排的下一轮要比"24小时整"晚一点，别每次都早到
assert SOUL_SPEC["buffer_seconds"] >= 300, SOUL_SPEC["buffer_seconds"]
assert parse("") == ("unknown", 0)
assert parse("莫名其妙的回复")[0] == "unknown"

taiyi = TAIYI_SPEC["parse"]
assert taiyi("你引动【水之道】，获得了 100点神识！\n并领悟了临时增益【润水之息】：\n普通闭关修炼时，获得的修为增加45%。") == (
    "success",
    12 * 3600,
)
assert taiyi("你引动【金之道】，获得了 100点神识！") == ("success", 12 * 3600)
assert taiyi("大道感悟需循序渐进，请在 11小时59分钟48秒 后再次引道。") == (
    "cooldown",
    11 * 3600 + 59 * 60 + 48,
)
assert taiyi("") == ("unknown", 0)
assert taiyi("点卯成功")[0] == "unknown"
from tg_game.features.sect.biz_taiyi_yindao import command_for_task, normalize_element
assert normalize_element("火") == "火"
assert command_for_task({"strategy": "金"}) == ".引道 金"
assert command_for_task({}) == ".引道 水"

# 成功但没解析出时长时退回默认间隔，避免排到 0
kind, seconds = parse("你的第二元神已开始闭关修炼。")
assert kind == "success" and seconds == SOUL_SPEC["default_interval_seconds"]

# 温养双修：成功/冷却/未缔结同参
dual = DUAL_SPEC["parse"]
assert dual(
    "【温养双修·大成】\n在同参契印的加持下，你与 @demo_main 灵力完美交融，事半功倍！\n"
    "@demo_alt_old 修为增加了 39 点，并获得 15 点宗门贡献！\n@demo_main 修为增加了 50 点！"
) == ("success", 3600)
# 未缔结同参：结构性问题，按整轮间隔再试（秒数 0 = 用默认间隔）
assert dual("你尚未缔结同参道侣，无法进行温养双修。请先使用 .缔结同参 寻找一位有缘人。") == (
    "cooldown",
    0,
)
# 实测冷却回包不带时长：小步重试，别错过整个窗口
kind, seconds = dual("道友 @demo_alt_old 心神尚未恢复，无法进行双修（冷却中）。")
assert kind == "cooldown" and 0 < seconds <= 600, (kind, seconds)
assert dual("道友 @demo_alt 心神未定，无法进行双修（冷却中）。")[0] == "cooldown"
assert dual("双修冷却中，请在 25分钟 后再试。") == ("cooldown", 25 * 60)
assert dual("") == ("unknown", 0)
assert DUAL_SPEC.get("timeout_as_success") is True
assert callable(DUAL_SPEC.get("align_cooldown_seconds"))


# 回复目标：只认道侣本人发的群消息（不是 bot、不是自己发的）
class _FakeProfile:
    def __init__(self, pid, username="", uid=""):
        self.id = pid
        self.telegram_username = username
        self.telegram_user_id = uid


class _FakeStorage:
    def __init__(self, rows, profiles=(), outgoing=()):
        self.rows = rows
        self.profiles = list(profiles)
        self.enqueued = []
        self.latest = None
        self.outgoing = list(outgoing)

    def list_outgoing_commands(self, profile_id=None, chat_id=None, limit=20, thread_id=None):
        return self.outgoing[:limit]

    def list_profiles(self):
        return self.profiles

    def get_profile_by_telegram_user_id(self, uid):
        for p in self.profiles:
            if str(p.telegram_user_id) == str(uid):
                return p
        return None

    def get_latest_outgoing_command(self, *args, **kwargs):
        return self.latest

    def enqueue_outgoing_command(self, profile_id, chat_id, text, thread_id=None,
                                 reply_to_msg_id=None, chat_type="group",
                                 bot_username="", delay_seconds=0):
        self.enqueued.append({"profile_id": profile_id, "chat_id": chat_id, "text": text})
        return len(self.enqueued)

    def list_partner_bound_messages(self, profile_id, chat_id, *, partner, limit=5):
        """照着真实实现筛：只按发送者，不看 profile / direction。"""
        key = str(partner or "").strip().lstrip("@").lower()
        hits = [
            row
            for row in self.rows
            if not int(row.get("is_bot") or 0)
            and (
                str(row.get("sender_username") or "").lower() == key
                or str(row.get("sender_id") or "") == key
            )
        ]
        return hits[:limit]


import time as _time
_NOW = _time.time()
TASK = {"profile_id": 3, "chat_id": -100, "strategy": "demo_main"}
# 只看"是不是道侣本人发的"，**不看 direction**：大号自己的存档是 outgoing，
# 小号库里的同一条是 incoming，message_id 一样，回复哪一份都一样。
# 列表按新到旧给，取第一条命中的。
assert (
    resolve_partner_message_id(
        _FakeStorage(
            [
                {"direction": "incoming", "is_bot": 1, "sender_username": "demo_main", "message_id": 1, "created_at": _NOW},
                {"direction": "incoming", "is_bot": 0, "sender_username": "someone", "message_id": 3, "created_at": _NOW},
                {"direction": "outgoing", "is_bot": 0, "sender_username": "demo_main", "message_id": 2, "created_at": _NOW},
                {"direction": "incoming", "is_bot": 0, "sender_username": "demo_main", "message_id": 4, "created_at": _NOW},
            ]
        ),
        TASK,
    )
    == 2
)
assert resolve_partner_message_id(_FakeStorage([]), TASK) is None
# 2026-09-10 实故障：大号进了 4 天深度闭关不再说话，群里又很热闹，
# 老实现只翻本档案最近 200 条，道侣那条早被挤出窗口 -> 双修静默停摆 18 小时。
# 现在是按发送者直接查，道侣消息再老也能找到。
_OLD_BUT_ONLY = [
    {"direction": "incoming", "is_bot": 1, "sender_username": "hantianzun24_bot", "message_id": 900 + i, "created_at": _NOW}
    for i in range(300)
] + [
    {"direction": "incoming", "is_bot": 0, "sender_username": "demo_main", "message_id": 12232011, "created_at": _NOW}
]
assert resolve_partner_message_id(_FakeStorage(_OLD_BUT_ONLY), TASK) == 12232011
# 2026-09-10 第二层：router 里普通消息**只有管理员档案入库**，小号库里几乎
# 没有大号的消息，只有偶然命中"股市消息无条件存储"的那一条（33 小时前）。
# 回复那条旧消息时链接被丢掉，bot 不认。message_id 在群里是全局的，所以要
# 跨 profile 找——大号自己存的 outgoing 记录同样能用，而且永远新鲜。
_CROSS_PROFILE = [
    {"direction": "outgoing", "is_bot": 0, "sender_username": "demo_main", "message_id": 12257999, "created_at": _NOW},
    {"direction": "incoming", "is_bot": 0, "sender_username": "demo_main", "message_id": 12232011, "created_at": _NOW},
]
assert resolve_partner_message_id(_FakeStorage(_CROSS_PROFILE), TASK) == 12257999
# bot 发的永远不能当回复目标
assert resolve_partner_message_id(
    _FakeStorage([{"direction": "incoming", "is_bot": 1, "sender_username": "demo_main", "message_id": 5, "created_at": _NOW}]),
    TASK,
) is None
# uid 也能当道侣标识
assert (
    resolve_partner_message_id(
        _FakeStorage(
            [{"direction": "incoming", "is_bot": 0, "sender_id": 1000000023, "message_id": 9, "created_at": _NOW}]
        ),
        {"profile_id": 3, "chat_id": -100, "strategy": "1000000023"},
    )
    == 9
)


# 2026-09-10 用户要的做法：道侣太久没说话，就先让它自己发一条 11，再回复这条。
# 目的是保证回复目标永远新鲜——盯着旧消息迟早被 Telegram 丢掉回复链接。
_PARTNER = _FakeProfile(2, username="demo_main", uid="1000000023")
_STALE = [{"direction": "outgoing", "is_bot": 0, "sender_username": "demo_main",
           "message_id": 111, "created_at": _NOW - 3 * 3600}]
st = _FakeStorage(_STALE, profiles=[_PARTNER])
assert resolve_partner_message_id(st, TASK, now=_NOW) is None, "目标太旧就不该用"
assert st.enqueued == [{"profile_id": 2, "chat_id": -100, "text": "11"}], st.enqueued

# 目标够新的时候不要打扰群里（不发 11）
_FRESH = [{"direction": "outgoing", "is_bot": 0, "sender_username": "demo_main",
           "message_id": 222, "created_at": _NOW - 60}]
st = _FakeStorage(_FRESH, profiles=[_PARTNER])
assert resolve_partner_message_id(st, TASK, now=_NOW) == 222
assert st.enqueued == [], "有新鲜目标时不该再发 11"

# 11 必须由**道侣自己的档案**发出去，不是发起方
assert st.enqueued == [] and _PARTNER.id == 2
# 找不到道侣档案就别乱发
st = _FakeStorage(_STALE, profiles=[])
assert resolve_partner_message_id(st, TASK, now=_NOW) is None
assert st.enqueued == [], "认不出道侣档案时不该发 11"

# 11 发出去就记 sent、不再等回包；刚发出 5 分钟内仍要挡住重复催发（防记录失败时每 45 秒补一条）
st = _FakeStorage(_STALE, profiles=[_PARTNER])
st.latest = {"status": "sent", "updated_at": _NOW - 60}
assert resolve_partner_message_id(st, TASK, now=_NOW) is None
assert st.enqueued == [], "刚催过的 11 已发出，5 分钟内不该再催"
st.latest = {"status": "sent", "updated_at": _NOW - 10 * 60}
assert resolve_partner_message_id(st, TASK, now=_NOW) is None
assert len(st.enqueued) == 1, "隔了 10 分钟还没等到目标，应当再催一次"

# 没回包的双修会进冷却：10 分钟重试要对齐到那次发送 +61 分钟，不能刷屏
_UNANSWERED_AT = _NOW - 13 * 60
_ALIGN_TASK = {"profile_id": 3, "chat_id": -100, "thread_id": 1}
assert align_cooldown_seconds(_FakeStorage([]), _ALIGN_TASK, 540, now=_NOW) == 540
assert align_cooldown_seconds(
    _FakeStorage(
        [],
        outgoing=[
            {"text": ".双修 温养", "status": "needs_manual_confirm", "created_at": _UNANSWERED_AT},
        ],
    ),
    _ALIGN_TASK,
    540,
    now=_NOW,
) == int(_UNANSWERED_AT + 3600 + 60 - _NOW)
# 回包自带时长时不要改
assert align_cooldown_seconds(
    _FakeStorage(
        [],
        outgoing=[
            {"text": ".双修 温养", "status": "needs_manual_confirm", "created_at": _UNANSWERED_AT},
        ],
    ),
    _ALIGN_TASK,
    25 * 60,
    now=_NOW,
) == 25 * 60

# 注册漏登记就是「自动引道打不开」的成因：SPEC 写好了，但执行器和网页都不认。
# 每个 SPEC 必须同时进这两张表，网页开关直接读 REPLY_DRIVEN_AUTO_SPECS。
from tg_game.runtime.executors import COMPANION_AUTO_FEATURES, REPLY_DRIVEN_AUTO_SPECS

for name, spec in (
    ("vase", VASE_SPEC),
    ("sword", SWORD_SPEC),
    ("soul", SOUL_SPEC),
    ("dual", DUAL_SPEC),
    ("taiyi", TAIYI_SPEC),
    ("divine", DIVINE_SPEC),
    ("xutian", XUTIAN_SPEC),
):
    key = next(k for k, v in REPLY_DRIVEN_AUTO_SPECS.items() if v is spec)
    assert COMPANION_AUTO_FEATURES.get(key, {}).get("command") == spec["command"], name
assert len(REPLY_DRIVEN_AUTO_SPECS) == 7, sorted(REPLY_DRIVEN_AUTO_SPECS)
from tg_game.web.biz_companion_view_model import COMPANION_AUTO_FEATURES as WEB_FEATURES
assert WEB_FEATURES["xutian_cauldron"]["command"] == ".虚天鼎"

# 虚天鼎：面板原文取自 10-13~10-14 群里实测（一个炼焰中的号、一个已圆满的号）
_XT_HEAD = "【虚天鼎】\n持有: 已得 | 祭炼: 已祭炼 | 本命: 否 | 祭出: 是\n鼎焰状态: 炼焰中\n"
_XT_TAIL = "\n命令：.法宝 祭炼 虚天鼎、.法宝 炼焰 虚天鼎、.法宝 通宝 虚天鼎、.法宝 乾蓝冰焰"
XT_COOLING = (
    _XT_HEAD + "炼焰进度: 2/9\n通宝进度: 0/6\n玄骨诀: 未得，可于【玄骨事件】中夺取\n已解锁: 鼎身镇压\n\n"
    "下一步：继续 .法宝 炼焰 虚天鼎，待九转圆满后，便可把【乾蓝冰焰】自鼎上完整取下。\n"
    "下一次炼焰：修为 260000 / 神识 360\n\n冷却\n- 炼焰冷却：6小时23分钟50秒\n" + _XT_TAIL
)
XT_READY = XT_COOLING.replace("- 炼焰冷却：6小时23分钟50秒", "- 当前无额外冷却")
XT_DONE = (
    "【虚天鼎】\n持有: 已得 | 祭炼: 已祭炼 | 本命: 否 | 祭出: 是\n鼎焰状态: 通宝诀成\n"
    "炼焰进度: 9/9\n通宝进度: 6/6\n玄骨诀: 已悟，可借魔诀分流寒焰\n"
    "已解锁: 鼎身镇压、乾蓝冰焰已离鼎、通宝诀\n\n"
    "下一步：若想继续提升焰路，应转去培养独立法宝【乾蓝冰焰】。\n已无后续阶段。\n\n冷却\n- 当前无额外冷却\n" + _XT_TAIL
)
xt = XUTIAN_SPEC["parse"]
# 冷却只算冷却那一行，不能把面板别处的数字加进去
assert xt(XT_COOLING) == ("cooldown", 6 * 3600 + 23 * 60 + 50), xt(XT_COOLING)
assert xt(XT_READY) == ("success", 8 * 3600)
assert xt(XT_DONE) == ("done", 0)
assert XUTIAN_SPEC["labels"]["done"]
# 「命令：」那行列着全部指令，没有虚天鼎（只有仿制品）的回包不能当面板
assert xt("【虚天鼎仿制品】已与你神魂绑定，不可赠予他人！")[0] == "unknown"
assert xt("你的神识不足，无法稳住【虚天鼎】鼎焰进行炼焰。")[0] == "unknown"


class _PayloadStorage:
    def __init__(self, payload):
        import json as _json
        self.me_json = _json.dumps(payload)

    def get_external_account(self, profile_id, provider):
        return {"me_json": self.me_json}


_XT_TASK = {"profile_id": 2}
_XT_REPLY = {"text": XT_READY, "message_id": 1}
follow = XUTIAN_SPEC["reply_followup"]
assert follow(_PayloadStorage({"cultivation_points": 5557012, "shenshi_points": 710}), _XT_TASK, _XT_REPLY) == (".法宝 炼焰 虚天鼎", 0)
# 神识不够下一次的 360 就别发（洞府寻宝「用完神识」的号常年是 0）
assert follow(_PayloadStorage({"cultivation_points": 5557012, "shenshi_points": 0}), _XT_TASK, _XT_REPLY) == ("", 0)
assert follow(_PayloadStorage({"cultivation_points": 100, "shenshi_points": 710}), _XT_TASK, _XT_REPLY) == ("", 0)
# 天机阁没同步到这两个字段时照发，交给 bot 判
assert follow(_PayloadStorage({}), _XT_TASK, _XT_REPLY) == (".法宝 炼焰 虚天鼎", 0)
# 炼焰满 9 次改通宝
_XT_TONGBAO = {"text": XT_READY.replace("炼焰进度: 2/9", "炼焰进度: 9/9"), "message_id": 1}
assert follow(_PayloadStorage({}), _XT_TASK, _XT_TONGBAO) == (".法宝 通宝 虚天鼎", 0)
assert xt(_XT_TONGBAO["text"]) == ("success", 8 * 3600)

print("ok")
