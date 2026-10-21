"""Deterministic grant ordering, not a wall-clock or speed assertion."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from concurrent.futures import Future
from contextvars import ContextVar
from unittest.mock import patch
from engine.search import acquisition_scheduler as scheduler


def load_baseline(path):
    spec = importlib.util.spec_from_file_location('task131_baseline', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ControlledExecutor:
    """Hold accepted work until wait: complete one operation per barrier turn."""
    def __init__(self, module):
        self.module = module
        self.work = {}
        self.pending_meta = {}
        self.grants = []
        self.maximum = 0

    def __call__(self, *, max_workers):
        self.limit = max_workers
        return self

    def __enter__(self): return self
    def __exit__(self, *args): return False

    def submit(self, fn, ctx, request, key, span):
        # Scheduler admission is inspected before completion can release a lock.
        assert len(self.work) < self.limit <= 3
        assert key not in [v[0] for v in self.pending_meta.values()]
        assert not any(set(request.hosts) & set(v[1]) for v in self.pending_meta.values())
        assert request.kind != 'discovery' or not any(v[2] == 'discovery' for v in self.pending_meta.values())
        future = Future()
        self.work[future] = (fn, (ctx, request, key, span))
        self.pending_meta[future] = (key, request.hosts, request.kind)
        self.grants.append((key, request.kind, request.hosts))
        self.maximum = max(self.maximum, len(self.work))
        return future

    def wait(self, pending, **kwargs):
        assert set(pending) == set(self.work)
        future = next(iter(self.work))
        fn, args = self.work.pop(future)
        self.pending_meta.pop(future)
        try: future.set_result(fn(*args))
        except BaseException as error: future.set_exception(error)
        return {future}, set(pending) - {future}

    def run(self, products, **kwargs):
        with patch.object(self.module, 'ThreadPoolExecutor', self), patch.object(self.module, 'wait', self.wait):
            return self.module.run_products(products, control=lambda:None, **kwargs)


class GrantCursorTests(unittest.TestCase):
    baseline_path = None
    rejected_path = None

    def scenario(self, module, *, peer_kind='own_discovery', diagnostics=None, clock=None):
        executor = ControlledExecutor(module)
        seen = []
        local = ContextVar('product')
        def product(key, count, kind, select=False):
            token = local.set(key)
            try:
                for index in range(count):
                    if select:
                        choice = yield module.SelectCard([(index, 'elektro.ru')])
                        self.assertEqual(choice, index)
                    def work(index=index):
                        self.assertEqual(local.get(), key)
                        seen.append((key, index))
                        return index
                    actual = yield module.Operation(kind, ('elektro.ru',), work)
                    self.assertEqual(actual, index)
                    self.assertEqual(local.get(), key)
            finally: local.reset(token)
            return list(range(count))
        products = {'hot':product('hot',9,'card',True),
                    'peer':product('peer',2,peer_kind,peer_kind=='card'),
                    'third':product('third',2,'card',True)}
        kwargs={} if clock is None else dict(clock=clock)
        result = executor.run(products, workers=3, diagnostics=diagnostics, **kwargs)
        return executor, seen, result

    def test_pinned_baseline_starves_peer_fixed_next_host_grant_and_full_tails(self):
        new, new_seen, new_result = self.scenario(scheduler)
        self.assertEqual([v[0] for v in new.grants[:6]], ['hot','peer','third']*2)
        self.assertEqual(new_result, {'hot':list(range(9)), 'peer':[0,1], 'third':[0,1]})
        self.assertEqual(len(new.grants),13)
        self.assertEqual(len(new_seen),13)
        for key in new_result:
            self.assertEqual([i for k,i in new_seen if k==key], new_result[key])
        self.assertEqual(new.maximum,1)
        if self.baseline_path is not None:
            old, old_seen, old_result = self.scenario(load_baseline(self.baseline_path))
            self.assertEqual([v[0] for v in old.grants[:10]], ['hot']*9+['peer'])
            self.assertEqual(sorted(old_seen), sorted(new_seen))
            self.assertEqual(old_result, new_result)

    def test_card_and_proof_peers_use_same_fair_cursor(self):
        for kind in ('card','proof'):
            with self.subTest(kind=kind):
                executor, _, _ = self.scenario(scheduler,peer_kind=kind)
                self.assertEqual([v[0] for v in executor.grants[:3]],['hot','peer','third'])

    def test_optional_observer_failure_and_ledger_preserve_grants_and_ready_wait(self):
        class Broken:
            def emit(self,*args,**kwargs): raise OSError('observer')
        plain, _, result = self.scenario(scheduler)
        broken, _, broken_result = self.scenario(scheduler,diagnostics=Broken())
        self.assertEqual((plain.grants,result),(broken.grants,broken_result))
        def bad_clock(): raise ValueError('clock')
        for clock in (lambda:0,bad_clock):
            observed, _, observed_result=self.scenario(scheduler,diagnostics=Broken(),clock=clock)
            self.assertEqual((plain.grants,result),(observed.grants,observed_result))
        with tempfile.TemporaryDirectory() as temp:
            blocked=Path(temp)/'occupied';blocked.write_text('file')
            disabled=scheduler.TimingLedger(blocked/'ledger.jsonl')
            self.assertTrue(disabled.disabled)
            refused, _, refused_result=self.scenario(scheduler,diagnostics=disabled)
            self.assertEqual((plain.grants,result),(refused.grants,refused_result))
            ledger=scheduler.TimingLedger(Path(temp)/'ledger.jsonl')
            measured, _, measured_result=self.scenario(scheduler,diagnostics=ledger)
            self.assertEqual((plain.grants,result),(measured.grants,measured_result))
            events=[json.loads(line) for line in ledger.path.read_text().splitlines()][1:]
            peer=hashlib.sha256(b'peer').hexdigest()
            ready=next(e for e in events if e['event']=='ready' and e.get('product')==peer)
            submit=next(e for e in events if e['event']=='submit' and e.get('product')==peer)
            self.assertEqual(submit['span'],ready['span'])
            self.assertEqual(submit['seconds'],submit['at']-ready['at'])
            self.assertGreaterEqual(submit['seconds'],0)

    def test_cursor_is_fresh_per_call_and_stable_at_worker_counts_one_to_three(self):
        for workers in (1,2,3,3):
            def product(key):
                for i in range(3): yield scheduler.Operation('card',('same',),lambda:None)
                return key
            executor=ControlledExecutor(scheduler)
            self.assertEqual(executor.run({k:product(k) for k in ('a','b','c')},workers=workers),dict(a='a',b='b',c='c'))
            self.assertEqual([v[0] for v in executor.grants],list('abc')*3)

    def test_unrelated_host_progress_and_global_xml_product_limits(self):
        executor=ControlledExecutor(scheduler)
        def product(key,kind,hosts):
            for i in range(4): yield scheduler.Operation(kind,hosts,lambda:(key,i))
            return key
        products={k:product(k,kind,hosts) for k,kind,hosts in (
            ('hot','card',('same',)),('peer','own_discovery',('same',)),
            ('free','card',('other',)),('xml1','discovery',()),('xml2','discovery',()))}
        self.assertEqual(executor.run(products,workers=3),{k:k for k in products})
        self.assertEqual(executor.maximum,3)
        self.assertEqual([v[0] for v in executor.grants[:3]],['hot','free','xml1'])
        self.assertEqual([v[0] for v in executor.grants if v[1]=='discovery'],['xml1','xml2']*4)
        self.assertEqual(sorted(v[0] for v in executor.grants),sorted(list(products)*4))

    def test_three_product_unrelated_grants_must_not_starve_queued_host_peer(self):
        # The global last-submit cursor can be reanchored by a different host.
        # Retain its failed candidate and prove age arbitration fixes the AC.
        def scenario(module,peer_kind):
            executor=ControlledExecutor(module)
            def product(host,count,kind):
                for i in range(count):
                    if kind=='card':
                        choice=yield module.SelectCard([(i,host)])
                        self.assertEqual(choice,i)
                    yield module.Operation(kind,(host,),lambda:None)
                return count
            result=executor.run({'hot':product('same',9,'card'),
                                 'peer':product('same',1,peer_kind),
                                 'free':product('other',9,'card')},workers=3)
            return executor,result
        for kind in ('own_discovery','card','proof'):
            with self.subTest(kind=kind):
                fixed,result=scenario(scheduler,kind)
                self.assertEqual(result,{'hot':9,'peer':1,'free':9})
                self.assertEqual(len(fixed.grants),19)
                self.assertEqual([v[0] for v in fixed.grants].count('hot'),9)
                self.assertEqual([v[0] for v in fixed.grants].count('free'),9)
                self.assertEqual([v[0] for v in fixed.grants].count('peer'),1)
                fixed_same=[v[0] for v in fixed.grants if v[2]==('same',)]
                self.assertEqual(fixed_same[:2],['hot','peer'],fixed.grants)
                self.assertEqual(fixed.maximum,2)
                if self.baseline_path is not None:
                    paths=[self.baseline_path]
                    if self.rejected_path is not None: paths.append(self.rejected_path)
                    for path in paths:
                        historical,historical_result=scenario(load_baseline(path),kind)
                        same=[v[0] for v in historical.grants if v[2]==('same',)]
                        self.assertEqual(same,['hot']*9+['peer'])
                        self.assertEqual(historical_result,result)
                        self.assertEqual(sorted(fixed.grants),sorted(historical.grants))

    def test_pending_select_age_survives_choice_resolution_without_clock(self):
        executor=ControlledExecutor(scheduler)
        def blocker(): yield scheduler.Operation('card',('same',),lambda:None)
        def older():
            self.assertEqual((yield scheduler.SelectCard([(0,'free')])),0)
            # The resolved operation still needs another currently held host.
            yield scheduler.Operation('proof',('same','free'),lambda:None)
        def newer(): yield scheduler.Operation('own_discovery',('same',),lambda:None)
        executor.run({'blocker':blocker(),'older':older(),'newer':newer()},workers=3)
        self.assertEqual([v[0] for v in executor.grants],['blocker','older','newer'])

    def test_select_and_park_do_not_claim_cursor_without_submit(self):
        executor=ControlledExecutor(scheduler); closed=[]
        def hot():
            for i in range(2): yield scheduler.Operation('card',('same',),lambda:None)
        def parked():
            try:
                yield scheduler.SelectCard([(0,'other')])
                yield scheduler.Operation('card',('other',),lambda:self.fail('parked dispatched'),before=lambda:False)
            except scheduler.OperationParked: pass
            finally: closed.append('parked')
        def peer(): yield scheduler.Operation('own_discovery',('same',),lambda:None)
        executor.run({'hot':hot(),'parked':parked(),'peer':peer()},workers=3)
        self.assertEqual([v[0] for v in executor.grants],['hot','peer','hot'])
        self.assertEqual(closed,['parked'])


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-scheduler',type=Path)
    parser.add_argument('--rejected-scheduler',type=Path)
    args=parser.parse_args()
    if args.rejected_scheduler and not args.baseline_scheduler:
        parser.error('--rejected-scheduler requires explicit --baseline-scheduler')
    GrantCursorTests.baseline_path=args.baseline_scheduler.resolve() if args.baseline_scheduler else None
    GrantCursorTests.rejected_path=args.rejected_scheduler.resolve() if args.rejected_scheduler else None
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(GrantCursorTests))
    sys.exit(not result.wasSuccessful())
