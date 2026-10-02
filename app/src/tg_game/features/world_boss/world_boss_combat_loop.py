"""Keep combat deadlines independent of synchronous Telegram event-loop work."""
import asyncio
from concurrent.futures import Future
import threading


async def run_combat_loop(operation, *, name="world-boss-combat"):
    """Run an async operation on a private thread/loop and join its cancellation.

    The operation must not use the Telegram client or parent-loop primitives.
    Cancelling the caller waits for the combat coroutine's finally blocks; an
    already-dispatched HTTP request may still finish, but no later action starts.
    """
    result = Future()
    cancelled = threading.Event()
    guard = threading.Lock()
    owner = {}

    async def invoke():
        with guard:
            owner.update(loop=asyncio.get_running_loop(), task=asyncio.current_task())
        if cancelled.is_set():
            raise asyncio.CancelledError()
        return await operation()

    def run():
        try:
            value = asyncio.run(invoke())
        except BaseException as exc:
            result.set_exception(exc)
        else:
            result.set_result(value)

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    pending = asyncio.wrap_future(result)
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        cancelled.set()
        with guard:
            loop, task = owner.get("loop"), owner.get("task")
        if loop is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The private loop has already finished.
        # A second parent cancellation must not abandon a still-active battle.
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not pending.cancelled():
            pending.exception()  # Retrieve a concurrent failure before re-raising cancellation.
        raise
