"""Offline actual-collector ordering with synthetic capture and current receipts."""
from contextlib import ExitStack
from pathlib import Path
import hashlib
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
from engine.models import Requisites, SpecPosition, PositionResult
from engine.offer_selection import offer_is_comparable, select_top_offers
from engine.search import agent, base_db
from engine.search.product_identity import rendered_identity
from engine.search.session import research_session
from engine.search.source_registry import SourceRegistry
from engine.schemas import PositionIn
from engine.test_price_capture import fixture_capture_result

class SellerEnrichmentStagingTests(unittest.TestCase):
    url = 'https://seller.example/product/ABC123'
    def collect(self, refusal='', *, seller_error=False, invalid_seller=False, hint=False, seller_deadline=False, seller_global=False, site_only=False,capture_deadline=False):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            for module, key, value in [('engine.config', 'CACHE_DIR', root),
                                       (agent, 'CACHE_DIR', root), (base_db, 'BASE_FILE', root/'base.json')]:
                stack.enter_context(patch(module+'.'+key, value) if isinstance(module,str) else patch.object(module,key,value))
            pos = SpecPosition(number=1,name='Cable',type_mark='ABC123',unit='м')
            position_in = PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м')
            registry = SourceRegistry(root/'registry.sqlite3')
            page = MagicMock(); page.url = page._kac_document_url = self.url; page._kac_response_status = 200
            page.title.return_value = 'Cable ABC123'
            page.locator.return_value.first.inner_text.return_value = 'Cable ABC123'
            browser = MagicMock(); browser.page.return_value.__enter__.return_value = page
            events = []
            data = dict(product_title='Cable ABC123',matches_requested_product=True,price_with_vat=120,
                        listed_price_with_vat=120,price_unit='упак' if refusal=='unit' else 'м',seller_inn='7842224734')
            if site_only: data['seller_inn']=''
            if refusal=='region':
                data.update(in_moscow_region=False,region_status='incompatible',region_evidence='Outside region')
            def capture(*args, **kwargs):
                events.append('capture')
                if capture_deadline:
                    from engine.search.session import SearchBudgetExceeded
                    raise SearchBudgetExceeded('offline capture deadline')
                if refusal in {'capture','unit'}:
                    Path(args[1]).write_bytes(b'offline refused pixels')
                    return {}, 'Offline capture refusal'
                return fixture_capture_result(*args, **kwargs)
            def late(*args, **kwargs):
                events.append('late_identity')
                if refusal=='identity':
                    kwargs['on_refusal']('article_mismatch'); return {}
                e = rendered_identity(self.url,self.url,'Cable ABC123','ABC123','ABC123','Cable','seller.example')
                pixels=b'offline identity pixels'
                (root/'offline_identity.png').write_bytes(pixels)
                e.update(observed_at=time.time(),identity_screenshot='offline_identity.png',identity_sha256=hashlib.sha256(pixels).hexdigest())
                return e
            def reconcile(evidence,identity,snapshot):
                events.append('reconcile'); return evidence,''
            def fetch(inn):
                events.append('seller_inn')
                if seller_global:
                    from engine.search.session import ResearchProgressError
                    raise ResearchProgressError('offline global control')
                if seller_deadline:
                    from engine.search.session import SearchBudgetExceeded
                    raise SearchBudgetExceeded('offline seller deadline')
                if seller_error: raise RuntimeError('offline seller unavailable')
                return None if invalid_seller else Requisites(name='Seller',inn=inn)
            def site(*args):
                events.append('seller_site'); return Requisites(name='Seller',inn='7842224734' if site_only else 'invalid')
            for target,value in [('_page_text_after_browser_check','Cable ABC123\nАртикул: ABC123'),
                                 ('_identity_card_text','Cable ABC123\nАртикул: ABC123'),('_extract_from_page',data),
                                 ('wl_requisites_for',Requisites(name='Seller',inn='7842224734') if hint else None)]:
                stack.enter_context(patch.object(agent,target,return_value=value))
            for target,value in [('engine.price_capture.ready_price_snapshot',{}),
                                 ('engine.price_capture.current_literal_quote_fields',({},{}))]:
                stack.enter_context(patch(target,return_value=value))
            for target,value in [('engine.price_capture.capture_price',capture),
                                 ('engine.price_capture.reconcile_capture_identity',reconcile),
                                 ('engine.search.product_identity.capture_current_identity',late)]:
                stack.enter_context(patch(target,side_effect=value))
            def context(_page,path,url):
                from PIL import Image
                Image.new('RGB',(100,100),'white').save(path)
                return {'version':1,'role':'review_context','filename':Path(path).name,'sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest(),'url':url,'captured_at':time.time(),'width':100,'height':100,'document_width':100,'document_height':100,'truncated':False},''
            stack.enter_context(patch('engine.review_context.capture_context',side_effect=context))
            stack.enter_context(patch.object(agent,'_save_product_screenshot'))
            stack.enter_context(patch.object(agent,'fetch_requisites',side_effect=fetch))
            stack.enter_context(patch.object(agent,'fetch_requisites_from_site',side_effect=site))
            if refusal=='model': stack.enter_context(patch.object(agent,'_needs_model_review',return_value=True))
            stale_path=root / f'prod_{abs(hash(self.url)) % 10**10}.png'
            stale_path.write_bytes(b'OLD PRIOR CAPTURE')
            seen=[]
            with research_session(on_offer=lambda o:events.append('accepted'),on_observed=seen.append,
                                  on_identity=lambda e:registry.remember_identity(position_in,e)), \
                    base_db.capture_current_observations() as produced, agent.capture_card_diagnostics() as diagnostics:
                try:
                    offers,checked=agent._collect_offers(browser,[self.url],1,'ABC123','Cable','м')
                except agent.ResearchProgressError as error:
                    if not seller_global: raise
                    self.assertEqual('offline global control',str(error))
                    events.append('global_control_propagated')
                    offers,checked=[seen[-1]],1
            if capture_deadline:
                self.assertEqual(3,len(seen));self.assertTrue(all(not offer_is_comparable(o,pos) for o in seen));self.assertTrue(all(not o.price_capture_evidence for o in seen));return dict(seen=seen,events=events,offers=offers)
            self.assertEqual(1,checked); self.assertEqual(1,len(offers))
            self.assertTrue(all(not offer_is_comparable(o,pos) for o in seen[:2]))
            self.assertTrue(all(not o.price_capture_evidence for o in seen[:2]))
            offer=offers[0]
            self.assertEqual(120,offer.price_with_vat); self.assertEqual(120,offer.listed_price_with_vat)
            self.assertEqual(self.url,offer.url)
            if offer.price_capture_evidence:self.assertTrue(Path(offer.screenshot_product).is_file())
            else:self.assertIsNone(offer.screenshot_product)
            result=base_db.upsert(pos,PositionResult(position=pos,offers=select_top_offers(offers,position=pos),all_offers=offers),
                                  confirmed_observations=produced)
            statuses=[r['status'] for entry in base_db.refresh_receipt_snapshot().values()
                      for context in entry['refresh_receipts'].values() for r in context.values()]
            return dict(events=events,offer=offer,comparable=offer_is_comparable(offer,pos),seen=len(seen),
                        statuses=statuses,selected=len(result.offers),registry=registry.load(position_in),diagnostics=diagnostics)
    def test_refused_prerequisites_skip_both_remote_enrichers_preserve_review(self):
        for refusal in ('unit','capture','identity','model','region'):
            with self.subTest(refusal=refusal):
                r=self.collect(refusal)
                self.assertFalse(any(e.startswith('seller_') for e in r['events']))
                self.assertFalse(r['comparable']); self.assertEqual(3,r['seen'])
                self.assertEqual([],r['statuses']); self.assertEqual(0,r['selected'])
                self.assertTrue(r['registry']['seller.example']['mappings'])
                if refusal!='identity':self.assertEqual('ABC123',r['offer'].identity_evidence['main_article'])
                if refusal in ('unit','capture','identity'):self.assertTrue(r['offer'].price_capture_review_reason)
    def test_eligible_quote_enriches_after_reconciliation_receipt_consumer_accept(self):
        r=self.collect()
        self.assertEqual(['capture','late_identity','reconcile','seller_inn','accepted'],r['events'])
        self.assertTrue(r['comparable']); self.assertEqual(['confirmed'],r['statuses']); self.assertEqual(1,r['selected'])
    def test_seller_error_invalid_requisites_preserve_proof_never_confirm(self):
        for options in ({'seller_error':True},{'invalid_seller':True}):
            with self.subTest(options=options):
                r=self.collect(**options)
                self.assertFalse(r['comparable'])
                self.assertEqual('stable_visible_main_card',r['offer'].price_capture_evidence['provenance'])
                self.assertEqual('ABC123',r['offer'].identity_evidence['main_article'])
                self.assertTrue(r['offer'].price_capture_review_reason)
                self.assertTrue(any(d['code']=='seller_unconfirmed' for d in r['diagnostics']))
                self.assertEqual([],r['statuses']); self.assertEqual(0,r['selected'])
    def test_valid_whitelist_hint_kept_without_remote_calls(self):
        r=self.collect(hint=True)
        self.assertFalse(any(e.startswith('seller_') for e in r['events']))
        self.assertEqual('7842224734',r['offer'].requisites.inn); self.assertEqual(['confirmed'],r['statuses'])

    def test_seller_deadline_preserves_current_review_candidate_and_typed_budget_diagnostic(self):
        r=self.collect(seller_deadline=True)
        self.assertFalse(r['comparable']); self.assertEqual(3,r['seen'])
        self.assertEqual('stable_visible_main_card',r['offer'].price_capture_evidence['provenance'])
        self.assertEqual('ABC123',r['offer'].identity_evidence['main_article'])
        self.assertIn('SearchBudgetExceeded',r['offer'].price_capture_review_reason)
        self.assertTrue(any(d['code']=='search_budget' for d in r['diagnostics']))
        self.assertEqual([],r['statuses']); self.assertEqual(0,r['selected'])

    def test_global_control_preserves_candidate_without_downgrading_exception(self):
        r=self.collect(seller_global=True)
        self.assertIn('global_control_propagated',r['events'])
        self.assertIn('ResearchProgressError',r['offer'].price_capture_review_reason)
        self.assertFalse(r['comparable']); self.assertEqual(3,r['seen'])
        self.assertEqual([],r['statuses']); self.assertEqual(0,r['selected'])

    def test_eligible_quote_without_valid_extracted_inn_uses_site_after_proof(self):
        r=self.collect(site_only=True)
        self.assertEqual(['capture','late_identity','reconcile','seller_site','accepted'],r['events'])
        self.assertEqual(['confirmed'],r['statuses'])

    def test_refused_candidate_retains_valid_cached_hint_without_enrichment(self):
        r=self.collect('capture',hint=True)
        self.assertEqual('7842224734',r['offer'].requisites.inn)
        self.assertFalse(any(e.startswith('seller_') for e in r['events']))
        self.assertFalse(r['comparable']); self.assertEqual([],r['statuses'])

    def test_capture_deadline_preserves_early_price_and_context_observations(self):
        r=self.collect(capture_deadline=True)
        self.assertEqual([120,120,120],[o.price_with_vat for o in r['seen']])
        self.assertFalse(r['seen'][0].screenshot_context)
        self.assertTrue(r['seen'][1].screenshot_context)
        self.assertEqual(r['seen'][1].context_capture_evidence,r['seen'][2].context_capture_evidence)
        self.assertIn('SearchBudgetExceeded',r['seen'][2].price_capture_review_reason)
        self.assertNotIn('late_identity',r['events'])
        self.assertNotIn('accepted',r['events'])
