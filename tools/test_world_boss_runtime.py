"""Offline checks for Boss profile scope, shutdown, and credential boundaries.

Run: python -B tools/test_world_boss_runtime.py
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

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

        async def checks():
            calls = []

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

            task = asyncio.create_task(runtime.run_monitor(client, storage))
            for _ in range(50):
                if len(client.handlers) == 2:
                    break
                await asyncio.sleep(0.02)
            assert len(client.handlers) == 2
            assert runtime.build_view(storage, profile.id)["monitoring"]
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
