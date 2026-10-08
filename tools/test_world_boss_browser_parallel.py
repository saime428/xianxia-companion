"""Exercise browser queue scheduling with real broker files, no browser/network."""
import tempfile
import threading
import time
import unittest

from test_world_boss_turnstile import browser, queue


def request(broker, account, number=123):
    return broker.create_request(event_fingerprint='offline-event', message_id=number,
        account=account, identity='main', challenge_id=f'challenge-{account}-{number}',
        action='offline_'+account.replace('-', '_'), origin=browser.ORIGIN)['request_id']


class ParallelBrowserTests(unittest.TestCase):
    def test_stalled_verification_does_not_hold_other_accounts(self):
        stalled, release = threading.Event(), threading.Event()
        entered = threading.Barrier(3)
        class FakeBrowser:
            def verify(self, origin, **kwargs):
                kwargs['on_event']('helper_ready')
                entered.wait(timeout=2)
                if kwargs['action'] == 'offline_slow':
                    stalled.set()
                    deadline = time.monotonic()+3
                    # Exercise real broker locking during another account's callback.
                    while not release.wait(.005):
                        if not kwargs['still_pending']() or time.monotonic() > deadline:
                            raise browser.BrowserVerificationError('turnstile_browser_timeout')
                kwargs['on_event']('token_generated')
                return 'token-for-'+kwargs['action']
            def close(self):
                pass
        with tempfile.TemporaryDirectory() as directory:
            broker = queue.WorldBossTurnstileBroker(directory)
            slow = request(broker, 'slow')
            fast = [request(broker, account) for account in ('fast_a', 'fast_b')]
            pool = browser.ConcurrentTurnstileWorker(broker, [FakeBrowser() for _ in range(3)])
            try:
                pool.run_once()
                self.assertTrue(stalled.wait(2))
                deadline = time.monotonic()+2
                while any(broker.get_request(r)['status'] == 'pending' for r in fast) and time.monotonic() < deadline:
                    pool.run_once()
                    time.sleep(.005)
                self.assertFalse(release.is_set())
                self.assertEqual(broker.get_request(slow)['status'], 'pending')
                self.assertEqual([broker.take_token(r) for r in fast],
                                 ['token-for-offline_fast_a', 'token-for-offline_fast_b'])
                self.assertTrue(all(broker.get_request(r)['browser_attempts'] == 1 for r in [slow, *fast]))
            finally:
                pool.stopping.set()
                release.set()
                pool.close()

    def test_three_accounts_start_before_any_callback_completes(self):
        release, stop = threading.Event(), threading.Event()
        started = [threading.Event() for _ in range(3)]
        class FakeBrowser:
            def __init__(self, index):
                self.index, self.calls = index, 0
            def verify(self, origin, **kwargs):
                self.calls += 1
                started[self.index].set()
                if not release.wait(3):
                    raise RuntimeError('test callback not released')
                return 'token-for-'+kwargs['action']
            def close(self):
                pass
        with tempfile.TemporaryDirectory() as directory:
            broker = queue.WorldBossTurnstileBroker(directory)
            ids = [request(broker, 'account-'+str(i)) for i in range(3)]
            browsers = [FakeBrowser(i) for i in range(3)]
            # The legacy scheduler provides the red comparison before the pool exists.
            pool_class = getattr(browser, 'ConcurrentTurnstileWorker', None)
            worker = pool_class(broker, browsers) if pool_class else browser.AutomaticTurnstileWorker(broker, browsers[0])
            def pump():
                while not stop.is_set():
                    worker.run_once()
                    time.sleep(.005)
            thread = threading.Thread(target=pump)
            thread.start()
            try:
                self.assertTrue(all(event.wait(.5) for event in started), 'Serial verification leaves later accounts waiting')
                release.set()
                deadline=time.monotonic()+2
                while time.monotonic()<deadline and any(broker.get_request(r)['status']=='pending' for r in ids):
                    time.sleep(.01)
                self.assertEqual([broker.get_request(r)['browser_attempts'] for r in ids], [1, 1, 1])
                self.assertEqual([broker.take_token(r) for r in ids], ['token-for-offline_account_'+str(i) for i in range(3)])
            finally:
                release.set()
                stop.set()
                thread.join(4)
                if pool_class:
                    worker.close()
            self.assertFalse(thread.is_alive())

    def test_same_account_waits_and_stopping_discards_late_token(self):
        started, ended = threading.Event(), threading.Event()
        class FakeBrowser:
            def __init__(self):
                self.calls = 0
            def verify(self, origin, **kwargs):
                self.calls += 1
                started.set()
                while kwargs['still_pending']():
                    time.sleep(.005)
                ended.set()
                return 'offline-late-token'
            def close(self):
                if self.calls:
                    assert ended.is_set(), 'Closed a browser while verification was using it'
        with tempfile.TemporaryDirectory() as directory:
            broker=queue.WorldBossTurnstileBroker(directory)
            ids=[request(broker, 'same-account', n) for n in (1, 2)]
            browsers=[FakeBrowser(), FakeBrowser()]
            pool=browser.ConcurrentTurnstileWorker(broker,browsers)
            try:
                pool.run_once()
                self.assertTrue(started.wait(1))
                for _ in range(5):pool.run_once()
                self.assertEqual(sum(b.calls for b in browsers),1)
            finally:
                pool.close()
            self.assertEqual([broker.get_request(r)['status'] for r in ids], ['pending','pending'])
            self.assertTrue(all(broker.take_token(r) is None for r in ids))

    def test_retry_cooldown_and_attempt_limit_survive_pool_dispatch(self):
        now=[1000.0]
        class FailedBrowser:
            def __init__(self):self.calls=0
            def verify(self, origin, **kwargs):
                self.calls+=1
                raise browser.BrowserVerificationError('turnstile_browser_timeout')
            def close(self):pass
        with tempfile.TemporaryDirectory() as directory:
            broker=queue.WorldBossTurnstileBroker(directory)
            rid=request(broker,'retry')
            browsers=[FailedBrowser(),FailedBrowser()]
            pool=browser.ConcurrentTurnstileWorker(broker,browsers,clock=lambda:now[0])
            def drain():
                deadline=time.monotonic()+2
                while pool.jobs and time.monotonic()<deadline:
                    pool.run_once()
                    time.sleep(.005)
                self.assertFalse(pool.jobs)
            try:
                pool.run_once();drain()
                self.assertEqual(sum(b.calls for b in browsers),1)
                now[0]+=4.9
                pool.run_once()
                self.assertEqual(sum(b.calls for b in browsers),1)
                now[0]+=.1
                pool.run_once();drain()
                self.assertEqual(sum(b.calls for b in browsers),2)
                self.assertEqual(broker.get_request(rid)['status'],'cancelled')
                self.assertEqual(broker.get_request(rid)['browser_attempts'],2)
            finally:pool.close()

    def test_pool_size_is_bounded(self):
        with self.assertRaises(ValueError):browser.ConcurrentTurnstileWorker(None,[])
        with self.assertRaises(ValueError):browser.ConcurrentTurnstileWorker(None,[None]*4)

    def test_fourth_account_waits_for_a_free_browser(self):
        releases = [threading.Event() for _ in range(3)]
        starts = [threading.Event() for _ in range(3)]
        class FakeBrowser:
            def __init__(self, index):
                self.index, self.calls = index, 0
            def verify(self, origin, **kwargs):
                self.calls += 1
                starts[self.index].set()
                if not releases[self.index].wait(3):
                    raise RuntimeError('test callback not released')
                return 'token-for-'+kwargs['action']
            def close(self):
                pass
        with tempfile.TemporaryDirectory() as directory:
            broker = queue.WorldBossTurnstileBroker(directory)
            ids = [request(broker, 'account-'+str(i)) for i in range(4)]
            browsers = [FakeBrowser(i) for i in range(3)]
            pool = browser.ConcurrentTurnstileWorker(broker, browsers)
            try:
                pool.run_once()
                self.assertTrue(all(event.wait(1) for event in starts))
                pool.run_once()
                self.assertEqual(sum(b.calls for b in browsers), 3)
                releases[0].set()
                deadline = time.monotonic()+2
                while sum(b.calls for b in browsers) < 4 and time.monotonic() < deadline:
                    pool.run_once()
                    time.sleep(.005)
                self.assertEqual(sum(b.calls for b in browsers), 4)
                for event in releases:
                    event.set()
                deadline = time.monotonic()+2
                while pool.jobs and time.monotonic() < deadline:
                    pool.run_once()
                    time.sleep(.005)
                self.assertFalse(pool.jobs)
                self.assertTrue(all(broker.get_request(r)['browser_attempts'] == 1 for r in ids))
                self.assertEqual([broker.take_token(r) for r in ids],
                                 ['token-for-offline_account_'+str(i) for i in range(4)])
                pool.run_once()
                self.assertEqual(pool.last_attempt, {})
                self.assertTrue(all(not worker.last_attempt for worker in pool.workers))
            finally:
                for event in releases:
                    event.set()
                pool.close()


if __name__ == '__main__':
    unittest.main()
