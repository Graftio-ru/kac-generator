"""Opt-in Cloud.ru Compute binding for RunnerCloud; no configuration/secret lookup.

GET one bound VM; PUT exactly one desired state. A 204 is only an ACK:
RunnerCloud's durable journal and later GET own reconciliation/no replay.
Official contract: Cloud.ru virtual-machines api-ref-v3 and console_api auth guide.
"""
import json
import math
import threading
import time
import urllib.error
import urllib.request
import uuid

from .runner_cloud import PowerState, RunnerRefused, VMState


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CloudHTTPProvider:
    COMPUTE = 'https://compute.api.cloud.ru'
    IAM = 'https://iam.api.cloud.ru/api/v1/auth/token'
    MAX_RESPONSE = 262144

    def __init__(self, *, vm_id, project_id, availability_zone_id, control_vm_id,
                 key_id='', secret='', enabled=False, opener=None,
                 clock=time.monotonic, timeout_seconds=10):
        for value in (vm_id, project_id, availability_zone_id, control_vm_id):
            try:
                if str(uuid.UUID(value)) != value:
                    raise ValueError
            except (TypeError, ValueError, AttributeError):
                raise RunnerRefused('cloud_identity_invalid') from None
        if vm_id == control_vm_id:
            raise RunnerRefused('control_vm_must_not_be_runner')
        if type(enabled) is not bool:
            raise RunnerRefused('cloud_enabled_invalid')
        if (type(timeout_seconds) not in (int, float)
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30):
            raise RunnerRefused('cloud_timeout_invalid')
        self.vm_id, self.project_id = vm_id, project_id
        self.zone_id, self.enabled = availability_zone_id, enabled
        self.clock, self.timeout = clock, timeout_seconds
        self._key_id = self._secret_value(key_id)
        self._secret = self._secret_value(secret)
        if enabled and (not self._key_id or not self._secret):
            raise RunnerRefused('cloud_credentials_required')
        self._opener = opener
        self._token, self._token_until = '', 0
        self._token_lock = threading.Lock()

    def __repr__(self):
        return f'CloudHTTPProvider(vm_id={self.vm_id!r}, enabled={self.enabled!r})'

    @staticmethod
    def _secret_value(value):
        if hasattr(value, 'get_secret_value'):
            value = value.get_secret_value()
        if type(value) is not str or len(value) > 16384:
            raise RunnerRefused('cloud_credential_invalid')
        return value

    def _check(self, vm_id, deadline):
        if not self.enabled:
            raise RunnerRefused('cloud_binding_disabled')
        if vm_id != self.vm_id:
            raise RunnerRefused('cloud_vm_not_bound')
        now = self.clock()
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or deadline <= now):
            raise RunnerRefused('cloud_deadline_exceeded')
        return min(self.timeout, deadline - now)

    def _wire(self, method, url, payload, token, deadline, *, iam=False):
        timeout = self._check(self.vm_id, deadline)
        headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        data = None if payload is None else json.dumps(payload, separators=(',', ':')).encode()
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        # No ambient proxy credential forwarding and no cross-origin redirects.
        if self._opener is None:
            self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with self._opener.open(request, timeout=timeout) as response:
                code = response.status
                raw = response.read(self.MAX_RESPONSE + 1)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            raise RunnerRefused(('iam' if iam else 'compute') + '_http_' + str(code)) from None
        except Exception:
            raise RunnerRefused(('iam' if iam else 'compute') + '_transport_unknown') from None
        self._check(self.vm_id, deadline)
        if len(raw) > self.MAX_RESPONSE:
            raise RunnerRefused('cloud_response_too_large')
        return code, raw

    @staticmethod
    def _object(raw):
        try:
            value = json.loads(raw)
            if type(value) is not dict:
                raise ValueError
            return value
        except (ValueError, UnicodeError, TypeError):
            raise RunnerRefused('cloud_response_invalid') from None

    def _access_token(self, deadline):
        if not self._token_lock.acquire(timeout=self._check(self.vm_id, deadline)):
            raise RunnerRefused('cloud_token_wait_deadline')
        try:
            self._check(self.vm_id, deadline)
            if self._token and self.clock() < self._token_until:
                return self._token
            code, raw = self._wire('POST', self.IAM,
                                   {'keyId': self._key_id, 'secret': self._secret}, '', deadline, iam=True)
            if code != 200:
                raise RunnerRefused('iam_response_status')
            token = self._object(raw).get('access_token')
            if (type(token) is not str or not token or len(token) > 16384
                    or any(ord(c) < 33 or ord(c) > 126 for c in token)):
                raise RunnerRefused('iam_token_invalid')
            self._token, self._token_until = token, self.clock() + 3300
            return token
        finally:
            self._token_lock.release()

    def _identity(self, vm_id, deadline):
        self._check(vm_id, deadline)
        code, raw = self._wire('GET', self.COMPUTE + '/api/v1/vms/' + self.vm_id,
                               None, self._access_token(deadline), deadline)
        if code != 200:
            raise RunnerRefused('compute_read_status')
        value = self._object(raw)
        project, zone = value.get('project'), value.get('availability_zone')
        if (value.get('id') != self.vm_id or type(project) is not dict
                or project.get('id') != self.project_id or type(zone) is not dict
                or zone.get('id') != self.zone_id or type(value.get('locked')) is not bool):
            raise RunnerRefused('cloud_response_identity_mismatch')
        # Every documented non-operational/error state remains UNKNOWN.
        state = value.get('state')
        if type(state) is not str:
            raise RunnerRefused('cloud_state_invalid')
        normalized = {'running': PowerState.RUNNING, 'stopped': PowerState.STOPPED,
                      'starting': PowerState.STARTING, 'stopping': PowerState.STOPPING}.get(state, PowerState.UNKNOWN)
        return VMState(self.vm_id, normalized), value['locked']

    def read_state(self, vm_id, *, deadline):
        state, _ = self._identity(vm_id, deadline)
        return state

    def _power(self, vm_id, target, deadline):
        state, locked = self._identity(vm_id, deadline)
        if locked:
            raise RunnerRefused('cloud_vm_locked')
        if state.state is target:
            return  # Read proved desired state already exists; no PUT.
        required = PowerState.STOPPED if target is PowerState.RUNNING else PowerState.RUNNING
        if state.state is not required:
            raise RunnerRefused('cloud_vm_transition_unsafe')
        code, raw = self._wire('PUT', self.COMPUTE + '/api/v1/vms',
                               [{'id': self.vm_id, 'state': target.value}],
                               self._access_token(deadline), deadline)
        if code != 204 or raw:
            raise RunnerRefused('compute_mutation_ack_unknown')

    def start(self, vm_id, *, deadline):
        self._power(vm_id, PowerState.RUNNING, deadline)

    def stop(self, vm_id, *, deadline):
        self._power(vm_id, PowerState.STOPPED, deadline)
