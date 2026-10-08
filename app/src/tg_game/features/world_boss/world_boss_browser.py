"""Bounded, unattended Turnstile verification through isolated native browsers.

The browser loads the real Mini App and uses its current Turnstile configuration.
Only Cloudflare's callback can produce a token. Managed widgets that become
interactive receive one checkbox click; this helper never waits for a Dashboard
fallback. No Telegram credentials are sent to the browser, and tokens never
appear in diagnostics. The game worker remains the only process that sends /begin.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request
import uuid

from .world_boss_turnstile import (
    MAX_TOKEN_LENGTH,
    TurnstileRequestError,
    WorldBossTurnstileBroker,
    normalize_turnstile_action,
    normalize_turnstile_page_path,
)


ORIGIN = "https://asc.aiopenai.app"
LOG = logging.getLogger("world_boss_browser")


class BrowserVerificationError(RuntimeError):
    def __init__(self, code: str, cf_code: str = ""):
        self.code = code
        self.cf_code = cf_code if re.fullmatch(r"[0-9]{3,6}", str(cf_code)) else ""
        super().__init__(code)


def _finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def _widget_age_ms(value):
    age = _finite_number(value)
    return age if age is not None and age >= 0 else -1.0


def _checkbox_point(state):
    x = _finite_number((state or {}).get("x"))
    y = _finite_number((state or {}).get("y"))
    if x is None or y is None:
        return None
    return {"x": x, "y": y}


def find_chrome(explicit: str | None = None) -> str:
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        raise BrowserVerificationError("browser_executable_missing")
    for name in ("google-chrome", "chromium", "chromium-browser"):
        candidate = shutil.which(name)
        if candidate:
            return candidate
    candidates = [Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")]
    candidates += sorted((Path.home() / ".cache/ms-playwright").glob("chromium-*/*/chrome"), reverse=True)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    raise BrowserVerificationError("browser_executable_missing")


class NativeTurnstileBrowser:
    def __init__(self, profile_dir, *, chrome=None, no_sandbox=False):
        self.profile_dir = Path(profile_dir).resolve()
        self.chrome = chrome
        self.no_sandbox = no_sandbox
        self.process = None
        self.connection = None
        self.sequence = 0
        self.origin = ""
        self.page_path = ""

    def start(self):
        if self.connection is not None:
            return
        try:
            import websocket
        except ImportError as exc:
            raise BrowserVerificationError("websocket_client_missing") from exc
        executable = find_chrome(self.chrome)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.profile_dir, 0o700)
        except OSError:
            pass
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        args = [executable, f"--remote-debugging-port={port}",
                f"--user-data-dir={self.profile_dir}", "--no-first-run",
                "--no-default-browser-check", "--disable-dev-shm-usage",
                "--window-size=1100,950", "about:blank"]
        startup = None
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            args.append("--window-position=-32000,-32000")
        else:
            # A software display also supports hosts without a GPU.
            args.append("--disable-gpu")
            if self.no_sandbox:
                args.append("--no-sandbox")
            if not os.environ.get("DISPLAY"):
                xvfb = shutil.which("xvfb-run")
                if not xvfb:
                    raise BrowserVerificationError("virtual_display_missing")
                args = [xvfb, "-a", "-s", "-screen 0 1280x1024x24", *args]
        self.process = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            startupinfo=startup, start_new_session=os.name != "nt",
        )
        deadline = time.monotonic() + 25
        try:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise BrowserVerificationError("browser_launch_failed")
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as response:
                        pages = json.load(response)
                    page = next(item for item in pages if item.get("type") == "page")
                    self.connection = websocket.create_connection(
                        page["webSocketDebuggerUrl"], timeout=10, suppress_origin=True,
                    )
                    self.command("Page.enable")
                    return
                except BrowserVerificationError:
                    raise
                except Exception:
                    time.sleep(0.25)
            raise BrowserVerificationError("browser_connection_timeout")
        except BaseException:
            self.close()
            raise

    def command(self, method, params=None):
        self.sequence += 1
        sequence = self.sequence
        try:
            self.connection.send(json.dumps({"id": sequence, "method": method, "params": params or {}}))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                message = json.loads(self.connection.recv())
                if message.get("id") == sequence:
                    if "error" in message:
                        raise BrowserVerificationError("browser_protocol_error")
                    return message.get("result") or {}
        except BrowserVerificationError:
            raise
        except Exception as exc:
            raise BrowserVerificationError("browser_disconnected") from exc
        raise BrowserVerificationError("browser_protocol_timeout")

    def evaluate(self, expression):
        result = self.command("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        if result.get("exceptionDetails"):
            raise BrowserVerificationError("browser_script_error")
        return result.get("result", {}).get("value")

    def verify(self, origin=ORIGIN, *, page_path="", action="", timeout=55, on_event=None, still_pending=None):
        if str(origin).rstrip("/") != ORIGIN:
            raise BrowserVerificationError("browser_origin_not_allowed")
        path = normalize_turnstile_page_path(page_path)
        deadline = time.monotonic() + max(10, min(90, float(timeout)))

        def emit(event, code=""):
            if on_event:
                on_event(event, code)

        if still_pending and not still_pending():
            raise BrowserVerificationError("verification_request_finished")
        self.start()
        self.command("Page.bringToFront")
        if self.origin != origin or self.page_path != path:
            self.command("Page.navigate", {"url": ORIGIN + path})
            while time.monotonic() < deadline:
                if still_pending and not still_pending():
                    raise BrowserVerificationError("verification_request_finished")
                if self.evaluate("Boolean(window.turnstile && window.__QYZ_TURNSTILE_CONFIG__)"):
                    self.origin = origin
                    self.page_path = path
                    break
                time.sleep(0.25)
            else:
                raise BrowserVerificationError("turnstile_script_unavailable")
        if self.evaluate("location.origin") != ORIGIN:
            raise BrowserVerificationError("browser_origin_not_allowed")
        emit("helper_ready")
        generation = uuid.uuid4().hex
        chosen_action = normalize_turnstile_action(action)
        self.evaluate("""(([generation, action]) => {
            const old = window.__qyzAutomaticVerification;
            if (old && old.widgetId != null) { try { window.turnstile.remove(old.widgetId); } catch (_) {} }
            document.getElementById('__qyz_automatic_widget')?.remove();
            const box = document.createElement('div'); box.id = '__qyz_automatic_widget';
            box.style.cssText = 'position:fixed;left:30px;top:30px;padding:30px;background:#fff;z-index:2147483647';
            document.body.append(box);
            const state = {generation, phase:'loading', token:'', error:'', created:performance.now(), widgetId:null};
            window.__qyzAutomaticVerification = state;
            const active = () => window.__qyzAutomaticVerification === state;
            const config = window.__QYZ_TURNSTILE_CONFIG__;
            if (!config.enabled || !config.siteKey) { state.phase='config_error'; return; }
            const chosenAction = action || config.action || '';
            window.turnstile.ready(() => {
                if (!active()) return;
                state.widgetId = window.turnstile.render(box, {
                    sitekey:config.siteKey, action:chosenAction,
                    theme:'dark', appearance:'always', execution:'render', size:'normal', retry:'never',
                    'refresh-expired':'manual',
                    callback:token=>{if(active()){state.token=String(token||'');state.phase='solved';}},
                    'error-callback':code=>{if(active()){state.error=String(code||'');state.phase='error';}},
                    'expired-callback':()=>{if(active()){state.token='';state.phase='expired';}},
                    'timeout-callback':()=>{if(active()){state.token='';state.phase='timeout';}},
                    'unsupported-callback':()=>{if(active()){state.phase='unsupported';}},
                    'before-interactive-callback':()=>{if(active()){state.phase='interactive';}},
                });
                if(state.phase==='loading') state.phase='ready';
            });
        })(""" + json.dumps([generation, chosen_action]) + ")")
        emit("widget_ready")
        clicked = False
        interactive_since = None
        while time.monotonic() < deadline:
            if still_pending and not still_pending():
                raise BrowserVerificationError("verification_request_finished")
            state = self.evaluate("""(() => {
                const s=window.__qyzAutomaticVerification;
                const box=document.getElementById('__qyz_automatic_widget');
                if(!s||!box) return {};
                const rect=box.getBoundingClientRect();
                return {phase:s.phase,error:s.error,generation:s.generation,age:performance.now()-s.created,
                        x:rect.left+51,y:rect.top+63};
            })()""") or {}
            if state.get("generation") != generation:
                raise BrowserVerificationError("verification_generation_changed")
            phase = state.get("phase")
            if phase == "interactive" and interactive_since is None:
                interactive_since = time.monotonic()
            if phase == "solved":
                token = self.evaluate("""(() => {const s=window.__qyzAutomaticVerification;
                    const token=s.token;s.token='';return token;})()""")
                if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH or any(ord(c) < 32 for c in token):
                    raise BrowserVerificationError("browser_token_invalid")
                emit("token_generated")
                return token
            if phase in {"error", "unsupported", "expired", "timeout", "config_error"}:
                code = str(state.get("error") or "")
                emit({"error":"widget_error", "timeout":"widget_timeout"}.get(phase, phase), code if code.isdigit() else "")
                raise BrowserVerificationError("turnstile_browser_" + phase, code)
            # Managed Turnstile has a standard checkbox at this position in a
            # normal-size widget. Only one click is attempted per fresh widget.
            if (
                not clicked
                and phase == "interactive"
                and _widget_age_ms(state.get("age")) >= 8000
                and interactive_since is not None
                and time.monotonic() - interactive_since >= 1
            ):
                emit("interaction_required")
                point = _checkbox_point(state)
                if point is not None:
                    self.command("Input.dispatchMouseEvent", {"type": "mouseMoved", **point})
                    time.sleep(0.12)
                    self.command("Input.dispatchMouseEvent", {
                        "type": "mousePressed", "button": "left", "buttons": 1, "clickCount": 1, **point,
                    })
                    time.sleep(0.12)
                    self.command("Input.dispatchMouseEvent", {
                        "type": "mouseReleased", "button": "left", "buttons": 0, "clickCount": 1, **point,
                    })
                # Invalid coordinates still consume the one attempt; do not keep
                # retrying a widget whose checkbox cannot be located.
                clicked = True
            time.sleep(0.25)
        emit("widget_timeout")
        raise BrowserVerificationError("turnstile_browser_timeout")

    def close(self):
        if self.connection is not None:
            try:
                self.command("Browser.close")
            except Exception:
                pass
            try:
                self.connection.close()
            except Exception:
                pass
        self.connection = None
        self.origin = ""
        self.page_path = ""
        if self.process is not None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    self.process.terminate()
                else:
                    os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        self.process.kill()
                    else:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=5)
        self.process = None


class AutomaticTurnstileWorker:
    def __init__(self, broker, browser, *, clock=time.monotonic, max_attempts=2, stopping=None):
        self.broker, self.browser, self.clock = broker, browser, clock
        self.stopping = stopping
        self.max_attempts = max(1, min(3, int(max_attempts)))
        self.last_attempt = {}
        self.last_work = clock()

    def run_once(self):
        rows = [row for row in self.broker.list_requests() if row.get("status") == "pending"]
        live = {row["request_id"] for row in rows}
        self.last_attempt = {key: value for key, value in self.last_attempt.items() if key in live}
        rows = [row for row in rows if self.clock() - self.last_attempt.get(row["request_id"], -1000) >= 5]
        rows.sort(key=lambda row: (int(row.get("browser_attempts") or 0), str(row.get("created_at", ""))))
        if not rows:
            if self.clock() - self.last_work >= 20:
                self.browser.close()
            return False
        row = rows[0]
        try:
            return self.process(row)
        finally:
            self.last_attempt[row["request_id"]] = self.last_work

    def process(self, row):
        request_id = row["request_id"]

        def pending():
            if self.stopping is not None and self.stopping.is_set():
                return False
            return (self.broker.get_request(request_id) or {}).get("status") == "pending"

        def event(stage, code=""):
            self.broker.record_browser_event(request_id, stage, code, source="automatic")

        try:
            row = self.broker.begin_browser_attempt(request_id, max_attempts=self.max_attempts)
            token = self.browser.verify(
                row.get("origin"),
                page_path=row.get("page_path"),
                action=row.get("action"),
                on_event=event,
                still_pending=pending,
            )
            if pending():
                self.broker.submit_token(request_id, token)
                LOG.info("[%s/%s] automatic browser token submitted", row.get("account"), row.get("identity"))
            token = ""
        except (BrowserVerificationError, TurnstileRequestError) as exc:
            LOG.warning("[%s/%s] automatic verification attempt %s: %s CF=%s",
                        row.get("account"), row.get("identity"), row.get("browser_attempts", 0),
                        exc.code, getattr(exc, "cf_code", ""))
            if pending():
                if exc.code == "turnstile_interaction_required":
                    event("interaction_required")
                terminal = isinstance(exc, TurnstileRequestError) or exc.code in {
                    "turnstile_interaction_required", "browser_origin_not_allowed",
                    "browser_executable_missing", "websocket_client_missing", "virtual_display_missing",
                    "turnstile_browser_unsupported", "turnstile_browser_config_error", "browser_token_invalid",
                    "verification_generation_changed",
                }
                if terminal or int(row.get("browser_attempts") or 0) >= self.max_attempts:
                    self.broker.cancel(request_id, reason=exc.code)
                elif not exc.code.startswith("turnstile_browser_"):
                    event("config_error")
            self.browser.close()
        except Exception as exc:
            LOG.error("Automatic verification attempt failed: %s", type(exc).__name__)
            if pending() and int(row.get("browser_attempts") or 0) >= self.max_attempts:
                self.broker.cancel(request_id, reason="browser_worker_failed")
            self.browser.close()
        finally:
            self.last_work = self.clock()
        return True


class ConcurrentTurnstileWorker:
    """One coordinator owns claims; each slot owns its browser until completion."""
    def __init__(self, broker, browsers, *, clock=time.monotonic):
        if not 1 <= len(browsers) <= 3:
            raise ValueError('Expected one to three browser slots')
        self.broker, self.clock = broker, clock
        self.stopping = threading.Event()
        self.workers = [AutomaticTurnstileWorker(broker, item, clock=clock, stopping=self.stopping) for item in browsers]
        self.executor = ThreadPoolExecutor(max_workers=len(browsers), thread_name_prefix='boss-verification')
        self.jobs, self.last_attempt = {}, {}

    def run_once(self):
        if self.stopping.is_set():
            return False
        for slot, (rid, account, future) in list(self.jobs.items()):
            if future.done():
                future.result()
                self.last_attempt[rid] = self.clock()
                del self.jobs[slot]
        rows = [row for row in self.broker.list_requests() if row.get('status') == 'pending']
        live = {row['request_id'] for row in rows}
        self.last_attempt = {rid: at for rid, at in self.last_attempt.items() if rid in live}
        claimed = {rid for rid, _, _ in self.jobs.values()}
        accounts = {account for _, account, _ in self.jobs.values()}
        rows.sort(key=lambda row: (int(row.get('browser_attempts') or 0), str(row.get('created_at', ''))))
        dispatched = False
        for slot, worker in enumerate(self.workers):
            if slot in self.jobs:
                continue
            row = next((row for row in rows if row['request_id'] not in claimed
                        and row.get('account') not in accounts
                        and self.clock() - self.last_attempt.get(row['request_id'], -1000) >= 5), None)
            if row is None:
                if self.clock() - worker.last_work >= 20:
                    worker.browser.close()
                continue
            rid, account = row['request_id'], row.get('account')
            claimed.add(rid)
            accounts.add(account)
            self.jobs[slot] = (rid, account, self.executor.submit(worker.process, row))
            dispatched = True
        return dispatched or bool(self.jobs)

    def close(self):
        self.stopping.set()
        # Native verification checks still_pending while waiting for callbacks.
        # Join before closing browsers so CDP connections are never shared.
        self.executor.shutdown(wait=True)
        for worker in self.workers:
            worker.browser.close()


@contextmanager
def worker_lease(queue_dir, *, cleanup=None):
    """Keep one coordinating worker per queue across all accounts."""
    path = Path(queue_dir) / ".browser-worker.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if not handle.tell():
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BrowserVerificationError("browser_worker_already_running") from exc
        try:
            yield
        finally:
            try:
                if cleanup:
                    cleanup()
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true", help="Check callback token generation only; does not verify /begin or join a battle")
    parser.add_argument("--count", type=int, default=1, choices=range(1, 5))
    parser.add_argument("--chrome")
    parser.add_argument("--queue-dir")
    parser.add_argument("--no-sandbox", action="store_true")
    parser.add_argument("--concurrency", type=int, default=3, choices=range(1, 4))
    args = parser.parse_args()
    broker = WorldBossTurnstileBroker(args.queue_dir)
    broker.queue_dir.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler()]
    if not args.probe:
        handlers.append(RotatingFileHandler(
            broker.queue_dir / "browser-worker.log", maxBytes=512 * 1024,
            backupCount=1, encoding="utf-8",
        ))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    profile = broker.queue_dir / "browser_profile"
    browser = NativeTurnstileBrowser(profile, chrome=args.chrome, no_sandbox=args.no_sandbox)
    browsers = [browser]
    if not args.probe:
        browsers.extend(NativeTurnstileBrowser(broker.queue_dir / f'browser_profile_{i+1}',
                        chrome=args.chrome, no_sandbox=args.no_sandbox) for i in range(1, args.concurrency))
    worker = None

    def cleanup():
        if worker is not None:
            worker.close()
        else:
            for item in browsers:
                item.close()

    def stopped(signum, frame):
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stopped)
    try:
        with worker_lease(broker.queue_dir, cleanup=cleanup):
            if args.probe:
                for index in range(args.count):
                    started = time.monotonic()
                    token = browser.verify(on_event=lambda event, code: LOG.info("Probe browser stage: %s CF=%s", event, code))
                    print(json.dumps({"verification": index + 1, "event": "token_generated",
                                      "token_length": len(token),
                                      "duration_seconds": round(time.monotonic() - started, 2)}), flush=True)
                    token = ""
            else:
                worker = ConcurrentTurnstileWorker(broker, browsers)
                LOG.info("Automatic Qing Yuanzi browser verification is ready (%s slots)", len(browsers))
                while True:
                    try:
                        worker.run_once()
                    except Exception as exc:
                        LOG.error("Automatic verification interrupted: %s", type(exc).__name__)
                        return 1
                    time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    except BrowserVerificationError as exc:
        print(json.dumps({"event": "verification_failed", "error": exc.code, "cf_code": exc.cf_code}), flush=True)
        return 1
    finally:
        cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
