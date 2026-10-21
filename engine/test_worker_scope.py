"""Resource affinity, lifetime, failure recovery and shared-queue integration."""
from collections import Counter
from contextlib import contextmanager
import threading
import time
import unittest
from unittest.mock import MagicMock
from engine.browser import Browser
from engine.search.worker_scope import ScopedWorkerPool, worker_browser_scope, borrow_browser
from engine.search.shared_pipeline import run_shared_pipeline


class FakeBrowser:
    def __init__(self, events):
        self.events = events
        self.owner = threading.get_ident()
        self.alive = True

    def __enter__(self):
        self.events.append(('open', self.owner, id(self)))
        return self

    def __exit__(self, *exc):
        assert self.owner == threading.get_ident()
        self.alive = False
        self.events.append(('close', self.owner, id(self)))

    def is_usable(self):
        assert self.owner == threading.get_ident()
        return self.alive


class WorkerScopeTests(unittest.TestCase):
    def test_connected_browser_with_closed_context_restarts(self):
        events=[]
        browser=Browser()
        browser._browser=MagicMock()
        browser._browser.is_connected.return_value=True
        browser.context=MagicMock(pages=[])
        browser._context_closed=False
        self.assertTrue(browser.is_usable())
        browser._on_context_close(browser.context)
        self.assertFalse(browser.is_usable())
        with worker_browser_scope():
            first=FakeBrowser(events)
            first.is_usable=browser.is_usable
            with borrow_browser(lambda:first):pass
            with borrow_browser(lambda:FakeBrowser(events)):pass
        self.assertEqual(['open','close']*2,[e[0]for e in events])

    def test_primary_failure_survives_cleanup_failure(self):
        class BadClose(FakeBrowser):
            def __exit__(self,*exc):
                super().__exit__(*exc)
                raise RuntimeError('cleanup')
        with worker_browser_scope():
            with self.assertRaisesRegex(ValueError,'persistence failure') as caught:
                with borrow_browser(lambda:BadClose([])):
                    raise ValueError('persistence failure')
            self.assertIn('RuntimeError',caught.exception.__notes__[0])

    def test_three_workers_reuse_and_close_their_own_browser(self):
        events=[]
        barrier=threading.Barrier(3)
        def work(first=False):
            with borrow_browser(lambda:FakeBrowser(events)) as browser:
                if first:barrier.wait(timeout=2)
                return threading.get_ident(),id(browser)
        with ScopedWorkerPool(3,worker_browser_scope) as pool:
            first=[pool.submit(work,True) for _ in range(3)]
            results=[f.result(timeout=3) for f in first]
            results += [f.result(timeout=3) for f in [pool.submit(work) for _ in range(24)]]
            self.assertEqual(3,len([e for e in events if e[0]=='open']))
            self.assertFalse(any(e[0]=='close' for e in events))
        self.assertEqual(Counter((t,i) for k,t,i in events if k=='open'),
                         Counter((t,i) for k,t,i in events if k=='close'))
        self.assertEqual(3,len(set(results)))

    def test_cache_only_workers_never_launch_browser(self):
        with ScopedWorkerPool(3,worker_browser_scope) as pool:
            self.assertEqual(7,pool.submit(lambda:7).result(timeout=2))

    def test_standalone_borrows_keep_independent_lifetimes(self):
        events=[]
        for _ in range(2):
            with borrow_browser(lambda:FakeBrowser(events)):pass
        self.assertEqual(['open','close','open','close'],[e[0] for e in events])

    def test_failed_and_disconnected_sessions_restart_without_losing_worker(self):
        events=[]
        def failure():
            with borrow_browser(lambda:FakeBrowser(events)):
                raise ValueError('card failed')
        def work(disconnect=False):
            with borrow_browser(lambda:FakeBrowser(events)) as browser:
                browser.alive=not disconnect
        with ScopedWorkerPool(1,worker_browser_scope) as pool:
            with self.assertRaisesRegex(ValueError,'card failed'):
                pool.submit(failure).result(timeout=2)
            pool.submit(work,True).result(timeout=2)
            pool.submit(work).result(timeout=2)
        self.assertEqual(['open','close']*3,[e[0] for e in events])

    def test_cleanup_failure_does_not_strand_other_workers(self):
        closed=[]
        barrier=threading.Barrier(3)
        @contextmanager
        def scope():
            try:yield
            finally:
                closed.append(threading.get_ident())
                raise RuntimeError('cleanup failed')
        with self.assertRaisesRegex(RuntimeError,'cleanup failed'):
            with ScopedWorkerPool(3,scope) as pool:
                futures=[pool.submit(lambda:barrier.wait(timeout=2)) for _ in range(3)]
                for f in futures:f.result(timeout=3)
        self.assertEqual(3,len(set(closed)))

    def test_shared_pipeline_reuses_browser_across_card_slices(self):
        events=[];finals={};calls=[]
        def cards(index,urls,row):
            with borrow_browser(lambda:FakeBrowser(events)) as browser:
                calls.append(id(browser))
            return dict(row,found=row['found']+1)
        run_shared_pipeline([0],seed=lambda i:({'status':'partial','found':0},[]),
            plan=lambda i:[1,2],discover=lambda i,t:([f'https://site/card{t}'],[]),
            cards=cards,publish=lambda i,r,d,last:finals.update({i:r}) if last else None,
            deadline=time.monotonic()+3,cancelled=lambda:False,target=5,
            card_workers=1,discovery_workers=1,card_worker_scope=worker_browser_scope)
        self.assertEqual(2,finals[0]['found'])
        self.assertEqual(1,len(set(calls)))
        self.assertEqual(['open','close'],[e[0] for e in events])
