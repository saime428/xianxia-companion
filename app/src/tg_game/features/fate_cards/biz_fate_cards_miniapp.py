"""天机命脉（洞府「外府」fate_cards，09-25 起自动）：每天问天一次 → 三张命牌 → 选命择 → 做完任务验命领奖。

页面 /miniapp/xianxia-fate-cards 是公开的，接口全是 POST {token, initData, ...}（09-25 读页面 JS 所得）：
- start → {challengeDate, traceBalance, questions, choices, hasDrawn, record}
- draw {questionKey} → {record, reward.balance, alreadyDrawn}，启牌就给天机残痕 +1
- interpret → {record}：AI 解读，纯文案，页面失败了也照样往下走
- choose {choiceKey: accept 顺势承命=一次修为积累 / defy 逆势改命=一局噬金虫 / hide 藏锋避劫=等三分钟} → {record.quest}，选了不能改
- settle → {record, reward: {tianjiTrace, kunwuPass, balance}, alreadySettled}；没做完回 error=quest_incomplete
入口和噬金虫一样：洞府 start → details → external(action=fate_cards) 换出带 fate_ token 的地址，initData 沿用洞府那份。
"""

import asyncio
import re
import time
from urllib.parse import parse_qs, urljoin, urlsplit

from tg_game.features.estate import biz_estate_miniapp as estate_miniapp
from tg_game.features.estate.biz_estate_constants import (
    ESTATE_MINIAPP_ALLOWED_API_HOSTS,
    ESTATE_MINIAPP_DEFAULT_API_BASE_URL,
)

FATE_CARDS_ACTION = "fate_cards"
FATE_CARDS_WEB_PATH = "/miniapp/xianxia-fate-cards"
FATE_CARDS_API_PREFIX = "/api/miniapp/xianxia-fate-cards/"
FATE_CARDS_ENDPOINTS = ("start", "draw", "interpret", "choose", "settle")
FATE_CARDS_TOKEN_PATTERN = re.compile(r"^fate[_-][A-Za-z0-9_-]{4,160}$")
# 藏锋避劫要等的秒数在这以内就在本轮里等完再验；更久的（修为、噬金虫任务）交给下一轮
MAX_INLINE_WAIT_SECONDS = 300
WAITING_RECHECK_SECONDS = 1800


def extract_fate_cards_launch(data: object) -> dict:
    """外府目录里天机命脉那张卡换出了地址 → {"token": "fate_…"}；还没换出来就是 {}。"""
    root = data if isinstance(data, dict) else {}
    account = root.get("account") if isinstance(root.get("account"), dict) else {}
    external = account.get("externalApps") if isinstance(account.get("externalApps"), dict) else {}
    for group in external.get("groups") or []:
        for app in (group.get("apps") or []) if isinstance(group, dict) else []:
            try:
                parsed = urlsplit(str((app or {}).get("url") or "") if isinstance(app, dict) else "")
            except ValueError:
                continue
            if parsed.path.rstrip("/") != FATE_CARDS_WEB_PATH:
                continue
            if parsed.hostname and parsed.hostname.lower() not in ESTATE_MINIAPP_ALLOWED_API_HOSTS:
                continue
            token = (parse_qs(parsed.query).get("startapp") or [""])[0].strip()
            if FATE_CARDS_TOKEN_PATTERN.match(token):
                return {"token": token}
    return {}


def build_fate_cards_request(
    endpoint: str,
    *,
    token: str,
    init_data: str,
    payload: dict | None = None,
    api_base_url: str = ESTATE_MINIAPP_DEFAULT_API_BASE_URL,
) -> dict:
    if endpoint not in FATE_CARDS_ENDPOINTS:
        raise ValueError(f"unknown fate cards endpoint: {endpoint}")
    if not FATE_CARDS_TOKEN_PATTERN.match(str(token or "")):
        raise ValueError("fate cards token not allowed")
    base = urlsplit(str(api_base_url or ""))
    url = urljoin(f"{base.scheme}://{base.netloc}/", f"{FATE_CARDS_API_PREFIX.lstrip('/')}{endpoint}")
    if (urlsplit(url).hostname or "").lower() not in ESTATE_MINIAPP_ALLOWED_API_HOSTS:
        raise ValueError("fate cards api host not allowed")
    return {
        "method": "POST",
        "url": url,
        "payload": {"token": str(token), "initData": str(init_data or ""), **(payload or {})},
    }


def _number(value: object) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _scalars(value: object) -> dict:
    items = value.items() if isinstance(value, dict) else ()
    return {str(k): v for k, v in items if v is None or isinstance(v, (bool, int, float, str))}


def _summary(record: dict) -> dict:
    quest = record.get("quest") if isinstance(record.get("quest"), dict) else {}
    return {
        "challenge_date": record.get("challengeDate"),
        "question": record.get("questionKey"),
        "choice": record.get("choiceKey"),
        "cards": [
            f"{c.get('positionName', '')}·{c.get('title', '')}（{c.get('orientation', '')}）"
            for c in record.get("cards") or []
            if isinstance(c, dict)
        ],
        "quest": {**_scalars(quest), "reward": _scalars(quest.get("reward"))},
    }


def run_fate_cards_flow(
    *,
    token: str,
    init_data: str,
    transport,
    question_key: str = "cultivation",
    choice_key: str = "hide",
    sleeper=time.sleep,
) -> dict:
    """把今天的命脉走完：没启牌就启牌、没选就选、做完了就验命；接着上次（或手动）停下的地方往下走。

    status：settled 今天领过了 / waiting 任务没做完（next_check_seconds 后再来）/ expired / failed。
    """

    def call(endpoint: str, **payload) -> dict:
        return estate_miniapp.execute_estate_miniapp_request(
            build_fate_cards_request(endpoint, token=token, init_data=init_data, payload=payload),
            transport,
        )

    def result(status: str, record: dict, **extra) -> dict:
        return {"ok": status != "failed", "status": status, **_summary(record), **extra}

    started = call("start")
    if not started.get("ok"):
        return result("failed", {}, error=started.get("error") or "start 失败")
    data = started.get("data") or {}
    choices = [_scalars(c) for c in data.get("choices") or [] if isinstance(c, dict)]
    record = data.get("record") if data.get("hasDrawn") and isinstance(data.get("record"), dict) else None
    if record is None:
        questions = [str(q.get("key")) for q in data.get("questions") or [] if isinstance(q, dict)]
        if questions and question_key not in questions:
            question_key = questions[0]
        drawn = call("draw", questionKey=question_key)
        if not drawn.get("ok"):
            return result("failed", {}, error=drawn.get("error") or "启牌失败", choices=choices)
        record = (drawn.get("data") or {}).get("record") or {}
    if not record.get("choiceKey"):
        if choices and choice_key not in {c.get("key") for c in choices}:
            # 选了就不能改：认不出的命择宁可不选，也别替用户随便挑一个
            return result("failed", record, error=f"命择 {choice_key} 不在今日可选里", choices=choices)
        if not (record.get("aiReading") or {}).get("overview"):
            call("interpret")  # 纯文案，失败不影响选命择
        chosen = call("choose", choiceKey=choice_key)
        if not chosen.get("ok"):
            return result("failed", record, error=chosen.get("error") or "承命失败", choices=choices)
        record = (chosen.get("data") or {}).get("record") or record
    quest = record.get("quest") if isinstance(record.get("quest"), dict) else {}
    if quest.get("status") in {"settled", "expired"}:
        return result(str(quest["status"]), record, choices=choices)
    if not quest.get("canSettle"):
        remaining = max(0, _number(quest.get("target")) - _number(quest.get("progress")))
        if quest.get("metric") != "wait_seconds" or remaining > MAX_INLINE_WAIT_SECONDS:
            return result("waiting", record, choices=choices, next_check_seconds=WAITING_RECHECK_SECONDS)
        sleeper(remaining + 5)
    settled = call("settle")
    if not settled.get("ok"):
        if settled.get("error") == "quest_incomplete":
            return result("waiting", record, choices=choices, next_check_seconds=WAITING_RECHECK_SECONDS)
        return result("failed", record, error=settled.get("error") or "验命失败", choices=choices)
    settled_data = settled.get("data") or {}
    return result(
        "settled",
        settled_data.get("record") or record,
        choices=choices,
        reward=_scalars(settled_data.get("reward")),
        already_settled=bool(settled_data.get("alreadySettled")),
    )


async def resolve_fate_cards_launch(client: object, storage: object, *, transport=None, sleeper=time.sleep) -> dict:
    """洞府公共入口 → WebView initData → 外府换出天机命脉的 fate_ token。"""
    discovery = await estate_miniapp.resolve_estate_public_miniapp_launch(client, storage)
    if not discovery.get("ok"):
        return {"ok": False, "error": discovery.get("error") or "公共洞府入口未找到"}
    launch = discovery.get("launch") if isinstance(discovery.get("launch"), dict) else {}
    init_data = await estate_miniapp.request_estate_miniapp_init_data(
        client,
        token=launch.get("token"),
        webview_url=launch.get("webview_url"),
        bot_username=launch.get("bot_username"),
        launch_context=launch,
    )
    request = estate_miniapp.build_estate_miniapp_request("start", token=launch.get("token"), init_data=init_data)
    lookup = await asyncio.to_thread(
        estate_miniapp.execute_estate_external_app_lookup,
        request,
        transport or estate_miniapp._urllib_transport,
        extract_fate_cards_launch,
        action=FATE_CARDS_ACTION,
        sleeper=sleeper,
    )
    token = str((lookup.get("launch") or {}).get("token") or "")
    if not token:
        error = (lookup.get("result") or {}).get("error") or "外府目录没有返回天机命脉入口"
        return {"ok": False, "error": estate_miniapp.sanitize_estate_miniapp_secret_text(error)}
    return {"ok": True, "token": token, "init_data": init_data}
