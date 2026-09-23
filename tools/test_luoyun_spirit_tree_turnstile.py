"""灵眼赛开局被盾挡时，带上浏览器队列给出的 token 再打一次。

run: PYTHONPATH=app/src python tools/test_luoyun_spirit_tree_turnstile.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))

from tg_game.features.luoyun_spirit_tree import (  # noqa: E402
    biz_luoyun_spirit_tree_miniapp as tree,
)
from tg_game.features.world_boss import world_boss_turnstile as queue  # noqa: E402


def transport_factory(fail_status=403, fail_body=None):
    calls = []
    if fail_body is None:
        fail_body = json.dumps({"ok": False, "error": "turnstile_failed"})

    def transport(request):
        payload = dict(request.get("payload") or {})
        calls.append(payload)
        if payload.get("turnstileToken"):
            return 200, json.dumps({"ok": True, "data": {"score": 11}})
        return fail_status, fail_body

    return transport, calls


def _request() -> dict:
    return {"payload": {"mode": "fly"}, "url": "https://asc.aiopenai.app/x", "method": "POST"}


def main() -> None:
    transport, calls = transport_factory()
    failed = tree.execute_luoyun_spirit_tree_miniapp_request_with_turnstile(
        _request(), transport
    )
    assert not failed.get("ok") and failed.get("error") == "turnstile_failed"
    assert "turnstileToken" not in calls[0]

    seen = []
    transport, calls = transport_factory()
    ok = tree.execute_luoyun_spirit_tree_miniapp_request_with_turnstile(
        _request(),
        transport,
        acquire_token=lambda mode="": seen.append(mode) or "offline-token",
    )
    assert ok.get("ok") is True, ok
    assert seen == ["fly"]
    assert calls[0].get("turnstileToken") is None
    assert calls[1]["turnstileToken"] == "offline-token"
    assert calls[1]["turnstileIdempotencyKey"]

    transport, calls = transport_factory(502, "<html>Bad Gateway</html>")
    edge = tree.execute_luoyun_spirit_tree_miniapp_request_with_turnstile(
        _request(), transport, acquire_token=lambda mode="": "offline-token"
    )
    assert edge.get("ok") is True, edge
    assert calls[1]["turnstileToken"] == "offline-token"

    assert tree.LUOYUN_SPIRIT_TREE_TURNSTILE_ACTIONS["fly"] == "luoyun_spirit_tree_fly_begin"
    assert tree.LUOYUN_SPIRIT_TREE_TURNSTILE_ACTIONS["jump"] == "luoyun_spirit_tree_jump_begin"
    assert (
        queue.normalize_turnstile_page_path("/miniapp/xianxia-spirit-tree")
        == "/miniapp/xianxia-spirit-tree"
    )
    assert queue.normalize_turnstile_action("luoyun_spirit_tree_fly_begin") == (
        "luoyun_spirit_tree_fly_begin"
    )
    assert queue.normalize_turnstile_action("bad action") == ""

    slept = []
    tree._wait_for_claimed_duration({"durationMs": 1500}, time.time() - 0.2, slept.append)
    assert len(slept) == 1 and 1.2 <= slept[0] <= 1.4
    tree._wait_for_claimed_duration({"durationMs": 100}, time.time() - 1, slept.append)
    assert len(slept) == 1
    print("luoyun spirit tree turnstile: ok")


if __name__ == "__main__":
    main()
