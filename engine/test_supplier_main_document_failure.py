"""Synthetic CDP at the real browser and native catalogue boundaries."""
import json
import time
import unittest
from contextlib import contextmanager
from hashlib import sha256
from unittest.mock import patch
from urllib.parse import urlsplit


class NativeFailureChecks(unittest.TestCase):
    """Use synthetic page transports and real native scheduling and storage."""

    def scenario(self, case, *, evidence=None, prior_unknown_next=False,
                 native_first_capture=False, next_transport=True):
        """Run a native card failure, optionally through the production capture adapter."""
        from playwright.sync_api import Error, TimeoutError
        from playwright._impl._errors import TargetClosedError
        from engine import app, browser as browser_module, config
        from engine.schemas import PositionIn
        from engine.search.catalogue import identity_for_position
        from engine.search.catalogue_producer import capture_and_publish
        from engine.search.failure_policy import supplier_card_transport_diagnostic
        from engine.search.search_engine import DiscoveryURLs
        from engine.search.session import research_session, transport_started
        from engine.test_browser_lifecycle import BrowserLifecycleTests
        from engine.test_catalogue_unknown import CatalogueUnknownTests

        fixture = CatalogueUnknownTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        browser, page = BrowserLifecycleTests().page_browser()
        primary = 'https://s0.test/card'
        page.context = browser.context
        browser._browser = browser.context.browser
        browser._browser.is_connected.return_value = True
        page.is_closed.return_value = False
        page._kac_owned_browser_failure = None
        page._kac_context_cleanup_required = False
        channel = browser.context.new_cdp_session.return_value
        channel.send.return_value = {'frameTree': {'frame': {'id': 'main'}}}
        next_card = 'https://s1.test/card'
        retained_url = 'https://s2.test/saved'
        position = PositionIn(number=12, name='Cable', type_mark='ABC123', unit='м', qty=1)
        retained = position.model_copy(update={'number': 23, 'type_mark': 'SAVED23'})
        identity = identity_for_position(retained)
        seed = fixture.store.acquire_unknown(identity, fixture.hosts, owner='seed-fixture')
        fixture.capture(fixture.store, identity, retained.to_result().position, retained_url,
                        browser=None, image_store=None, acquisition=seed, supplier_host='s2.test')
        self.assertTrue(fixture.store.finish_acquisition(seed))
        error = {
            'unclassified': Error('SYNTHETIC_FIXTURE_TEXT'),
            'local_timeout': TimeoutError('SYNTHETIC_FIXTURE_TEXT'),
            'context_closed': TargetClosedError('SYNTHETIC_FIXTURE_TEXT'),
            'application': RuntimeError('SYNTHETIC_FIXTURE_TEXT'),
            'ownership': Error('SYNTHETIC_FIXTURE_TEXT'),
            'cancel': Error('SYNTHETIC_FIXTURE_TEXT'),
            'deadline': Error('SYNTHETIC_FIXTURE_TEXT'),
            'legacy_network_error': Error('SYNTHETIC_FIXTURE_TEXT net::ERR_CONNECTION_RESET'),
            'unrecognized_token': Error('SYNTHETIC_FIXTURE_TEXT net::ERR_FAILED'),
            'forged_context': Error('SYNTHETIC_FIXTURE_TEXT scope=local cause=network_error'),
        }[case]
        calls, rpc, diagnostics = [], [], []
        revision = 1
        monotonic = time.monotonic
        elapsed = 0

        def clock():
            """Advance only upon the synthetic deadline event at goto."""
            return monotonic() + elapsed

        @contextmanager
        def recovery_channel(page, deadline):
            """Supply frame state without entering Playwright's SDK loop in this fixture."""
            if evidence == 'cdp_recovery':
                raise RuntimeError('SYNTHETIC_FIXTURE_TEXT')
            yield channel

        def selected(url, expected=None):
            """Return the same synthetic authorized browser revision for either fake supplier."""
            return dict(host=urlsplit(url).hostname, method='browser', revision=revision,
                        state='configured_unchecked')

        def methods_call(action, payload=None):
            """Observe method-update intentions without changing any actual settings."""
            self.assertEqual(action, 'failure')
            rpc.append(payload)
            if payload['revision'] != revision:
                return {'recorded': False, 'stale': True}
            if evidence == 'unknown_method_receipt':
                return {}
            return {'recorded': True, 'stale': False}

        def search(query, hosts, **kwargs):
            """Offer exactly two synthetic cards in their original discovery order."""
            urls = [url for url in (primary,next_card) if urlsplit(url).hostname in hosts]
            observations = {}
            for host in hosts:
                candidates = [url for url in urls if urlsplit(url).hostname == host]
                observations[host] = dict(origin='response', window='below_limit', observed_at=1_700_000_000,
                                          candidate_count=len(candidates), candidate_urls=candidates, diagnostics=[])
            return DiscoveryURLs(urls, origin='response', window='below_limit', host_observations=observations)

        def goto(*args, **kwargs):
            """Inject only one upstream browser error, optionally changing native control state."""
            nonlocal revision, elapsed
            listeners = dict(call.args for call in channel.on.call_args_list)
            request = listeners['Network.requestWillBeSent']
            fail = listeners['Network.loadingFailed']
            if evidence:
                url = {'foreign': 'https://other.test/card',
                       'query': 'https://s0.test/card?different=1'}.get(evidence, primary)
                first = dict(requestId='first', loaderId='loader',
                             frameId='sub' if evidence == 'subframe' else 'main',
                             type='Script' if evidence == 'subresource' else 'Document', request={'url': url})
                if evidence == 'redirect':
                    first['redirectResponse'] = {'status': 302}
                request(first)
                if evidence in ('repeat', 'stale'):
                    request(dict(requestId='first' if evidence == 'repeat' else 'second', loaderId='next',
                                 type='Document', frameId='main', request={'url': primary}))
                payload = dict(requestId='first', type='Document', errorText='net::ERR_CONNECTION_REFUSED')
                if evidence == 'aborted':
                    payload['canceled'] = True
                if evidence == 'unknown':
                    payload['errorText'] = 'net::ERR_FAILED'
                if evidence == 'proxy_code':
                    payload['errorText'] = 'net::ERR_PROXY_CONNECTION_FAILED'
                if evidence == 'response':
                    listeners['Network.responseReceived'](dict(requestId='first', response={'url': primary, 'status': 200}))
                if evidence == 'late_failure':
                    def late_frame_tree(*unused):
                        """Send the failure after goto exits, during the read-only recovery probe."""
                        fail(payload)
                        return {'frameTree': {'frame': {'id': 'main'}}}

                    channel.send.side_effect = late_frame_tree
                else:
                    fail(payload)
                    if evidence == 'duplicate_failure':
                        fail(payload)
                if evidence == 'context':
                    page.is_closed.return_value = True
                if evidence == 'disconnected':
                    browser._browser.is_connected.return_value = False
                if evidence == 'revision':
                    revision += 1
            if case == 'ownership':
                running = next(value for value in app.RESEARCH_JOBS.values() if value['status'] == 'running')
                self.assertTrue(fixture.store.acquisition_owner_matches(
                    identity_for_position(position).key, owner=running['job_id'],
                    expected_identity=identity_for_position(position)))
                fixture.store.cancel_acquisition(owner_grant[0])
            if case == 'cancel':
                running = next(value for value in app.RESEARCH_JOBS.values() if value['status'] == 'running')
                running['cancel_requested'] = True
            if case == 'deadline':
                elapsed = 60
            raise error

        def capture(store, product, native_pos, url, **kwargs):
            """Invoke actual Browser.page on the first source and a saved-offer fixture on the second."""
            calls.append(url)
            if url == primary:
                owner_grant[:] = [kwargs['acquisition']]
                if prior_unknown_next:
                    prior = store.card_operation(product.key, 's1.test', next_card,
                                                 operation_id='c' * 32, acquisition=kwargs['acquisition'])
                    self.assertTrue(prior['allowed'])
                if evidence == 'cleanup':
                    page.close.side_effect = RuntimeError('SYNTHETIC_FIXTURE_TEXT')
                if evidence == 'cdp_detach':
                    channel.detach.side_effect = RuntimeError('SYNTHETIC_FIXTURE_TEXT')
                if case != 'application':
                    page.goto.side_effect = goto
                try:
                    if native_first_capture:
                        return capture_and_publish(store, product, native_pos, url, **{**kwargs, 'browser': browser})
                    with browser.page(url):
                        raise error
                except BaseException as observed:
                    diagnostics.append(supplier_card_transport_diagnostic(observed, 's0.test'))
                    raise
            self.assertEqual(url, next_card)
            if next_transport:
                if native_first_capture:
                    # Enter the real HTTP wrapper with only the physical request stubbed out.
                    with patch.object(browser_module.httpx, 'get', return_value=None) as wire:
                        browser_module._supplier_http_get_unchecked(url)
                        wire.assert_called_once()
                else:
                    transport_started('supplier_http', url)
            kwargs.pop('photo_only', None)  # The inherited saved-offer fixture predates this optional argument.
            return fixture.capture(store, product, native_pos, url, **kwargs)

        owner_grant = []
        proxy = 'http://example.test:8080' if evidence == 'proxy' else ''
        with patch.object(browser_module.settings, 'browser_proxy_server', proxy), \
             patch.object(browser_module, 'CACHE_DIR', fixture.root), \
             patch.object(config, 'CACHE_DIR', fixture.root), \
             patch('engine.search.agent.CACHE_DIR', fixture.root), \
             patch('engine.bounded_cdp.page_cdp', side_effect=recovery_channel), \
             patch('engine.supplier_methods.selected', side_effect=selected), \
             patch('engine.supplier_methods._call', side_effect=methods_call), \
             patch('time.monotonic', side_effect=clock), \
             research_session(deadline=clock() + 30 if case == 'deadline' else None):
            _, public = fixture.job([position, retained], search=search, capture=capture)
        self.assertEqual(calls[0], primary)
        page.goto.assert_called_once()
        page.close.assert_called_once()
        safe = public.get('first_stop') or {}
        if evidence == 'query':
            traces = list((fixture.root / 'browser_transport_traces').glob('*.json'))
            self.assertEqual(len(traces), 1)
            self.assertNotIn('different=1', traces[0].read_text())
        self.assertNotIn('SYNTHETIC_FIXTURE_TEXT', str(public))
        retained_offers = public['rows'][1]['offers'] + public['rows'][1]['candidates']
        self.assertTrue(any(row['url'] == retained_url and row['price_with_vat'] == 150 for row in retained_offers))
        captures = fixture.store.lookup(identity_for_position(position))
        ledger = [a['card_operation']['state'] for a in captures['refresh_attempts']
                  if a.get('card_operation') and a.get('url') == primary]
        self.assertEqual(ledger, ['pending'])
        print(json.dumps(dict(
            case=case, evidence=evidence, job=public['status'], category=safe.get('code'),
            cause=safe.get('failure_cause'), diagnostic=diagnostics[0]['code'] if diagnostics[0] else None,
            next_supplier=next_card in calls, retained_price_present=True, failed_card_ledger=ledger,
            method_requests=[item['cause'] for item in rpc],
            fatal_origin=(safe.get('failure_provenance') or {}).get('frames', [])[-2:],
        )))
        return public, calls, diagnostics, fixture, position

    def attribution_labels(self, snapshot):
        """Read typed labels from the same row metrics exposed to the diagnostic GET."""
        from engine.research_publication import SUPPLIER_CARD_ATTRIBUTION, valid_supplier_card_attribution

        labels = []
        for row in snapshot['rows']:
            if row is None:
                continue
            for entry in row['metrics'].get('events', []):
                if entry['stage'] == SUPPLIER_CARD_ATTRIBUTION:
                    label = json.loads(entry['label'])
                    self.assertTrue(valid_supplier_card_attribution(label))
                    self.assertEqual(label['job_id'], snapshot['job_id'])
                    self.assertEqual(label['position_index'], row['index'])
                    self.assertEqual(label['product_id'], row['catalogue_product_id'])
                    labels.append(label)
        return labels

    def test_attribution_survives_durable_runner_checkpoint_and_status_get(self):
        """Read both ordered, bound records through the real runner projection and ASGI route."""
        from fastapi.testclient import TestClient
        from engine import app
        from engine.research_publication import IntegrationStore
        from engine.runner_control import RunnerControl
        from engine.runner_executor import DirectControl
        from engine.runner_protocol import canonical
        from types import SimpleNamespace

        native, calls, _, fixture, _ = self.scenario('unclassified', evidence='valid', native_first_capture=True)
        job_id = native['job_id']
        cold_store = IntegrationStore(fixture.root/'jobs')
        self.addCleanup(cold_store.close)
        snapshot = cold_store.public(job_id)
        self.assertEqual(snapshot['status'], 'done')
        control_root = fixture.root/'runner-control-attribution'
        seal = 'a' * 64
        control = RunnerControl(control_root, seal)
        vm = 'de4fbff1-83ab-449b-8b35-b3dbcc3816db'
        with control.lifecycle_fence(vm, 'start', 'attribution-fixture') as grant:
            grant.publish_intent('synthetic-start')
            control.reconcile_lifecycle(grant, 'running')
        req = {'positions': [{'number': 12}, {'number': 23}], 'acquisition_mode': 'missing_data'}
        with patch('engine.runner_control.uuid.uuid4', return_value=SimpleNamespace(hex=job_id)):
            self.assertEqual(control.enqueue(req, 'supplier_attribution_fixture', 'server-basic:fixture'), job_id)
        claim = control.claim('fixture-runner', seal)
        client = DirectControl(control)
        credentials = (claim['lease_token'], claim['fence'])
        client.action(job_id, 'begin', *credentials)
        client.action(job_id, 'checkpoint', *credentials, sequence=0,
                      public_snapshot=json.loads(canonical(snapshot)))
        # Read from a fresh CONTROL instance so the observed values come from persisted bytes.
        with patch.object(app, '_remote_control', return_value=True), \
             patch.object(app, '_runner_store', return_value=RunnerControl(control_root, seal)), \
             TestClient(app.app) as http:
            response = http.get(f'/research/{job_id}')
        self.assertEqual(response.status_code, 200)
        published = response.json()
        self.assertTrue(published['runner_snapshot'])
        projected = published['rows'][0]['metrics']['events']
        self.assertIn('supplier_card_attribution', [event['stage'] for event in projected])
        failure, continuation = self.attribution_labels(published)
        self.assertEqual([failure, continuation], self.attribution_labels(native))
        self.assertEqual((failure['sequence'], continuation['sequence']), (1, 2))
        self.assertEqual((failure['event'], continuation['event']),
                         ('structural_failure_accepted', 'other_supplier_transport_entered'))
        self.assertEqual(continuation['transport'], 'supplier_http')
        self.assertEqual(failure['proof_id'], continuation['proof_id'])
        self.assertNotEqual(failure['operation_id'], continuation['operation_id'])
        self.assertEqual(failure['transport_error_code'], 'ERR_CONNECTION_REFUSED')
        self.assertNotIn('transport_error_code', continuation)
        self.assertEqual([failure['supplier_sha256'], continuation['supplier_sha256']],
                         [sha256(host.encode()).hexdigest() for host in ('s0.test', 's1.test')])
        self.assertEqual(calls[:2], ['https://s0.test/card', 'https://s1.test/card'])
        self.assertEqual(calls.count('https://s0.test/card'), 1)
        self.assertNotIn('https://', json.dumps([failure, continuation]))
        self.assertNotIn('SYNTHETIC_FIXTURE_TEXT', json.dumps([failure, continuation]))
        ledger = published['rows'][0]['catalogue_refresh_attempts']
        for label, states in ((failure, {'pending'}), (continuation, {'pending', 'resolved'})):
            entries = [row['card_operation'] for row in ledger if
                       (row.get('card_operation') or {}).get('operation_id') == label['operation_id']]
            self.assertEqual({entry['state'] for entry in entries}, states)

    def test_attribution_survives_without_claiming_a_ledger_blocked_start(self):
        """An unresolved continuation is still pending and creates no new start record."""
        public, calls, _, _, _ = self.scenario('unclassified', evidence='valid', prior_unknown_next=True)
        self.assertEqual(calls, ['https://s0.test/card'])
        failure, = self.attribution_labels(public)
        self.assertEqual(failure['event'], 'structural_failure_accepted')

    def test_admitted_callback_without_native_transport_does_not_prove_fresh_start(self):
        """A card callback returning saved facts without a transport signal emits no continuation."""
        public, calls, _, _, _ = self.scenario('unclassified', evidence='valid', next_transport=False)
        self.assertEqual(calls[:2], ['https://s0.test/card', 'https://s1.test/card'])
        self.assertEqual(public['status'], 'done')
        failure, = self.attribution_labels(public)
        self.assertEqual(failure['event'], 'structural_failure_accepted')

    def test_old_immutable_telemetry_policy_keeps_existing_card_behavior(self):
        """A job created with the previous policy can run without an attribution stage."""
        from engine import app

        policy = app._research_policy()
        policy['stages'] = [stage for stage in policy['stages'] if stage != 'supplier_card_attribution']
        with patch.object(app, '_research_policy', return_value=policy):
            public, calls, diagnostics, _, _ = self.scenario('unclassified', evidence='valid')
        self.assertEqual(public['status'], 'done')
        self.assertEqual(diagnostics[0]['code'], 'supplier_card_network_error')
        self.assertIn('https://s1.test/card', calls)
        self.assertEqual(self.attribution_labels(public), [])

    def test_attribution_accepts_only_a_fresh_distinct_supplier_and_closed_label(self):
        """Incomplete or unsafe labels cannot become durable evidence."""
        from engine.research_publication import IntegrationStore, STAGES, SupplierCardAttribution

        attribution = SupplierCardAttribution()
        binding = dict(job_id='a' * 12, product_id='b' * 64, task_id='c' * 64,
                       operation_id='d' * 32, generation=1, position_index=0)
        self.assertIsNone(attribution.transport_entered(binding, 's1.test', 'supplier_http'))
        accepted = attribution.accepted(binding, 's0.test', 'ERR_CONNECTION_REFUSED')
        self.assertIsNone(attribution.transport_entered(binding, 's1.test', 'supplier_http'))
        changed_operation = dict(binding, operation_id='e' * 32, product_id='e' * 64, position_index=1)
        self.assertIsNone(attribution.transport_entered(changed_operation, 's0.test', 'supplier_http'))
        self.assertIsNone(attribution.transport_entered(dict(changed_operation, job_id='f' * 12), 's1.test', 'supplier_http'))
        self.assertIsNone(attribution.transport_entered(changed_operation, 's1.test', 'unknown'))
        self.assertIsNotNone(attribution.transport_entered(changed_operation, 's1.test', 'supplier_http'))
        self.assertIsNone(attribution.transport_entered(dict(binding, operation_id='f' * 32), 's2.test', 'supplier_navigation'))
        policy = dict(stages=STAGES, usage={}, labels=[''])
        self.assertEqual(IntegrationStore._sanitize(accepted, policy), accepted)
        label = json.loads(accepted['label'])
        invalid_labels = (
            '',
            json.dumps(dict(label, url='https://example.test/card?secret=sentinel')),
            json.dumps(dict(label, transport_error_code='UNTRUSTED_ERROR_TEXT')),
            json.dumps(dict(label, supplier_sha256='https://example.test')),
            json.dumps(dict(label, sequence=True)),
        )
        for invalid in invalid_labels:
            self.assertEqual(IntegrationStore._sanitize(dict(accepted, label=invalid), policy),
                             {'stage': 'telemetry_rejected', 'reason_code': 'invalid_stage'})

    def test_passive_transport_observer_preserves_authority_and_scope(self):
        """The existing callback still executes first and can refuse before observation."""
        from unittest.mock import Mock
        from engine.search.session import observe_transport_started, research_session, transport_started

        prior = Mock()
        observer = Mock(side_effect=RuntimeError('observer fixture'))
        wire = ('supplier_http', 'https://s0.test/card')
        with research_session(on_transport=prior):
            with observe_transport_started(observer):
                transport_started(*wire)
                observer.assert_called_once_with(*wire)
                prior.side_effect = RuntimeError('authority fixture')
                with self.assertRaisesRegex(RuntimeError, 'authority fixture'):
                    transport_started(*wire)
                observer.assert_called_once()
            prior.side_effect = None
            transport_started(*wire)
        self.assertEqual(prior.call_count, 3)
        observer.assert_called_once()

    def test_proven_current_main_document_stays_local(self):
        """A generic goto error with verified CDP facts must not stop another supplier."""
        from engine.search.catalogue import identity_for_position

        public,calls,diagnostics,fixture,position = self.scenario('unclassified',evidence='valid')
        self.assertEqual(public['status'],'done')
        self.assertIn('https://s1.test/card',calls)
        self.assertEqual(diagnostics[0]['code'],'supplier_card_network_error')
        self.assertEqual(diagnostics[0]['transport_error_code'],'ERR_CONNECTION_REFUSED')
        self.assertFalse(public.get('first_stop'))
        checks = {r['host']:r for r in public['rows'][0]['source_checks']}
        self.assertEqual(checks['s0.test']['status'],'review')
        self.assertEqual(checks['s0.test']['product_presence'],'unknown')
        self.assertEqual(checks['s1.test']['status'],'found')
        retry = fixture.store.card_operation(identity_for_position(position).key,'s0.test','https://s0.test/card',
                                             operation_id='a'*32)
        self.assertFalse(retry['allowed'])

    def test_unbound_main_document_evidence_stays_global(self):
        """Transport tokens cannot promote stale, foreign or unhealthy evidence."""
        for evidence in ('foreign','query','repeat','stale','redirect','subframe','subresource','aborted',
                         'unknown','response','duplicate_failure','late_failure','context','disconnected','proxy','proxy_code',
                         'revision','unknown_method_receipt','cleanup','cdp_recovery','cdp_detach'):
            with self.subTest(evidence=evidence):
                public,calls,diagnostics,_,_ = self.scenario('unclassified',evidence=evidence)
                self.assertEqual((public['status'],public['first_stop']['code'],len(calls)),('error','internal_error',1))
                self.assertEqual(diagnostics,[None])
                self.assertEqual(self.attribution_labels(public), [])
                self.doCleanups()

    def test_existing_network_error_stays_local_without_cdp(self):
        """Preserve the existing supported browser annotation without any CDP evidence."""
        public, calls, diagnostics, _, _ = self.scenario('legacy_network_error')
        self.assertEqual(public['status'], 'done')
        self.assertIn('https://s1.test/card', calls)
        self.assertEqual(diagnostics[0]['code'], 'supplier_card_network_error')
        self.assertEqual(diagnostics[0]['transport_error_code'], 'ERR_CONNECTION_RESET')

    def test_structured_fallback_does_not_replace_existing_annotation(self):
        """A valid CDP code leaves an existing, different diagnostic code intact."""
        public, calls, diagnostics, _, _ = self.scenario('legacy_network_error', evidence='valid')
        self.assertEqual(public['status'], 'done')
        self.assertIn('https://s1.test/card', calls)
        self.assertEqual(diagnostics[0]['transport_error_code'], 'ERR_CONNECTION_RESET')
        self.assertEqual(self.attribution_labels(public), [])

    def test_unrecognized_or_forged_message_without_cdp_is_fatal(self):
        """An unknown code or text which resembles a diagnostic cannot authorize the fallback."""
        for case in ('unrecognized_token', 'forged_context'):
            with self.subTest(case=case):
                public, calls, diagnostics, _, _ = self.scenario(case)
                self.assertEqual((public['status'], public['first_stop']['code'], len(calls)),
                                 ('error', 'internal_error', 1))
                self.assertEqual(diagnostics, [None])
                self.doCleanups()

    def test_unclassified_real_goto_path_is_fatal(self):
        """An unannotated generic navigation exception reaches the global fallback."""
        public,calls,diagnostics,_,_=self.scenario('unclassified')
        self.assertEqual((public['status'],public['first_stop']['code'],len(calls)),('error','internal_error',1))
        self.assertEqual(diagnostics,[None])
        self.assertTrue(any(frame['file']=='engine/browser.py' and frame['function']=='page'
                            for frame in public['first_stop']['failure_provenance']['frames']))

    def test_supplier_timeout_is_local(self):
        """A typed, annotated timeout leaves the failed operation unresolved and runs the next supplier."""
        public,calls,diagnostics,_,_=self.scenario('local_timeout')
        self.assertEqual(public['status'],'done')
        self.assertIn('https://s1.test/card',calls)
        self.assertEqual(diagnostics[0]['code'],'supplier_card_network_timeout')
        self.assertFalse(public.get('first_stop'))

    def test_browser_context_loss_stays_fatal(self):
        """An ownership/lifecycle exception does not become a supplier timeout."""
        public,calls,diagnostics,_,_=self.scenario('context_closed',evidence='valid')
        self.assertEqual((public['status'],public['first_stop']['code'],len(calls)),('error','internal_error',1))
        self.assertEqual(diagnostics,[None])

    def test_application_error_stays_fatal(self):
        """A consumer failure after navigation still preserves the global stop."""
        public,calls,diagnostics,_,_=self.scenario('application')
        self.assertEqual((public['status'],public['first_stop']['code'],len(calls)),('error','internal_error',1))
        self.assertEqual(diagnostics,[None])

    def test_lost_owner_cancel_or_deadline_wins_over_local_annotation(self):
        """Actual control checks suppress subsequent card operations on this product."""
        for mode in ('ownership','cancel','deadline'):
            with self.subTest(mode=mode):
                public,calls,diagnostics,_,_=self.scenario(mode,evidence='valid')
                self.doCleanups()
                self.assertEqual(diagnostics,[None])
                self.assertEqual(self.attribution_labels(public), [])
                self.assertIn(public['status'],('error','cancelled'))
                self.assertEqual(len(calls),1)
                if mode=='cancel':
                    self.assertEqual(public['status'],'cancelled')

