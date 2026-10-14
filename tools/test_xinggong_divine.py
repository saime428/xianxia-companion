"""星宫自动观星自检：结算前才发 .观星 → 认回包 → 事件在要抢的名单里才把 .改换星移 压到结算前最后一刻；
别人已经把这一档观成 Bad 或不要的事件就换后备档；群里的观星/结算长期留存。临时数据库，不连接游戏。

运行：uv run --with-requirements requirements.txt python -X utf8 -B tools/test_xinggong_divine.py
"""
import asyncio
import re
import runpy
import sys
import tempfile
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "app" / "src"
sys.path.insert(0, str(SRC))
from tg_game.features.xinggong import biz_xinggong_divine as divine
from tg_game.runtime import executors
from tg_game.storage import Storage

SETTLEMENT = datetime(2026, 10, 11, 4, tzinfo=timezone.utc).timestamp()  # 北京 12:00 的结算
NOW = SETTLEMENT - divine.READ_LEAD_SECONDS + 5  # 观星窗口刚开
OFF_WINDOW = SETTLEMENT + 5 * 3600  # 北京 17:00
AFTER_MIDNIGHT = SETTLEMENT + 12 * 3600 + 60  # 观完排到明天 00:01，再由发送窗口排到首选档
NEXT_LISTED = SETTLEMENT + 6 * 3600 - divine.READ_LEAD_SECONDS  # 名单「12,18」里 18 点那档的窗口
CHAT = -100000000001
COMMAND_MSG, PANEL_MSG = 501, 502
PANEL = (
    "【星盘显化】\n@alt_acct 闭目凝神，推演天机...星盘之上，天机已然显现！\n\n"
    "下一次天道演化将是: 【{fate}】\n当前天命所归: @{holder}\n\n"
    "你有 5分钟 时间，可以尝试回复此消息并使用 .改换星移 @新目标 来扭转此人天命！"
)
SHIFT = ".改换星移 @main_acct"
SHIFT_REPLY = "你开始消耗 82878 点修为，尝试扭转因果...\n（成功率: 50%）"  # 10-11 实跑的原文


def run(reply_text, *, strategy="main_acct", seen=None):
    """开着自动观星跑两轮调度：第一轮发 .观星，第二轮处理机器人回包。

    reply_text 为 None 只跑第一轮；seen 是别人此前公开观出来的星盘（时刻, 文本）。
    """
    with tempfile.TemporaryDirectory() as folder:
        storage = Storage(Path(folder) / "divine.db")
        storage.init_schema()
        pid = storage.create_profile("divine-test").id
        storage.update_profile_sect_info(pid, sect_name="星宫")
        storage.create_chat_binding(pid, CHAT, bot_username="fanrenxiuxian_bot")
        client = SimpleNamespace(_tg_game_profile_id=pid)
        storage.upsert_companion_auto_task(
            profile_id=pid, chat_id=CHAT, feature_key=divine.FEATURE_KEY, enabled=True,
            strategy=strategy, next_run_at=NOW, bot_username="fanrenxiuxian_bot",
        )
        if seen:
            storage.append_fate_evolution_log(
                chat_id=CHAT, message_id=400, kind="reading", text=seen[1], created_at=seen[0],
            )

        def tick():
            asyncio.run(executors._run_companion_auto_scheduler(
                client, storage, run_once=True, include_tianxing=False,
            ))

        tick()
        queued = storage.list_outgoing_commands(profile_id=pid)
        if reply_text is None:
            return {c["text"]: c for c in queued}, storage.get_companion_auto_task(pid, CHAT, divine.FEATURE_KEY)
        assert [c["text"] for c in queued] == [divine.DIVINE_COMMAND], queued
        with storage.connect() as conn:
            conn.execute("UPDATE outgoing_commands SET status='confirmed'")
        storage.upsert_bound_message(pid, CHAT, None, COMMAND_MSG, None, 0, "", "outgoing", False, divine.DIVINE_COMMAND)
        storage.upsert_bound_message(pid, CHAT, None, PANEL_MSG, COMMAND_MSG, 0, "", "incoming", True, reply_text)
        # 第一轮把下次检查排到几秒后；时间是冻住的，手动拨回来
        task_id = storage.get_companion_auto_task(pid, CHAT, divine.FEATURE_KEY)["id"]
        storage.update_companion_auto_task(task_id, next_run_at=NOW)
        tick()
        commands = {c["text"]: c for c in storage.list_outgoing_commands(profile_id=pid)}
        return commands, storage.get_companion_auto_task(pid, CHAT, divine.FEATURE_KEY)


def check():
    # 名单里的事件、天命在别人身上：回复星盘那条消息改到大号，但压到结算前才发；明天再观
    commands, task = run(PANEL.format(fate="Good - 星辰异象", holder="lucky_one"))
    shift = commands.get(SHIFT)
    assert shift and shift["reply_to_msg_id"] == PANEL_MSG, commands
    assert shift["scheduled_at"] == SETTLEMENT - divine.SHIFT_LEAD_SECONDS, shift
    assert task["next_run_at"] == AFTER_MIDNIGHT and not task["workflow_state"], task
    assert SHIFT in task["last_error"], task["last_error"]
    # 不改命的三种：Bad；默认名单里没有的好事（天雷竹也不抢）；已经在大号身上
    for fate, holder in (("Bad - 丹炉炸裂", "lucky_one"), ("Good - 天雷竹现世", "lucky_one"),
                         ("Good - 古修洞府现世", "lucky_one"), ("Good - 星辰异象", "main_acct")):
        commands, task = run(PANEL.format(fate=fate, holder=holder))
        assert list(commands) == [divine.DIVINE_COMMAND], (fate, commands)
        assert task["next_run_at"] == AFTER_MIDNIGHT, task
    # 名单是按任务配的：勾了天雷竹就抢天雷竹
    commands, _task = run(PANEL.format(fate="Good - 天雷竹现世", holder="lucky_one"),
                          strategy="main_acct|12,15,18,21|天雷竹现世")
    assert SHIFT in commands, commands
    # 没填目标：只观星留档，什么都不改
    commands, _task = run(PANEL.format(fate="Good - 星辰异象", holder="lucky_one"), strategy="|12,15,18,21")
    assert list(commands) == [divine.DIVINE_COMMAND], commands
    # 今天已经观过：不重发，排到明天
    commands, task = run("你今日已观星一次，天机不可多泄，请明日再来。")
    assert list(commands) == [divine.DIVINE_COMMAND], commands
    assert task["next_run_at"] == AFTER_MIDNIGHT and task["enabled"], task
    # 认不出的回包：6 小时后才重试，不会刷屏
    commands, task = run("道友正在闭关，无暇观星。")
    assert list(commands) == [divine.DIVINE_COMMAND], commands
    assert task["next_run_at"] == NOW + divine.RETRY_INTERVAL_SECONDS, task

    # 后备档：别人一小时前已经把 12 点这次观成 Bad，或观成我们不抢的事件 —— 不花次数，换到名单里的下一档
    for fate, why in (("Bad - 心魔大劫", "心魔大劫（Bad）"), ("Good - 天雷竹现世", "天雷竹现世（不在要抢的名单里）")):
        seen = (NOW - 3600, PANEL.format(fate=fate, holder="lucky_one"))
        commands, task = run(None, strategy="main_acct|12,18", seen=seen)
        assert not commands, (fate, commands)
        assert task["next_run_at"] == NEXT_LISTED, task
        assert why in task["last_error"] and "18:00" in task["last_error"], task["last_error"]
    # 别人观出的正是我们要的，或者那条是上一档（09 点）的：照常观
    for seen in ((NOW - 3600, PANEL.format(fate="Good - 五彩缤纷", holder="lucky_one")),
                 (SETTLEMENT - 3 * 3600 - 600, PANEL.format(fate="Bad - 心魔大劫", holder="x"))):
        commands, _task = run(None, strategy="main_acct|12,18", seen=seen)
        assert list(commands) == [divine.DIVINE_COMMAND], (seen[0], commands)
    # 12 点不在名单里：这个窗口不观，等名单里的 18 点
    commands, task = run(None, strategy="main_acct|18,21")
    assert not commands and task["next_run_at"] == NEXT_LISTED, task


def check_observer():
    """群里任何人的观星 / 结算都落进长期日志，三个号各看到一遍也只记一次；无关消息不记。"""
    with tempfile.TemporaryDirectory() as folder:
        storage = Storage(Path(folder) / "fate.db")
        storage.init_schema()

        def context(message_id, text, username="stargazer"):
            event = SimpleNamespace(id=message_id, raw_text=text, chat_id=CHAT, sender_id=77,
                                    sender=SimpleNamespace(username=username))
            return SimpleNamespace(event=event, text=text, chat_id=CHAT, sender_id=77, reply_to_msg_id=9,
                                   chat_binding=object(), profile=None, is_bot_sender=username.endswith("_bot"))

        panel = PANEL.format(fate="Bad - 丹炉炸裂", holder="unlucky_one")
        assert executors.observe_fate_evolution(context(1, panel, "hantianzun16_bot"), storage)
        assert not executors.observe_fate_evolution(context(1, panel, "hantianzun16_bot"), storage)
        assert executors.observe_fate_evolution(context(2, ".观星"), storage)
        # 发送者实体没加载、认不出是机器人时，以路由落库时判定的 is_bot 为准
        storage.upsert_bound_message(None, CHAT, None, 3, None, 77, "", "incoming", True, "【天机阁快报 - 丹炉炸裂】")
        assert executors.observe_fate_evolution(context(3, "【天机阁快报 - 丹炉炸裂】\n\n天机演化结果: ..."), storage)
        # 人转发的旧星盘不算数：否则谁都能用一条 Bad 骗我们跳过这一档
        assert not executors.observe_fate_evolution(context(6, PANEL.format(fate="Bad - 心魔大劫", holder="x")), storage)
        assert not executors.observe_fate_evolution(context(4, ".我的侍妾"), storage)
        unbound = context(5, ".观星")
        unbound.chat_binding = None
        assert not executors.observe_fate_evolution(unbound, storage)
        # 改命的真实回包（没有【改换星移】抬头）也要留档，才统计得出烧了多少修为
        assert executors.observe_fate_evolution(context(7, SHIFT_REPLY, "hantianzun23_bot"), storage)
        logs = storage.list_fate_evolution_logs(chat_id=CHAT)
        assert [(row["message_id"], row["kind"]) for row in logs] == [
            (1, "reading"), (2, "read_cmd"), (3, "settlement"), (7, "shift_reply")], logs
        assert logs[1]["sender_username"] == "stargazer" and logs[1]["reply_to_msg_id"] == 9, logs[1]
        assert divine.public_fate(storage, CHAT, NOW + 60) == ("Bad", "丹炉炸裂")
        assert divine.public_fate(storage, CHAT + 1, NOW + 60) == ("", "")


def check_shift_record():
    """改命记录：排队时记一笔 → 回包记修为 → 目标号同步天机阁时按结算前后的背包对账。"""
    import json

    from tg_game.services import external_sync

    def payload(**counts):
        materials = {key: value for key, value in counts.items() if key.startswith("mat_")}
        items = [{"item_id": key, "name": {"quest_item_ycs_b": "元磁山核·乙"}.get(key, key), "quantity": value}
                 for key, value in counts.items() if not key.startswith("mat_")]
        return {"sect_name": "散修", "cultivation_points": 5000, "inventory": {"materials": materials, "items": items}}

    with tempfile.TemporaryDirectory() as folder:
        storage = Storage(Path(folder) / "shift.db")
        storage.init_schema()
        shifter = storage.create_profile("shifter").id
        target = storage.create_profile("target").id
        storage.bind_profile_telegram_account(shifter, telegram_user_id="11", telegram_username="alt_acct")
        storage.bind_profile_telegram_account(target, telegram_user_id="22", telegram_username="Main_Acct")
        storage.upsert_external_account(target, "asc_aiopenai", "22", "main_acct", "connected", "session=offline", payload(mat_204=9), "")
        task = {"profile_id": shifter, "chat_id": CHAT, "strategy": "main_acct|12|星辰异象,五彩缤纷,天雷竹现世"}

        def attempt_for(fate, strategy=None):
            reply = {"text": PANEL.format(fate=fate, holder="lucky_one"), "created_at": NOW, "message_id": PANEL_MSG}
            assert divine.shift_followup(storage, {**task, "strategy": strategy or task["strategy"]}, reply)[0]
            return storage.list_fate_shift_attempts()[-1]

        def sync(at, *, took=1.0, **counts):
            """目标号的一次天机阁同步：at 时刻发起，took 秒后返回。"""
            divine.settle_shift_attempts(storage, target, payload(**counts), started_at=at, finished_at=at + took)
            return storage.list_fate_shift_attempts()[-1]

        def announce(at):
            """观察器记下这一档的结算快报（奖励就是这时候发的）。"""
            storage.append_fate_evolution_log(chat_id=CHAT, message_id=int(at), kind="settlement",
                                              text="【天机阁快报 - ...】", created_at=at)

        # 排队时：认出目标是自己的号，带上当时缓存的背包；待对账
        row = attempt_for("Good - 星辰异象")
        assert (row["outcome"], row["event"], row["target"], row["target_profile_id"]) == ("pending", "星辰异象", "main_acct", target), row
        assert row["settlement_at"] == SETTLEMENT and json.loads(row["before_json"])["mat_204"] == 9, row
        # 回包：只有发改命的那个号的记录会记上修为
        assert not divine.note_shift_reply(storage, target, CHAT, SHIFT_REPLY)
        assert divine.note_shift_reply(storage, shifter, CHAT, SHIFT_REPLY)
        assert storage.list_fate_shift_attempts()[-1]["cost"] == 82878
        # 结算前的同步只更新「之前」的快照
        assert json.loads(sync(SETTLEMENT - 10, mat_204=10)["before_json"])["mat_204"] == 10
        # 跨着结算的慢请求两头都不算：它带回的背包可能已经有奖励，不能当「之前」
        row = sync(SETTLEMENT - 3, took=8, mat_204=10, quest_item_ycs_b=1)
        assert row["outcome"] == "pending" and "quest_item_ycs_b" not in json.loads(row["before_json"]), row
        # 快报还没出（这次是整点后 95 秒才出）：不对账；快报出来之前发起的请求也不算
        assert sync(SETTLEMENT + 92, mat_204=10)["outcome"] == "pending"
        announce(SETTLEMENT + 95)
        assert sync(SETTLEMENT + 94, mat_204=10)["outcome"] == "pending"
        # 快报之后：奖励多出来了 = 拿到
        row = sync(SETTLEMENT + 200, mat_204=10, mat_001=71, quest_item_ycs_b=1)
        assert row["outcome"] == "got" and "元磁山核·乙 +1" in row["gained"], row
        # 没多出奖励 = 白烧；别的东西变多不算
        attempt_for("Good - 天雷竹现世")
        assert divine.note_shift_reply(storage, shifter, CHAT, SHIFT_REPLY)
        row = sync(SETTLEMENT + 200, mat_204=9, mat_001=500)
        assert row["outcome"] == "missed" and "mat_001 +" in row["gained"], row
        # 奖励编号不知道的事件：判不了，把多出来的东西原样记下
        attempt_for("Good - 五彩缤纷")
        assert divine.note_shift_reply(storage, shifter, CHAT, SHIFT_REPLY)
        row = sync(SETTLEMENT + 200, mat_204=9, mystery_soil=1)
        assert row["outcome"] == "unclear" and "mystery_soil +1" in row["gained"], row
        # 指令没发出去（没有回包）或被游戏拒了（回包里没有烧修为）：不是一次改命，不能算进「没拿到」
        attempt_for("Good - 星辰异象")
        row = sync(SETTLEMENT + 200, mat_204=9)
        assert row["outcome"] == "unknown" and row["gained"] == "没见到烧修为的回包：没有回包", row
        attempt_for("Good - 星辰异象")
        assert divine.note_shift_reply(storage, shifter, CHAT, "【改换星移】无法读取你回复的星盘消息。\n请确认……")
        row = sync(SETTLEMENT + 200, mat_204=9)
        assert row["outcome"] == "unknown" and row["gained"].endswith("【改换星移】无法读取你回复的星盘消息。"), row
        # 这次同步没带背包（或背包是空的）：不动，等下一次
        attempt_for("Good - 星辰异象")
        assert divine.note_shift_reply(storage, shifter, CHAT, SHIFT_REPLY)
        for empty in ({"sect_name": "散修"}, {"inventory": {"materials": {}, "items": []}}):
            divine.settle_shift_attempts(storage, target, empty, started_at=SETTLEMENT + 200, finished_at=SETTLEMENT + 201)
            assert storage.list_fate_shift_attempts()[-1]["outcome"] == "pending"
        # 真正的挂接点：目标号任何一次天机阁同步都会顺手对账
        with patch.object(external_sync, "fetch_cultivator_payload",
                          lambda *a, **k: (payload(mat_204=9, quest_item_ycs_b=2), "main_acct", "", "")), \
                patch("time.time", return_value=SETTLEMENT + 300):
            external_sync.sync_external_account(storage, target, cookie_text="session=offline")
        assert storage.list_fate_shift_attempts()[-1]["outcome"] == "got"
        # 目标不是自己的号：看不到背包，直接标成对不了账
        assert attempt_for("Good - 星辰异象", "stranger_x|12|星辰异象")["outcome"] == "unknown"
        # 观察器：本号 .改换星移 的回包才记修为（别的号看到同一条不记）
        row = attempt_for("Good - 星辰异象")
        storage.upsert_bound_message(shifter, CHAT, None, 601, PANEL_MSG, 11, "alt_acct", "outgoing", False, SHIFT)
        storage.upsert_bound_message(target, CHAT, None, 601, PANEL_MSG, 11, "alt_acct", "incoming", False, SHIFT)
        for pid in (target, shifter):
            event = SimpleNamespace(id=602, raw_text=SHIFT_REPLY, chat_id=CHAT, sender_id=77, sender=None)
            executors.observe_fate_evolution(SimpleNamespace(
                event=event, text=SHIFT_REPLY, chat_id=CHAT, sender_id=77, reply_to_msg_id=601,
                chat_binding=object(), profile=SimpleNamespace(id=pid), is_bot_sender=True), storage)
            assert storage.list_fate_shift_attempts()[-1]["cost"] == (82878 if pid == shifter else 0), pid


def check_template():
    """网页上的勾选项是写死在模板里的，必须和代码认的事件、默认勾选一一对应。"""
    html = (ROOT / "app/assets/templates/modules/sect.html").read_text(encoding="utf-8")
    options = re.search(r"\{% set divine_event_options = \[(.*?)\] %\}", html).group(1)
    assert dict(re.findall(r"\('([^']+)', '([^']+)'\)", options)) == divine.GOOD_EVENTS, options
    defaults = re.search(r"else \[('星辰异象'.*?)\] %\}", html).group(1)
    assert tuple(re.findall(r"'([^']+)'", defaults)) == divine.DEFAULT_SHIFT_EVENTS, defaults


async def check_web_toggle():
    """网页开关：目标、档位、事件名单写进 strategy 并过校验；等回包时关掉再开，不能带着旧的等待状态。"""
    import importlib

    import httpx
    from tg_game.config import get_settings

    with tempfile.TemporaryDirectory() as folder:
        storage = Storage(Path(folder) / "divine-web.db")
        storage.init_schema()
        pid = storage.create_profile("divine-web").id
        storage.update_profile_sect_info(pid, sect_name="星宫")
        storage.bind_profile_telegram_account(pid, telegram_user_id="123", telegram_username="alt_acct")
        # 页面按绑定的指令群去读任务，和表单提交的 chat_id 要是同一个
        storage.create_chat_binding(pid, CHAT, bot_username="fanrenxiuxian_bot")
        storage.upsert_external_account(pid, "asc_aiopenai", "123", "alt_acct", "connected", "session=offline", {"sect_name": "星宫"}, "")
        settings = get_settings()
        with patch.multiple(settings, database_path=storage.path, authorized_user_id="123", bound_chat_id=None), ExitStack() as mocks:
            web = importlib.import_module("tg_game.web.app")
            # 渲染页面时别去连天机阁
            for name in ("_sync_bootstrap_if_needed", "_sync_all_items_if_needed", "_sync_shop_items_if_needed",
                         "_sync_profile_from_cultivator", "sync_cultivation_session"):
                mocks.enter_context(patch.object(web, name, return_value=None))
            # 这两张表是运行时才建的，空库里没有
            mocks.enter_context(patch.object(Storage, "get_cultivation_session", return_value=None))
            mocks.enter_context(patch.object(Storage, "get_sect_session", return_value=None))
            token = storage.create_app_session(pid)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.create_app()), base_url="http://test") as client:
                client.cookies.set(web.APP_SESSION_COOKIE, token)

                async def toggle(target, slots="", events=()):
                    form = dict(chat_id=str(CHAT), feature_key=divine.FEATURE_KEY, bot_username="fanrenxiuxian_bot",
                                divine_target=target, divine_slots=slots, divine_events=list(events))
                    response = await client.post("/runtime/commands/companion-auto", data=form)
                    assert response.status_code == 303, response.text
                    return storage.get_companion_auto_task(pid, CHAT, divine.FEATURE_KEY)

                async def checked_boxes():
                    """真的把宗门页渲染出来，看观星卡片里勾着哪些事件。"""
                    page = await client.get("/modules/sect")
                    assert page.status_code == 200 and "自动观星" in page.text, page.status_code
                    return re.findall(r'name="divine_events" value="([^"]+)" checked', page.text)

                assert await checked_boxes() == list(divine.DEFAULT_SHIFT_EVENTS)  # 还没开过：默认勾这三样
                task = await toggle("@main_acct", "21, 9", ["天雷竹现世", "乱写的事件", "星辰异象"])
                assert task["enabled"] and task["strategy"] == "main_acct|9,21|星辰异象,天雷竹现世", task
                assert await checked_boxes() == ["星辰异象", "天雷竹现世"]
                storage.update_companion_auto_task(task["id"], workflow_state=divine.AWAIT_REPLY_STATE)
                assert not (await toggle(""))["enabled"]
                # 乱填的目标和档位、一个事件都没勾：目标清空，档位回到默认，事件就是空的（都不改）；旧的等待状态被清掉
                task = await toggle("main_acct; .赠送 灵石", "0,13")
                assert task["enabled"] and not task["workflow_state"], task
                assert divine.split_strategy(task) == ("", divine.DEFAULT_SLOT_HOURS, ()), task
                assert await checked_boxes() == [], "一个都没勾，页面不能自己勾回去"
                # 全勾上、档位全填、32 位用户名：一个字都不能被截掉
                assert not (await toggle(""))["enabled"]
                task = await toggle("a" * 32, ",".join(map(str, divine.SLOT_HOURS)), list(divine.GOOD_EVENTS))
                assert divine.split_strategy(task) == ("a" * 32, divine.SLOT_HOURS, tuple(divine.GOOD_EVENTS)), task


runpy.run_path(str(SRC / "tg_game/features/xinggong/biz_xinggong_divine.py"), run_name="__main__")
with patch("time.time", return_value=NOW):
    check()
    check_observer()
    check_shift_record()
# 不在结算前的窗口里：不发，推到下一个可用档（18:00）的窗口
with patch("time.time", return_value=OFF_WINDOW):
    commands, task = run(None)
    assert not commands, commands
    assert task["next_run_at"] == OFF_WINDOW + 3600 - divine.READ_LEAD_SECONDS, task
    assert task["last_error"].startswith("等 18:00 结算前"), task
check_template()
asyncio.run(check_web_toggle())
print("xinggong divine: ok")
