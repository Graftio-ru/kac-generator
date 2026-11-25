"""Exercise lazy external discovery and durable refusal without live traffic."""

from pathlib import Path
from io import BytesIO
import tempfile
import time
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import httpx
from openpyxl import load_workbook
from starlette.requests import Request

from .llm import LLMRequestGuard
from .research_store import Conflict, ResearchStore
from .search import search_engine, search_route, supplier_discovery
from .search.article_query_plan import QueryStep
from .search.catalogue_xmlriver import CatalogueXmlRiver, STATE_KEY, catalogue_xmlriver_scope
from .search.failure_policy import SearchServiceUnavailable, SearchTransportAttempt
from .search.session import research_session


class CatalogueXmlRiverTests(unittest.TestCase):
    """Run discovery with the production guard, with all provider I/O replaced."""

    def setUp(self) -> None:
        """Give each case its own provider state and record each durable transition."""
        self.saved = []
        self.access = CatalogueXmlRiver({'state': 'pending'}, self.saved.append)
        self.guard = LLMRequestGuard()
        for field in ('xmlriver_user', 'xmlriver_key'):
            setting = patch.object(search_engine.settings, field, 'test-only')
            setting.start()
            self.addCleanup(setting.stop)

    def test_own_search_leaves_xmlriver_unchecked(self) -> None:
        """A supplier-only operation must finish without even probing the account."""
        host = 'electro-mpo.ru'
        url = 'https://www.electro-mpo.ru/catalog/a/b/card/'
        query = '"3526210" цена'
        observation = dict(origin='supplier_own_search', diagnostics=[], candidate_count=1)
        with (catalogue_xmlriver_scope(self.access), research_session(llm_guard=self.guard),
              patch.object(search_route, 'resolve_initial_route') as resolve,
              patch.object(supplier_discovery, 'discover', return_value=([url], observation))):
            result = search_engine.search_on_sites(query, [host], query_step=QueryStep(query, 'exact_article', '3526210'),
                                                   own_only=True)
        self.assertEqual(result, [url])
        self.assertEqual(self.saved, [])
        resolve.assert_not_called()

    def test_account_tls_failure_keeps_owned_card_and_reports_external_host(self) -> None:
        """A TLS refusal leaves existing links usable and accounts for external hosts once."""
        own, external = 'electro-mpo.ru', 'external.example'
        url = 'https://www.electro-mpo.ru/catalog/a/b/card/'
        query = '"3526210" цена'
        stamp = time.time()
        failure = SearchServiceUnavailable('transport_error', transport_attempts=(
            SearchTransportAttempt('ConnectError', stamp, 0.05, False, transport_category='tls'),
        ))
        own_result = ([url], dict(origin='supplier_own_search', diagnostics=[], candidate_count=1))
        with (catalogue_xmlriver_scope(self.access), research_session(llm_guard=self.guard),
              patch.object(search_route, 'resolve_initial_route', side_effect=failure) as resolve,
              patch.object(supplier_discovery, 'discover', return_value=own_result) as discover,
              patch.object(search_engine, 'xmlriver_search') as wire):
            mixed = search_engine.search_on_sites(query, [own, external],
                                                   query_step=QueryStep(query, 'exact_article', '3526210'))
            retry = search_engine.search_on_sites(query, [external])
            later = search_engine.search_on_sites(query, [own], query_step=QueryStep(query, 'exact_article', '3526210'),
                                                  own_only=True)
        self.assertEqual(mixed, [url])
        self.assertEqual(later, [url])
        self.assertEqual(discover.call_count, 2)
        self.assertEqual(mixed.host_observations[own]['diagnostics'], [])
        self.assertEqual(retry.host_observations[external]['origin'], 'unknown')
        diagnostic = mixed.host_observations[external]['diagnostics'][0]
        self.assertEqual(diagnostic['failure_cause'], 'tls_verification_or_handshake')
        self.assertEqual(diagnostic['failure_scope'], 'provider')
        self.assertEqual([value['state'] for value in self.saved], ['checking', 'unavailable'])
        self.assertIsNone(self.guard.diagnostic())
        resolve.assert_called_once()
        wire.assert_not_called()

    def test_search_failure_after_successful_account_check_is_not_global(self) -> None:
        """Failure in the actual query closes only the XMLRiver lane for this job."""
        with (catalogue_xmlriver_scope(self.access), research_session(llm_guard=self.guard),
              patch.object(search_route, 'resolve_initial_route', return_value=(None, None)) as resolve,
              patch.object(search_engine.settings, 'xmlriver_user', 'fixture-user'),
              patch.object(search_engine.settings, 'xmlriver_key', 'fixture-key'),
              patch.object(search_engine, '_search_get', side_effect=SearchServiceUnavailable('transport_error')) as wire):
            for host in ('first.example', 'second.example'):
                results = search_engine.search_on_sites('fixture', [host])
                self.assertEqual(results, [])
                self.assertIsNone(results.observed_at)
                self.assertEqual(results.host_observations[host]['diagnostics'][0]['failure_cause'], 'transport_error')
        self.assertEqual([value['state'] for value in self.saved], ['checking', 'ready', 'unavailable'])
        self.assertIsNone(self.guard.diagnostic())
        resolve.assert_called_once()
        wire.assert_called_once()

    def test_interrupted_account_check_is_not_repeated_after_reload(self) -> None:
        """Treat a persisted in-progress check as unknown and never send it again."""
        restored = CatalogueXmlRiver({'state': 'checking'}, self.saved.append)
        with (catalogue_xmlriver_scope(restored), research_session(llm_guard=self.guard),
              patch.object(search_route, 'resolve_initial_route') as resolve):
            result = search_engine.search_on_sites('fixture', ['external.example'])
        resolve.assert_not_called()
        self.assertEqual(self.saved, [{'state': 'unavailable', 'cause': 'account_check_outcome_unknown', 'status': None}])
        self.assertEqual(result.host_observations['external.example']['origin'], 'unknown')

    def test_missing_configuration_is_durably_unavailable_without_requests(self) -> None:
        """An unset XMLRiver account must never become a ready provider state."""
        with (catalogue_xmlriver_scope(self.access), research_session(llm_guard=self.guard),
              patch.object(search_engine.settings, 'xmlriver_key', ''),
              patch.object(search_route, 'resolve_initial_route') as resolve,
              patch.object(search_engine, 'xmlriver_search') as wire):
            result = search_engine.search_on_sites('fixture', ['external.example'])
        resolve.assert_not_called()
        wire.assert_not_called()
        self.assertEqual(self.saved[-1], {'state': 'unavailable', 'cause': 'configuration_missing', 'status': None})
        self.assertEqual(result.host_observations['external.example']['origin'], 'unknown')

    def test_selected_route_is_detached_and_restored_without_a_second_check(self) -> None:
        """Mutation by a caller cannot change the backend or the saved route."""
        profile = search_route.select_route({'engine': {'yandex': 0, 'google': 1}, 'balance_positive': True},
                                            'https://xmlriver.com/search_yandex/xml', 213)
        receipt = {'observed_at': time.time(), 'selection_reason': 'primary_engine_disabled'}
        with patch.object(search_route, 'resolve_initial_route', return_value=(profile, receipt)) as resolve:
            selection = self.access.resolve(deadline=None, configured=True)
            self.assertIsNotNone(selection)
            selection['locale']['lr'] = 'en'
            self.assertEqual(self.access.resolve(deadline=None, configured=True), profile)
            restored = CatalogueXmlRiver(self.saved[-1], self.saved.append)
            self.assertEqual(restored.resolve(deadline=None, configured=True), profile)
        resolve.assert_called_once()
        self.assertEqual(self.saved[-1]['profile'], profile)

    def test_persisted_provider_outcome_cannot_be_reopened(self) -> None:
        """A job lifecycle update cannot re-enable calls after provider refusal."""
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchStore(Path(directory))
            try:
                job = 'a' * 12
                store.create_job(job, [{'name': 'Fixture'}], ['external.example'])
                for operation, state in (
                    ('pending', {'state': 'pending'}), ('checking', {'state': 'checking'}),
                    ('failed', {'state': 'unavailable', 'cause': 'transport_error', 'status': None}),
                ):
                    store.commit_update(job, operation, lifecycle={STATE_KEY: state})
                with self.assertRaises(Conflict):
                    store.commit_update(job, 'reset', lifecycle={STATE_KEY: {'state': 'pending'}})
            finally:
                store.close()

    def test_normal_job_keeps_supplier_candidate_after_external_refusal(self) -> None:
        """Exercise normal job creation and durable rows with retained supplier HTML."""
        from . import app, browser, supplier_methods
        from .schemas import PositionIn, ResearchReportRequest
        from .builders.research_package import build_research_package
        from .builders.research_report_xlsx import build_research_report
        from .search import catalogue_producer, selected_proofs
        from .search.catalogue_projection import catalogue_scope
        from .test_catalogue_unknown import CatalogueUnknownTests

        fixture = CatalogueUnknownTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        host, external = 'electro-mpo.ru', 'external.example'
        body_path = Path(__file__).resolve().parents[2] / 'sources/kac-generator/mpo-own-search-20261013/response.html'
        body = body_path.read_bytes()
        supplier_calls = []

        def supplier_page(url, **kwargs):
            """Deliver the captured public search window only to the supplier adapter."""
            self.assertTrue(url.startswith('https://www.electro-mpo.ru/search/?q='))
            supplier_calls.append(url)
            return httpx.Response(200, headers={'content-type': 'text/html; charset=utf-8'}, content=body,
                                  request=httpx.Request('GET', url))

        def proof_attempt(*args, selected_url, attempted, on_attempt, **kwargs):
            """Checkpoint a simulated proof attempt without a browser or object store."""
            on_attempt(set(attempted) | {selected_url})
            return {'paused': False, 'positions': []}

        with (patch('engine.search.whitelist.whitelist_hosts', return_value=[host, external]),
              patch.object(app, '_remote_control', return_value=False),
              patch.object(app, '_launch_research_job') as launch,
              patch.object(browser, 'supplier_http_get', side_effect=supplier_page),
              patch.object(supplier_methods, 'selected', return_value=None),
              patch.object(search_route, 'resolve_initial_route', side_effect=SearchServiceUnavailable('transport_error')) as resolve,
              patch.object(search_engine, 'xmlriver_search') as wire,
              patch.object(catalogue_producer, 'capture_and_publish', side_effect=fixture.capture),
              patch.object(selected_proofs, 'capture_selected_proofs', side_effect=proof_attempt)):
            position = PositionIn(number=1, name='Лоток перфорированный', type_mark='3526210', unit='м', qty=1)
            request = Request({'type': 'http', 'method': 'POST', 'path': '/research', 'headers': []})
            created = app.create_research(app.ResearchCreateRequest(positions=[position], acquisition_mode='missing_data'), request)
            job_id = created['job_id']
            self.addCleanup(app.RESEARCH_JOBS.pop, job_id, None)
            self.addCleanup(app._RESEARCH_INPUTS.pop, job_id, None)
            launch.assert_called_once_with(job_id)
            app._run_research_job(job_id)
            public = app._research_store().public(job_id)
            report, store, view, _ = app._catalogue_export_snapshot(
                ResearchReportRequest.model_validate({'positions': [position.model_dump()]}), local_only=True,
            )
            with catalogue_scope(store, snapshot=view):
                workbook_data = build_research_report(report)
                package_data = build_research_package(report)

        self.assertEqual(public['status'], 'done', public.get('error'))
        self.assertFalse(public.get('first_stop'))
        self.assertTrue(public['partial'])
        self.assertFalse(public['business_complete'])
        self.assertEqual(public[STATE_KEY]['state'], 'unavailable')
        self.assertEqual(len(supplier_calls), 1)
        row = public['rows'][0]
        self.assertTrue(any(candidate['url'].startswith('https://www.electro-mpo.ru/') for candidate in row['candidates']))
        source = next(check for check in row['source_checks'] if check['host'] == external)
        self.assertEqual(source['product_presence'], 'unknown')
        self.assertIn('xmlriver_discovery_unavailable', {item['code'] for item in source['diagnostics']})
        with ZipFile(BytesIO(package_data)) as archive:
            packed_workbook = archive.read('supplier-research-summary.xlsx')
        for payload in (workbook_data, packed_workbook):
            workbook = load_workbook(BytesIO(payload), read_only=True)
            try:
                prices = list(workbook['Предложения'].values)
                coverage = list(workbook['Позиции × сайты'].values)
                self.assertTrue(any(150 in row for row in prices[1:]))
                self.assertTrue(any(external in row and any('XMLRiver' in str(value) for value in row) for row in coverage[1:]))
            finally:
                workbook.close()
        resolve.assert_called_once()
        wire.assert_not_called()
