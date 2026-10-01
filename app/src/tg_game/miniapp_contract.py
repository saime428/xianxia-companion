"""Shared response rules for HTTP envelopes with an inner action result."""


def action_failure(body):
    if not isinstance(body, dict):
        return ""
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    action = data.get("actionResult") or body.get("actionResult")
    if isinstance(action, dict) and action.get("ok") is False:
        return str(action.get("error") or action.get("message") or "action_rejected")
    if data.get("ok") is False:
        return str(data.get("error") or data.get("message") or "action_rejected")
    return ""
