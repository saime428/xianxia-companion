"""Regression checks for real send timing, late tickets and per-window jitter.

python tools/test_world_boss_reliability.py
No Telegram or real HTTP is used. Temporary files follow the platform temp dir.
"""
import asyncio
import logging
import random
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from email.utils import format_datetime

from test_world_boss_combat import Clock, ENTRY, boss, monitor_for, window
import test_world_boss_timing_replay as replay
from tg_game.features.world_boss.world_boss_combat_loop import run_combat_loop
from tg_game.features.world_boss.world_boss_timing import CombatTiming
from tg_game.features.world_boss import world_boss_support as support


class RealtimeServer:
    def __init__(self):
        self.started = threading.Event()
        self.starts_at = 0.0
        self.charged_at = 0.0
        self.hit_at = None
        self.challenge = {
            "challengeId": "offline-realtime", "durationMs": 2200,
            "maxDurationMs": 2200, "phase": 1,
            "windows": [window(0, 1600)], "windowCount": 1,
        }

    async def post(self, origin, path, payload, timeout):
        endpoint = path.rsplit("/", 1)[-1]
        if endpoint == "begin":
            await asyncio.sleep(.02)
            self.starts_at = time.monotonic() + .1
            self.started.set()
            return {"ok": True, "startsInMs": 100}
        if endpoint == "charge-start":
            await asyncio.sleep(.005)
            self.charged_at = time.monotonic()
            return {"ok": True, "chargeTicket": "offline-ticket"}
        if endpoint == "hit":
            await asyncio.sleep(.005)
            self.hit_at = time.monotonic()
            delta = abs((self.hit_at - self.starts_at) * 1000 - 1600)
            hold = (self.hit_at - self.charged_at) * 1000
            return {"ok": True, "hit": {
                "damageYi": 2, "deltaMs": delta, "holdMs": hold,
                "perfect": delta <= 150 and 520 <= hold <= 1250,
            }}
        if endpoint == "finish":
            return {"ok": True, "result": {"grade": "A", "score": 100, "player_hp": 100}}
        raise AssertionError(endpoint)


class ReliabilityChecks(unittest.IsolatedAsyncioTestCase):
    async def test_slow_hit_followed_by_slow_charge_keeps_recovery_margin(self):
        # The first late hit must not cut the next hold to ~730 ms. A slow
        # charge then leaves too little ticket age, even when its hit is on time.
        for seed in range(20):
            clock = Clock()
            server = replay.TimedServer(clock)
            server.starts_at = 0
            server.windows = [dict(w, perfectMs=210) for w in server.windows]

            async def post(origin, path, payload, timeout):
                if path.endswith('/hit') and payload['windowId'] == 'w0':
                    await clock.sleep(.45)
                if path.endswith('/charge-start') and payload['windowId'] != 'w0':
                    await clock.sleep(.60)
                return await server.post(origin, path, payload, timeout)

            with tempfile.TemporaryDirectory() as directory:
                monitor, _ = monitor_for(Path(directory), clock)
                monitor.post_json = post
                monitor._timing = CombatTiming(rng=random.Random(seed))
                monitor._start_combat(server.challenge, {'maxHp': 100}, 0, server.windows)
                try:
                    for index, target in enumerate(server.windows):
                        result = await clock.run(monitor._hit_window(
                            ENTRY, 'offline', 'offline', 'offline', 0, target, index))
                        if index:
                            self.assertTrue(result['accepted_perfect'], (seed, index, result['diagnostic']))
                        self.assertEqual(result['action']['holdMs'],
                                         result['diagnostic']['sent_elapsed_ms']
                                         - result['diagnostic']['charge']['requested_elapsed_ms'])
                finally:
                    await monitor.stop()

    async def test_repeated_hold_skew_still_reduces_overcharging(self):
        for seed in range(10):
            clock = Clock()
            server = replay.TimedServer(clock)
            server.starts_at = 0
            server.windows = [dict(window(i, 2000 + 3500 * i), perfectMs=210) for i in range(8)]

            async def post(origin, path, payload, timeout):
                if path.endswith('/hit'):
                    await clock.sleep(.18)
                return await server.post(origin, path, payload, timeout)

            with tempfile.TemporaryDirectory() as directory:
                monitor, _ = monitor_for(Path(directory), clock)
                monitor.post_json = post
                monitor._timing = CombatTiming(rng=random.Random(seed))
                monitor._start_combat(server.challenge, {'maxHp': 100}, 0, server.windows)
                try:
                    for index, target in enumerate(server.windows):
                        result = await clock.run(monitor._hit_window(
                            ENTRY, 'offline', 'offline', 'offline', 0, target, index))
                        if index >= 1:
                            self.assertGreaterEqual(result['diagnostic']['server_hold_ms'], 520)
                            self.assertLessEqual(result['diagnostic']['server_hold_ms'], 1250)
                finally:
                    await monitor.stop()

    async def test_repeated_extreme_skew_is_confirmed_and_learned(self):
        clock = Clock()
        server = replay.TimedServer(clock)
        server.starts_at = 0
        server.windows = [dict(window(i, 2000 + 3500 * i), perfectMs=210) for i in range(8)]

        async def post(origin, path, payload, timeout):
            if path.endswith('/hit'):
                await clock.sleep(.40)
            return await server.post(origin, path, payload, timeout)

        with tempfile.TemporaryDirectory() as directory:
            monitor, _ = monitor_for(Path(directory), clock)
            monitor.post_json = post
            monitor._timing = CombatTiming(rng=random.Random(0))
            monitor._start_combat(server.challenge, {'maxHp': 100}, 0, server.windows)
            hits = []
            try:
                for index, target in enumerate(server.windows):
                    result = await clock.run(monitor._hit_window(
                        ENTRY, 'offline', 'offline', 'offline', 0, target, index))
                    hits.append(result['diagnostic'])
                self.assertEqual(hits[0]['hold_feedback'], 'late_hit_spike_pending')
                self.assertEqual(hits[1]['hold_feedback'], 'repeated_late_hit_skew')
                self.assertTrue(all(520 <= h['server_hold_ms'] <= 1250 for h in hits[2:]), hits)
                monitor._reset_hold_skew()
                self.assertFalse(monitor._pending_hold_spike)
            finally:
                await monitor.stop()

    async def test_token_replacement_does_not_bypass_retry_after(self):
        clock, calls = Clock(), []
        delay = 3

        async def post(*args):
            calls.append(clock.now)
            if len(calls) == 1:
                raise boss.MiniAppBeastError("turnstile_required", 403)
            if len(calls) == 2:
                raise boss.MiniAppBeastError("server_busy", 429, retry_after=delay)
            return {"ok": True, "startsInMs": 100}

        async def token(*args):
            await clock.sleep(.1)
            return "offline-token", ""

        with tempfile.TemporaryDirectory() as directory:
            monitor, _ = monitor_for(Path(directory), clock)
            monitor.post_json = post
            monitor._wait_for_turnstile_token = token
            try:
                await clock.run(monitor._begin_with_turnstile(ENTRY, "offline", "offline", "offline", "主魂"))
                self.assertEqual(len(calls), 3)
                self.assertGreaterEqual(calls[2] - calls[1], 3)
                calls.clear()
                delay = 30
                with self.assertRaises(boss.MiniAppBeastError):
                    await clock.run(monitor._begin_with_turnstile(ENTRY, "offline", "offline", "offline", "主魂"))
                self.assertEqual(len(calls), 2, "Long cooldown was bypassed by another token")
            finally:
                await monitor.stop()

    async def test_first_latency_spike_does_not_poison_following_windows(self):
        for seed in range(20):
            clock = Clock()
            server = replay.TimedServer(clock)
            server.starts_at = 0
            server.windows = [dict(w, perfectMs=210) for w in server.windows]
            hit_count = 0

            async def post(origin, path, payload, timeout):
                nonlocal hit_count
                if path.endswith("/hit"):
                    hit_count += 1
                    if hit_count == 1:
                        await clock.sleep(.45)
                return await server.post(origin, path, payload, timeout)

            with tempfile.TemporaryDirectory() as directory:
                monitor, _ = monitor_for(Path(directory), clock)
                monitor.post_json = post
                monitor._timing = CombatTiming(rng=random.Random(seed))
                monitor._start_combat(server.challenge, {"maxHp": 100}, 0, server.windows)
                try:
                    with patch.object(boss, "WORLD_BOSS_HOLD_MS", 1000):
                        for index, target in enumerate(server.windows):
                            result = await clock.run(monitor._hit_window(ENTRY, "offline", "offline", "offline", 0, target))
                            if index:
                                self.assertTrue(result["accepted_perfect"], (seed, index, result["diagnostic"]))
                finally:
                    await monitor.stop()

    async def test_retry_honors_server_delay_and_request_budget(self):
        for delay, expected in ((.8, 2), (5, 1)):
            clock, calls = Clock(), []

            async def post(*args):
                calls.append(clock.now)
                if len(calls) == 1:
                    error = boss.MiniAppBeastError("server_busy", 429)
                    error.retry_after = delay
                    raise error
                return {"ok": True}

            with tempfile.TemporaryDirectory() as directory:
                monitor, _ = monitor_for(Path(directory), clock)
                monitor.post_json = post
                try:
                    operation = monitor._request(ENTRY.origin, support.API_PREFIX + "window", {}, retries=1, timeout=2)
                    if expected == 1:
                        with self.assertRaises(boss.MiniAppBeastError):
                            await clock.run(operation)
                    else:
                        await clock.run(operation)
                        self.assertGreaterEqual(calls[1] - calls[0], delay)
                    self.assertEqual(len(calls), expected)
                finally:
                    await monitor.stop()

    async def test_hit_gateway_failure_is_not_replayed(self):
        clock, hits = Clock(), []
        server = replay.TimedServer(clock)
        server.starts_at = 0

        async def post(origin, path, payload, timeout):
            if path.endswith("/hit"):
                hits.append(dict(payload))
                await clock.sleep(.02)
                raise boss.MiniAppBeastError("request_failed", 502)
            return await server.post(origin, path, payload, timeout)

        with tempfile.TemporaryDirectory() as directory:
            monitor, _ = monitor_for(Path(directory), clock)
            monitor.post_json = post
            monitor._start_combat(server.challenge, {"maxHp": 100}, 0, server.windows)
            try:
                result = await clock.run(monitor._hit_window(ENTRY, "offline", "offline", "offline", 0, server.windows[0]))
                self.assertFalse(result["ok"])
                self.assertEqual(len(hits), 1)
            finally:
                await monitor.stop()

    async def test_cancellation_joins_private_loop_cleanup(self):
        started, finished = threading.Event(), threading.Event()

        async def operation():
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                await asyncio.sleep(.03)
                finished.set()

        task = asyncio.create_task(run_combat_loop(operation))
        while not started.is_set():
            await asyncio.sleep(.001)
        task.cancel()
        await asyncio.sleep(.005)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set(), "Caller escaped while the battle was still active")

    async def test_database_stop_is_checked_inside_private_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            template, enabled = monitor_for(Path(directory), Clock())
            server = RealtimeServer()
            monitor = boss.WorldBossMonitor(template.actor, "offline-stop", post_json=server.post,
                                             turnstile_broker=template.turnstile_broker)
            task = asyncio.create_task(monitor._fight(ENTRY, "offline", "offline", {
                "challenge": server.challenge, "player": {"maxHp": 100},
            }))
            try:
                while not server.started.is_set():
                    await asyncio.sleep(.005)
                enabled["value"] = False
                with self.assertRaises(boss._WorldBossDisabledError):
                    await asyncio.wait_for(task, 1)
                self.assertIsNone(server.hit_at)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await monitor.stop()

    async def delayed_charge(self, delay, base_hold, *, returning=False):
        clock = Clock()
        server = replay.TimedServer(clock)
        server.starts_at = 0
        target = dict(server.windows[0], perfectMs=210)
        server.windows = [target]

        async def post(origin, path, payload, timeout):
            charge = path.endswith("/charge-start")
            if charge and not returning:
                await clock.sleep(delay)
            result = await server.post(origin, path, payload, timeout)
            if charge and returning:
                await clock.sleep(delay)
            return result

        with tempfile.TemporaryDirectory() as directory:
            monitor, _ = monitor_for(Path(directory), clock, server)
            monitor.post_json = post
            monitor._start_combat(server.challenge, {"maxHp": 100}, 0, [target])
            try:
                with patch.object(boss, "WORLD_BOSS_HOLD_MS", base_hold):
                    result = await clock.run(monitor._hit_window(ENTRY, "offline", "offline", "offline", 0, target))
            finally:
                await monitor.stop()
        hit = result["diagnostic"]
        self.assertEqual(hit["hold_ms"], hit["actual_elapsed_ms"] - hit["charge"]["requested_elapsed_ms"])
        return hit

    async def test_late_ticket_uses_remaining_perfect_window(self):
        for returning in (False, True):
            hit = await self.delayed_charge(.600, 1000, returning=returning)
            self.assertTrue(hit["accepted_perfect"], hit)
            self.assertGreater(hit["late_charge_adjustment_ms"], 0)

    async def test_uncertain_ticket_does_not_overcharge_slow_response(self):
        for returning in (False, True):
            hit = await self.delayed_charge(.750, 1180, returning=returning)
            self.assertEqual(hit["late_charge_adjustment_ms"], 0, hit)
            self.assertEqual(hit["accepted_perfect"], returning, hit)
            self.assertEqual(hit["charge_recovery"], "no_safe_interval")

    async def test_main_event_loop_stall_does_not_delay_combat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template, _ = monitor_for(root, Clock())
            template.actor.config["world_boss"]["timing_jitter"] = False
            server = RealtimeServer()
            monitor = boss.WorldBossMonitor(
                template.actor, "offline-isolated", post_json=server.post,
                turnstile_broker=template.turnstile_broker,
            )
            task = asyncio.create_task(monitor._fight(
                ENTRY, "offline-init", "offline-session",
                {"challenge": server.challenge, "player": {"maxHp": 100}},
            ))
            try:
                while not server.started.is_set():
                    await asyncio.sleep(.005)
                await asyncio.sleep(max(0, server.starts_at + 1.50 - time.monotonic()))
                # Reproduce a synchronous task blocking the Telegram event loop.
                time.sleep(.35)
                result = await task
                self.assertEqual(result["perfect_count"], 1, result["diagnostics"]["hits"])
                self.assertLess(result["diagnostics"]["hits"][0]["wake_lateness_ms"], 100)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await monitor.stop()

    async def test_jittered_battle_preserves_perfect_hits_and_truthful_times(self):
        clock = Clock()
        server = replay.TimedServer(clock, reveal_lead_ms=1500)
        server.windows = [window(i, 2000 + 3500 * i) for i in range(16)]
        server.challenge.update(windowCount=16, durationMs=60000, maxDurationMs=60000)
        with tempfile.TemporaryDirectory() as directory:
            monitor, _ = monitor_for(Path(directory), clock, server)
            monitor._timing_jitter = True
            monitor._timing_rng = random.Random(42)
            monitor.post_json = None

            async def post(origin, path, payload, timeout, **kwargs):
                if path.endswith("/begin"):
                    server.starts_at = clock.now + .110
                    await clock.sleep(.020)
                    kwargs["timing"].update(transport_ms=20, loop_resume_ms=0, executor_queue_ms=0)
                    return {"ok": True, "startsInMs": 100}
                return await server.post(origin, path, payload, timeout)

            original = CombatTiming.plan
            with patch.object(boss, "_post_json", post), \
                    patch.object(CombatTiming, "plan", autospec=True, side_effect=original) as plan:
                result = await clock.run(monitor._run_identity(ENTRY, "主魂", "offline"))
            await monitor.stop()
        hits = result["diagnostics"]["hits"]
        self.assertEqual(plan.call_count, 16, "Replanning a strike consumed new random draws")
        self.assertEqual(result["perfect_count"], 16, hits)
        self.assertGreater(len({h["timing_offset_ms"] for h in hits}), 8)
        self.assertGreater(len({h["planned_hold_ms"] for h in hits}), 8)
        self.assertLess(abs(result["diagnostics"]["strategy"]["drift_lead_ms"]), 10,
                        "The network estimator learned the intentional action offset")
        for hit, action in zip(hits, server.proof["actions"]):
            self.assertEqual(hit["actual_elapsed_ms"], hit["sent_elapsed_ms"])
            self.assertEqual(action["t"], hit["sent_elapsed_ms"])
            self.assertEqual(action["holdMs"], hit["sent_elapsed_ms"] - hit["charge"]["requested_elapsed_ms"])


class DistributionChecks(unittest.TestCase):
    def test_variable_extreme_skew_does_not_remain_pending_forever(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, _ = monitor_for(Path(directory), Clock())
            feedback = []
            for error in (400, 550) * 5:
                _, state = monitor._observe_hold_feedback(
                    1000 + error, 1000, late_delta_ms=400, perfect_ms=210)
                feedback.append(state)
            self.assertEqual(feedback[0], 'late_hit_spike_pending')
            self.assertTrue(all(state == 'repeated_late_hit_skew' for state in feedback[1:]))
            self.assertGreater(monitor._hold_skew_ms, 300)
            monitor._observe_hold_feedback(1000, 1000, late_delta_ms=None, perfect_ms=210)
            self.assertFalse(monitor._pending_hold_spike)
            _, state = monitor._observe_hold_feedback(1500, 1000, late_delta_ms=400, perfect_ms=210)
            self.assertEqual(state, 'late_hit_spike_pending')

    def test_reentering_same_battle_does_not_clear_concurrent_death(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, _ = monitor_for(Path(directory), Clock())
            monitor._prepare_boss_lifecycle(ENTRY)
            stopped = monitor._boss_defeated
            original_is_set = stopped.is_set

            def concurrent_death():
                # Another combat loop learns of death just after a false read.
                stopped.set()
                return False

            with patch.object(stopped, "is_set", side_effect=concurrent_death):
                monitor._prepare_boss_lifecycle(ENTRY)
            self.assertTrue(original_is_set(), "Re-entering the same battle lost the shared stop signal")

    def test_retry_after_from_http_headers(self):
        import httpx
        cases = [("2", 2), ("invalid", 0), ("NaN", 0), ("inf", 0), ("-1", 0),
                 (format_datetime(datetime.fromtimestamp(1030, timezone.utc), usegmt=True), 30)]
        for value, expected in cases:
            def handler(request):
                return httpx.Response(429, headers={"Retry-After": value}, json={"ok": False, "error": "server_busy"})
            with httpx.Client(transport=httpx.MockTransport(handler)) as client, patch.object(support.time, "time", return_value=1000):
                with self.assertRaises(boss.MiniAppBeastError) as caught:
                    support._json_post_with_client(client, support.ORIGIN, support.API_PREFIX + "window", {}, 1)
                self.assertEqual(caught.exception.retry_after, expected)

    def test_bounded_variation_and_reproducibility(self):
        first, second = CombatTiming(rng=random.Random(4)), CombatTiming(rng=random.Random(4))
        plans = [first.plan(210, 1180) for _ in range(1000)]
        self.assertEqual(plans, [second.plan(210, 1180) for _ in range(1000)])
        self.assertTrue(all(abs(p.offset_ms) <= 116 and 560 <= p.hold_ms <= 1150 for p in plans))
        self.assertLess(sum(abs(p.offset_ms) <= 40 for p in plans) / len(plans), .75)
        self.assertGreater(max(p.hold_ms for p in plans) - min(p.hold_ms for p in plans), 80)
        self.assertTrue(all(.36 <= first.poll_delay() <= .45 for _ in range(100)))

    def test_observed_hold_variability_reduces_upper_target(self):
        stable, variable = CombatTiming(rng=random.Random(1)), CombatTiming(rng=random.Random(1))
        for error in (0, 180, 240, 200):
            variable.observe_hold(1000 + error, 1000)
        a, b = stable.plan(210, 1180), variable.plan(210, 1180)
        self.assertGreater(b.hold_reserve_ms, a.hold_reserve_ms)
        self.assertLess(b.hold_ms, a.hold_ms)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
