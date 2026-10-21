"""Offline attribution controls; synthetic intervals are never throughput claims."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import Future
from unittest.mock import patch
from engine.search import acquisition_scheduler as scheduler


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


class SchedulerTimingTests(unittest.TestCase):
    baseline_path = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = scheduler.TimingLedger(self.root/'scheduler.jsonl')

    def test_exact_queue_submit_worker_and_advance_clock_intervals(self):
        now = [0]
        class Executor:
            def __init__(self, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def submit(self, fn, *args):
                future = Future()
                # Separate executor acceptance from actual operation entry.
                now[0] += 7
                try: future.set_result(fn(*args))
                except BaseException as error: future.set_exception(error)
                return future
        def product(initial):
            now[0] += initial
            def work(): now[0] += 11; return 'value'
            value = yield scheduler.Operation('card', ('chipdip.ru',), work)
            self.assertEqual(value, 'value')
            now[0] += 13
            return 'done'
        with patch.object(scheduler, 'ThreadPoolExecutor', Executor):
            result = scheduler.run_products({'a':product(2), 'b':product(3)},
                control=lambda:None, workers=1, diagnostics=self.ledger, clock=lambda:now[0])
        self.assertEqual(result, {'a':'done', 'b':'done'})
        events = rows(self.ledger.path)[1:]
        for key, expected in [('a', (2,5,12,23,3)), ('b', (5,36,43,54,31))]:
            product_hash = hashlib.sha256(key.encode()).hexdigest()
            own = [e for e in events if e.get('product')==product_hash]
            by = {e['event']:e for e in own if e['event'] not in ('advance_start','advance_end')}
            self.assertEqual(tuple(by[k]['at'] for k in ('ready','submit','worker_start','worker_end')), expected[:4])
            self.assertEqual(by['submit']['seconds'], expected[4])
            self.assertEqual(by['worker_end']['seconds'], 11)
            self.assertEqual(len({by[k]['span'] for k in by}), 1)
            self.assertEqual([e['seconds'] for e in own if e['event']=='advance_end'], [2 if key=='a' else 3,13])
        self.assertEqual(events[-1]['event'], 'run_end')

    def test_persisted_worker_start_before_completion_and_pending_is_unknown(self):
        entered = threading.Event(); release = threading.Event(); errors=[]
        def product():
            def work():
                entered.set()
                if not release.wait(3): raise AssertionError('barrier timed out')
            yield scheduler.Operation('card', ('secret-url.test?token=raw',), work)
        def drive():
            try: scheduler.run_products({'secret-product-query':product()}, control=lambda:None, diagnostics=self.ledger)
            except BaseException as error: errors.append(error)
        thread = threading.Thread(target=drive); thread.start()
        try:
            self.assertTrue(entered.wait(3))
            data = rows(self.ledger.path)
            self.assertEqual(data[0]['missing_end'], 'unknown')
            self.assertIn('worker_start', [e.get('event') for e in data])
            self.assertNotIn('worker_end', [e.get('event') for e in data])
            self.assertNotIn('run_end', [e.get('event') for e in data])
            self.assertNotIn('secret', self.ledger.path.read_text())
            self.assertEqual(next(e for e in data if e.get('event')=='worker_start')['hosts'], ['host_' + hashlib.sha256(b'secret-url.test?token=raw').hexdigest()])
        finally:
            release.set();thread.join(3)
        self.assertFalse(thread.is_alive());self.assertEqual(errors, [])

    def test_unknown_hosts_are_distinct_hashes_without_sentinel_payloads(self):
        hosts=('untrusted-a.test?token=SENTINEL', 'untrusted-b.test?token=SENTINEL')
        self.ledger.emit('ready',run='fixture',key='product',kind='card',hosts=hosts,at=0)
        data=rows(self.ledger.path)[1]
        self.assertEqual(len(data['hosts']),2)
        self.assertEqual(set(data['hosts']),{'host_' + hashlib.sha256(h.encode()).hexdigest() for h in hosts})
        self.assertNotIn('SENTINEL',self.ledger.path.read_text())
        self.assertNotIn('untrusted',self.ledger.path.read_text())

    def test_bounded_truncation_and_resume_keep_prefix_and_cumulative_drops(self):
        ledger = scheduler.TimingLedger(self.root/'small.jsonl', max_bytes=1024)
        def product():
            for i in range(80): yield scheduler.Operation('card', (), lambda:None)
            return 'same-result'
        self.assertEqual(scheduler.run_products({'a':product()},control=lambda:None,diagnostics=ledger), {'a':'same-result'})
        first = ledger.path.read_bytes(); header = rows(ledger.path)[0]
        self.assertLessEqual(len(first), 1024);self.assertTrue(header['truncated']);self.assertGreater(header['dropped'], 0)
        again = scheduler.TimingLedger(ledger.path, max_bytes=1024)
        scheduler.run_products({'a':product()},control=lambda:None,diagnostics=again)
        after = ledger.path.read_bytes()
        self.assertEqual(after[256:], first[256:]);self.assertLessEqual(len(after),1024)
        self.assertGreater(rows(ledger.path)[0]['dropped'],header['dropped'])

    def test_io_failure_at_construction_and_midrun_never_changes_results(self):
        blocked = self.root/'file';blocked.write_text('occupied')
        disabled = scheduler.TimingLedger(blocked/'scheduler.jsonl')
        self.assertTrue(disabled.disabled)
        def product():
            value = yield scheduler.Operation('card', (), lambda:42)
            return value
        self.assertEqual(scheduler.run_products({'a':product()},control=lambda:None,diagnostics=disabled), {'a':42})
        with patch.object(Path,'open',side_effect=OSError('raw credential error')):
            self.assertEqual(scheduler.run_products({'a':product()},control=lambda:None,diagnostics=self.ledger), {'a':42})
        self.assertTrue(self.ledger.disabled)
        self.assertNotIn('credential',self.ledger.path.read_text())

    def test_observer_and_clock_failure_cannot_mask_worker_or_cleanup_errors(self):
        class Broken:
            def emit(self,*args,**kwargs):raise RuntimeError('observer')
        def clock():raise ValueError('clock')
        original = KeyboardInterrupt('raw secret failure')
        def product():
            def work():raise original
            try:yield scheduler.Operation('card', (), work)
            except KeyboardInterrupt as error:
                self.assertIs(error,original);return 'caught'
        self.assertEqual(scheduler.run_products({'a':product()},control=lambda:None,diagnostics=Broken(),clock=clock),{'a':'caught'})
        def uncaught():
            yield scheduler.Operation('card', (), lambda:(_ for _ in ()).throw(original))
        with self.assertRaises(KeyboardInterrupt) as raised:
            scheduler.run_products({'a':uncaught()},control=lambda:None,diagnostics=self.ledger)
        self.assertIs(raised.exception,original)
        self.assertNotIn('raw',self.ledger.path.read_text())
        self.assertEqual(next(e for e in rows(self.ledger.path) if e.get('event')=='worker_end')['outcome'],'error')

    def test_three_worker_cap_host_discovery_product_exclusion_with_real_barriers(self):
        lock=threading.Lock(); gate=threading.Barrier(3);active=set();max_active=[0];seen=[]
        def product(key,host):
            def call():
                with lock:
                    self.assertNotIn(host,active);active.add(host);max_active[0]=max(max_active[0],len(active))
                gate.wait(3)
                with lock:active.remove(host);seen.append(key)
            yield scheduler.Operation('card',(host,),call)
            return key
        result=scheduler.run_products({str(i):product(str(i),str(i)) for i in range(6)},control=lambda:None,diagnostics=self.ledger)
        self.assertEqual(max_active[0],3);self.assertEqual(set(seen),set(result));self.assertEqual(result,{str(i):str(i) for i in range(6)})

    def test_control_park_invalid_and_exception_order_match_original_scheduler(self):
        # Original is supplied explicitly by the offline runner, never discovered broadly.
        baseline_path = getattr(self, 'baseline_path', None)
        if not baseline_path:
            self.skipTest('Historical differential requires explicit --baseline-scheduler; other timing controls remain active')
        spec=importlib.util.spec_from_file_location('task128_baseline',baseline_path)
        baseline=importlib.util.module_from_spec(spec)
        import sys
        sys.modules[spec.name]=baseline;spec.loader.exec_module(baseline)
        def scenario(module,mode,diagnostics=None):
            log=[];checks=[0]
            def control():
                checks[0]+=1
                if mode=='cancel' and checks[0]>=3:raise RuntimeError('cancel')
            def product(key):
                try:
                    if mode=='invalid':yield object()
                    if mode=='select':
                        choice=yield module.SelectCard([(0,'same'),(1,'tail')]);log.append(('choice',key,choice))
                    def work():
                        log.append(('work',key))
                        if mode=='error':raise ValueError('worker')
                        return 42
                    value=yield module.Operation('discovery' if mode=='discovery' else 'card',('same',),work,before=lambda:mode!='park')
                    log.append(('result',key,value))
                except BaseException as error:log.append(('caught',key,type(error).__name__))
                finally:log.append(('close',key))
                return key
            try:
                kw={} if diagnostics is None else dict(diagnostics=diagnostics)
                result=module.run_products({k:product(k) for k in ('a','b')},control=control,workers=1,**kw)
                return result,log,checks
            except BaseException as error:return type(error).__name__,log,checks
        for mode in ('ok','cancel','park','invalid','error','select','discovery'):
            with self.subTest(mode=mode):self.assertEqual(scenario(baseline,mode),scenario(scheduler,mode,self.ledger))


class AppTimingWiringTests(unittest.TestCase):
    def test_real_indexed_app_path_persists_under_offline_fixture_and_io_refusal(self):
        from engine import app,test_catalogue_unknown as fixtures
        from engine.schemas import PositionIn
        position=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)
        for fail_io in (False,True):
            with self.subTest(fail_io=fail_io):
                fixture=fixtures.CatalogueUnknownTests();fixture.setUp()
                try:
                    fixture.hosts=['s0.test']
                    if fail_io:
                        blocked=fixture.root/'jobs'/'diagnostics'
                        blocked.parent.mkdir(parents=True,exist_ok=True);blocked.write_text('occupied')
                    job,public=fixture.job([position])
                    self.assertEqual(public['status'],'done',job.get('error'))
                    self.assertEqual(len(fixture.calls),1);self.assertEqual(len(fixture.captures),2)
                    if not fail_io:
                        path=app.RESEARCH_JOB_DIR/'diagnostics'/hashlib.sha256(job['job_id'].encode()).hexdigest()/'scheduler.jsonl'
                        events=rows(path)[1:]
                        self.assertTrue({'ready','submit','worker_start','worker_end','advance_start','advance_end','run_end'} <= {e['event'] for e in events})
                        self.assertEqual({e['kind'] for e in events if e['event']=='worker_start'},{'discovery','card','proof'})
                        self.assertNotIn('https:',path.read_text());self.assertNotIn('ABC123',path.read_text())
                finally:fixture.doCleanups()

    def test_app_cancellation_keeps_persisted_started_worker_without_replay(self):
        from engine import app,test_catalogue_unknown as fixtures
        from engine.schemas import PositionIn
        from engine.search import session
        fixture=fixtures.CatalogueUnknownTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.hosts=['s0.test'];entered=threading.Event();release=threading.Event();output=[];errors=[]
        def capture(*args,**kwargs):
            entered.set()
            if not release.wait(3):raise AssertionError('barrier timeout')
            session.bounded_timeout(1)
            raise AssertionError('Cancellation must stop this operation')
        def drive():
            try:output.append(fixture.job([PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)],capture=capture))
            except BaseException as error:errors.append(error)
        worker=threading.Thread(target=drive);worker.start()
        try:
            self.assertTrue(entered.wait(3))
            path=next((fixture.root/'jobs'/'diagnostics').rglob('scheduler.jsonl'))
            data=rows(path)[1:]
            start=next(e for e in data if e['event']=='worker_start' and e['kind']=='card')
            self.assertFalse(any(e['event']=='worker_end' and e['span']==start['span'] for e in data))
            job=next(j for k,j in app.RESEARCH_JOBS.items() if hashlib.sha256(k.encode()).hexdigest()==path.parent.name)
            job['cancel_requested']=True
        finally:release.set();worker.join(3)
        self.assertFalse(worker.is_alive());self.assertEqual(errors,[])
        self.assertEqual(output[0][1]['status'],'cancelled')
        data=rows(path)[1:]
        self.assertEqual(len([e for e in data if e['event']=='worker_start' and e['kind']=='card']),1)
        self.assertEqual(next(e for e in data if e['event']=='worker_end' and e['span']==start['span'])['outcome'],'error')


if __name__ == '__main__':
    import argparse
    import sys
    import fitz
    from engine.benchmark_atomic_service import offline_boundary
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-scheduler',type=Path)
    args=parser.parse_args()
    SchedulerTimingTests.baseline_path=args.baseline_scheduler.resolve() if args.baseline_scheduler else None
    font=fitz.Font
    # Source-only inventory excludes bundled font assets. No PDF is generated.
    with offline_boundary(),patch.object(fitz,'Font',side_effect=lambda *a,**kw:font(fontname='helv')):
        suite=unittest.TestSuite([
            unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]),
            unittest.defaultTestLoader.loadTestsFromName('engine.test_acquisition_scheduler.SchedulerQueueTests')])
        result=unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
