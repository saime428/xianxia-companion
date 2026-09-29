"""Server-clock replay: delayed coroutine wakeups must not shift actual hits.

python -X utf8 tools/test_world_boss_timing_replay.py
Uses the existing virtual clock and transport fixtures. No Telegram or HTTP.
"""
import asyncio
import logging
from pathlib import Path
import tempfile
from unittest.mock import patch

from test_world_boss_combat import Clock, Server, ENTRY, monitor_for, window, boss


class TimedServer(Server):
    def __init__(self, clock, reveal_lead_ms=None):
        super().__init__(clock, [window(0, 2000), window(1, 5500), window(2, 9000)])
        self.starts_at = None
        self.charged_at = {}
        self.reveal_lead_ms = reveal_lead_ms
        self.revealed_at = {}

    async def post(self, origin, path, payload, timeout):
        endpoint = path.rsplit('/', 1)[-1]
        if endpoint == 'window' and self.reveal_lead_ms is not None:
            previous = payload['afterWindowId']
            index = next((i + 1 for i, item in enumerate(self.windows) if item['id'] == previous), 0)
            if index < len(self.windows):
                item = self.windows[index]
                if self.clock.now < self.starts_at + (item['centerMs'] - self.reveal_lead_ms) / 1000:
                    raise boss.MiniAppBeastError('boss_window_not_ready')
                self.revealed_at[item['id']] = self.clock.now
        if endpoint in ('charge-start', 'hit'):
            self.requests.append((endpoint, dict(payload), self.clock.now))
            await self.clock.sleep(.01)
            if endpoint == 'charge-start':
                if self.reveal_lead_ms is not None:
                    assert self.clock.now >= self.revealed_at[payload['windowId']]
                self.charged_at[payload['windowId']] = self.clock.now
                result = {'ok': True, 'chargeTicket': 'offline-ticket'}
            else:
                target = next(w for w in self.windows if w['id'] == payload['windowId'])
                delta = abs((self.clock.now - self.starts_at) * 1000 - target['centerMs'])
                hold = (self.clock.now - self.charged_at[payload['windowId']]) * 1000
                result = {'ok': True, 'hit': {'damageYi': 2, 'deltaMs': delta, 'holdMs': hold,
                                             'perfect': delta <= target['perfectMs'] and 520 <= hold <= 1250}}
            await self.clock.sleep(.01)
            return result
        return await super().post(origin, path, payload, timeout)


async def replay(root, resume_ms, *, verification=False, metadata=True, account='offline', reveal_lead_ms=None):
    clock = Clock()
    server = TimedServer(clock, reveal_lead_ms)
    monitor, _ = monitor_for(root, clock, server)
    monitor.account = account
    monitor.post_json = None

    async def post(origin, path, payload, timeout, **kwargs):
        if not path.endswith('/begin'):
            return await server.post(origin, path, payload, timeout)
        needs_verification = verification and not payload.get('turnstileToken')
        transport_ms = 20 if needs_verification or not verification else 320
        wait_ms = 50 if needs_verification else resume_ms
        if not needs_verification:
            # StartsInMs is made after verification work; one-way return is 10ms.
            server.starts_at = clock.now + (transport_ms - 10 + 100) / 1000
        await clock.sleep((transport_ms + wait_ms) / 1000)
        if metadata:
            kwargs['timing'].update(transport_ms=transport_ms, executor_queue_ms=0, loop_resume_ms=wait_ms)
        if needs_verification:
            raise boss.MiniAppBeastError('turnstile_required', 403)
        return {'ok': True, 'startsInMs': 100}

    async def verify(*args):
        await clock.sleep(.5)
        return 'offline-verification', ''

    monitor._wait_for_turnstile_token = verify
    with patch.object(boss, '_post_json', post):
        result = await clock.run(monitor._run_identity(ENTRY, '主魂', 'offline-init-data'))
    await monitor.stop()
    assert result['status'] == 'completed', result
    assert result['perfect_count'] == 3, result
    assert len([r for r in server.requests if r[0] == 'hit']) == 3
    assert all(h['actual_elapsed_ms'] == h['sent_elapsed_ms'] for h in result['diagnostics']['hits'])
    return result


async def main():
    with tempfile.TemporaryDirectory(prefix='boss-timing-replay-') as directory:
        root = Path(directory)
        for verification in (False, True):
            for delay in (0, 50, 250, 1000):
                result = await replay(root, delay, verification=verification)
                timing = result['diagnostics']['clock_sync']
                assert timing['request']['response_resume_ms'] == delay
                assert timing['round_trip_ms'] == 20, timing
                assert max(h['server_hit']['deltaMs'] for h in result['diagnostics']['hits']) < 1.1
        # Injected/older transports without timing keep their original contract.
        result = await replay(root, 0, metadata=False)
        assert result['diagnostics']['clock_sync']['request']['response_resume_ms'] == 0
        for stagger in (70, 0):
            with patch.object(boss, 'WORLD_BOSS_PROFILE_STAGGER_MS', stagger):
                for slot, account in enumerate(('profile_2', 'profile_3', 'profile_4')):
                    result = await replay(root, 250, verification=True, account=account, reveal_lead_ms=1200)
                    assert all(h['account_offset_ms'] == slot * stagger for h in result['diagnostics']['hits'])
                    assert all(abs(h['server_hit']['deltaMs'] - slot * stagger) < 1.1 for h in result['diagnostics']['hits'])
        for attempt in ({}, {'duration_ms':20,'loop_resume_ms':float('nan')},
                        {'duration_ms':20,'loop_resume_ms':500}, {'duration_ms':-1,'loop_resume_ms':0}):
            assert boss.WorldBossMonitor._response_resume_ms(attempt) == 0
    print('world boss timing replay: ok (offline server clock)')


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    asyncio.run(main())
