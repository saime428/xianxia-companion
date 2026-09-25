"""两名侍妾（游戏 2026-09-19 上线）：面板只读随行那位的那一段，远航按「谁闲着换谁出来」轮换。

面板、天机阁数据都是 09-19 线上原样（cncc01 的两段面板；丁真人、甲真人、乙真人的 companion / companion_residence）。
坑：藏娇阁那位的「远航状态: …已归航，待结算」也印在面板里，老代码会对着随行的人狂发 .远航归来。

run: PYTHONPATH=app/src python tools/test_two_companions.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app" / "src"))

from tg_game.features.companion import biz_companion_roster as roster  # noqa: E402
from tg_game.features.companion import biz_companion_voyage as voyage  # noqa: E402
from tg_game.runtime import executors as ex  # noqa: E402
from tg_game.storage import ASC_EXTERNAL_PROVIDER, Storage  # noqa: E402

CHAT = -1001000000001
NOW = time.time()
FOOTER = (
    "---\n\n常住洞府与随行均保留既有神通，沿用原情缘、消耗、冷却与远航限制。\n"
    "你已有两名侍妾，暂不可继续寻缘。可使用 .召回侍妾 名字 切换随行侍妾。"
)


def block(index, name, status, *, dream, heart, divination, voyage_line="", kind="道心侍妾"):
    return (
        f"{index}. 你的{kind}: 【{name}】 (状态: {status})\n\n情缘值: 570\n已解锁神通:\n"
        " - 【红袖添香】: 闭关失败时，修为惩罚降低30%。\n\n【掩月心契】\n- 当前誓约: 无\n"
        "命令: .立誓 护道/守秘/共修、.毁誓\n\n【第二期机缘】\n- 天机代卜链: 无\n- 坠魔谷护持: 可用（剩余 900分钟）\n"
        f"- 入梦寻图冷却: {dream}\n- 共历心劫冷却: {heart}\n- 天机代卜冷却: {divination}\n"
        "- 梦图拼片: 虚天 0/4 | 苍坤 1/4\n命令: .入梦寻图、.残图、.拼图、.共历心劫、.坠魔心劫、.天机代卜\n\n"
        + (f"{voyage_line}\n\n" if voyage_line else "")
    )


def panel(*blocks):
    return "".join(blocks) + FOOTER


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def payload_for(attending, residents):
    return {
        "companion": attending,
        "companion_status": "随行中",
        "dongfu": {"companion_residence": json.dumps(residents, ensure_ascii=False)},
    }


def sailing(end_ts, route="均衡"):
    return {"status": "sailing", "route": route, "start_time": iso(end_ts - 8 * 3600), "end_time": iso(end_ts)}


def check_panel_parsing():
    # cncc01 16:14：随行的董萱儿空闲，藏娇阁的冰凤还在海上
    text = panel(
        block(1, "董萱儿", "随行中", dream="479分钟", heart="可施展", divination="713分钟"),
        block(2, "冰凤", "居于藏娇阁", dream="62分钟", heart="535分钟", divination="可施展",
              voyage_line="远航状态: 均衡航线进行中，剩余约 62 分钟。"),
    )
    blocks = voyage.split_companion_panel_blocks(text)
    assert [(b["name"], b["attending"]) for b in blocks] == [("董萱儿", True), ("冰凤", False)], blocks
    assert voyage.attending_companion_panel_name(text) == "董萱儿"
    reply = {"text": text, "created_at": NOW}
    assert voyage.build_companion_voyage_state_from_reply(reply)["status"] == "idle"  # 在海上的是冰凤，不是随行这位
    assert ex._resolve_companion_panel_cooldown_target(reply, "heart_tribulation") == 0.0
    assert ex._resolve_companion_panel_cooldown_target(reply, "divination_chain") == NOW + 713 * 60  # 不是冰凤的「可施展」

    # 藏娇阁那位归航待结算：不能对着随行的人发 .远航归来
    waiting = panel(
        block(1, "绾绾", "随行中", dream="可施展", heart="可施展", divination="可施展"),
        block(2, "陈巧倩", "居于藏娇阁", dream="1分钟", heart="1分钟", divination="1分钟",
              voyage_line="远航状态: 均衡航线已归航，待结算（.远航归来）。"),
    )
    assert voyage.build_companion_voyage_state_from_reply({"text": waiting, "created_at": NOW})["status"] == "idle"

    # 随行的排在第二段也认状态，不认顺序
    swapped = panel(
        block(1, "冰凤", "居于藏娇阁", dream="1分钟", heart="1分钟", divination="1分钟"),
        block(2, "董萱儿", "随行中", dream="1分钟", heart="1分钟", divination="1分钟",
              voyage_line="远航状态: 均衡航线进行中，剩余约 30 分钟。"),
    )
    state = voyage.build_companion_voyage_state_from_reply({"text": swapped, "created_at": NOW})
    assert state["status"] == "voyaging" and state["target_ts"] == NOW + 1800, state

    # 只有一位（甲真人 19:26：南宫婉住在藏娇阁）照旧整块读
    single = block(1, "南宫婉·月影", "居于藏娇阁", dream="111分钟", heart="597分钟", divination="714分钟",
                   voyage_line="远航状态: 月殿寻痕航线进行中，剩余约 357 分钟。")
    assert voyage.attending_companion_panel_text(single) == single
    assert voyage.attending_companion_panel_name(single) == ""
    assert voyage.build_companion_voyage_state_from_reply({"text": single, "created_at": NOW})["status"] == "voyaging"
    assert voyage.resolve_companion_voyage_strategy("月殿寻痕", "莎儿") == "均衡"
    assert voyage.resolve_companion_voyage_strategy("月殿寻痕", "南宫婉·月影") == "月殿寻痕"
    assert voyage.resolve_companion_voyage_strategy("月殿寻痕", "") == "月殿寻痕"
    assert voyage.resolve_companion_voyage_strategy("冒险", "莎儿") == "冒险"


def check_roster():
    # 丁真人 20:21：新寻的绾绾随行，老的陈巧倩在藏娇阁、还在海上
    chen = {"name": "陈巧倩", "affection": 337, "voyage": sailing(NOW + 3600)}
    p4 = payload_for({"name": "绾绾", "affection": 0}, [chen])
    assert [(c["name"], c["attending"]) for c in roster.list_companions(p4)] == [("绾绾", True), ("陈巧倩", False)]
    assert roster.plan_companion_rotation(p4, now=NOW, strategy="均衡") == ("", roster.voyage_end_ts(chen))  # 她归航时叫醒
    assert roster.plan_companion_rotation(p4, now=NOW + 3601, strategy="均衡") == ("陈巧倩", 0.0)  # 归航待结算

    # 甲真人：南宫婉随行，新寻的莎儿情缘 0，均衡航线要 70 —— 不换；送够灵石之后才换
    p2 = payload_for({"name": "南宫婉·月影", "affection": 674, "voyage": sailing(NOW + 3600, "月殿寻痕")}, [{"name": "莎儿"}])
    assert roster.plan_companion_rotation(p2, now=NOW, strategy="月殿寻痕") == ("", 0.0)
    p2_gifted = payload_for(p2["companion"], [{"name": "莎儿", "affection": 70}])
    assert roster.plan_companion_rotation(p2_gifted, now=NOW, strategy="月殿寻痕") == ("莎儿", 0.0)

    # 乙真人：只有凌玉灵，住在藏娇阁，companion 和 residence 是同一个人
    ling = {"name": "凌玉灵", "affection": 7}
    p3 = {"companion": ling, "companion_status": "居于藏娇阁", "dongfu": {"companion_residence": json.dumps([ling])}}
    assert [(c["name"], c["attending"]) for c in roster.list_companions(p3)] == [("凌玉灵", False)]
    assert roster.plan_companion_rotation(p3, now=NOW, strategy="均衡") == ("", 0.0)
    # 09-20 乙真人：莎儿随行情缘 14、凌玉灵在藏娇阁情缘 7，稳妥航线也要 30 —— 谁都出不了航，不能来回召回
    assert not roster.can_start_voyage({"name": "莎儿", "affection": 14}, "稳妥")
    p3_two = payload_for({"name": "莎儿", "affection": 14}, [ling])
    assert roster.plan_companion_rotation(p3_two, now=NOW, strategy="稳妥") == ("", 0.0)
    assert roster.plan_companion_rotation(payload_for({"name": "莎儿", "affection": 14}, [{"name": "凌玉灵", "affection": 30}]), now=NOW, strategy="稳妥") == ("凌玉灵", 0.0)
    # 09-19 之前的老数据：residence 是单个对象、dongfu 整个是 JSON 字符串
    old = {"companion": ling, "dongfu": json.dumps({"companion_residence": json.dumps(ling)})}
    assert [(c["name"], c["attending"]) for c in roster.list_companions(old)] == [("凌玉灵", False)]
    assert roster.list_companions({}) == [] and roster.list_companions({"companion": None, "dongfu": None}) == []


# ---- 调度器整轮：真库、真调度循环，只把天机阁刷新换成手里的 payload ----

def queued(storage, profile_id):
    with storage.connect() as conn:
        return [r[0] for r in conn.execute("select text from outgoing_commands where profile_id=? order by id", (profile_id,))]


def confirm_all(storage):
    with storage.connect() as conn:
        conn.execute("update outgoing_commands set status='confirmed', updated_at=?", (time.time() - 120,))


def say(storage, profile_id, message_id, text, *, reply_to=None, bot=False):
    storage.upsert_bound_message(profile_id, CHAT, None, message_id, reply_to, None, "", "incoming" if bot else "outgoing", bot, text)


def voyage_task(storage, profile_id):
    return next(t for t in storage.list_active_companion_auto_tasks(profile_id) if t["feature_key"] == ex.COMPANION_VOYAGE_FEATURE_KEY)


async def run_round(storage, profile_id, payload):
    storage.update_companion_auto_task(voyage_task(storage, profile_id)["id"], next_run_at=0)
    storage.upsert_external_account(profile_id, ASC_EXTERNAL_PROVIDER, "", "", "connected", "", payload, "")
    client = SimpleNamespace(_tg_game_profile_id=profile_id, _tg_game_storage=storage)
    with patch.object(ex, "_refresh_companion_payload", return_value=payload):
        await ex._run_companion_auto_scheduler(client, storage, run_once=True, include_tianxing=False)


async def check_scheduler():
    cooling = dict(dream="400分钟", heart="500分钟", divination="600分钟")
    recent = {k: iso(NOW - 60) for k in ("last_dream_map_seek_time", "last_divination_chain_time", "last_companion_heart_tribulation_time")}
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "t.db")
        storage.init_schema()
        pid = storage.create_profile("丁真人").id
        storage.create_chat_binding(pid, CHAT, bot_username="fanrenxiuxian_bot")
        storage.upsert_companion_auto_task(profile_id=pid, chat_id=CHAT, feature_key=ex.COMPANION_VOYAGE_FEATURE_KEY, enabled=True, strategy="均衡", bot_username="fanrenxiuxian_bot")

        # 1) 丁真人今晚：绾绾随行（情缘 0 出不了航），陈巧倩在藏娇阁归航待结算
        wan = {"name": "绾绾", "affection": 0, **recent}
        chen = {"name": "陈巧倩", "affection": 337, "voyage": sailing(NOW - 60), **recent}
        say(storage, pid, 100, ".我的侍妾")
        say(storage, pid, 101, panel(
            block(1, "绾绾", "随行中", **cooling),
            block(2, "陈巧倩", "居于藏娇阁", **cooling, voyage_line="远航状态: 均衡航线已归航，待结算（.远航归来）。"),
        ), reply_to=100, bot=True)
        await run_round(storage, pid, payload_for(wan, [chen]))
        assert queued(storage, pid) == [".召回侍妾 陈巧倩"], queued(storage, pid)  # 不对着绾绾发 .远航归来 / .侍妾远航

        # 2) 召回之前的面板不再算数：先重查面板，再结算、再起航
        confirm_all(storage)
        swapped = payload_for(chen, [wan])
        await run_round(storage, pid, swapped)
        assert queued(storage, pid)[1:] == [".我的侍妾"], queued(storage, pid)
        confirm_all(storage)
        say(storage, pid, 102, ".我的侍妾")
        say(storage, pid, 103, panel(
            block(1, "陈巧倩", "随行中", **cooling, voyage_line="远航状态: 均衡航线已归航，待结算（.远航归来）。"),
            block(2, "绾绾", "居于藏娇阁", **cooling),
        ), reply_to=102, bot=True)
        await run_round(storage, pid, swapped)
        assert queued(storage, pid)[2:] == [".远航归来"], queued(storage, pid)
        assert ex._load_companion_recall_state(storage, pid).get("settled") is True

        # 3) 陈巧倩出航后：绾绾情缘不够，不换人，睡到陈巧倩归航
        confirm_all(storage)
        say(storage, pid, 104, ".我的侍妾")
        say(storage, pid, 105, panel(
            block(1, "陈巧倩", "随行中", **cooling, voyage_line="远航状态: 均衡航线进行中，剩余约 480 分钟。"),
            block(2, "绾绾", "居于藏娇阁", **cooling),
        ), reply_to=104, bot=True)
        away = payload_for({**chen, "voyage": sailing(NOW + 8 * 3600)}, [wan])
        await run_round(storage, pid, away)
        assert queued(storage, pid)[3:] == [], queued(storage, pid)
        assert voyage_task(storage, pid)["next_run_at"] > NOW + 7.9 * 3600

        # 4) 给绾绾送过灵石（情缘 70）：陈巧倩在途就换她出来；十分钟内不再换第二次
        with storage.connect() as conn:  # 上一次召回算它是一小时前的事
            conn.execute("update app_runtime_state set value=? where key=?", (json.dumps({"name": "陈巧倩", "at": NOW - 3600, "settled": True}), f"companion_recall:{pid}"))
        gifted = payload_for(away["companion"], [{**wan, "affection": 70}])
        await run_round(storage, pid, gifted)
        assert queued(storage, pid)[3:] == [".召回侍妾 绾绾"], queued(storage, pid)
        confirm_all(storage)
        say(storage, pid, 106, ".我的侍妾")
        say(storage, pid, 107, panel(  # 游戏没换成：随行的还是陈巧倩
            block(1, "陈巧倩", "随行中", **cooling, voyage_line="远航状态: 均衡航线进行中，剩余约 470 分钟。"),
            block(2, "绾绾", "居于藏娇阁", **cooling),
        ), reply_to=106, bot=True)
        await run_round(storage, pid, gifted)
        assert queued(storage, pid)[4:] == [], queued(storage, pid)  # 限频：不连着召回

        # 5) 换人之前的远航回包说的是上一位：入梦/代卜/心劫的远航门不能再拿它当门
        latest = ex._get_latest_companion_voyage_reply(storage, profile_id=pid, chat_id=CHAT, thread_id=None)
        assert latest and "陈巧倩" in latest["text"], latest
        ex._save_companion_recall_state(storage, pid, {"name": "绾绾", "at": time.time() + 1})
        assert ex._get_latest_companion_voyage_reply(storage, profile_id=pid, chat_id=CHAT, thread_id=None) is None
    print("two companions: ok")


async def check_cooldown_rotation():
    """不送灵石：藏娇阁那位出不了航，但入梦/代卜/心劫好了也要换她出来做（心劫 +7 情缘，攒到 70 才进远航轮换）。"""
    wan_away = {"name": "南宫婉·月影", "affection": 674, "voyage": sailing(NOW + 5 * 3600, "月殿寻痕")}
    away_block = dict(dream="100分钟", heart="500分钟", divination="600分钟", voyage_line="远航状态: 月殿寻痕航线进行中，剩余约 300 分钟。")

    async def round_with(resident_block, *, recall_state=None, resident_affection=0, features=("heart",), sent_commands=()):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "t.db")
            storage.init_schema()
            pid = storage.create_profile("甲真人").id
            storage.create_chat_binding(pid, CHAT, bot_username="fanrenxiuxian_bot")
            storage.upsert_companion_auto_task(profile_id=pid, chat_id=CHAT, feature_key=ex.COMPANION_VOYAGE_FEATURE_KEY, enabled=True, strategy="月殿寻痕", bot_username="fanrenxiuxian_bot")
            for feature_key in features:
                if feature_key == "heart":
                    storage.upsert_companion_heart_tribulation_task(profile_id=pid, chat_id=CHAT, enabled=True, bot_username="fanrenxiuxian_bot", next_run_at=NOW + 9 * 3600)
                else:  # 单独的入梦/代卜任务排到很久以后，这一轮只看远航任务怎么决定
                    storage.upsert_companion_auto_task(profile_id=pid, chat_id=CHAT, feature_key=feature_key, enabled=True, bot_username="fanrenxiuxian_bot", next_run_at=NOW + 9 * 3600)
            if recall_state:
                ex._save_companion_recall_state(storage, pid, recall_state)
            payload = payload_for(wan_away, [{"name": "莎儿", "affection": resident_affection}])
            storage.upsert_external_account(pid, ASC_EXTERNAL_PROVIDER, "", "", "connected", "", payload, "")  # 预置指令要过侍妾作用域守卫
            for text, age, status in sent_commands:  # 「刚发过的指令」：(文本, 几秒前, 状态)
                storage.enqueue_outgoing_command(profile_id=pid, chat_id=CHAT, text=text, bot_username="fanrenxiuxian_bot")
                with storage.connect() as conn:
                    conn.execute("update outgoing_commands set status=?, created_at=?, updated_at=? where text=?", (status, time.time() - age, time.time() - age, text))
            say(storage, pid, 200, ".我的侍妾")
            say(storage, pid, 201, panel(block(1, "南宫婉·月影", "随行中", kind="红尘道侣", **away_block), resident_block), reply_to=200, bot=True)
            await run_round(storage, pid, payload)
            sent = queued(storage, pid)
            for text, _, _ in sent_commands:  # 预置的那几条不是这一轮发的，扣掉
                sent.remove(text)
            return sent, float(voyage_task(storage, pid)["next_run_at"])

    ready = block(2, "莎儿", "居于藏娇阁", kind="红尘道侣", dream="200分钟", heart="可施展", divination="300分钟")
    sent, _ = await round_with(ready)
    assert sent == [".召回侍妾 莎儿"], sent  # 心劫好了：趁南宫婉在海上换她出来

    cooling = block(2, "莎儿", "居于藏娇阁", kind="红尘道侣", dream="120分钟", heart="200分钟", divination="可施展")
    sent, next_run_at = await round_with(cooling)  # 代卜没开自动，不算；心劫 200 分钟后好
    assert sent == [] and abs(next_run_at - (time.time() + 200 * 60)) < 120, (sent, next_run_at - time.time())
    sent, next_run_at = await round_with(cooling, features=())  # 什么自动都没开：睡到南宫婉归航
    assert sent == [] and next_run_at > time.time() + 4.9 * 3600, (sent, next_run_at - time.time())

    # 道心侍妾情缘不到 300：代卜面板写「可施展」也不算活（会被拒），否则每趟远航都白换一次
    star = block(2, "莎儿", "居于藏娇阁", dream="300分钟", heart="400分钟", divination="可施展")
    sent, _ = await round_with(star, features=("divination_chain",))
    assert sent == [], sent
    # 红尘道侣不检定情缘，代卜好了就算活。同一轮里排在后面的代卜任务发现面板作废，会跟着补一条 .我的侍妾
    sent, _ = await round_with(block(2, "莎儿", "居于藏娇阁", kind="红尘道侣", dream="300分钟", heart="400分钟", divination="可施展"), features=("divination_chain",))
    assert sent[0] == ".召回侍妾 莎儿" and set(sent[1:]) <= {".我的侍妾"}, sent

    # 两小时内为冷却的活换过一次：不再换（修为不够之类，面板照写可施展、做却次次被拒）
    sent, next_run_at = await round_with(ready, recall_state={"name": "莎儿", "at": time.time() - 1800, "cooldown_at": time.time() - 1800, "settled": True})
    assert sent == [] and abs(next_run_at - (time.time() + 5400)) < 120, (sent, next_run_at - time.time())

    # 09-22 21:31 线上实况：南宫婉 8.5 分钟前被召回、随即出海；为她发的 .入梦寻图 没回包（sent）。
    # 老逻辑把这条算到莎儿头上 → 「没活」→ 远航任务一觉睡到归航，莎儿闲了 6 小时。
    seek_ready = block(2, "莎儿", "居于藏娇阁", kind="红尘道侣", dream="可施展", heart="500分钟", divination="600分钟")
    recall = {"name": "南宫婉·月影", "at": time.time() - 510, "cooldown_at": time.time() - 4 * 3600, "settled": True}
    for_attending = [(".入梦寻图", 420, "sent")]  # 召回之后发的 = 南宫婉的
    sent, next_run_at = await round_with(seek_ready, recall_state=recall, features=("dream_seek",), sent_commands=for_attending)
    # 召回限频还差 90 秒：这一轮不换，但只睡到限频到期，绝不是睡到归航
    assert set(sent) <= {".我的侍妾"} and 60 < next_run_at - time.time() < 200, (sent, next_run_at - time.time())
    sent, _ = await round_with(seek_ready, recall_state={**recall, "at": time.time() - 900}, features=("dream_seek",), sent_commands=for_attending)
    assert sent[:1] == [".召回侍妾 莎儿"], sent  # 那条 .入梦寻图 是南宫婉的；莎儿的入梦好了就换她出来

    # 09-23 01:07 线上实况：莎儿那段写「入梦寻图冷却: 0分钟」「天机代卜冷却: 117分钟」。
    # 老逻辑把「0分钟」当读不到、整项丢掉，只剩代卜的 117 分钟 → 召回拖到 03:05。
    almost = block(2, "莎儿", "居于藏娇阁", kind="红尘道侣", dream="0分钟", heart="500分钟", divination="117分钟")
    sent, next_run_at = await round_with(almost, recall_state={**recall, "at": time.time() - 3600}, features=("dream_seek", "divination_chain"))
    assert set(sent) <= {".我的侍妾"} and 30 < next_run_at - time.time() < 120, (sent, next_run_at - time.time())

    # 换人之前发的才是她自己的：20 分钟前莎儿做过入梦、面板还没刷 → 不换，但只等到 30 分钟窗口到期再看
    hers = [(".入梦寻图", 1200, "confirmed")]
    sent, next_run_at = await round_with(seek_ready, recall_state={**recall, "at": time.time() - 900}, features=("dream_seek",), sent_commands=hers)
    assert set(sent) <= {".我的侍妾"} and 500 < next_run_at - time.time() < 700, (sent, next_run_at - time.time())


async def check_requirement_backoff():
    """星宫号情缘不到 300：.天机代卜 被拒之后，面板还写着「可施展」。入梦/代卜这支每 5 秒整个重算一遍，
    拒绝要是拦在查面板之后，被拒的 6 小时里每 2 分钟白发一条 .我的侍妾。"""
    rejection = "你与侍妾情缘未至，至少需 300 情缘方可代卜天机。"

    async def round_with(mode, kind="红尘道侣"):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "t.db")
            storage.init_schema()
            pid = storage.create_profile("丁真人").id
            storage.create_chat_binding(pid, CHAT, bot_username="fanrenxiuxian_bot")
            storage.upsert_companion_auto_task(
                profile_id=pid, chat_id=CHAT, feature_key="divination_chain", enabled=True, bot_username="fanrenxiuxian_bot",
                last_run_at=time.time() - 60 if mode == "just_sent" else 0,
            )
            say(storage, pid, 300, ".我的侍妾")
            say(storage, pid, 301, block(1, "绾绾", "随行中", kind=kind, dream="100分钟", heart="100分钟", divination="可施展") + FOOTER, reply_to=300, bot=True)
            with storage.connect() as conn:  # 面板是 10 分钟前的：过了 120 秒保鲜期，要判冷却就得重查
                conn.execute("update bound_messages set created_at=?, updated_at=?", (time.time() - 600, time.time() - 600))
            if mode == "rejected":
                say(storage, pid, 302, ".天机代卜")
                say(storage, pid, 303, rejection, reply_to=302, bot=True)
            payload = {"companion": {"name": "绾绾", "affection": 0}, "companion_status": "随行中", "dongfu": {}}
            storage.upsert_external_account(pid, ASC_EXTERNAL_PROVIDER, "", "", "connected", "", payload, "")
            if mode == "unanswered":  # 22:00 线上实况：发出去了，游戏压根不回
                storage.enqueue_outgoing_command(profile_id=pid, chat_id=CHAT, text=".天机代卜", bot_username="fanrenxiuxian_bot")
                with storage.connect() as conn:
                    conn.execute("update outgoing_commands set status='needs_manual_confirm', updated_at=?", (time.time() - 300,))
            client = SimpleNamespace(_tg_game_profile_id=pid, _tg_game_storage=storage)
            for _ in range(2):  # 两轮：确认不会一轮一轮往外发
                await ex._run_companion_auto_scheduler(client, storage, run_once=True, include_tianxing=False)
            task = next(t for t in storage.list_active_companion_auto_tasks(pid) if t["feature_key"] == "divination_chain")
            return [text for text in queued(storage, pid) if text != ".天机代卜"], task

    sent, task = await round_with("rejected", kind="道心侍妾")
    assert sent == [] and task["last_error"] == rejection and task["next_run_at"] > time.time() + 5.9 * 3600, (sent, task["last_error"])
    sent, task = await round_with("unanswered")  # 游戏偶尔不回：一小时后再试，期间不查面板
    assert sent == [] and "没有回包" in task["last_error"] and 3000 < task["next_run_at"] - time.time() < 3600, (sent, task["last_error"])
    sent, _ = await round_with("just_sent")  # 刚发过：半小时宽限期内不为判冷却去查面板
    assert sent == [], sent
    sent, task = await round_with("none", kind="道心侍妾")  # 星宫号情缘 0：必被拒的代卜干脆不发，也不查面板
    assert sent == [] and "300" in task["last_error"], (sent, task["last_error"])
    sent, _ = await round_with("none")  # 对照：红尘道侣不检定情缘，没被拒、没发过，就照常去查面板
    assert sent == [".我的侍妾"], sent


def check_preflight_waits_for_reply():
    """起航预检：前置指令发出去还没回包就等回包，别当它做过了直接起航
    （09-22 21:24 的 .入梦寻图 没回包，21:29 照样起航，随后被 6 小时远航锁死）。"""

    def run(status, age):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "t.db")
            storage.init_schema()
            pid = storage.create_profile("甲真人").id
            storage.create_chat_binding(pid, CHAT, bot_username="fanrenxiuxian_bot")
            payload = payload_for({"name": "南宫婉·月影", "affection": 674, "last_divination_chain_time": iso(NOW - 60)}, [])
            storage.upsert_external_account(pid, ASC_EXTERNAL_PROVIDER, "", "", "connected", "", payload, "")
            say(storage, pid, 400, ".我的侍妾")
            say(storage, pid, 401, block(1, "南宫婉·月影", "随行中", kind="红尘道侣", dream="可施展", heart="500分钟", divination="600分钟") + FOOTER, reply_to=400, bot=True)
            storage.enqueue_outgoing_command(profile_id=pid, chat_id=CHAT, text=".入梦寻图", bot_username="fanrenxiuxian_bot")
            with storage.connect() as conn:
                conn.execute("update outgoing_commands set status=?, created_at=?, updated_at=?", (status, time.time() - age, time.time() - age))
            return ex._run_companion_voyage_preflight(
                storage, payload=payload, profile_id=pid, chat_id=CHAT, thread_id=None,
                chat_type="group", bot_username="fanrenxiuxian_bot", now=time.time(),
            )

    ok, note, wake = run("sent", 120)
    assert ok is False and "等待回包" in note and 0 < wake - time.time() <= 61, (ok, note)
    _, note, _ = run("confirmed", 120)  # 真回了包：算做过，预检往下走
    assert "等待回包" not in note, note
    _, note, _ = run("sent", 3600)  # sent 卡了一小时还没被确认扫描器改状态：放行，别把远航永远卡死
    assert "等待回包" not in note, note


def check_zero_minute_cooldown():
    """面板写「0分钟」= 不到一分钟就好，不是读不到（09-23 01:07 大号莎儿的入梦因此晚召回 2 小时）。"""
    now = time.time()

    def reply(dream):
        return {"text": block(1, "莎儿", "随行中", kind="红尘道侣", dream=dream, heart="可施展", divination="117分钟") + FOOTER, "created_at": now}

    target = ex._resolve_companion_panel_cooldown_target(reply("0分钟"), "dream_seek")
    assert target is not None and abs(target - (now + 60)) < 1, target
    assert ex._resolve_companion_panel_cooldown_target(reply("0分钟"), "heart_tribulation") == 0.0
    assert abs(ex._resolve_companion_panel_cooldown_target(reply("0分钟"), "divination_chain") - (now + 117 * 60)) < 1
    assert ex._resolve_companion_panel_cooldown_target(reply("冷却中"), "dream_seek") is None  # 没数字的才是真读不到


async def check_paired_commands_staggered():
    """代卜和入梦同一轮一起到点：只发一条，等它回包再发另一条
    （09-18~09-23 实测成对后发的无回包 67~75%，单发 0~2%，无回包那项要退避 1 小时）。"""
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "t.db")
        storage.init_schema()
        pid = storage.create_profile("甲真人").id
        storage.create_chat_binding(pid, CHAT, bot_username="fanrenxiuxian_bot")
        for feature_key in ("divination_chain", "dream_seek"):
            storage.upsert_companion_auto_task(profile_id=pid, chat_id=CHAT, feature_key=feature_key, enabled=True, bot_username="fanrenxiuxian_bot")
        payload = {"companion": {"name": "莎儿", "affection": 42}, "companion_status": "随行中", "dongfu": {}}
        storage.upsert_external_account(pid, ASC_EXTERNAL_PROVIDER, "", "", "connected", "", payload, "")
        say(storage, pid, 500, ".我的侍妾")
        say(storage, pid, 501, block(1, "莎儿", "随行中", kind="红尘道侣", dream="可施展", heart="400分钟", divination="可施展") + FOOTER, reply_to=500, bot=True)
        client = SimpleNamespace(_tg_game_profile_id=pid, _tg_game_storage=storage)
        pair = (".天机代卜", ".入梦寻图")

        def sent_pair():
            return [text for text in queued(storage, pid) if text in pair]

        await ex._run_companion_auto_scheduler(client, storage, run_once=True, include_tianxing=False)
        first = sent_pair()
        assert len(first) == 1, queued(storage, pid)  # 同一轮只发一条
        with storage.connect() as conn:  # 发出去了，在等游戏回包
            conn.execute("update outgoing_commands set status='awaiting_confirm', updated_at=?", (time.time(),))
        await ex._run_companion_auto_scheduler(client, storage, run_once=True, include_tianxing=False)
        assert sent_pair() == first, queued(storage, pid)  # 还没回包：另一条继续等
        confirm_all(storage)  # 回包到了
        await ex._run_companion_auto_scheduler(client, storage, run_once=True, include_tianxing=False)
        assert sorted(sent_pair()) == sorted(pair), queued(storage, pid)  # 下一轮发另一条

        # 另一条卡在「等回包」超过 5 分钟（游戏压根没回）：不能把这一条永远堵住
        with storage.connect() as conn:
            conn.execute("delete from outgoing_commands")
            conn.execute("update companion_auto_tasks set last_run_at=0, next_run_at=0")
        storage.enqueue_outgoing_command(profile_id=pid, chat_id=CHAT, text=".天机代卜", bot_username="fanrenxiuxian_bot")
        with storage.connect() as conn:
            conn.execute("update outgoing_commands set status='awaiting_confirm', created_at=?, updated_at=?", (time.time() - 400, time.time() - 400))
        assert ex._companion_sibling_command_in_flight(storage, profile_id=pid, chat_id=CHAT, thread_id=None, feature_key="dream_seek", now=time.time()) is False


def check_preflight_waits_for_imminent():
    """起航预检：前置任务不到一分钟就好，等它做完再走，别为一分钟搭上 6 小时远航。"""
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "t.db")
        storage.init_schema()
        pid = storage.create_profile("甲真人").id
        storage.create_chat_binding(pid, CHAT, bot_username="fanrenxiuxian_bot")
        # 天机阁：入梦 8 小时冷却还差 30 秒；代卜刚做过
        payload = payload_for({
            "name": "南宫婉·月影", "affection": 674,
            "last_dream_map_seek_time": iso(NOW - 8 * 3600 + 30),
            "last_divination_chain_time": iso(NOW - 60),
        }, [])
        storage.upsert_external_account(pid, ASC_EXTERNAL_PROVIDER, "", "", "connected", "", payload, "")
        say(storage, pid, 600, ".我的侍妾")
        say(storage, pid, 601, block(1, "南宫婉·月影", "随行中", kind="红尘道侣", dream="0分钟", heart="500分钟", divination="700分钟") + FOOTER, reply_to=600, bot=True)
        ok, note, wake = ex._run_companion_voyage_preflight(
            storage, payload=payload, profile_id=pid, chat_id=CHAT, thread_id=None,
            chat_type="group", bot_username="fanrenxiuxian_bot", now=time.time(),
        )
        assert ok is False and "即将就绪" in note and 0 < wake - time.time() < 60, (ok, note, wake - time.time())


async def check_chart_puzzle():
    """随行那位残图四种残纹齐了就 .拼图（09-24 实测远航途中也能拼、只认随行那位）。
    拼成了半小时内、「仍缺」/没回包 6 小时内不重发；和入梦/代卜错开，免得被游戏吞。"""
    full = {"cangkun_chart_mulan": 1, "cangkun_chart_gate": 3, "cangkun_chart_jade": 1, "cangkun_chart_taimiao": 1}
    short = {**full, "cangkun_chart_jade": 0}  # 甲真人 09-24：苍坤 3/4 缺玉匣
    xutian = {"xutian_chart_north": 1, "xutian_chart_south": 2, "xutian_chart_east": 1, "xutian_chart_west": 1}
    wan = {"name": "南宫婉·月影", "affection": 674}
    assert roster.attending_has_complete_chart(payload_for({**wan, "cangkun_fragment_bag": full}, []))
    assert not roster.attending_has_complete_chart(payload_for({**wan, "cangkun_fragment_bag": short}, []))
    assert roster.attending_has_complete_chart(payload_for({**wan, "xutian_fragment_bag": json.dumps(xutian)}, []))
    assert not roster.attending_has_complete_chart(payload_for({"name": "银月"}, []))  # 新侍妾整个碎片袋都没有
    assert not roster.attending_has_complete_chart(payload_for(wan, [{"name": "莎儿", "cangkun_fragment_bag": full}]))  # 藏娇阁那位齐了不算
    assert not roster.attending_has_complete_chart(
        {"companion": {**wan, "cangkun_fragment_bag": full}, "companion_status": "居于藏娇阁", "dongfu": {}}
    )

    success = "【苍坤残图·拼合成功】\n侍妾【南宫婉·月影】为你拼齐残图，锁定出苍坤上人洞府外层太妙神禁的薄弱方位。\n你获得：苍坤残图 x1、修为 +505。"
    missing = "虚天残图仍缺：东离残纹、西极残纹。\n苍坤残图仍缺：玉匣残纹。\n请继续使用 .入梦寻图。"

    async def round_with(bag, *, puzzle=None, others=()):
        """puzzle = (上一条 .拼图 几秒前发的, 回包文本或 None)；others = 之前发过的 (指令, 几秒前, 状态)。
        返回这两轮新发的指令。"""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "t.db")
            storage.init_schema()
            pid = storage.create_profile("甲真人").id
            storage.create_chat_binding(pid, CHAT, bot_username="fanrenxiuxian_bot")
            storage.upsert_companion_auto_task(profile_id=pid, chat_id=CHAT, feature_key="dream_seek", enabled=True, bot_username="fanrenxiuxian_bot")
            payload = payload_for({
                **wan, "cangkun_fragment_bag": bag, "last_dream_map_seek_time": iso(NOW - 3600),
                "voyage": sailing(NOW + 5 * 3600, "月殿寻痕"),
            }, [])
            storage.upsert_external_account(pid, ASC_EXTERNAL_PROVIDER, "", "", "connected", "", payload, "")
            say(storage, pid, 700, ".我的侍妾")  # 南宫婉在海上：入梦走不到，拼图照样发
            say(storage, pid, 701, block(
                1, "南宫婉·月影", "随行中", kind="红尘道侣", dream="420分钟", heart="500分钟", divination="600分钟",
                voyage_line="远航状态: 月殿寻痕航线进行中，剩余约 300 分钟。",
            ) + FOOTER, reply_to=700, bot=True)
            if puzzle:
                age, reply = puzzle
                storage.enqueue_outgoing_command(profile_id=pid, chat_id=CHAT, text=".拼图", bot_username="fanrenxiuxian_bot")
                with storage.connect() as conn:
                    conn.execute("update outgoing_commands set status='confirmed', created_at=?, updated_at=?", (time.time() - age, time.time() - age))
                if reply:
                    say(storage, pid, 702, ".拼图")
                    say(storage, pid, 703, reply, reply_to=702, bot=True)
            for text, age, status in others:
                storage.enqueue_outgoing_command(profile_id=pid, chat_id=CHAT, text=text, bot_username="fanrenxiuxian_bot")
                with storage.connect() as conn:
                    conn.execute(
                        "update outgoing_commands set status=?, created_at=?, updated_at=? where text=?",
                        (status, time.time() - age, time.time() - age, text),
                    )
            before = len(queued(storage, pid))
            client = SimpleNamespace(_tg_game_profile_id=pid, _tg_game_storage=storage)
            for _ in range(2):  # 两轮：第二轮那条还没回包，不能再发
                await ex._run_companion_auto_scheduler(client, storage, run_once=True, include_tianxing=False)
            return queued(storage, pid)[before:]

    assert await round_with(full) == [".拼图"]
    assert await round_with(short) == []
    assert await round_with(full, puzzle=(600, success)) == []  # 刚拼成，等天机阁刷新
    assert await round_with(full, puzzle=(1900, success)) == [".拼图"]  # 刷新后还齐（重复藏本够）：接着拼
    assert await round_with(full, puzzle=(3600, missing)) == []  # 天机阁说齐、游戏说缺：6 小时后再试
    assert await round_with(full, puzzle=(2 * 3600, None)) == []  # 没回包，同上
    assert await round_with(full, puzzle=(7 * 3600, missing)) == [".拼图"]
    assert await round_with(full, others=[(".入梦寻图", 5, "awaiting_confirm")]) == []  # 入梦还在等回包：先不发
    # 复审指出：刚换过人时天机阁缓存里的随行者可能还是上一位，拼到别人身上只换来「仍缺」和 6 小时退避
    assert await round_with(full, others=[(".召回侍妾 莎儿", 300, "confirmed")]) == []
    assert await round_with(full, others=[(".召回侍妾 莎儿", 25 * 60, "confirmed")]) == [".拼图"]
    # 起航刚发：挨着发游戏会吞一条
    assert await round_with(full, others=[(".侍妾远航 月殿寻痕", 30, "awaiting_confirm")]) == []
    assert await round_with(full, others=[(".侍妾远航 月殿寻痕", 600, "confirmed")]) == [".拼图"]

    # 反过来：.拼图 在等回包时，入梦/代卜、远航任务（召回/起航/归来）都要等它
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "t.db")
        storage.init_schema()
        pid = storage.create_profile("甲真人").id
        storage.create_chat_binding(pid, CHAT, bot_username="fanrenxiuxian_bot")
        storage.upsert_companion_auto_task(profile_id=pid, chat_id=CHAT, feature_key=ex.COMPANION_VOYAGE_FEATURE_KEY, enabled=True, strategy="月殿寻痕", bot_username="fanrenxiuxian_bot")
        storage.upsert_external_account(pid, ASC_EXTERNAL_PROVIDER, "", "", "connected", "", payload_for(wan, []), "")
        storage.enqueue_outgoing_command(profile_id=pid, chat_id=CHAT, text=".拼图", bot_username="fanrenxiuxian_bot")
        with storage.connect() as conn:
            conn.execute("update outgoing_commands set status='awaiting_confirm'")
        assert ex._companion_sibling_command_in_flight(storage, profile_id=pid, chat_id=CHAT, thread_id=None, feature_key="dream_seek", now=time.time())
        client = SimpleNamespace(_tg_game_profile_id=pid, _tg_game_storage=storage)
        await ex._run_companion_auto_scheduler(client, storage, run_once=True, include_tianxing=False)
        assert queued(storage, pid) == [".拼图"], queued(storage, pid)
        assert voyage_task(storage, pid)["last_error"] == "已有远航命令待发送，稍后复查。", voyage_task(storage, pid)["last_error"]


def main() -> None:
    logging.disable(logging.CRITICAL)
    check_panel_parsing()
    check_roster()
    asyncio.run(check_scheduler())
    asyncio.run(check_cooldown_rotation())
    asyncio.run(check_requirement_backoff())
    check_preflight_waits_for_reply()
    check_zero_minute_cooldown()
    asyncio.run(check_paired_commands_staggered())
    check_preflight_waits_for_imminent()
    asyncio.run(check_chart_puzzle())


if __name__ == "__main__":
    main()
