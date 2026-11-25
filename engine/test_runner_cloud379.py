"""Synthetic HTTP URLs below are fixtures, NOT guessed Cloud.ru API routes."""
import importlib.util
import json
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import httpx

spec=importlib.util.spec_from_file_location('runner_cloud379',Path(__file__).with_name('runner_cloud.py'))
import sys
cloud=importlib.util.module_from_spec(spec);sys.modules[spec.name]=cloud;spec.loader.exec_module(cloud)
VM='11111111-1111-4111-8111-111111111111'
OTHER='22222222-2222-4222-8222-222222222222'

class FakeHTTPProvider:
    def __init__(self):
        self.state=cloud.PowerState.STOPPED;self.calls=[];self.missed=None;self.on_mutation=None;self.return_vm=VM
        self.client=httpx.Client(transport=httpx.MockTransport(self.handle),trust_env=False)
    def handle(self,request):
        self.calls.append((request.method,request.url.path))
        if request.method=='GET':return httpx.Response(200,json={'vm_id':self.return_vm,'state':self.state.value})
        action=request.url.path.rsplit('/',1)[-1]
        if self.on_mutation:self.on_mutation(action)
        if self.missed:raise httpx.ReadTimeout('sensitive transport text',request=request)
        self.state=cloud.PowerState.STARTING if action=='start' else cloud.PowerState.STOPPING
        return httpx.Response(202,json={'ack':True})
    def read_state(self,vm_id,*,deadline):
        result=self.client.get('https://fixture.invalid/vm/'+vm_id).json()
        return cloud.VMState(result['vm_id'],cloud.PowerState(result['state']))
    def start(self,vm_id,*,deadline):self.client.post('https://fixture.invalid/vm/'+vm_id+'/start')
    def stop(self,vm_id,*,deadline):self.client.post('https://fixture.invalid/vm/'+vm_id+'/stop')
    @property
    def mutations(self):return [v for v in self.calls if v[0]=='POST']

class Gate:
    """Unit seam only. Actual378 persistent controller integration is separate."""
    def __init__(self):self.busy=0;self.queued=0;self.unacked=0;self.generation=1;self.pending=None;self.published=[];self.allow=True
    @contextmanager
    def fence(self,vm_id,action,request_id):
        owner=self
        if action=='stop'and not self.pending and(self.busy or self.queued or self.unacked):raise cloud.RunnerRefused('not_idle')
        class Grant:
            generation=owner.generation
            def __init__(self):self.vm_id=vm_id;self.action=action;self.request_id=request_id
            def assert_current(self):return owner.allow and self.generation==owner.generation
            def publish_intent(self,intent_id):
                if not self.assert_current():raise cloud.RunnerRefused('stale')
                owner.pending=(intent_id,action,request_id,self.generation);owner.published.append(owner.pending)
        yield Grant()
    def reconcile(self,grant,state):
        if not grant.assert_current():raise cloud.RunnerRefused('stale')
        self.pending=None

class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.guards=[patch(name,side_effect=AssertionError('forbidden_external')) for name in ['socket.socket.connect','socket.socket.connect_ex','socket.getaddrinfo','subprocess.Popen']]
        for guard in self.guards:guard.start();self.addCleanup(guard.stop)
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'journal.sqlite3';self.provider=FakeHTTPProvider();self.gate=Gate()
    def tearDown(self):self.provider.client.close();self.temp.cleanup()
    def adapter(self,**kwargs):
        return cloud.RunnerCloud(vm_id=VM,provider=self.provider,journal_path=self.path,lifecycle_fence=self.gate.fence,reconcile_lifecycle=self.gate.reconcile,enabled=True,**kwargs)
    def deadline(self):return time.monotonic()+10
    def test_disabled_makes_no_journal_or_http(self):
        a=cloud.RunnerCloud(vm_id=VM,provider=self.provider,journal_path=self.path,lifecycle_fence=self.gate.fence,reconcile_lifecycle=self.gate.reconcile)
        with self.assertRaisesRegex(cloud.RunnerRefused,'runner_disabled'):a.ensure_started('one',deadline=self.deadline())
        self.assertFalse(self.path.exists());self.assertEqual(self.provider.calls,[])
    def test_exact_vm_and_no_remote_discovery(self):
        with self.assertRaisesRegex(cloud.RunnerRefused,'invalid_bound_vm'):cloud.RunnerCloud(vm_id='../arbitrary',provider=None,journal_path=self.path,lifecycle_fence=None,reconcile_lifecycle=None)
        a=self.adapter();self.provider.return_vm=OTHER
        with self.assertRaisesRegex(cloud.RunnerRefused,'provider_state_unbound'):a.ensure_started('one',deadline=self.deadline())
        self.assertEqual(self.provider.mutations,[])
        with self.assertRaises(AttributeError):a.vm_id=OTHER
    def test_start_ack_not_ready_then_read_confirms_and_samekey_not_restarted(self):
        a=self.adapter();r=a.ensure_started('one',deadline=self.deadline());self.assertTrue(r.pending);self.assertEqual(r.state,cloud.PowerState.STARTING);self.assertFalse(r.cpu_ram_stopped_confirmed)
        self.provider.state=cloud.PowerState.RUNNING;r=a.reconcile(deadline=self.deadline());self.assertFalse(r.pending);self.assertEqual(len(self.provider.mutations),1)
        self.provider.state=cloud.PowerState.STOPPED;r=a.ensure_started('one',deadline=self.deadline());self.assertEqual(r.state,cloud.PowerState.STOPPED);self.assertEqual(r.code,'request_already_settled');self.assertEqual(len(self.provider.mutations),1)
    def test_postsend_missed_ack_restart_and_newkey_never_resend(self):
        a=self.adapter();self.provider.missed=True;r=a.ensure_started('one',deadline=self.deadline());self.assertEqual(r.code,'mutation_outcome_unknown');self.assertNotIn('sensitive',repr(r));self.assertEqual(len(self.provider.mutations),1)
        a=self.adapter();r=a.ensure_started('different',deadline=self.deadline());self.assertTrue(r.pending);self.assertEqual(r.state,cloud.PowerState.STOPPED);self.assertEqual(len(self.provider.mutations),1)
        self.provider.state=cloud.PowerState.RUNNING;r=a.reconcile(deadline=self.deadline());self.assertFalse(r.pending);self.assertEqual(len(self.provider.mutations),1)
    def test_stop_queued_busy_unacked_denied_and_stopping_is_not_billing_confirmation(self):
        a=self.adapter();self.provider.state=cloud.PowerState.RUNNING
        for key in ['busy','queued','unacked']:
            setattr(self.gate,key,1)
            with self.assertRaisesRegex(cloud.RunnerRefused,'not_idle'):a.stop_if_idle(key,deadline=self.deadline())
            setattr(self.gate,key,0)
        self.assertEqual(self.provider.mutations,[]);r=a.stop_if_idle('idle',deadline=self.deadline());self.assertEqual(r.state,cloud.PowerState.STOPPING);self.assertFalse(r.cpu_ram_stopped_confirmed)
        self.gate.queued=1;r=a.ensure_started('newjob',deadline=self.deadline());self.assertTrue(r.pending);self.assertEqual(len(self.provider.mutations),1)
        self.provider.state=cloud.PowerState.STOPPED;r=a.reconcile(deadline=self.deadline());self.assertTrue(r.cpu_ram_stopped_confirmed);self.assertFalse(r.all_billing_stopped);self.assertFalse(r.pending)
        r=a.ensure_started('newjob',deadline=self.deadline());self.assertEqual(len(self.provider.mutations),2);self.assertEqual(r.state,cloud.PowerState.STARTING)
    def test_durable_pending_is_committed_before_mutation(self):
        a=self.adapter()
        def check(action):
            other=self.adapter();row=other.snapshot()['pending'];self.assertTrue(row['wire_claimed']);self.assertEqual(self.gate.pending[0],row['id'])
        self.provider.on_mutation=check;a.ensure_started('one',deadline=self.deadline())
    def test_bare_bool_or_stale_lease_cannot_dispatch(self):
        a=self.adapter()
        @contextmanager
        def bad(*args):yield True
        a.fence=bad
        with self.assertRaisesRegex(cloud.RunnerRefused,'invalid_lifecycle_grant'):a.ensure_started('one',deadline=self.deadline())
        self.gate.allow=False;a.fence=self.gate.fence
        with self.assertRaisesRegex(cloud.RunnerRefused,'stale_lifecycle_grant'):a.ensure_started('one',deadline=self.deadline())
        self.assertEqual(self.provider.calls,[])
    def test_deadline_and_native_control_priority_preserved(self):
        a=self.adapter()
        with self.assertRaisesRegex(cloud.RunnerRefused,'deadline_exceeded'):a.ensure_started('one',deadline=0)
        class Cancelled(Exception):pass
        def cancel():raise Cancelled('original')
        a.control=cancel
        with self.assertRaises(Cancelled):a.ensure_started('one',deadline=0)
        self.assertEqual(self.provider.calls,[])
    def test_transition_unknown_and_boot_budget_do_not_mutate(self):
        a=self.adapter();self.provider.state=cloud.PowerState.STOPPING;r=a.ensure_started('one',deadline=self.deadline());self.assertEqual(r.code,'provider_transition_wait');self.assertEqual(self.provider.mutations,[])
        self.provider.state=cloud.PowerState.STOPPED;self.provider.missed=True;a.ensure_started('one',deadline=self.deadline());a.wall=lambda:time.time()+1000;r=a.reconcile(deadline=self.deadline());self.assertEqual(r.code,'boot_timeout');self.assertTrue(r.pending);self.assertEqual(len(self.provider.mutations),1)
    def test_invalid_state_and_foreign_journal_never_skip(self):
        a=self.adapter();self.provider.state='bad'
        with self.assertRaisesRegex(cloud.RunnerRefused,'provider_read_unknown'):a.ensure_started('one',deadline=self.deadline())
        self.provider.state=cloud.PowerState.STOPPED;a.ensure_started('one',deadline=self.deadline())
        with self.assertRaisesRegex(cloud.RunnerRefused,'journal_vm_mismatch'):cloud.RunnerCloud(vm_id=OTHER,provider=self.provider,journal_path=self.path,lifecycle_fence=self.gate.fence,reconcile_lifecycle=self.gate.reconcile,enabled=True)

    def test_concurrent_new_keys_cannot_claim_two_mutations(self):
        a=self.adapter();b=self.adapter();barrier=threading.Barrier(2);read=self.provider.read_state
        def simultaneous(vm_id,*,deadline):
            state=read(vm_id,deadline=deadline);barrier.wait(timeout=2);return state
        self.provider.read_state=simultaneous;errors=[]
        def invoke(adapter,key):
            try:adapter.ensure_started(key,deadline=self.deadline())
            except cloud.RunnerRefused as e:errors.append(str(e))
        threads=[threading.Thread(target=invoke,args=(a,'a')),threading.Thread(target=invoke,args=(b,'b'))]
        for t in threads:t.start()
        for t in threads:t.join(timeout=3);self.assertFalse(t.is_alive())
        self.assertEqual(len(self.provider.mutations),1);self.assertEqual(errors,['concurrent_lifecycle_intent']);self.assertIsNotNone(a.snapshot()['pending'])

    def test_lease_lost_after_wire_claim_still_prevents_dispatch(self):
        a=self.adapter();update=a._update
        def lose(operation,**kwargs):
            update(operation,**kwargs)
            if kwargs.get('claim'):self.gate.generation+=1
        a._update=lose
        with self.assertRaisesRegex(cloud.RunnerRefused,'stale_lifecycle_grant'):a.ensure_started('one',deadline=self.deadline())
        self.assertEqual(self.provider.mutations,[]);self.assertTrue(a.snapshot()['pending']['wire_claimed'])

if __name__=='__main__':unittest.main()
