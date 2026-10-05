"""Exercise timing policy against asymmetric upload/return latency, without HTTP."""
import asyncio
import logging
from pathlib import Path
import random
import tempfile
import unittest

from test_world_boss_combat import Clock, ENTRY, monitor_for
from tg_game.features.world_boss.world_boss_timing import CombatTiming, WindowPlan


class LatencyServer:
    def __init__(self, clock, windows, delays):
        self.clock, self.windows, self.delays = clock, windows, delays
        self.charged, self.requests = {}, []

    async def post(self, origin, path, payload, timeout):
        endpoint = path.rsplit('/', 1)[-1]
        index = next(
            i for i, window in enumerate(self.windows)
            if window['id'] == payload['windowId']
        )
        before, after = self.delays[index][endpoint]
        self.requests.append((endpoint, index))
        await self.clock.sleep(before / 1000)
        if endpoint == 'charge-start':
            self.charged[index] = self.clock.now
            result = {'ok': True, 'chargeTicket': 'offline-ticket'}
        else:
            window = self.windows[index]
            delta = abs(self.clock.now * 1000 - window['centerMs'])
            hold = (self.clock.now - self.charged[index]) * 1000
            result = {
                'ok': True,
                'hit': {
                    'damageYi': int(delta <= window['hitMs']),
                    'deltaMs': delta,
                    'holdMs': hold,
                    'perfect': delta <= window['perfectMs'] and 520 <= hold <= 1250,
                },
            }
        await self.clock.sleep(after / 1000)
        if endpoint == 'hit' and self.delays[index].get('unconfirmed'):
            return {'ok': True}
        return result


async def run_case(
    delays, *, seed=0, windows=None, ready=None, lead=10,
    recorded_plans=None, concurrent=False,
):
    clock = Clock()
    windows = windows or [
        dict(id=f'w{i}', centerMs=2500 + 5000 * i, hitMs=620, perfectMs=210)
        for i in range(len(delays))
    ]
    server = LatencyServer(clock, windows, delays)
    with tempfile.TemporaryDirectory(prefix='boss-latency-') as directory:
        monitor, _ = monitor_for(Path(directory), clock, server)
        monitor._timing = CombatTiming(rng=random.Random(seed))
        if recorded_plans:
            plans = iter(recorded_plans)
            monitor._timing.plan = lambda *args: next(plans)
        monitor._start_combat(
            {'maxDurationMs': windows[-1]['centerMs'] + 5000},
            {'maxHp': 1000}, 0, windows,
        )
        hits = []
        try:
            async def act(i, window):
                if ready:
                    await clock.sleep(max(0, ready[i] / 1000 - clock.now))
                return await monitor._hit_window(
                    ENTRY, 'offline', 'offline', 'offline', 0, window, i + 1, lead,
                )
            if concurrent:
                async def together():
                    return await asyncio.gather(
                        *(act(i, window) for i, window in enumerate(windows))
                    )
                results = await clock.run(together())
            else:
                results = []
                for i, window in enumerate(windows):
                    results.append(await clock.run(act(i, window)))
            for result in results:
                diagnostic = result['diagnostic']
                diagnostic['damage'] = result['damage']
                hits.append(diagnostic)
                assert result['action']['t'] == diagnostic['sent_elapsed_ms']
                assert result['action']['holdMs'] == (
                    diagnostic['sent_elapsed_ms']
                    - diagnostic['charge']['requested_elapsed_ms']
                )
            assert len(server.requests) == 2 * len(windows), server.requests
            return hits
        finally:
            await monitor.stop()


def delay(charge=10, hit=10, charge_return=10, hit_return=10):
    return {'charge-start': (charge, charge_return), 'hit': (hit, hit_return)}


def summary(hits):
    return [
        (h['sequence'], h.get('accepted_perfect'), round(h.get('server_hold_ms', 0)),
         round((h.get('server_hit') or {}).get('deltaMs', 0)))
        for h in hits
    ]


class LatencyPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_charge_return_plus_delayed_hit_keeps_original_release(self):
        # A late response doesn't prove that charging started late. Waiting
        # longer here would turn a perfect hit into both an overhold and a miss.
        hits = await run_case(
            [delay(charge_return=700, hit=190)],
            recorded_plans=[WindowPlan(0, 1050, 100)],
        )
        self.assertTrue(hits[0]['accepted_perfect'], summary(hits))
        self.assertEqual(hits[0]['late_charge_adjustment_ms'], 0)

    async def test_one_spike_does_not_spoil_following_normal_hits(self):
        for seed in range(10):
            hits = await run_case(
                [delay(hit=550), *[delay() for _ in range(5)]], seed=seed,
            )
            self.assertTrue(
                all(h['accepted_perfect'] for h in hits[1:]), (seed, summary(hits)),
            )
            self.assertTrue(all(h['damage'] > 0 for h in hits), (seed, summary(hits)))

    async def test_sustained_delay_adapts_across_narrow_phase_then_recovers(self):
        samples = [
            *[delay(charge=300, hit=300, charge_return=300, hit_return=300) for _ in range(7)],
            *[delay() for _ in range(3)],
        ]
        windows = [
            dict(id=f'w{i}', centerMs=2500 + 5000 * i, hitMs=620,
                 perfectMs=210 if i < 3 else 150)
            for i in range(10)
        ]
        hits = await run_case(
            samples, windows=windows, recorded_plans=[WindowPlan(0, 1100, 100)] * 10,
        )
        self.assertEqual(hits[2]['arrival_inference']['update_mode'], 'sustained_latency')
        self.assertTrue(all(h['accepted_perfect'] for h in hits[3:7]), summary(hits))
        self.assertTrue(all(h['damage'] > 0 for h in hits), summary(hits))
        # The first faster request is unknowable in advance; feedback must
        # recover instead of promising that every phase-change hit is perfect.
        self.assertTrue(hits[-1]['accepted_perfect'], summary(hits))

    async def test_unconfirmed_window_breaks_consecutive_samples(self):
        samples = [
            delay(charge=300, hit=300, charge_return=300, hit_return=300)
            for _ in range(6)
        ]
        samples[2]['unconfirmed'] = True
        hits = await run_case(samples, recorded_plans=[WindowPlan(0, 1100, 100)] * 6)
        self.assertNotEqual(hits[3]['arrival_inference']['update_mode'], 'sustained_latency')
        self.assertEqual(hits[5]['arrival_inference']['update_mode'], 'sustained_latency')

    async def test_out_of_order_response_cannot_update_newer_route_estimate(self):
        windows = [
            dict(id=f'w{i}', centerMs=center, hitMs=620, perfectMs=210)
            for i, center in enumerate((2500, 3000, 5500))
        ]
        hits = await run_case(
            [delay(hit=30, hit_return=1500), delay(), delay()],
            windows=windows, concurrent=True,
            recorded_plans=[WindowPlan(0, 1100, 100)] * 3,
        )
        self.assertEqual(hits[0]['arrival_inference']['update_mode'], 'out_of_order')
        self.assertFalse(hits[0]['arrival_inference']['update_applied'])
        self.assertTrue(all(h['accepted_perfect'] for h in hits), summary(hits))

    async def test_clamped_outliers_are_not_misread_as_stable_latency(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, _ = monitor_for(Path(directory), Clock())
            try:
                for index, sample in enumerate((450, 650, 500), 1):
                    observation = monitor._record_drift(
                        sample, center_ms=1000, sent_elapsed_ms=990,
                        request_completed_elapsed_ms=1020 + sample,
                        request_lead_ms=10, window_index=index,
                    )
                self.assertNotEqual(observation['update_mode'], 'sustained_latency')
                self.assertEqual(monitor._drift_consistent_samples, [450, 650, 500])
                monitor._reset_drift()
                self.assertEqual(monitor._drift_consistent_samples, [])
                self.assertEqual(monitor._drift_last_window_index, 0)
            finally:
                await monitor.stop()


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    unittest.main()
