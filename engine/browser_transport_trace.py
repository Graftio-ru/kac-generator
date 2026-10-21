"""Bounded passive document arrival metadata; never quote or health authority."""
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import json
import math
import re
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

from .search.failure_policy import ResearchControlError

_DIAGNOSTICS = ContextVar('browser_trace_diagnostics', default=None)


@contextmanager
def trace_diagnostics(sink):
    token = _DIAGNOSTICS.set(sink)
    try:
        yield
    finally:
        _DIAGNOSTICS.reset(token)


def safe_url(value):
    if value in ('about:blank', 'chrome-error://chromewebdata/'):
        return value
    if type(value) is not str:
        return ''
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            return ''
        # Queries, fragments and userinfo are never retained.
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))[:2048]
    except ValueError:
        return ''


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _reason(value):
    match = re.search(r'(?:net::)?(ERR_[A-Z0-9_]+)\b', value if type(value) is str else '')
    return match.group(1) if match else 'unknown'


class ArrivalTrace:
    MAX_REQUESTS = 256
    MAX_DOCUMENTS = 16
    MAX_POINTS = 64
    MAX_FAILURES = 12

    def __init__(self, requested_url, started=None):
        self.started = time.monotonic() if started is None else started
        self.sink = _DIAGNOSTICS.get()
        self.page = self.channel = None
        self.main_frame = None
        self.requests = {}
        self.native_requests = {}
        self.attempt = 0
        self.generation = 0
        self.data = dict(version=1, role='browser_transport_trace', requested_url=safe_url(requested_url),
                         clock='python_monotonic_event_receipt', capture='unavailable',
                         attempts=[], documents=[], points=[], resources={}, failures=[],
                         first_facts=None, final_facts=None, first_observation=None, final_observation=None, errors=[], dropped=0)
        self.closed = False

    def elapsed(self):
        return round(max(0, time.monotonic() - self.started), 6)

    def error(self, stage, error):
        if len(self.data['errors']) < 8:
            self.data['errors'].append(dict(stage=stage, error_type=type(error).__name__))

    def point(self, stage):
        if stage == 'page_close_start':
            self.data['cleanup_started_at'] = self.elapsed()
        if len(self.data['points']) < self.MAX_POINTS:
            self.data['points'].append(dict(stage=stage, at=self.elapsed(), attempt=self.attempt,
                                            generation=self.generation or None))
        else:
            self.data['dropped'] += 1

    def attach(self, page):
        self.page = page
        page._kac_arrival_trace = self
        self.point('page_acquired')
        try:
            page.on('domcontentloaded', lambda: self.point('domcontentloaded'))
            page.on('load', lambda: self.point('load'))
            self.channel = page.context.new_cdp_session(page)
            tree = self.channel.send('Page.getFrameTree')
            frame = tree.get('frameTree', {}).get('frame', {}) if type(tree) is dict else {}
            self.main_frame = frame.get('id')
            if type(self.main_frame) is not str:
                raise ValueError('main_frame_unavailable')
            for event, handler in [('Network.requestWillBeSent', self.request),
                                   ('Network.responseReceived', self.response),
                                   ('Network.loadingFinished', self.finished),
                                   ('Network.loadingFailed', self.failed)]:
                self.channel.on(event, self.guard(handler))
            # Local protocol observation only; no navigation, interception or request.
            self.channel.send('Network.enable')
            self.data['capture'] = 'cdp_network'
        except ResearchControlError:
            raise
        except Exception as error:
            self.error('cdp_attach', error)
            self.data['capture'] = 'unavailable'

    def guard(self, handler):
        def observe(params):
            if self.closed:
                return
            try:
                handler(params)
            except ResearchControlError:
                raise
            except Exception as error:
                self.error('event', error)
        return observe

    def begin_attempt(self, mode='native_goto'):
        self.attempt += 1
        if len(self.data['attempts']) < 8:
            self.data['attempts'].append(dict(id=self.attempt, mode=mode, start=self.elapsed(),
                                              commit=None, response_url=None, status=None, error=None))
        if mode != 'native_goto':
            self.generation += 1
            if len(self.data['documents']) < self.MAX_DOCUMENTS:
                self.data['documents'].append(dict(attempt=self.attempt, generation=self.generation,
                    delivery_mode=mode, started_at=self.elapsed(), response_at=None, completed_at=None,
                    completion='unknown', encoded_bytes=None, status=None, content_applied_at=None))
        self.point('attempt_start')

    def commit(self, response_url, status):
        if self.data['attempts'] and self.data['attempts'][-1]['id'] == self.attempt:
            self.data['attempts'][-1].update(commit=self.elapsed(), response_url=safe_url(response_url),
                                           status=status if type(status) is int else None)
        mode = self.data['attempts'][-1]['mode'] if self.data['attempts'] else 'unknown'
        if mode != 'native_goto':
            for document in reversed(self.data['documents']):
                if document.get('attempt') == self.attempt and document.get('delivery_mode') == mode:
                    document.update(content_applied_at=self.elapsed(), response_url=safe_url(response_url),
                                    status=status if type(status) is int else None)
                    break
        self.point('commit' if mode == 'native_goto' else 'content_applied')

    def attempt_error(self, error):
        if self.data['attempts'] and self.data['attempts'][-1]['id'] == self.attempt:
            self.data['attempts'][-1]['error'] = dict(at=self.elapsed(), error_type=type(error).__name__,
                                                       reason=_reason(str(error)))
        self.point('attempt_error')

    def request(self, params):
        rid = params.get('requestId')
        if type(rid) is not str or len(rid) > 128:
            return
        previous = self.requests.get(rid)
        if previous is None and len(self.requests) >= self.MAX_REQUESTS:
            self.data['dropped'] += 1
            return
        kind = params.get('type')
        kind = kind if kind in ('Document', 'Script', 'XHR', 'Fetch', 'Stylesheet', 'Image', 'Font', 'Media') else 'Other'
        url = safe_url(params.get('request', {}).get('url'))
        redirect = params.get('redirectResponse')
        if previous and type(redirect) is dict:
            previous.update(response_at=self.elapsed(), status=redirect.get('status') if type(redirect.get('status')) is int else None,
                            completion='redirect', completed_at=self.elapsed(), encoded_bytes=_number(redirect.get('encodedDataLength')))
        main = kind == 'Document' and params.get('frameId') == self.main_frame
        record = dict(attempt=self.attempt, started_at=self.elapsed(), response_at=None, completed_at=None,
                      completion='pending', encoded_bytes=None, status=None, kind=kind,
                      host=urlsplit(url).hostname or '', main=main)
        if main:
            self.generation += 1
            record.update(generation=self.generation, delivery_mode='native_goto', url=url, redirect_from=previous.get('generation') if previous else None)
            if len(self.data['documents']) < self.MAX_DOCUMENTS:
                self.data['documents'].append(record)
            else:
                self.data['dropped'] += 1
        self.requests[rid] = record
        if main:
            loader=params.get('loaderId')
            if type(loader) is str and loader and len(loader)<=128:
                self.native_requests[rid]=dict(frame_id=self.main_frame,loader_id=loader,generation=self.generation,url=None,status=None,failed=False)
            else:self.native_requests.pop(rid,None)
        group = self.data['resources'].setdefault(kind, dict(started=0, completed=0, failed=0,
                                                            known_encoded_bytes=None, byte_observations=0))
        group['started'] += 1

    def response(self, params):
        record = self.requests.get(params.get('requestId'))
        if record is None:
            return
        response = params.get('response', {})
        record.update(response_at=self.elapsed(), status=response.get('status') if type(response.get('status')) is int else None)
        native=self.native_requests.get(params.get('requestId'))
        if native is not None:
            native.update(url=response.get('url'),status=response.get('status'))
        if record['main']:
            record['response_url'] = safe_url(response.get('url'))

    def native_frame_response(self,frame):
        """Private actual response binding, distinct from sanitized passive diagnostics."""
        if self.closed or type(frame) is not dict or frame.get('id')!=self.main_frame or not self.native_requests:return None
        latest=max(self.native_requests.values(),key=lambda v:v['generation'])
        if latest['generation']!=self.generation or latest['failed'] or latest['loader_id']!=frame.get('loaderId'):return None
        url=latest['url'];status=latest['status']
        if type(url) is not str or type(status) is not int or not 200<=status<400:return None
        try:
            parsed=urlsplit(url)
            if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:return None
            # A fragment is client navigation, never a different wire document.
            if urlsplit(frame.get('url',''))._replace(fragment='')!=parsed._replace(fragment=''):return None
        except (ValueError,TypeError):return None
        return dict(latest)

    def finished(self, params):
        record = self.requests.get(params.get('requestId'))
        if record is None:
            return
        record.update(completion='finished', completed_at=self.elapsed(), during_cleanup='cleanup_started_at' in self.data, encoded_bytes=_number(params.get('encodedDataLength')))
        group = self.data['resources'][record['kind']]
        group['completed'] += 1
        if record['encoded_bytes'] is not None:
            group['known_encoded_bytes'] = (group['known_encoded_bytes'] or 0) + record['encoded_bytes']
            group['byte_observations'] += 1

    def failed(self, params):
        native=self.native_requests.get(params.get('requestId'))
        if native is not None:native['failed']=True
        record = self.requests.get(params.get('requestId'))
        if record is None:
            return
        reason = _reason(params.get('errorText'))
        record.update(completion='failed', completed_at=self.elapsed(), during_cleanup='cleanup_started_at' in self.data, failure_reason=reason)
        self.data['resources'][record['kind']]['failed'] += 1
        if len(self.data['failures']) < self.MAX_FAILURES:
            self.data['failures'].append(dict(at=self.elapsed(), attempt=record['attempt'],
                generation=record.get('generation'), kind=record['kind'], host=record['host'], reason=reason))
        else:
            self.data['dropped'] += 1

    def facts(self, phase, fields, reference):
        # Only field counts/readiness and owned evidence pointers; never content.
        record = dict(at=self.elapsed(), attempt=self.attempt, generation=self.generation or None, phase=phase,
                      kind=fields.get('kind') or fields.get('observation_status'),
                      body_length=len(fields['body']) if type(fields.get('body')) is str else None,
                      title_length=len(fields.get('main_title', '')) if type(fields.get('main_title')) is str else None,
                      missing_fields=[x for x in fields.get('missing_fields', []) if x in ('title', 'article', 'price', 'sale_unit')],
                      reference={k:reference[k] for k in ('ref', 'sha256', 'observed_at') if k in reference})
        prefix = 'facts' if phase == 'coherent_main_facts' else 'observation'
        if self.data['first_' + prefix] is None:
            self.data['first_' + prefix] = record
        self.data['final_' + prefix] = record

    def finalize(self, cache_dir, error=None):
        if self.closed:
            return
        self.closed = True
        control = None
        self.data.update(ended_at=self.elapsed(), outcome='error' if error else 'returned',
                         primary_error_type=type(error).__name__ if error else None)
        if self.channel is not None:
            try:
                self.channel.detach()
            except ResearchControlError as cleanup:
                control = cleanup
                self.error('cdp_detach', cleanup)
            except Exception as cleanup:
                self.error('cdp_detach', cleanup)
        try:
            folder = cache_dir / 'browser_transport_traces'
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (uuid.uuid4().hex + '.json')
            raw = json.dumps(self.data, ensure_ascii=False, allow_nan=False).encode()
            with path.open('xb') as file:
                file.write(raw)
            reference = dict(ref=str(path.relative_to(cache_dir)), sha256=sha256(raw).hexdigest())
            if self.page is not None:
                self.page._kac_browser_transport_trace = reference
            if error is not None:
                error._kac_browser_transport_trace = reference
            if self.sink is not None:
                self.sink.append(dict(code='browser_transport_trace', reason='Passive arrival metadata retained',
                                      browser_transport_trace=reference))
        except Exception as retention:
            # Observability cannot replace an extraction/control/cleanup exception.
            if self.sink is not None:
                self.sink.append(dict(code='browser_transport_trace_unavailable', reason='Passive trace retention unavailable',
                                      error_type=type(retention).__name__))
        if control is not None:
            if error is None:
                raise control
            error.add_note('Passive trace cleanup control: ' + type(control).__name__)
