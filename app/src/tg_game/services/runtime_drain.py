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
DRAIN_CACHE_SECONDS = 1.0
_drain_cache = {}  # storage.path -> (monotonic time, value)


def drain_requested(storage, *, fresh=False):
    """Every uncached read opens and closes a database connection, and this is called per
    message and per task, so reads within a second share one result.

    ponytail: a cached "not draining" can be up to DRAIN_CACHE_SECONDS stale. That is safe only
    because admission never rests on it: begin_flow re-reads with fresh=True after writing its
    row, and callers that start work without a flow row must pass fresh=True themselves. The
    deployment tool needs two quiet polls 2 s apart, longer than the cache lifetime.
    """
    if storage is None or not hasattr(storage, "get_runtime_state"):
        return False
    path = getattr(storage, "path", None)
    now = time.monotonic()
    cached = _drain_cache.get(path) if path else None
    if cached and not fresh and now - cached[0] < DRAIN_CACHE_SECONDS:
        return cached[1]
    value = str(storage.get_runtime_state(DRAIN_KEY) or "") not in {"", "0"}
    if path:
        _drain_cache[path] = (now, value)
    return value


def begin_flow(storage, name):
    if drain_requested(storage):
        return ""
    key = FLOW_PREFIX + uuid.uuid4().hex
    storage.set_runtime_state(key, json.dumps({"pid": os.getpid(), "flow": name, "started_at": time.time()}))
    if drain_requested(storage, fresh=True):
        storage.delete_runtime_state(key)
        return ""
    return key


def end_flow(storage, key):
    if key:
        storage.delete_runtime_state(key)


class LoopFlow:
    """One registration for a polling loop, kept across passes instead of a row written and
    deleted every pass. The row stays until the loop itself sees the drain request, so the
    deployment tool keeps waiting for a pass that started just before the request.
    """

    def __init__(self, storage, name):
        self.storage, self.name, self.key = storage, name, ""

    def enter(self):
        """Call at the top of each pass. False means draining: the row is gone, skip the pass."""
        if not self.key:
            self.key = begin_flow(self.storage, self.name)
            return bool(self.key)
        if drain_requested(self.storage):
            self.close()
            return False
        return True

    def close(self):
        end_flow(self.storage, self.key)
        self.key = ""


def polling_flow(name):
    """Give a polling coroutine(client, storage, ...) its LoopFlow as the `loop_flow` keyword
    and remove the registration however the coroutine ends (return, error, cancellation)."""
    def wrap(function):
        @wraps(function)
        async def run(client, storage, *args, **kwargs):
            flow = LoopFlow(storage, name)
            try:
                return await function(client, storage, *args, loop_flow=flow, **kwargs)
            finally:
                flow.close()
        return run
    return wrap


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
