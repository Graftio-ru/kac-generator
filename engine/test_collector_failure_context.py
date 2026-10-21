"""A blocked card is a local observation, not an unbound collector variable."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from engine.search import agent, session
from engine.search.failure_policy import ResearchInternalFailure
from engine.search.supplier_health_store import SupplierHealthStore
from engine.llm import LLMRequestGuard


class CollectorFailureContextTests(unittest.TestCase):
    def page(self, url, status, text):
        page=MagicMock();page.url=page._kac_document_url=url
        page._kac_response_status=status;page._kac_retry_after=None
        page.title.return_value='Access' if status>=400 else 'Cable ABC123'
        page.inner_text.return_value=text
        page.locator.return_value.count.return_value=0
        return page

    def collect(self, url, page=None, exception=None):
        browser=MagicMock()
        if exception:browser.page.return_value.__enter__.side_effect=exception
        else:browser.page.return_value.__enter__.return_value=page
        with tempfile.TemporaryDirectory() as directory:
            host=agent._canonical_host(url)
            store=SupplierHealthStore(Path(directory)/'health.sqlite3')
            health=session.PersistentSupplierHealth(store=store,hosts=[host])
            guard=LLMRequestGuard()
            with session.research_session(health,deadline=time.monotonic()+10,llm_guard=guard), \
                 agent.capture_card_diagnostics() as diagnostics, \
                 patch.object(agent,'_extract_from_page',side_effect=AssertionError('Blocked card reached extraction')):
                offers,checked=agent._collect_offers(browser,[url],1,supplier_hosts={host})
            return offers,checked,list(diagnostics),guard,store.snapshot(host)

    def test_plain_http403_multiple_suppliers_stays_local_no_model_or_price(self):
        for host in ('chipdip.ru','elektro.ru','xcom-shop.ru'):
            with self.subTest(host=host):
                url=f'https://{host}/product/item'
                offers,checked,diagnostics,guard,state=self.collect(url,self.page(url,403,'Forbidden'))
                self.assertEqual(([],1),(offers,checked));self.assertIsNone(guard.failure)
                self.assertEqual('http_blocked',diagnostics[0]['code'])
                self.assertEqual('local',diagnostics[0]['failure_scope'])
                self.assertEqual(403,diagnostics[0]['http_status'])
                self.assertIn('защита от ботов не подтверждена',diagnostics[0]['reason'])

    def test_marker_specific_http403_keeps_observed_protection_and_local_control(self):
        url='https://chipdip.ru/product/item'
        offers,checked,diagnostics,guard,state=self.collect(url,self.page(url,403,'Cloudflare verify you are human'))
        self.assertEqual(([],1),(offers,checked));self.assertIsNone(guard.failure)
        self.assertEqual('bot_protection',diagnostics[0]['failure_cause'])
        self.assertIn('Cloudflare',diagnostics[0]['reason'])

    def test_actual_supplier_transport_exception_remains_local(self):
        url='https://chipdip.ru/product/item'
        error=httpx.ReadTimeout('Fixture',request=httpx.Request('GET',url))
        offers,checked,diagnostics,guard,state=self.collect(url,exception=error)
        self.assertEqual(([],1),(offers,checked));self.assertIsNone(guard.failure)
        self.assertEqual('local',diagnostics[0]['failure_scope'])
        self.assertEqual('supplier_http',diagnostics[0]['failure_source'])
        self.assertEqual('supplier_local_error',diagnostics[0]['code'])

    def test_unknown_programming_exception_still_stops_globally(self):
        url='https://chipdip.ru/product/item'
        with self.assertRaises(ResearchInternalFailure) as caught:
            self.collect(url,exception=ValueError('Fixture programming failure'))
        self.assertEqual('programming_failure',caught.exception.diagnostic()['failure_cause'])


    def test_blocked_card_does_not_stop_next_supplier_healthy_card(self):
        from engine.models import Requisites
        from engine.offer_selection import offer_is_comparable
        blocked='https://chipdip.ru/product/ABC123'
        healthy='https://supplier.example/product/ABC123'
        bad=self.page(blocked,403,'Cloudflare verify you are human')
        good=self.page(healthy,200,'Cable ABC123\nАртикул: ABC123\n120 ₽\nЦена за 1 м')
        good.locator.return_value.count.return_value=1
        good.locator.return_value.first.inner_text.return_value='Cable ABC123'
        browser=MagicMock()
        def opened(url):
            context=MagicMock();context.__enter__.return_value=bad if url==blocked else good;return context
        browser.page.side_effect=opened
        with tempfile.TemporaryDirectory() as directory:
            health=session.PersistentSupplierHealth(store=SupplierHealthStore(Path(directory)/'health.sqlite3'),
                hosts=['chipdip.ru','supplier.example'])
            guard=LLMRequestGuard()
            with session.research_session(health,deadline=time.monotonic()+10,llm_guard=guard), \
                 agent.capture_card_diagnostics() as diagnostics, \
                 patch.object(agent,'CACHE_DIR',Path(directory)), \
                 patch.object(agent,'_extract_from_page',return_value={'product_title':'Cable ABC123',
                    'matches_requested_product':True,'price_with_vat':120,'listed_price_with_vat':120,'price_unit':'м'}) as extract, \
                 patch.object(agent,'wl_requisites_for',return_value=Requisites(name='Supplier',inn='7842224734')), \
                 patch('engine.review_context.capture_context',return_value=({},'Attachment fixture unavailable')):
                offers,checked=agent._collect_offers(browser,[blocked,healthy],1,'ABC123','Cable','м',
                    supplier_hosts={'chipdip.ru','supplier.example'})
            self.assertEqual(2,checked);self.assertEqual(1,len(offers));self.assertEqual(healthy,offers[0].url)
            self.assertTrue(offer_is_comparable(offers[0],{'unit':'м','type_mark':'ABC123','name':'Cable'}))
            extract.assert_not_called();self.assertIsNone(guard.failure)
            self.assertEqual('http_blocked',diagnostics[0]['code'])

    def test_existing_actual_healthy_collector_fact_and_attachment_path(self):
        from engine.test_source_quote import CollectorNoImageProofTests
        CollectorNoImageProofTests('test_actual_collector_attachment_failure_still_qualifies_source_facts').test_actual_collector_attachment_failure_still_qualifies_source_facts()

if __name__=='__main__':unittest.main()
