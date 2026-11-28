"""Ordinary owned capture survives interruption without another supplier request."""
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from .schemas import PositionIn
from .search import agent, http_card, whitelist
from .search.acquisition_card_input import link_input, recover_input
from .search.card_attachments import collect_current
from .search.card_batch import CardSnapshot
from .search.catalogue import CatalogueStore, identity_for_position
from .search.product_identity import rendered_identity


class AcquisitionCardInputTests(unittest.TestCase):
    def setUp(self):
        """Keep the catalogue, captured files and supplier reference in a disposable root."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = CatalogueStore(self.root/'catalogue.sqlite3')
        suppliers = self.root/'suppliers.md'
        suppliers.write_text('| ООО Seller 0 | | 7707083893 | https://one.test |\n')
        from . import app
        for obj, field, value in ((agent, 'CACHE_DIR', self.root), (http_card, 'CACHE_DIR', self.root),
                                  (app, 'CACHE_DIR', self.root),
                                  (whitelist, 'SUPPLIERS_FILE', suppliers)):
            context = patch.object(obj, field, value)
            context.start()
            self.addCleanup(context.stop)

    def snapshot(self, position):
        """Build a typed DOM fixture under the clock of the current acquisition."""
        from .test_purchase_characteristics335 import PurchaseCharacteristicsTests

        builder = PurchaseCharacteristicsTests()
        builder.cache = self.root
        # The shared source fixture dates its frame five seconds before its clock.
        with patch('engine.test_purchase_characteristics335.time.time', return_value=time.time()+5):
            proof = builder.make(title='Изделие ABC123', host='one.test')
        frame = http_card._read_render_frame(proof['source']['main_facts'])
        url, stamp = proof['url'], proof['observed_at']
        identity_proof = rendered_identity(url, url, proof['title'], 'ABC123', 'ABC123', position.name,
                                           'one.test', observed_at=stamp)
        facts = dict(url=url, requested_url=url, supplier_host='one.test', captured_at=stamp,
            text=frame['owner_text'], title=proof['title'], main_title=proof['title'], page_article='ABC123',
            expected_type_mark='ABC123', expected_name=position.name, expected_unit=position.unit,
            expected_qty=position.qty, adapter={}, roll_length=None, purchase_text='',
            main_facts=proof['source']['main_facts'], rendered_source=proof['source'], purchase_row_v2=proof,
            screenshot_context='', context_capture_evidence={}, context_capture_review_reason='',
            identity_evidence={**identity_proof, 'rendered_source':proof['source']})
        return CardSnapshot.create(facts, time.monotonic()+30)

    def interrupted_card(self):
        """Capture typed native fixture facts and interrupt before any financial selection."""
        position = PositionIn(number=1, name='Изделие', type_mark='ABC123', unit='шт', qty=1).to_result().position
        identity = identity_for_position(position)
        owner = 'fixture-old'
        acquisition = self.store.acquire_unknown(identity, ['one.test'], owner=owner)
        snapshot = self.snapshot(position)
        url, stamp = (snapshot.facts()[name] for name in ('url', 'captured_at'))
        binding = dict(product_id=identity.key, host='one.test', url=url, operation_id=uuid4().hex)
        reservation = self.store.card_operation(**binding, acquisition=acquisition,
            evidence=dict(job_id=owner, generation=acquisition.generation))
        self.assertTrue(reservation['allowed'])
        retained = partial(link_input, self.store, acquisition, binding, owner, self.root, position)
        with patch.object(agent,'_capture_card',return_value=snapshot), \
             patch.object(agent,'_deterministic_card_data',side_effect=RuntimeError('Fixture interrupted extraction')):
            with self.assertRaisesRegex(RuntimeError,'interrupted extraction'):
                collect_current(None,position,url,on_retained=retained)
        pending = self.store.pending_acquisition_input(acquisition,owner=owner,host='one.test',url=url)
        self.assertEqual(pending['evidence']['captured_input']['captured_at'],stamp)
        self.assertIsNone(self.store.resolve_observation(identity.key,binding['operation_id']))
        self.assertTrue(self.store.finish_acquisition(acquisition,error='Fixture interrupted'))
        return position, identity, binding, pending['evidence']['captured_input']

    def fresh_admission(self, identity):
        """Acquire the same product under a distinct current owner."""
        fresh = self.store.acquire_missing(identity,['one.test'],owner='fixture-new',
            expected_revision=self.store.lookup(identity)['revision'],mode='missing_data')
        self.assertTrue(fresh.allowed)
        return fresh

    def test_stopped_capture_finishes_from_same_dated_source(self):
        """A fresh acquisition resolves the original pending card and retains the source price."""
        position, identity, binding, reference = self.interrupted_card()
        current = self.fresh_admission(identity)
        with patch.object(agent,'_capture_card',side_effect=AssertionError('No supplier re-fetch')), \
             patch.object(agent,'complete_json',side_effect=AssertionError('No model re-fetch')):
            restored = recover_input(self.store,current,'fixture-new',identity,position,
                host='one.test',url=binding['url'],root=self.root)
        observed = restored['observation']
        self.assertEqual((observed['observation_id'],observed['observed_at'],observed['raw_amount'],observed['sale_unit']),
                         (binding['operation_id'],reference['captured_at'],100,'шт'))
        self.assertIsNone(self.store.pending_acquisition_input(current,owner='fixture-new',host='one.test',url=binding['url']))

    def test_changed_request_or_file_preserves_uncertain_outcome(self):
        """A changed request or damaged input never gains authority to fetch or alter history."""
        position, identity, binding, reference = self.interrupted_card()
        current = self.fresh_admission(identity)
        request = dict(host='one.test',url=binding['url'],root=self.root)
        self.assertIsNone(recover_input(self.store,current,'fixture-new',identity,
                                       position.model_copy(update={'unit':'компл.'}),**request))
        (self.root/reference['ref']).write_text('{}')
        self.assertIsNone(recover_input(self.store,current,'fixture-new',identity,position,**request))
        pending = self.store.pending_acquisition_input(current,owner='fixture-new',host='one.test',url=binding['url'])
        self.assertEqual(pending['operation_id'],binding['operation_id'])
        self.assertIsNone(self.store.resolve_observation(identity.key,binding['operation_id']))

    def test_insufficient_saved_input_does_not_close_pending_operation(self):
        """A reproducible extraction refusal confers neither a quote nor permission to recapture."""
        position, identity, binding, _ = self.interrupted_card()
        current = self.fresh_admission(identity)
        with patch.object(agent, '_deterministic_card_data', return_value=None):
            self.assertIsNone(recover_input(self.store,current,'fixture-new',identity,position,
                                           host='one.test',url=binding['url'],root=self.root))
        pending = self.store.pending_acquisition_input(current,owner='fixture-new',host='one.test',url=binding['url'])
        self.assertEqual(pending['operation_id'], binding['operation_id'])
        self.assertIsNone(self.store.resolve_observation(identity.key, binding['operation_id']))

    def test_native_runner_publishes_recovered_price_without_card_transport(self):
        """Exercise scheduler, fenced publication and cold report downloads with retained input."""
        from contextlib import ExitStack
        from datetime import UTC, datetime
        from io import BytesIO
        from zipfile import ZipFile

        from fastapi.testclient import TestClient
        from openpyxl import load_workbook

        from . import app
        from .runner_control import RunnerControl
        from .runner_executor import DirectControl, NativeResearchRunner
        from .runner_protocol import digest
        from .llm import LLMOperationFailed
        from .search.acquisition_source_intent import components
        from .search.search_engine import DiscoveryURLs
        from .search.session import transport_started
        from .telemetry import record_stage
        from .test_card_replay281 import event
        from .test_catalogue_unknown import CatalogueUnknownTests

        fixture = CatalogueUnknownTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.hosts[0] = 'one.test'
        fixture.stack.enter_context(patch.object(app, '_catalogue_store', return_value=self.store))
        fixture.stack.enter_context(patch.object(whitelist, 'whitelist_hosts', return_value=fixture.hosts))
        position = PositionIn(number=1, name='Изделие', type_mark='ABC123', unit='шт', qty=1)
        identity = identity_for_position(position)
        url = 'https://one.test/product/tray'
        seal = digest(components(Path(app.__file__).parent))
        control = RunnerControl(self.root/'control', seal, lease_seconds=1800)
        with control.lifecycle_fence(str(uuid4()), 'start', 'offline') as grant:
            grant.publish_intent('offline')
            control.reconcile_lifecycle(grant, 'running')
        payload = {'positions': [position.model_dump(mode='json')], 'acquisition_mode': 'missing_data'}
        runner = NativeResearchRunner(self.root/'runner', DirectControl(control), app, heartbeat_seconds=10000)

        def discover(query, hosts, **kwargs):
            """Replay parsed discovery metadata so the scheduler reaches the saved card."""
            transport_started('search_api')
            stamp = time.time()
            sources = {host: [url] if host == 'one.test' else [] for host in hosts}
            items = [item for group in sources.values() for item in group]
            observations = {
                host: dict(origin='response', window='below_limit', observed_at=stamp,
                           candidate_count=len(group), candidate_urls=group, diagnostics=[])
                for host, group in sources.items()
            }
            return DiscoveryURLs(items, origin='response', window='below_limit', observed_at=stamp,
                                 raw_count=len(items), host_observations=observations)

        def capture(*args, **kwargs):
            """Provide the supplier response once, before injecting the model failure."""
            return self.snapshot(position.to_result().position)

        def model_presend_failure(*args, **kwargs):
            """Record a pre-send failure of the model after the supplier snapshot was linked."""
            proof = event(sent=False, presend=True)
            record_stage('provider_transport', .1, ok=False, label=proof['label'])
            raise LLMOperationFailed('transport_error')

        control.enqueue(payload, 'fixture_retain_and_interrupt_1234', 'server-basic:fixture')
        with patch('engine.search.search_engine.search_on_sites', side_effect=discover), \
             patch.object(agent, '_capture_card', side_effect=capture) as first_capture, \
             patch.object(agent, '_deterministic_card_data', side_effect=model_presend_failure):
            runner.execute(control.claim('offline-fixture', seal), seal)
            first_capture.assert_called_once()
        attempts = self.store.lookup(identity)['refresh_attempts']
        operation = [entry['card_operation'] for entry in attempts if entry.get('card_operation')][-1]
        self.assertEqual(operation['state'], 'pending')
        retained = operation['evidence']['captured_input']
        observation_id = operation['operation_id']
        self.assertIsNone(self.store.resolve_observation(identity.key, observation_id))

        job = control.enqueue(payload, 'fixture_local_recovery_1234', 'server-basic:fixture')
        claim = control.claim('offline-fixture', seal)
        with ExitStack() as stack:
            stack.enter_context(patch('engine.search.search_engine.search_on_sites', side_effect=discover))
            capture = stack.enter_context(patch.object(agent, '_capture_card', side_effect=AssertionError('No card transport')))
            photos = stack.enter_context(patch.object(agent, '_capture_rendered_card',
                side_effect=AssertionError('No attachment recapture')))
            self.assertEqual(runner.execute(claim, seal)['state'], 'done', app._research_store().public(job).get('error'))
            capture.assert_not_called()
            photos.assert_not_called()
        self.assertEqual(self.store.resolve_observation(identity.key,observation_id)
                         ['observation']['observed_at'], retained['captured_at'])

        cold = RunnerControl(control.root, seal, lease_seconds=1800)
        files = {}
        with patch.object(app,'_remote_control',return_value=True), \
             patch.object(app,'_runner_store',return_value=cold), TestClient(app.app) as web:
            status = web.get('/research/'+job)
            self.assertEqual(status.status_code, 200)
            for entry in status.json()['downloads']:
                reply = web.get(entry['url'])
                self.assertEqual(reply.status_code, 200)
                files[entry['name']] = reply.content
        workbook = files['supplier-research-summary.xlsx']
        with ZipFile(BytesIO(files['supplier-research-package.zip'])) as archive:
            self.assertIsNone(archive.testzip())
            packaged = archive.read('supplier-research-summary.xlsx')
        for content in (workbook, packaged):
            book = load_workbook(BytesIO(content), read_only=True, data_only=True)
            try:
                sheet = book['Предложения']
                self.assertEqual((sheet['I2'].value, sheet['L2'].value, sheet['M2'].value),
                                 (url, 'шт', 100))
                stamp = datetime.fromtimestamp(retained['captured_at'], UTC)
                self.assertEqual(sheet['Y2'].value, stamp.strftime('%Y-%m-%d %H:%M:%S UTC'))
            finally:
                book.close()
