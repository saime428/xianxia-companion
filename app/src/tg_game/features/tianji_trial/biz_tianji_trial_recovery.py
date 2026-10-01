"""Private checkpoints; never return proofs or challenge bodies to the view."""
from copy import deepcopy
import time

from tg_game.game_clock import game_day
from . import biz_tianji_trial_view_state as state


def checkpoint_payload(payload, owner, checkpoint):
    if not state.is_tianji_trial_request_owned(payload, owner):
        raise RuntimeError("trial request ownership lost")
    updated = deepcopy(payload)
    trial = updated["tianji_trial"]
    trial["pending_submission"] = deepcopy(checkpoint)
    return state.mark_tianji_trial_request_status(updated, "running", execution_owner=owner)


def finish_payload(payload, owner, result, run, entry=None):
    if not state.is_tianji_trial_request_owned(payload, owner):
        return payload
    updated = state.merge_tianji_trial_payload(payload, entry=entry, run=run)
    trial = updated["tianji_trial"]
    request = dict(trial.get("miniapp_request") or {})
    status = str(result.get("status") or "failed")
    retry = status == "retry_pending"
    unresolved = status == "settlement_unknown"
    attempts = int(request.get("retry_count") or 0) + 1
    if retry or unresolved:
        request.update(status="queued" if retry and attempts <= 3 else "needs_review",
                       retry_count=attempts, not_before=time.time() + min(300 * attempts, 900),
                       execution_owner="", lease_expires_at=0)
        trial["miniapp_request"] = request
    else:
        trial.pop("miniapp_request", None)
        trial.pop("pending_submission", None)
    return updated


def reconcile_checkpoint(checkpoint, start_data):
    """A new start is observed only; ambiguous identity is never resubmitted."""
    previous = checkpoint or {}
    if previous.get("day") != game_day():
        return "expired"
    challenge = start_data.get("challenge") or {}
    trial = start_data.get("trial") or {}
    progress = start_data.get("dailyProgress") or {}
    completed = progress.get("completed", trial.get("completedToday"))
    before = (previous.get("trial") or {}).get("completedToday")
    if completed is not None and before is not None and int(completed) > int(before):
        return "confirmed"
    old_id = (previous.get("challenge") or {}).get("challengeId")
    if old_id and old_id == challenge.get("challengeId"):
        # Only a checkpoint made before sending is safe to continue automatically.
        return "prepared" if previous.get("stage") == "prepared" else "unconfirmed"
    return "unconfirmed"
