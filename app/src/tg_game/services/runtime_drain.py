"""Stop admitting work and expose in-flight flows to local deployment tooling."""
import asyncio
from functools import wraps
import inspect
import json
import os
import time
import uuid

DRAIN_KEY = "deployment_drain"
FLOW_PREFIX = "runtime_inflight:"


def drain_requested(storage):
    if storage is None or not hasattr(storage, "get_runtime_state"):
        return False
    return str(storage.get_runtime_state(DRAIN_KEY) or "") not in {"", "0"}


def begin_flow(storage, name):
    if drain_requested(storage):
        return ""
    key = FLOW_PREFIX + uuid.uuid4().hex
    storage.set_runtime_state(key, json.dumps({"pid": os.getpid(), "flow": name, "started_at": time.time()}))
    if drain_requested(storage):
        storage.delete_runtime_state(key)
        return ""
    return key


def end_flow(storage, key):
    if key:
        storage.delete_runtime_state(key)


def tracked_flow(function=None, *, ready=None):
    """Track through cancellation; an optional read-only preflight avoids idle writes."""
    if function is None:
        return lambda target: tracked_flow(target, ready=ready)
    signature = inspect.signature(function)
    def denied():
        if signature.return_annotation in {dict, "dict"}:
            return {"ok": False, "status": "draining", "error": "部署排空中，稍后执行"}
        return False

    @wraps(function)
    async def run(*args, **kwargs):
        # This is only a fast rejection. Admission is rechecked by begin_flow after
        # the preflight; the function must still claim its work under its own lock.
        if ready is not None and not ready(*args, **kwargs):
            return False
        bound = signature.bind(*args, **kwargs).arguments
        storage = bound.get("storage") or bound.get("discovery_storage")
        if storage is None and args:
            storage = getattr(getattr(args[0], "actor", None), "runtime_storage", None)
        if storage is None or not hasattr(storage, "set_runtime_state"):
            return await function(*args, **kwargs)
        key = begin_flow(storage, function.__name__)
        if not key:
            return denied()
        try:
            task = asyncio.create_task(function(*args, **kwargs))
            cancelled = False
            while True:
                try:
                    result = await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    if task.done():
                        raise
                    # Repeated shutdown cancellation must not remove a live flow.
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError
            return result
        finally:
            end_flow(storage, key)
    return run
