"""Dwelling resource policies and one-action flows with explicit reconciliation."""
import asyncio
from copy import deepcopy
import json
import math
import time
import uuid

from tg_game.game_clock import game_day
from tg_game.services.external_sync import ASC_PROVIDER
from tg_game.services.runtime_drain import tracked_flow
from . import biz_estate_miniapp as api

DEFAULT_POLICY = {
    "observe_enabled": False, "durability_threshold": 30,
    "auto_repair": False, "repair_target": "all", "stone_budget": 0, "cultivation_budget": 0,
    "meditation_enabled": False, "meditation_verified": False, "lingqi_reserve": 0,
    "skip_full_sermon": True,
}
ACTIONS = {"refresh", "chest_status", "chest_open", "repair", "meditation", "sermon"}


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def policy(value):
    source = value if isinstance(value, dict) else {}
    result = dict(DEFAULT_POLICY)
    for key, default in result.items():
        if key not in source:
            continue
        if isinstance(default, bool):
            result[key] = source[key] is True
        elif isinstance(default, (int, float)):
            parsed = number(source[key])
            if parsed is None or parsed < 0:
                raise ValueError(f"invalid {key}")
            result[key] = parsed
        else:
            result[key] = str(source[key]).strip()[:100]
    if result["durability_threshold"] > 100:
        raise ValueError("耐久告警阈值应为0至100")
    return result


def queue(payload, action, *, owner_id="", interaction=None, source="manual"):
    if action not in ACTIONS:
        raise ValueError("unsupported dwelling action")
    if source not in {"manual", "automatic"}:
        raise ValueError("unsupported dwelling request source")
    if owner_id and not action.startswith("chest_"):
        raise ValueError("仅宝箱支持访客角色")
    if owner_id and (not str(owner_id).isdigit() or len(str(owner_id)) > 20):
        raise ValueError("拜访角色ID格式无效")
    safe_interaction = normalize_interaction(interaction) if action == "chest_open" else {}
    updated = deepcopy(payload)
    root = updated.setdefault("dongfu_resources", {})
    if (root.get("request") or {}).get("status") in {"queued", "running"}:
        return updated
    if root.get("needs_review") or (root.get("request") or {}).get("status") == "needs_review":
        root["needs_review"] = True
    if root.get("needs_review") and action not in {"refresh", "chest_status"}:
        raise ValueError("上一动作尚待核对，请先刷新资源状态")
    root["request"] = {"id": uuid.uuid4().hex, "status": "queued", "action": action,
                       "source": source,
                       "owner_id": str(owner_id or ""), "interaction": safe_interaction,
                       "queued_at": time.time(), "day": game_day()}
    return updated


def check_request_policy(request, settings):
    if request.get("source") != "automatic":
        return
    action = request.get("action")
    if not settings["observe_enabled"]:
        raise ValueError("定时观测已关闭，已停止自动资源动作")
    if action == "repair" and not settings["auto_repair"]:
        raise ValueError("自动修理已关闭")
    if action == "meditation" and not (settings["meditation_enabled"] and settings["meditation_verified"]):
        raise ValueError("静室自动结算已关闭或尚未核实入账")


def observations(data, previous=None, *, now=None):
    now = time.time() if now is None else now
    dwelling = data.get("dwelling") or {}
    account = data.get("account") or {}
    meditation = dwelling.get("meditation") or {}
    bag = account.get("bagTreasure") or {}
    sample = {"at": now, "last_update_ms": meditation.get("lastUpdateMs"),
              "pool": dwelling.get("lingqiPool"), "cultivation": meditation.get("currentCultivation")}
    history = list((previous or {}).get("samples") or [])[-23:]
    if meditation:
        history.append(sample)
    return {"updated_at": now, "meditation": {key: meditation[key] for key in (
                "canSettle", "consumableLingqi", "projectedLingqi", "projectedGain", "currentCultivation",
                "lastUpdateMs", "serverTimeMs", "productionRate", "maxConsumptionRate", "conversionRate") if key in meditation},
            "pool": dwelling.get("lingqiPool"), "samples": history,
            "treasures": [{key: item[key] for key in ("itemId", "name", "durability", "maxDurability", "active", "needsRepair") if key in item}
                          for item in bag.get("treasures") or [] if isinstance(item, dict)],
            "repair": deepcopy(bag.get("repair") or {}),
            "small_world": {"summary": (account.get("smallWorld") or {}).get("summary") or {},
                            "actions": (account.get("smallWorld") or {}).get("actions") or {}}}


def full_sermon_resources(summary):
    values = [number(summary.get(key)) for key in ("faith", "stability", "population", "populationCap")]
    return all(value is not None for value in values) and values[0] >= 100 and values[1] >= 100 and values[3] > 0 and values[2] >= values[3]


def repair_payload(snapshot, settings):
    quote = snapshot.get("repair") or {}
    target = settings["repair_target"]
    items = quote.get("items") or []
    if not items:
        raise ValueError("没有需修理的法宝或报价未返回")
    if target != "all":
        quote = next((item for item in items if item.get("itemId") == target), {})
    costs = [number(quote.get(key)) for key in ("stoneCost", "cultivationCost")]
    if any(cost is None or cost < 0 for cost in costs):
        raise ValueError("该目标没有服务端报价，未发起修理")
    if costs[0] > settings["stone_budget"] or costs[1] > settings["cultivation_budget"]:
        raise ValueError("最新修理报价超出预算")
    return {"target": target}


def normalize_interaction(interaction):
    value = interaction if isinstance(interaction, dict) else {}
    date = value.get("date")
    slot, position = value.get("slot"), value.get("position")
    if not isinstance(date, str) or len(date) != 10 or any(c not in "0123456789-" for c in date):
        raise ValueError("宝箱日期格式无效")
    if not isinstance(slot, int) or isinstance(slot, bool) or slot < 0:
        raise ValueError("无效宝箱藏点")
    if not isinstance(position, list) or len(position) != 3 or any(number(v) is None for v in position):
        raise ValueError("缺少当前客户端交互位置")
    if value.get("controlMode") != "companion":
        raise ValueError("须使用侍妾控制模式的交互请求")
    return {"date": date, "slot": slot, "position": [float(v) for v in position], "controlMode": "companion"}


def chest_payload(info, interaction):
    """Accept a current client interaction, never invent reachability from the seed."""
    if not info.get("enabled") or info.get("opened"):
        raise ValueError("宝箱未启用或今日已开")
    interaction = normalize_interaction(interaction)
    if info.get("date") != game_day() or interaction.get("date") != info.get("date"):
        raise ValueError("宝箱日期已变化，请重新靠近")
    slot = interaction.get("slot")
    spots = info.get("spots") or []
    position = interaction.get("position")
    if not isinstance(slot, int) or isinstance(slot, bool) or not 0 <= slot < len(spots):
        raise ValueError("无效宝箱藏点")
    if not isinstance(position, list) or len(position) != 3 or any(number(v) is None for v in position):
        raise ValueError("缺少当前客户端交互位置")
    x, z = spots[slot]
    if math.hypot(float(position[0]) - float(x), float(position[2]) - float(z)) > 1.4:
        raise ValueError("当前位置未靠近藏点")
    if interaction.get("controlMode") != "companion":
        raise ValueError("须使用侍妾控制模式的交互请求")
    # The service remains authoritative for reachability and the selected slot.
    return {key: interaction[key] for key in ("date", "slot", "position", "controlMode")}


def run_flow(*, token, init_data, transport, request, settings, previous=None, checkpoint=None):
    settings = policy(settings)
    action = request.get("action", "refresh")
    events = []

    def call(endpoint, payload=None):
        result = api.execute_estate_miniapp_request(api.build_estate_miniapp_request(
            endpoint, token=token, init_data=init_data, payload=payload), transport)
        events.append({"endpoint": endpoint, "ok": bool(result.get("ok")), "status_code": result.get("status_code"), "error": result.get("error", "")})
        return result

    def read():
        start = call("start")
        if not start.get("ok"):
            raise ValueError(start.get("error") or "读取洞府失败")
        data = deepcopy(start.get("data") or {})
        for endpoint, params in (("details", {}), ("section", {"section": "inventory"})):
            result = call(endpoint, params)
            if not result.get("ok"):
                raise ValueError(result.get("error") or "完整资料未返回")
            incoming = result.get("data") or {}
            for key in ("account", "dwelling"):
                data[key] = {**data.get(key, {}), **incoming.get(key, {})}
        return data

    result = {"ok": False, "status": "failed", "events": events, "action": action}
    submitted = False
    try:
        check_request_policy(request, settings)
        before_data = read()
        before = observations(before_data, previous)
        result["snapshot"] = before
        if action == "refresh":
            result.update(ok=True, status="synced")
            return result
        owner = str(request.get("owner_id") or "")
        host = {"ownerId": owner} if owner else {}
        if owner:
            visit = call("visits", {"action": "enter", **host})
            if not visit.get("ok"):
                raise ValueError(visit.get("error") or "拜访失败")
        if action.startswith("chest_"):
            status = call("daily_chest", {"action": "status", **host})
            info = (status.get("data") or {}).get("dailyChest") or {}
            if not status.get("ok") or bool(info.get("visiting")) != bool(owner):
                raise ValueError("宝箱身份或状态未确认")
            result["chest"] = {"owner_id": owner, **info}
            if action == "chest_status" or info.get("opened"):
                result.update(ok=True, status="synced" if action == "chest_status" else "already_open")
                return result
            payload = {"action": "open", **host, **chest_payload(info, request.get("interaction") or {})}
            endpoint = "daily_chest"
        elif action == "repair":
            payload, endpoint = repair_payload(before, settings), "repair"
        elif action == "meditation":
            med = before["meditation"]
            if not settings["meditation_enabled"] or not settings["meditation_verified"]:
                raise ValueError("静室自动结算未启用，或尚未验证修为入账")
            remaining = number(med.get("projectedLingqi"))
            if not med.get("canSettle") or remaining is None or remaining < settings["lingqi_reserve"]:
                raise ValueError("静室当前不可结算或低于保留灵气额度")
            endpoint, payload = "meditation", {}
        elif action == "sermon":
            world = before["small_world"]
            if settings["skip_full_sermon"] and full_sermon_resources(world["summary"]):
                result.update(ok=True, status="skipped_full")
                return result
            cooldown = number(world["actions"].get("edictRemainingSeconds"))
            if cooldown is None or cooldown > 0:
                raise ValueError("神谕冷却未结束或未确认")
            endpoint, payload = "small_world", {"action": "miracle_sermon"}
        else:
            raise ValueError("unsupported dwelling action")
        if checkpoint:
            checkpoint({"stage": "submitting", "action": action, "before": before, "day": game_day()})
        submitted = True
        result["status"] = "submitting"
        mutation = call(endpoint, payload)
        receipt = (mutation.get("data") or {}).get("actionResult") or {}
        result["receipt"] = {"ok": receipt.get("ok"), "message": api.sanitize_estate_miniapp_secret_text(receipt.get("message") or "")}
        uncertain = int(mutation.get("status_code") or 0) == 0 or int(mutation.get("status_code") or 0) >= 500
        result.update(ok=bool(mutation.get("ok")), status="settled" if mutation.get("ok") else "needs_review" if uncertain else "failed", error=mutation.get("error", ""))
        if action == "chest_open":
            verified = call("daily_chest", {"action": "status", **host})
            info = (verified.get("data") or {}).get("dailyChest") or {}
            result["chest"] = {"owner_id": owner, **info}
            if verified.get("ok") and bool(info.get("visiting")) == bool(owner) and info.get("opened") and info.get("date") == payload["date"]:
                result.update(ok=True, status="settled" if mutation.get("ok") else "reconciled")
            else:
                result.update(ok=False, status="needs_review")
        result["snapshot"] = observations(read(), before)
        if action == "repair" and result["status"] == "needs_review":
            targets = [item.get("itemId") for item in before["repair"].get("items") or []
                       if payload["target"] == "all" or item.get("itemId") == payload["target"]]
            after = {item.get("itemId"): item for item in result["snapshot"]["treasures"]}
            if targets and all(target in after and number(after[target].get("maxDurability")) is not None and
                number(after[target].get("durability")) is not None and float(after[target]["durability"]) >= float(after[target]["maxDurability"]) for target in targets):
                result.update(ok=True, status="reconciled", error="已确认法宝耐久恢复；实际扣费明细未返回")
        return result
    except Exception as exc:
        result["error"] = api.sanitize_estate_miniapp_secret_text(exc)
        if submitted and result.get("status") != "failed":
            result.update(ok=False, status="needs_review")
        return result


async def run_public(client, storage, *, request, settings, previous=None, checkpoint=None):
    try:
        discovery = await api.resolve_estate_public_miniapp_launch(client, storage)
        if not discovery.get("ok"):
            raise ValueError(discovery.get("error") or "公共入口未找到")
        launch = discovery["launch"]
        init_data = await api.request_estate_miniapp_init_data(client, token=launch.get("token"),
            webview_url=launch.get("webview_url"), bot_username=launch.get("bot_username"), launch_context=launch)
        return await asyncio.to_thread(run_flow, token=launch.get("token"), init_data=init_data, transport=api._urllib_transport,
            request=request, settings=settings, previous=previous, checkpoint=checkpoint)
    except Exception as exc:
        return {"ok": False, "status": "failed", "error": api.sanitize_estate_miniapp_secret_text(exc)}


def build_view(payload):
    root = (payload or {}).get("dongfu_resources") or {}
    settings = policy(root.get("policy"))
    snapshot = root.get("snapshot") or {}
    alerts = [item for item in snapshot.get("treasures") or []
              if number(item.get("durability")) is not None and float(item["durability"]) < settings["durability_threshold"]]
    return {"policy": settings, "snapshot": snapshot, "alerts": alerts,
            "chests": root.get("chests") or {}, "last_result": root.get("last_result") or {},
            "request_status": (root.get("request") or {}).get("status", "idle")}


def _resources_ready(client, storage, profile_id, payload=None):
    current = json.loads((storage.get_external_account(int(profile_id), ASC_PROVIDER) or {}).get("me_json") or "{}")
    root = current.get("dongfu_resources") or {}
    request = root.get("request") or {}
    if request:
        return request.get("status") == "queued" or (
            request.get("status") == "running" and float(request.get("lease_until") or 0) <= time.time())
    return bool(policy(root.get("policy"))["observe_enabled"] and
                time.time() - float((root.get("snapshot") or {}).get("updated_at") or 0) >= 900)


@tracked_flow(ready=_resources_ready)
async def run_pending(client, storage, profile_id, payload=None):
    current = json.loads((storage.get_external_account(int(profile_id), ASC_PROVIDER) or {}).get("me_json") or "{}")
    root = current.get("dongfu_resources") or {}
    settings = policy(root.get("policy"))
    request = root.get("request") or {}
    def update(transform):
        return storage.update_external_account_payload(int(profile_id), ASC_PROVIDER, transform)
    if not request and settings["observe_enabled"] and time.time() - float((root.get("snapshot") or {}).get("updated_at") or 0) >= 900:
        current = update(lambda latest: queue(latest, "refresh", source="automatic"))
        root, request = current["dongfu_resources"], current["dongfu_resources"]["request"]
    if request.get("status") == "running" and float(request.get("lease_until") or 0) <= time.time():
        def interrupt(latest):
            board = latest.get("dongfu_resources") or {}
            if (board.get("request") or {}).get("id") == request.get("id"):
                board["request"]["status"] = "needs_review"
                board["needs_review"] = True
                board["last_result"] = {"status": "needs_review", "error": "执行中断，请刷新核对；没有重复资源动作"}
            return latest
        update(interrupt)
        return True
    if request.get("status") != "queued":
        return False
    claimed = False
    def claim(latest):
        nonlocal claimed
        actual = (latest.get("dongfu_resources") or {}).get("request") or {}
        if actual.get("id") == request.get("id") and actual.get("status") == "queued":
            actual.update(status="running", lease_until=time.time() + 600)
            claimed = True
        return latest
    update(claim)
    if not claimed:
        return False
    def checkpoint(value):
        def save(latest):
            board = latest.get("dongfu_resources") or {}
            if (board.get("request") or {}).get("id") != request.get("id"):
                raise RuntimeError("dwelling resource request replaced")
            # This transaction is the local admission point for the resource write.
            # A changed target/budget must not authorize a payload built under old settings.
            latest_settings = policy(board.get("policy"))
            check_request_policy(request, latest_settings)
            if latest_settings != settings:
                raise ValueError("资源策略在读取期间已变化，本次未提交，请重新发起")
            if request.get("day") != game_day():
                raise ValueError("资源请求已跨日，请重新发起")
            board["checkpoint"] = value
            return latest
        update(save)
    group_manages_sermon = request.get("action") == "sermon" and any(
        task.get("feature_key") in {"small_world_auto", "small_world_preach_auto"}
        for task in storage.list_active_companion_auto_tasks(int(profile_id)))
    if group_manages_sermon:
        result = {"ok": False, "status": "failed", "error": "群内自动小世界正在管理神谕，请使用现有任务"}
    elif request.get("day") != game_day() and request.get("action") not in {"refresh", "chest_status"}:
        result = {"ok": False, "status": "failed", "error": "资源请求已跨日，请重新发起"}
    else:
        result = await run_public(client, storage, request=request, settings=settings, previous=root.get("snapshot"), checkpoint=checkpoint)
    def finish(latest):
        board = latest.get("dongfu_resources") or {}
        if (board.get("request") or {}).get("id") != request.get("id"):
            return latest
        if result.get("snapshot"):
            board["snapshot"] = result["snapshot"]
        if result.get("chest"):
            chest = result["chest"]
            board.setdefault("chests", {})["visitor:" + str(chest["owner_id"]) if chest["owner_id"] else "own"] = chest
            if chest.get("visiting"):
                board["visitor_daily"] = {"date": chest.get("date"), "opened": bool(chest.get("opened"))}
        board["last_result"] = {key: result[key] for key in ("ok", "status", "error", "events", "receipt", "action") if key in result}
        unresolved = bool(board.get("needs_review") or board.get("checkpoint")) and request.get("action") in {"refresh", "chest_status"}
        if result.get("status") == "needs_review" or unresolved:
            board["request"]["status"] = "needs_review"
            board["needs_review"] = True
        else:
            board.pop("request", None)
            board.pop("checkpoint", None)
        latest["dongfu_resources"] = board
        if request.get("action") == "refresh" and result.get("ok") and not unresolved:
            fresh = build_view(latest)
            current_settings = fresh["policy"]
            if not current_settings["observe_enabled"]:
                return latest
            if current_settings["auto_repair"] and fresh["alerts"]:
                try:
                    repair_payload(fresh["snapshot"], current_settings)
                    return queue(latest, "repair", source="automatic")
                except ValueError:
                    pass
            med = fresh["snapshot"].get("meditation") or {}
            if current_settings["meditation_enabled"] and current_settings["meditation_verified"] and med.get("canSettle"):
                return queue(latest, "meditation", source="automatic")
        return latest
    update(finish)
    return True
