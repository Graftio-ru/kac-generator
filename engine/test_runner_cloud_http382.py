"""Synthetic HTTP contract checks: never load Settings or call any cloud service."""
import io
import json
import tempfile
import time
import unittest
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from .runner_cloud import PowerState, RunnerCloud, RunnerRefused
from .runner_cloud_http import CloudHTTPProvider, _NoRedirect

VM = '00000000-0000-4000-8000-000000000001'
PROJECT = '00000000-0000-4000-8000-000000000002'
ZONE = '00000000-0000-4000-8000-000000000003'
CONTROL = '00000000-0000-4000-8000-000000000004'


class Response(io.BytesIO):
    def __init__(self, status, payload):
        super().__init__(payload)
        self.status = status


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.state, self.locked = 'stopped', False
        self.project, self.zone, self.vm = PROJECT, ZONE, VM
        self.fail_put, self.fail_get, self.token = False, False, 'synthetic-token'
        self.put_status, self.iam_status = 204, 200
        self.raw_get = None

    def open(self, request, timeout):
        self.calls.append((request.get_method(), request.full_url, request.data, dict(request.headers), timeout))
        if request.full_url == CloudHTTPProvider.IAM:
            return Response(self.iam_status, json.dumps({'access_token': self.token}).encode())
        if request.get_method() == 'GET':
            if self.fail_get:
                raise urllib.error.URLError('synthetic secret must not be exposed')
            value = {'id': self.vm, 'project': {'id': self.project},
                     'availability_zone': {'id': self.zone}, 'state': self.state, 'locked': self.locked}
            return Response(200, self.raw_get if self.raw_get is not None else json.dumps(value).encode())
        if request.get_method() == 'PUT':
            value = json.loads(request.data)
            self.state = value[0]['state']
            if self.fail_put:
                raise urllib.error.URLError('synthetic secret must not be exposed')
            return Response(self.put_status, b'')
        raise AssertionError('unexpected synthetic request')


class CloudHTTPTests(unittest.TestCase):
    def setUp(self):
        self.http = FakeHTTP()
        self.now = time.monotonic()
        self.deadline = self.now + 20

    def provider(self, **kw):
        args = dict(vm_id=VM, project_id=PROJECT, availability_zone_id=ZONE,
                    control_vm_id=CONTROL, key_id='synthetic-id', secret='synthetic-secret',
                    enabled=True, opener=self.http)
        args.update(kw)
        return CloudHTTPProvider(**args)

    def test_disabled_wrong_vm_and_control_vm_never_call_http(self):
        with self.assertRaises(RunnerRefused):
            self.provider(enabled=False).read_state(VM, deadline=self.deadline)
        with self.assertRaises(RunnerRefused):
            self.provider().start(CONTROL, deadline=self.deadline)
        with self.assertRaises(RunnerRefused):
            self.provider(control_vm_id=VM)
        self.assertEqual(self.http.calls, [])

    def test_exact_get_iam_put_contract_and_token_cached(self):
        p = self.provider()
        self.assertEqual(p.read_state(VM, deadline=self.deadline).state, PowerState.STOPPED)
        p.start(VM, deadline=self.deadline)
        self.assertEqual(p.read_state(VM, deadline=self.deadline).state, PowerState.RUNNING)
        iam = [c for c in self.http.calls if c[1] == p.IAM]
        self.assertEqual(len(iam), 1)
        self.assertEqual(json.loads(iam[0][2]), {'keyId':'synthetic-id', 'secret':'synthetic-secret'})
        puts = [c for c in self.http.calls if c[0] == 'PUT']
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0][1], p.COMPUTE + '/api/v1/vms')
        self.assertEqual(json.loads(puts[0][2]), [{'id': VM, 'state':'running'}])
        self.assertTrue(all(c[1] in (p.IAM,p.COMPUTE+'/api/v1/vms',p.COMPUTE+'/api/v1/vms/'+VM) for c in self.http.calls))
        self.assertTrue(all(0 < c[4] <= 10 for c in self.http.calls))

    def test_identity_and_lock_prevent_mutation(self):
        for field, value in [('project', CONTROL), ('zone', CONTROL), ('vm', CONTROL), ('locked', True)]:
            with self.subTest(field=field):
                self.setUp()
                setattr(self.http,field,value)
                with self.assertRaises(RunnerRefused):
                    self.provider().start(VM,deadline=self.deadline)
                self.assertFalse(any(c[0]=='PUT' for c in self.http.calls))

    def test_nonoperational_states_never_mutate(self):
        for state in ('creating','error','updating','rebooting','deleting','new-unknown-state'):
            with self.subTest(state=state):
                self.setUp(); self.http.state=state
                p=self.provider()
                self.assertEqual(p.read_state(VM,deadline=self.deadline).state,PowerState.UNKNOWN)
                with self.assertRaises(RunnerRefused):p.start(VM,deadline=self.deadline)
                self.assertFalse(any(c[0]=='PUT' for c in self.http.calls))

    def test_starting_stopping_are_not_terminal_and_already_stopped_no_put(self):
        p=self.provider()
        p.stop(VM,deadline=self.deadline)
        self.assertFalse(any(c[0]=='PUT' for c in self.http.calls))
        for raw,state in [('starting',PowerState.STARTING),('stopping',PowerState.STOPPING)]:
            self.http.state=raw
            self.assertEqual(p.read_state(VM,deadline=self.deadline).state,state)

    def test_bad_token_body_and_large_response_fail_without_mutation(self):
        self.http.token='bad\r\nInjected: header'
        with self.assertRaisesRegex(RunnerRefused,'iam_token_invalid'):self.provider().start(VM,deadline=self.deadline)
        self.assertEqual(len(self.http.calls),1)
        self.setUp();self.http.raw_get=b'x'*(CloudHTTPProvider.MAX_RESPONSE+1)
        with self.assertRaisesRegex(RunnerRefused,'too_large'):self.provider().start(VM,deadline=self.deadline)
        self.assertFalse(any(c[0]=='PUT' for c in self.http.calls))

    def test_errors_and_repr_never_expose_credentials_or_response(self):
        self.http.fail_get=True;p=self.provider()
        with self.assertRaises(RunnerRefused) as caught:p.start(VM,deadline=self.deadline)
        self.assertEqual(str(caught.exception),'compute_transport_unknown')
        self.assertNotIn('synthetic',repr(p))

    def test_no_redirect_forwarding(self):
        self.assertIsNone(_NoRedirect().redirect_request(None,None,302,'redirect',{},'https://other.invalid'))

    def test_expired_deadline_no_http(self):
        with self.assertRaises(RunnerRefused):self.provider().read_state(VM,deadline=time.monotonic()-1)
        self.assertEqual(self.http.calls,[])

    def test_iam_refresh_before_one_hour(self):
        clock=[100.0];p=self.provider(clock=lambda:clock[0])
        p.read_state(VM,deadline=10000)
        clock[0]=3401
        p.read_state(VM,deadline=10000)
        self.assertEqual(sum(c[1]==p.IAM for c in self.http.calls),2)

    def test_core_ack_loss_reconciles_without_replaying_put(self):
        self.http.fail_put=True
        generation=1
        settled=[]
        @contextmanager
        def fence(vm,action,key):
            yield SimpleNamespace(vm_id=vm,action=action,request_id=key,generation=generation,
                                  assert_current=lambda:True,publish_intent=lambda receipt:None)
        with tempfile.TemporaryDirectory() as tmp:
            def core():
                return RunnerCloud(vm_id=VM,provider=self.provider(),journal_path=Path(tmp)/'power.sqlite',
                                   lifecycle_fence=fence,reconcile_lifecycle=lambda g,s:settled.append(s),enabled=True)
            first=core().ensure_started('one-request',deadline=self.deadline)
            self.assertTrue(first.pending)
            self.assertEqual(first.state,PowerState.UNKNOWN)
            last=core().reconcile(deadline=self.deadline)
            self.assertFalse(last.pending)
            self.assertEqual(last.state,PowerState.RUNNING)
            self.assertEqual(sum(c[0]=='PUT' for c in self.http.calls),1)
            self.assertEqual(settled,[PowerState.RUNNING])


if __name__=='__main__':unittest.main()
