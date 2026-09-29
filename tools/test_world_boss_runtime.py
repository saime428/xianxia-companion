"""Offline checks for Boss profile scope, shutdown, and credential boundaries.

Run: python -B tools/test_world_boss_runtime.py
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))


def main():
    with tempfile.TemporaryDirectory() as temporary:
        os.environ["WORLD_BOSS_TURNSTILE_QUEUE_DIR"] = str(Path(temporary) / "queue")
        from tg_game.config import get_settings
        settings = get_settings()
        settings.database_path = Path(temporary) / "test.db"
        settings.authorized_user_id = "101"
        settings.bound_chat_id = None
        settings.bound_bot_id = None
        from tg_game.storage import Storage
        from tg_game.features.world_boss import world_boss_runtime as runtime
        from tg_game.features.world_boss import world_boss_support as support
        from tg_game.services.automation_switch import pause_automation, resume_automation
        from tg_game.services.profile_schedules import stop_current_profile_schedules

        storage = Storage(settings.database_path)
        storage.init_schema()
        profile = storage.create_profile("Boss test")
        other = storage.create_profile("Other profile")
        storage.bind_profile_telegram_account(profile.id, "101", "boss_test", telegram_session_name="unused")
        storage.create_chat_binding(profile.id, -100101, bot_username="fanrenxiuxian_bot")
        storage.create_chat_binding(other.id, -100101, bot_username="fanrenxiuxian_bot")
        assert not runtime.is_enabled(storage, profile.id)
        runtime.set_enabled(storage, profile.id, True)
        assert runtime.is_enabled(storage, profile.id)
        assert not runtime.is_enabled(storage, other.id)
        pause_automation(storage, now=time.time())
        assert not runtime.is_enabled(storage, profile.id)
        assert runtime.build_view(storage, profile.id)["paused"]
        resume_automation(storage)
        storage.set_runtime_state(f"profile_rebirth:{profile.id}", '{"active":true}')
        assert not runtime.is_enabled(storage, profile.id)
        storage.set_runtime_state(f"profile_rebirth:{profile.id}", '{}')
        storage.set_runtime_state(f"telegram_network_pause_until:{profile.id}", str(time.time() + 60))
        assert not runtime.is_enabled(storage, profile.id)
        storage.set_runtime_state(f"telegram_network_pause_until:{profile.id}", "0")

        actor = runtime._actor(None, storage, profile.id, SimpleNamespace(id=101, username="boss_test"))
        assert actor.is_world_boss_enabled()
        # The 5 s heartbeat writes only a timestamp; the ~0.5 MB state is
        # rewritten only to retry a save that failed.
        state_key, beat_key = runtime._state_key(profile.id), runtime._heartbeat_key(profile.id)
        real_set, failures = storage.set_runtime_state, [1]

        def flaky_set(key, value):
            if key == state_key and failures[0]:
                failures[0] -= 1
                raise sqlite3.OperationalError("unable to open database file")
            return real_set(key, value)

        with patch.object(storage, "set_runtime_state", side_effect=flaky_set) as written:
            runtime._heartbeat(storage, profile.id, actor)
            runtime._heartbeat(storage, profile.id, actor)
            assert [c.args[0] for c in written.call_args_list] == [beat_key] * 2, "Heartbeat rewrote the full state"
            try:
                actor.save_state()
                raise AssertionError("A failed state write was swallowed")
            except sqlite3.OperationalError:
                pass
            assert actor.has_unsaved_state()
            written.reset_mock()
            runtime._heartbeat(storage, profile.id, actor)
            runtime._heartbeat(storage, profile.id, actor)
            assert [c.args[0] for c in written.call_args_list] == [state_key, beat_key, beat_key], \
                "Heartbeat must retry a failed save exactly once"
        assert not actor.has_unsaved_state() and "heartbeat_at" not in runtime._read_state(storage, profile.id)
        assert time.time() - float(storage.get_runtime_state(beat_key)) < 5
        binding = storage.list_chat_bindings(profile.id)[0]
        storage.set_chat_binding_thread_id(profile.id, binding.chat_id, 123)
        assert not actor.is_world_boss_enabled(), "Old listener must stop as soon as its binding changes"
        actor = runtime._actor(None, storage, profile.id, SimpleNamespace(id=101, username="boss_test"))
        assert actor.is_world_boss_enabled()
        stop_current_profile_schedules(storage, profile.id)
        assert not actor.is_world_boss_enabled(), "Stop-all must include Boss tasks"
        runtime.set_enabled(storage, profile.id, True)
        actor = runtime._actor(None, storage, profile.id, SimpleNamespace(id=101, username="boss_test"))
        storage.bind_profile_telegram_account(profile.id, "102", "changed", telegram_session_name="changed")
        assert not actor.is_world_boss_enabled(), "A replaced account must not keep using the old Telegram client"
        storage.bind_profile_telegram_account(profile.id, "101", "boss_test", telegram_session_name="unused")

        # Exercise the real getter/ClosingConnection contract, including a
        # separate writer committing a stop between guard reads.
        actor = runtime._actor(None, storage, profile.id, SimpleNamespace(id=101))
        original_connect = storage.connect
        connections, queries = [], []
        hook = [None]

        def observe(sql):
            queries.append(sql)
            if hook[0] and hook[0][0] in sql:
                _, key, value = hook[0]
                hook[0] = None
                with original_connect() as writer:
                    writer.execute("UPDATE app_runtime_state SET value=? WHERE key=?", (value, key))

        def counted_connect():
            conn = original_connect()
            conn.set_trace_callback(observe)
            connections.append(conn)
            return conn

        def assert_closed():
            assert len(connections) == 1, len(connections)
            try:
                connections[0].execute("SELECT 1")
                raise AssertionError("Guard leaked its connection")
            except sqlite3.ProgrammingError:
                pass
            assert not any(sql.strip().upper() == "BEGIN" for sql in queries)

        storage.connect = counted_connect
        try:
            assert actor.is_world_boss_enabled()
            assert_closed()
            assert storage.connect is counted_connect
            for key, reset, stopped in (
                ("automation_paused_at", "0", "1"),
                (f"profile_rebirth:{profile.id}", "{}", '{"active":true}'),
                (f"telegram_network_pause_until:{profile.id}", "0", str(time.time() + 60)),
            ):
                with original_connect() as writer:
                    writer.execute("UPDATE app_runtime_state SET value=? WHERE key=?", (reset, key))
                connections.clear()
                queries.clear()
                hook[0] = (f"'{key}'", key, stopped)
                assert not actor.is_world_boss_enabled(), f"Missed mid-check stop: {key}"
                assert hook[0] is None
                assert_closed()
                connections.clear()
                assert not actor.is_world_boss_enabled(), "The next check reused stale state"
                assert_closed()
                with original_connect() as writer:
                    writer.execute("UPDATE app_runtime_state SET value=? WHERE key=?", (reset, key))

            original_get_profile = storage.get_profile
            def broken_get_profile(_):
                raise RuntimeError("offline read failure")
            storage.get_profile = broken_get_profile
            connections.clear()
            try:
                actor.is_world_boss_enabled()
                raise AssertionError("Read failure was ignored")
            except RuntimeError as exc:
                assert str(exc) == "offline read failure"
            finally:
                storage.get_profile = original_get_profile
            assert_closed()
        finally:
            storage.connect = original_connect
        storage.set_runtime_state("offline_guard_check", "write still allowed")
        assert actor.is_world_boss_enabled()

        async def checks():
            calls = []

            # Actual executor boundary, injected transport: no network. The
            # deterministic clock makes queue/transport/resume attribution exact.
            original_post, original_time = support._json_post_sync, support.time
            try:
                for fails in (False, True):
                    ticks = iter((100.0, 102.0, 107.0, 110.0))
                    support.time = SimpleNamespace(monotonic=lambda: next(ticks))
                    def transport(*args, timing=None):
                        timing["http_headers_wait_ms"] = 4000.0
                        if fails:
                            raise support.MiniAppBeastError("server_error", 503)
                        return {"ok": True}
                    support._json_post_sync = transport
                    timing = {}
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        try:
                            result = await support._post_json(support.ORIGIN, support.API_PREFIX + "window",
                                                              {}, 1, executor=executor, timing=timing)
                            assert not fails and result["ok"]
                        except support.MiniAppBeastError as exc:
                            assert fails and exc.code == "server_error"
                    assert timing == {"executor_queue_ms": 2000.0, "transport_ms": 5000.0,
                                      "loop_resume_ms": 3000.0, "http_headers_wait_ms": 4000.0}, timing

                support.time = original_time
                started, release = threading.Event(), threading.Event()
                def delayed_transport(*args, timing=None):
                    started.set()
                    assert release.wait(3)
                    timing["http_headers_wait_ms"] = 500
                    return {"ok": True}
                support._json_post_sync = delayed_transport
                timing = {}
                with ThreadPoolExecutor(max_workers=1) as executor:
                    task = asyncio.create_task(support._post_json(
                        support.ORIGIN, support.API_PREFIX + "window", {}, 1,
                        executor=executor, timing=timing))
                    try:
                        assert await asyncio.to_thread(started.wait, 2)
                        task.cancel()
                        result = await asyncio.gather(task, return_exceptions=True)
                        assert isinstance(result[0], asyncio.CancelledError)
                        frozen = dict(timing)
                        assert "transport_ms" not in frozen and "loop_resume_ms" not in frozen
                    finally:
                        release.set()
                assert timing == frozen, "A cancelled worker changed the saved diagnostics"
            finally:
                support._json_post_sync, support.time = original_post, original_time

            async def fake_post(origin, path, payload, timeout):
                calls.append(path)
                return {"ok": True, "result": {"score": 50}}

            for origin, path in [("https://example.com", support.API_PREFIX + "start"),
                                 (support.ORIGIN, "/api/another-game/start")]:
                try:
                    await support._post_json(origin, path, {}, 5, post_json=fake_post)
                    raise AssertionError("Unexpected credential destination accepted")
                except support.MiniAppBeastError:
                    pass
            assert not calls
            await support._post_json(support.ORIGIN, support.API_PREFIX + "finish", {}, 5, post_json=fake_post)
            assert len(calls) == 1

            if support.httpx is not None:
                import httpx

                def handler(request: httpx.Request) -> httpx.Response:
                    if request.url.host != "asc.aiopenai.app":
                        return httpx.Response(400, json={"ok": False, "error": "origin_not_allowed"})
                    if request.url.path.endswith("/start"):
                        return httpx.Response(302, headers={"Location": "https://example.com/steal"})
                    if request.url.path.endswith("/hit"):
                        return httpx.Response(409, json={"ok": False, "error": "boss_hit_outside_window"})
                    return httpx.Response(200, json={"ok": True, "result": {"score": 1}})

                pooled = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
                try:
                    try:
                        support._json_post_with_client(
                            pooled, support.ORIGIN, support.API_PREFIX + "start", {}, 1,
                        )
                        raise AssertionError("Redirect was followed or accepted")
                    except support.MiniAppBeastError as exc:
                        assert exc.code == "api_redirect_rejected"
                    try:
                        support._json_post_with_client(
                            pooled, support.ORIGIN, support.API_PREFIX + "hit", {}, 1,
                        )
                        raise AssertionError("HTTP error body was not mapped")
                    except support.MiniAppBeastError as exc:
                        assert exc.code == "boss_hit_outside_window" and exc.status == 409
                    payload = support._json_post_with_client(
                        pooled, support.ORIGIN, support.API_PREFIX + "finish", {}, 1,
                    )
                    assert payload["ok"] is True
                    try:
                        support._json_post_with_client(
                            pooled, "https://example.com", support.API_PREFIX + "finish", {}, 1,
                        )
                        raise AssertionError("Foreign origin accepted")
                    except support.MiniAppBeastError as exc:
                        assert exc.code == "origin_not_allowed"
                finally:
                    pooled.close()
                # httpcore's trace hook is per request. Keep only stage durations,
                # including failed stages; never retain the supplied trace info.
                for fail_headers in (False, True):
                    elapsed, timing = [0.0], {}
                    def traced_handler(request):
                        callback = request.extensions["trace"]
                        elapsed[0] += .007
                        for phase, duration in (("connect_tcp", .003), ("start_tls", .005),
                                                ("send_request_headers", .001), ("send_request_body", .002),
                                                ("receive_response_headers", .4), ("receive_response_body", .01)):
                            prefix = "connection" if phase in {"connect_tcp", "start_tls"} else "http11"
                            callback(f"{prefix}.{phase}.started", {"token": "private"})
                            elapsed[0] += duration
                            failed = fail_headers and phase == "receive_response_headers"
                            callback(f"{prefix}.{phase}.{'failed' if failed else 'complete'}", {"cookie": "private"})
                            if failed:
                                raise httpx.ReadTimeout("offline timeout")
                        return httpx.Response(200, json={"ok": True})
                    with httpx.Client(transport=httpx.MockTransport(traced_handler)) as client:
                        with patch.object(support, "time", SimpleNamespace(monotonic=lambda: elapsed[0])):
                            try:
                                support._json_post_with_client(client, support.ORIGIN, support.API_PREFIX + "window", {}, 1, timing=timing)
                                assert not fail_headers
                            except support.MiniAppBeastError as exc:
                                assert fail_headers and exc.code == "api_timeout"
                    assert timing["http_pool_dispatch_ms"] == 7 and timing["http_connect_ms"] == 3
                    assert timing["http_tls_ms"] == 5 and timing["http_write_ms"] == 3
                    assert timing["http_headers_wait_ms"] == 400 and "private" not in str(timing)
                    assert ("http_body_read_ms" in timing) is not fail_headers
            try:
                await support._post_json(support.ORIGIN, support.API_PREFIX + "finish", {}, 5,
                                         post_json=lambda *args: {"ok": "true"})
                raise AssertionError("Non-boolean success accepted")
            except support.MiniAppBeastError:
                pass

            init_data = urlencode({"user": '{"id":101,"first_name":"A&B+道友"}', "hash": "offline"})

            class Client:
                _tg_game_profile_id = profile.id

                def __init__(self):
                    self.handlers = []
                    self.url = support.ORIGIN + support.WEB_PATH + "#" + urlencode({"tgWebAppData": init_data})

                async def get_input_entity(self, value):
                    from telethon.tl.types import InputPeerUser
                    return InputPeerUser(101, 1)

                async def __call__(self, request):
                    assert request.start_param == "qyz_offline"
                    return SimpleNamespace(url=self.url)

                async def get_me(self):
                    return SimpleNamespace(id=101, username="boss_test")

                async def get_messages(self, *args, **kwargs):
                    return []

                def add_event_handler(self, handler, event):
                    self.handlers.append(handler)

                def remove_event_handler(self, handler):
                    self.handlers.remove(handler)

            client = Client()
            assert await support.request_webview_init_data(client, "fanrenxiuxian_bot", "qyz_offline") == init_data
            client.url = "https://example.com/miniapp/xianxia-world-boss#tgWebAppData=offline"
            try:
                await support.request_webview_init_data(client, "fanrenxiuxian_bot", "qyz_offline")
                raise AssertionError("Foreign WebView accepted")
            except support.MiniAppBeastError:
                pass

            storage.set_runtime_state(runtime._heartbeat_key(profile.id), "0")
            task = asyncio.create_task(runtime.run_monitor(client, storage))
            for _ in range(50):
                if len(client.handlers) == 2 and runtime.build_view(storage, profile.id)["monitoring"]:
                    break
                await asyncio.sleep(0.02)
            assert len(client.handlers) == 2
            assert runtime.build_view(storage, profile.id)["monitoring"], "First loop pass must write the heartbeat"
            task.cancel()
            done, pending = await asyncio.wait({task}, timeout=2)
            if pending:
                task.cancel()
                await asyncio.wait({task}, timeout=2)
                raise AssertionError("Monitor swallowed shutdown cancellation")
            await asyncio.gather(task, return_exceptions=True)
            assert not client.handlers
            assert not runtime.build_view(storage, profile.id)["monitoring"]

        asyncio.run(checks())
    print("test_world_boss_runtime: passed (no network, temporary database)")


if __name__ == "__main__":
    main()
