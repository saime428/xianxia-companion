"""副本定义：现行指令认得出，bot 回包能认出来、能归到对的副本（昆吾山、虚天殿、苍坤洞府、坠魔谷）。

真实回包标题都带「【」，很多标题不带副本名，而且「【第一幕结果】」「【第 N 回合】」
昆吾山、坠魔谷、苍坤洞府、虚天殿都在用；虚天殿进本后的播报几乎都不回复任何消息。
样例摘自 2026-08~10 的群消息，用户名已换。
run: PYTHONPATH=app/src python tools/test_dungeon_defs.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.dungeon_defs import (  # noqa: E402
    DUNGEON_DEFINITIONS,
    dungeon_reply_matches,
    is_dungeon_command_text,
    is_dungeon_reply_text,
)
from tg_game.web.biz_dungeon_view_model import (  # noqa: E402
    extract_dungeon_command_buttons,
    list_dungeon_feed_source_messages,
)

CHAT = -1005550000555

# 1. 现行指令认得出，旧版岔路指令和无关的 .使用 不再算副本指令
for command in [
    ".开启昆吾山",
    ".加入昆吾山 12",
    ".进入昆吾山",
    ".昆吾抉择 路径3",
    ".昆吾抉择 2",
    ".昆吾抉择 夺",
    ".昆吾山状态",
    ".昆吾山奖励",
    ".昆吾山帮助",
    ".使用 化龙玺残印*2",
    ".使用 封魔塔真符 元合五极山",
    ".使用 昆吾灵钥",
    ".解散昆吾山",
    ".催动挪移令",
    # 虚天殿：道路之后的阵策、后殿两步、请离
    ".阵策 势",
    ".选择战术 稳扎稳打",
    ".后殿抉择 冲关",
    ".后殿阵策 卦",
    ".请离 @member_b",
    ".虚天殿帮助",
    ".坠魔谷帮助",
    # 黄龙夺灯：进本后每人每轮自己发 .夺灯行动
    ".开启夺灯",
    ".加入夺灯 4414e0b054",
    ".退出夺灯",
    ".进入夺灯",
    ".夺灯路线 摄焰",
    ".夺灯行动 3 防守 @member_b",
    ".夺灯退路 雾谷",
    ".夺灯奖励",
    ".元明灯 重聚",
]:
    assert is_dungeon_command_text(command), command
for command in [".选择 岔路2", ".选择 强行摘取", ".选择 静待时机", ".使用 幸运符", ".赠送 昆吾通行令*2", ".支援慕兰 破灯"]:
    assert not is_dungeon_command_text(command), command

# 快捷按钮取自 help_lines，每个都得是本副本的白名单指令
for entry in DUNGEON_DEFINITIONS:
    for button in extract_dungeon_command_buttons(entry):
        assert any(button.startswith(p) for p in entry["command_prefixes"]), (entry["key"], button)

# 2. 真实回包：认得出，而且只归一个副本
ACT1_RESULT = (
    "【第一幕结果】\n化龙玺残印与山脉龙气相合，队伍脚下禁纹渐稳，推进节奏更从容。\n\n"
    "当前状态：封印 67 | 魔气 5 | 山禁 42 | 宝压 0 | 玲珑 22 | 士气 106\n\n"
    "【第二幕：昆吾遗宫】\n使用 .昆吾抉择 1/2/3 继续。"
)
TOWER_ROUND = (
    "【第 1 回合】\n塔心魔影引动 太阴焚神：士气 -7，魔气 +6，封印 -5，宝压 +1\n"
    " > 队伍反击造成 1950004521 点伤害。\n"
    " > 塔心血量：5414802798/7364807319 | 封印 160 | 魔气 34 | 山禁 2 | 宝压 40 | 玲珑 46 | 士气 124"
)
XUTIAN_FORK = (
    "【第二关·冰火之路】\n前方出现了两条岔路：一条寒气逼人，一条烈焰熊熊。\n"
    "队长 @captain_a，请在2分钟内点击下方按钮抉择，或使用 .选择道路 冰 / .选择道路 火 做出决定！"
)
XUTIAN_ROUND = (
    "【第 1 回合】\n > 【偏殿异变·裂焰分流】阵眼周围的烈焰忽然分成数道岔流。\n"
    " > 众人合力对【焚天烈焰】造成了 187.24亿 伤害！\n > 破阵进度: 56% (187.24亿/334.13亿)"
)
XUTIAN_BOSS_ROUND = (
    "【第 1 回合】\n万天明之影驱使数道魔影轮番突刺，队伍在不断换位中被迫消耗心神。\n"
    " > 你们对万天明之影造成了 434.9亿 伤害！\n > 万天明之影血量: 84% (2429.9亿/2864.8亿) | 我方士气: 113%"
)
CANGKUN_ACT1 = (
    "【第一幕结果】\n你们借外界杂乱灵息伪装身形，勉强混过了外围巡查。\n\n"
    "当前状态：禁制裂隙 110 / 神魂稳度 102 / 慕兰警戒 39 / 贪念 4 / 神识余量 548\n\n"
    "请队长使用 .苍坤抉择 1/2/3 做出取宝选择。"
)
CANGKUN_END = (
    "【苍坤上人洞府·脱身失败】\n众人只得弃掉大半战果狼狈退走。\n"
    "最终禁制裂隙：110 | 神魂稳度：90 | 慕兰警戒：79 | 卷轴线索：3"
)
ZHUIMO_ROUND = (
    "【第 1 回合】\n古魔施展了 魔潮冲顶：士气 -11，魔染 +2，封印稳定度 -2\n"
    " > 残识血量：4066695447/4746986006 | 魔染：6 | 封印：92/100 | 士气：125"
)
ZHUIMO_ACT1 = "【第一幕结果】\n你们借残阵阴影潜行。\n\n当前状态：魔染 14 / 封印 38 / 士气 112\n\n使用 .坠魔抉择 1 或 .坠魔抉择 2。"
REPLIES = {
    "kunwu": [
        "【昆吾山·集结】\n道友 @captain_a 准备开启昆吾山试炼，正在召集同伴！(1/5)\n房间ID: 12",
        "【昆吾山·第一幕：封魔山门】\n众人踏入昆吾山麓，玲珑塔影倒悬天际。\n"
        "初始状态：封印 45 | 魔气 0 | 山禁 60 | 宝压 0 | 玲珑 20 | 士气 100",
        ACT1_RESULT,
        "【第二幕结果】\n你们护住灵缈园残阵。\n\n当前状态：封印 79 | 魔气 0 | 山禁 36 | 宝压 2 | 玲珑 36 | 士气 116",
        "【八灵尺阵眼处理完毕】\n你们以灵力镇住尺光，八灵封印重新归位。\n\n"
        "当前状态：封印 107 | 魔气 0 | 山禁 36 | 宝压 6 | 玲珑 36 | 士气 119\n\n【下一阵眼：化龙玺阵眼】",
        "【化龙玺阵眼处理完毕】\n当前状态：封印 121 | 魔气 9 | 山禁 30 | 宝压 15 | 玲珑 36 | 士气 119",
        "【第三幕结果】\n黑风旗影裹住众人直入塔心。\n\n"
        "当前状态：封印 137 | 魔气 26 | 山禁 26 | 宝压 27 | 玲珑 36 | 士气 119\n三处阵眼已定，众人冲入玲珑封魔塔心。",
        "【昆吾碎片支援】\n昆吾碎片支援发动：【镇山封纹】x1、【圭灵牌影】x1。封印 +33，魔气 -2，山禁 -24，宝压 +12。",
        "【第四幕·玲珑封魔塔】\n封魔塔心开裂。\n你们必须在 7 回合内 击溃塔心魔影，并将封印推进至 100。",
        TOWER_ROUND,
        "【昆吾山·封魔塔镇压成功】\n塔心魔影被镇灭，玲珑封魔塔重新闭合。",
        "【昆吾山·封魔塔镇压失败】\n魔气冲破山禁，塔心魔影反噬众人神魂。",
        "【昆吾山状态 · 房间 13】\n阶段：集结中 | 状态：组队中",
        "【昆吾山·封魔塔奖励】\n- 通关保底：每位队员 8000 修为、600 贡献",
        "【昆吾山·封魔塔指令】\n- .开启昆吾山：队长开房（需昆吾通行令）",
    ],
    # 一场虚天殿从进本到后殿，按出现顺序
    "xutian": [
        "队伍已进入虚天殿...石门缓缓关闭，前路凶险未知！",
        "【第一幕·前殿探路】\n你们刚踏入虚天殿外层，残破宫阙间便接连涌现尸煞、迷雾与古禁。",
        "【卦象验阵】\n【卦象词条】 巽风上坎水下 · 二爻守中",
        "【前殿异变·1】摄魂迷雾\n一片灰白迷雾自殿顶垂落，神识一触便传来针扎般的刺痛。",
        "【灵医】稳魂定魄，【影刃】探出安全路径，队伍迅速穿过了迷雾区。\n主力道友：@member_b、@member_c\n当前前殿气势：92%",
        "【前殿已破】\n你们连过 2 道险关，终于杀入虚天殿中殿。",
        XUTIAN_FORK,
        "【第二关·玄冰秘径】\n你们已定下 玄冰秘径，但要破此偏殿，还需先定推进阵策。",
        "你们选定了 【焚炎秘径】，主攻阵眼锁定为 【焚天烈焰】。\n当前阵策：猎机夺势\n开局士气：116%",
        XUTIAN_ROUND,
        "【焚天烈焰】的光芒被成功压制！冰火炼心阵威力大减，你们通过了第二关的考验。",
        "【第三关·决战】\n一名手执血幡、目光阴鸷的青袍身影自鼎焰后步出——正是【万天明之影】！",
        "万天明之影借鼎焰凝成了数层替劫幻身，显然不打算与你们硬碰硬。",
        XUTIAN_BOSS_ROUND,
        # 首领在万天明之影、蛮胡子之影里随机（.虚天殿奖励 面板）；蛮胡子一场没抓到样本，照万天明的格式写
        "【第 2 回合】\n > 蛮胡子之影血量: 61% | 我方士气: 104%",
        "【鼎前抉择】\n鼎心禁制并未平息，反倒有新的机缘在暗处浮沉。",
        "【战利品结算·夺鼎】\n你们逆着鼎火冲入核心。\n所有队员均获得 5000修为 和 500贡献！",
        "【后殿余波】\n第三关的战利品已先行封存。",
        "【第四关·后殿试阵】 你们决定以 借卦逆推 强闯后殿。",
        "【第五关·虚天鼎灵·残焰】\n后殿真正的考验终于现身。",
        "【后殿第 1 回合】\n > 残焰血量：67% (59.44万/87.61万) | 鼎压：40/100 | 士气：96%",
        "【后殿冲关止步】\n回合耗尽，鼎灵残焰仍未被真正压灭。",
    ],
    "cangkun_cave": [
        "【苍坤上人洞府·集结】\n@captain_a 以【苍坤残图】锁定了太妙神禁的薄弱方位！\n房间ID: 69",
        "【苍坤上人洞府·第一幕】\n你们沿慕兰草原边缘潜行而来。",
        CANGKUN_END,
        "【苍坤上人洞府·脱身成功】\n通关保底：每位队员获得 6256修为、515贡献。",
    ],
    "zhuimo": [
        "【坠魔谷·集结】",
        "  【坠魔谷·第一幕：裂隙外谷】",
        "【第三幕·古魔祭坛】\n古魔残识自祭坛深处苏醒，封印阵纹与魔潮同时暴走！\n（古魔残识血量：4746986006）",
        ZHUIMO_ROUND,
        "【坠魔谷·封魔成功】\n古魔残识被彻底镇压，封印重塑完成！",
    ],
    "blood_trial": ["【血色试炼·撤离成功】"],
    # 2026-10-12 上线当晚只见过帮助、预检、重聚进度三种面板，还没人进过本
    "duodeng": [
        "【黄龙夺灯】五人侧翼助战，牵制乐上师、破阵夺灯、带焰突围。\n每人结丹初期以上，每日一次，出发时每人消耗20 LDC门票一张。",
        "【黄龙夺灯·队伍预检·4414e0b054】\n路线：封阵｜2/5人｜每人门票20 LDC\n@captain_a：炼虚初期｜破军→破阵\n"
        "@member_b：元婴后期｜影刃→潜袭\n尚未扣票；用 .进入夺灯 正式出发。",
        "【元明灯·重聚进度】\n灯座纹芯：未得\n灯盏灵枢：未得\n青焰本源：未得\n祭灯古诀：未得",
    ],
}
for key, texts in REPLIES.items():
    for text in texts:
        owners = [e["key"] for e in DUNGEON_DEFINITIONS if dungeon_reply_matches(e, text)]
        assert owners == [key] and is_dungeon_reply_text(text), (key, owners, text)
# 不是副本回包：全群残图广播、别的面板、空串
for text in [
    "【全群异闻·虚天残图】\n@member_b 共梦得见 南渊残纹（重复藏本），虚天残图进度已至 3/4！",
    "【全群广播·苍坤残图拼合】\n@member_b 拼合苍坤残图成功，已获得一张可开启副本的完整残图！",
    "【法宝谱】\n昆吾封印: 无",
    # 慕兰谍影的军报天天提「夺灯」，和副本无关
    "【慕兰谍影】\n日期：2026-10-11\n状态：待公开\n\n军报匣\n1. 圣灯已熄，只需正面冲阵便可夺灯。（未辨）",
    "",
]:
    assert not is_dungeon_reply_text(text), text

# 3. 归属：同样标题的消息按正文/回复链分到各自副本
MESSAGES = [
    (1, False, 0, ".昆吾抉择 路径1"),
    (2, True, 1, ACT1_RESULT),
    (3, True, 0, TOWER_ROUND),
    (4, True, 0, XUTIAN_FORK),
    (5, True, 0, XUTIAN_ROUND),
    (6, False, 0, ".坠魔抉择 3"),
    (7, True, 6, ZHUIMO_ACT1),
    (8, True, 0, "【坠魔谷·封魔成功】\n古魔残识被彻底镇压，封印重塑完成！"),
    (9, False, 0, ".使用 镇山封纹"),
    (10, True, 9, "你催动【镇山封纹】，其气机暂时融入昆吾山封魔塔影。"),
    (11, True, 0, XUTIAN_BOSS_ROUND),
    (12, False, 0, ".苍坤抉择 2"),
    (13, True, 12, CANGKUN_ACT1),
    (14, True, 0, CANGKUN_END),
    (15, True, 0, ZHUIMO_ROUND),
    (16, False, 0, ".阵策 势"),
]


class FakeStorage:
    rows = [
        {"chat_id": CHAT, "message_id": mid, "is_bot": is_bot, "reply_to_msg_id": reply_to, "text": text, "created_at": mid}
        for mid, is_bot, reply_to, text in MESSAGES
    ]

    def list_bound_messages(self, profile_id=None, chat_id=None, limit=200):
        return list(self.rows)

    def get_bound_message(self, chat_id, message_id):
        return next((row for row in self.rows if row["message_id"] == message_id), None)


def feed(dungeon_key):
    return {m["message_id"] for m in list_dungeon_feed_source_messages(FakeStorage(), CHAT, dungeon_key)}


assert feed("kunwu") == {1, 2, 3, 9, 10}, feed("kunwu")
assert feed("zhuimo") == {6, 7, 8, 15}, feed("zhuimo")
assert feed("xutian") == {4, 5, 11, 16}, feed("xutian")
assert feed("cangkun_cave") == {12, 13, 14}, feed("cangkun_cave")
print("dungeon defs: ok")
