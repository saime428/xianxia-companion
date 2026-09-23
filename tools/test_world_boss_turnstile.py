"""Offline queue/browser checks; no Chrome or game API is used.

Run: python -X utf8 tools/test_world_boss_turnstile.py
"""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import logging
import multiprocessing
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))

from tg_game.features.world_boss import world_boss_browser as browser
from tg_game.features.world_boss import world_boss_turnstile as queue

TOKEN = "offline-callback-token-not-a-real-credential"


class Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self):
        return self.now


class FakeBrowser:
    def __init__(self, outcome=TOKEN):
        self.outcome = outcome
        self.calls = 0
        self.closed = 0

    def verify(self, origin=browser.ORIGIN, **kwargs):
        self.calls += 1
        self.page_path = kwargs.get("page_path")
        self.action = kwargs.get("action")
        assert origin == browser.ORIGIN
        if isinstance(self.outcome, Exception):
            raise self.outcome
        kwargs["on_event"]("token_generated", "")
        return self.outcome

    def close(self):
        self.closed += 1


def expect_error(code, operation):
    try:
        operation()
    except (queue.TurnstileRequestError, browser.BrowserVerificationError) as exc:
        assert exc.code == code, exc.code
    else:
        raise AssertionError(f"Expected {code}")


def request(broker, **extra):
    return broker.create_request(
        event_fingerprint="event", message_id=123, account="main", identity="大号",
        challenge_id="challenge-1", origin=browser.ORIGIN, **extra,
    )["request_id"]


def consume_in_process(directory, request_id, ready, done, result):
    broker = queue.WorldBossTurnstileBroker(directory)
    ready.set()
    try:
        result.put(broker.take_token(request_id) == TOKEN)
    finally:
        done.set()


def check_queue(directory):
    clock = Clock()
    broker = queue.WorldBossTurnstileBroker(directory, clock=clock)
    rid = request(broker)
    expect_error("turnstile_token_invalid", lambda: broker.submit_token(rid, "bad\ntoken"))
    broker.submit_token(rid, TOKEN)
    expect_error("turnstile_request_already_submitted", lambda: broker.submit_token(rid, TOKEN))
    expect_error("turnstile_request_mismatch", lambda: broker.take_token(rid, challenge_id="other"))
    expect_error("turnstile_request_mismatch", lambda: broker.take_token(rid, origin="https://other.invalid"))
    assert broker.record_result(rid, accepted=True)["status"] == "submitted"
    assert TOKEN not in json.dumps(broker.list_requests())
    assert broker.take_token(rid, challenge_id="challenge-1", origin=browser.ORIGIN) == TOKEN
    assert broker.take_token(rid) is None
    assert not list(Path(directory).glob("token_*.txt"))
    assert broker.get_request(rid)["status"] == "consumed"
    assert broker.record_result(rid, accepted=True, http_status=200)["status"] == "accepted"
    broker.record_result(rid, accepted=False, error="late_failure")
    broker.cancel(rid, reason="late_cancel")
    assert broker.get_request(rid)["status"] == "accepted"

    rid = request(broker, ttl_seconds=600)
    broker.submit_token(rid, TOKEN)
    clock.now += queue.MAX_REQUEST_TTL_SECONDS
    assert broker.take_token(rid) is None
    assert broker.get_request(rid)["status"] == "expired"
    assert not list(Path(directory).glob("token_*.txt"))
    expect_error("turnstile_request_expired", lambda: broker.submit_token(rid, TOKEN))
    rid = request(broker)
    broker.cancel(rid, reason="world_boss_disabled")
    expect_error("turnstile_request_already_submitted", lambda: broker.submit_token(rid, TOKEN))
    assert broker.take_token(rid) is None


def check_cross_process_lock(directory):
    broker = queue.WorldBossTurnstileBroker(directory)
    rid = request(broker)
    broker.submit_token(rid, TOKEN)
    ctx = multiprocessing.get_context("spawn")
    ready, done, result = ctx.Event(), ctx.Event(), ctx.Queue()
    process = ctx.Process(target=consume_in_process, args=(directory, rid, ready, done, result))
    try:
        with queue._queue_lock(broker.lock_path):
            process.start()
            assert ready.wait(10), "Consumer did not start"
            assert not done.wait(0.3), "Another process entered a locked queue"
        assert done.wait(10), "Consumer remained blocked after unlock"
        assert result.get(timeout=2) is True
        process.join(5)
        assert process.exitcode == 0
        assert broker.take_token(rid) is None
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        result.close()
    with browser.worker_lease(directory):
        def acquire_second_worker():
            with browser.worker_lease(directory):
                raise AssertionError("Two browser workers acquired the same queue")

        expect_error("browser_worker_already_running", acquire_second_worker)


def check_worker(directory):
    clock = Clock()
    broker = queue.WorldBossTurnstileBroker(directory, clock=clock)
    rid = request(
        broker,
        page_path="/miniapp/xianxia-spirit-tree",
        action="luoyun_spirit_tree_fly_begin",
    )
    fake = FakeBrowser()
    worker = browser.AutomaticTurnstileWorker(broker, fake, clock=clock)
    assert worker.run_once() is True
    assert broker.get_request(rid)["status"] == "submitted"
    assert broker.get_request(rid)["page_path"] == "/miniapp/xianxia-spirit-tree"
    assert broker.get_request(rid)["action"] == "luoyun_spirit_tree_fly_begin"
    assert fake.page_path == "/miniapp/xianxia-spirit-tree"
    assert fake.action == "luoyun_spirit_tree_fly_begin"
    assert broker.get_request(rid)["browser_attempts"] == 1
    assert worker.run_once() is False
    assert fake.calls == 1
    assert broker.take_token(rid) == TOKEN

    for code in ("turnstile_interaction_required", "browser_executable_missing"):
        rid = request(broker)
        fake = FakeBrowser(browser.BrowserVerificationError(code))
        worker = browser.AutomaticTurnstileWorker(broker, fake, clock=clock)
        worker.run_once()
        state = broker.get_request(rid)
        assert state["status"] == "cancelled" and state["cancel_reason"] == code
        assert state["browser_attempts"] == 1
        assert not worker.run_once() and fake.calls == 1

    rid = request(broker)
    fake = FakeBrowser(browser.BrowserVerificationError("turnstile_browser_timeout"))
    worker = browser.AutomaticTurnstileWorker(broker, fake, clock=clock)
    worker.run_once()
    assert broker.get_request(rid)["status"] == "pending"
    assert not worker.run_once() and fake.calls == 1
    # A restart must keep the first failed attempt in the budget.
    worker = browser.AutomaticTurnstileWorker(broker, fake, clock=clock)
    worker.run_once()
    assert broker.get_request(rid)["status"] == "cancelled"
    assert broker.get_request(rid)["browser_attempts"] == 2 and fake.calls == 2

    # Simulate process crashes after claiming both attempts, before reporting.
    rid = request(broker)
    broker.begin_browser_attempt(rid)
    broker.begin_browser_attempt(rid)
    fake = FakeBrowser()
    browser.AutomaticTurnstileWorker(broker, fake, clock=clock).run_once()
    assert fake.calls == 0
    assert broker.get_request(rid)["cancel_reason"] == "turnstile_attempts_exhausted"

    rid = request(broker)
    fake = FakeBrowser(RuntimeError(TOKEN))
    worker = browser.AutomaticTurnstileWorker(broker, fake, clock=clock)
    worker.run_once()
    clock.now += 5
    worker.run_once()
    assert broker.get_request(rid)["cancel_reason"] == "browser_worker_failed"


def check_native_callback(directory):
    native = browser.NativeTurnstileBrowser(directory)
    phase = "solved"
    solve_on_click = True
    input_events = []
    clock = {"now": 1000.0}

    def evaluate(expression):
        if expression == "location.origin":
            return browser.ORIGIN
        if expression.startswith("Boolean("):
            return True
        if "return {phase:" in expression:
            return {"phase": phase, "generation": "offline", "error": "", "age": 8000, "x": 81, "y": 93}
        if "const token=s.token" in expression:
            return TOKEN

    def command(method, params=None):
        if str(method).startswith("Input."):
            input_events.append((method, dict(params or {})))
            if solve_on_click and (params or {}).get("type") == "mouseReleased":
                nonlocal phase
                phase = "solved"
        return {}

    def monotonic():
        return clock["now"]

    def sleep(seconds):
        clock["now"] += float(seconds)

    with patch.object(native, "start"), patch.object(native, "command", side_effect=command), \
            patch.object(native, "evaluate", side_effect=evaluate), \
            patch.object(browser.uuid, "uuid4", return_value=SimpleNamespace(hex="offline")), \
            patch.object(browser.time, "monotonic", monotonic), \
            patch.object(browser.time, "sleep", sleep):
        assert native.verify() == TOKEN
        assert input_events == []

        phase = "interactive"
        clock["now"] = 1000.0
        assert native.verify() == TOKEN
        pressed = [params for _method, params in input_events if params.get("type") == "mousePressed"]
        assert len(pressed) == 1
        assert pressed[0]["x"] == 81 and pressed[0]["y"] == 93 and pressed[0]["button"] == "left"

        phase = "interactive"
        solve_on_click = False
        input_events.clear()
        clock["now"] = 1000.0
        expect_error("turnstile_browser_timeout", lambda: native.verify(timeout=10))
        assert len([params for _method, params in input_events if params.get("type") == "mousePressed"]) == 1
        expect_error("browser_origin_not_allowed", lambda: native.verify("https://other.invalid"))


def check_probe(directory):
    fake = FakeBrowser()
    output = io.StringIO()
    with patch.object(browser, "NativeTurnstileBrowser", return_value=fake), \
            patch.object(browser.signal, "signal"), \
            patch.object(sys, "argv", ["world_boss_browser", "--probe", "--queue-dir", directory]), \
            redirect_stdout(output):
        assert browser.main() == 0
    report = json.loads(output.getvalue())
    assert report["event"] == "token_generated"
    assert "success" not in report and "battle_success" not in report
    assert TOKEN not in output.getvalue()


def main():
    logs = io.StringIO()
    handler = logging.StreamHandler(logs)
    browser.LOG.addHandler(handler)
    browser.LOG.propagate = False
    try:
        with tempfile.TemporaryDirectory() as root:
            for check in (check_queue, check_cross_process_lock, check_worker, check_native_callback, check_probe):
                check(str(Path(root) / check.__name__))
        assert TOKEN not in logs.getvalue(), "A token leaked into diagnostics"
    finally:
        browser.LOG.removeHandler(handler)
    print("test_world_boss_turnstile: ok")


if __name__ == "__main__":
    main()
