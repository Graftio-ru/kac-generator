"""Offline current-card proof and persistence regressions."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
from engine.search.product_identity import capture_current_identity,rendered_identity
from engine.search.source_registry import SourceRegistry
from engine.search.session import observed_identity,research_session
from engine.schemas import PositionIn

class LateIdentityTests(unittest.TestCase):
    url='https://seller.example/product/ABC123'
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'identity.png'
        self.page=MagicMock();self.page.url=self.url;self.page._kac_document_url=self.url
        self.page._kac_response_status=200;self.page.title.return_value='Cable'
        self.page.inner_text.return_value='Cable\nАртикул: ABC123'
        self.page.screenshot.side_effect=lambda **kw:Path(kw['path']).write_bytes(b'fixture pixels')
        self.page.evaluate.return_value=dict(url=self.url,title='Cable',text='Cable\nАртикул: ABC123')
    def capture(self):
        return capture_current_identity(self.page,self.path,self.url,'ABC123','Cable','seller.example','Cable')
    def test_missing_then_delayed_current_article_and_dated_capture(self):
        self.page.evaluate.side_effect=[dict(url=self.url,title='Cable',text='Cable'),*[self.page.evaluate.return_value]*3]
        start=time.time();proof=self.capture()
        self.assertEqual('ABC123',proof['main_article']);self.assertGreaterEqual(proof['observed_at'],start)
        self.assertEqual(self.path.name,proof['identity_screenshot']);self.assertEqual(64,len(proof['identity_sha256']))
    def test_wrong_article_heading_redirect_protection_and_http_refuse(self):
        cases=[('article',dict(text='Cable\nАртикул: XYZ999')),
               ('heading',dict(title='Other ABC123')),('redirect',dict(url='https://evil.example/product/ABC123'))]
        for _,change in cases:
            self.page.evaluate.return_value.update(change)
            self.assertEqual({},self.capture())
            self.page.evaluate.return_value=dict(url=self.url,title='Cable',text='Cable\nАртикул: ABC123')
        self.page.inner_text.return_value='Checking your browser verify you are human'
        self.assertEqual({},self.capture())
        self.page.inner_text.return_value='Cable\nАртикул: ABC123';self.page._kac_response_status=403
        self.assertEqual({},self.capture())
    def test_capture_failure_missing_file_and_postshot_wrong_card_refuse(self):
        self.page.screenshot.side_effect=RuntimeError('capture failed');self.assertEqual({},self.capture())
        self.page.screenshot.side_effect=lambda **kw:None;self.assertEqual({},self.capture())
        self.page.screenshot.side_effect=lambda **kw:Path(kw['path']).write_bytes(b'fixture')
        self.page.evaluate.side_effect=[self.page.evaluate.return_value]*2+[dict(url=self.url,title='Cable',text='Артикул: XYZ999')]
        self.assertEqual({},self.capture())
    def test_old_http_document_cannot_mask_new_navigation(self):
        self.page.url='https://seller.example/product/XYZ999'
        self.assertEqual({},self.capture())
    def test_callback_copy_and_registry_duplicate_stale_observation_preserve_date_history(self):
        proof=self.capture();original=json.loads(json.dumps(proof));observations=[]
        def mutate(e):observations.append(e);e['main_article']='WRONG'
        with research_session(on_identity=mutate):observed_identity(proof)
        self.assertEqual(original,proof)
        position=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м')
        registry=SourceRegistry(Path(self.tmp.name)/'registry.sqlite3')
        self.assertTrue(registry.remember_identity(position,proof))
        first=registry.load(position)['seller.example']['mappings'][0]
        self.assertTrue(registry.remember_identity(position,proof))
        self.assertFalse(registry.remember_identity(position,{**proof,'observed_at':proof['observed_at']-1}))
        reopened=SourceRegistry(registry.path)
        self.assertEqual(first,reopened.load(position)['seller.example']['mappings'][0])
        self.assertEqual([self.url],reopened.load(position)['seller.example']['urls'])
        self.assertEqual([],registry.history(position,'seller.example'))

    def test_actual_collector_late_identity_precedes_extraction_price_and_seller_refusals(self):
        from engine.search import agent
        from engine.models import Requisites
        position=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м')
        registry=SourceRegistry(Path(self.tmp.name)/'registry.sqlite3')
        browser=MagicMock();browser.page.return_value.__enter__.return_value=self.page
        self.page.locator.return_value.first.inner_text.return_value='Cable'
        for refusal in ('extract','price','seller','review'):
            registry=SourceRegistry(Path(self.tmp.name)/(refusal+'.sqlite3'))
            data=dict(product_title='Cable ABC123',matches_requested_product=True,
                      listed_price_with_vat=120,price_with_vat=120,price_unit='упак')
            if refusal=='extract':data=None
            if refusal=='price':data.update(listed_price_with_vat=None,price_with_vat=None)
            callbacks=[]
            def persist(e):callbacks.append(e);registry.remember_identity(position,e)
            with research_session(on_identity=persist), \
                 patch.object(agent,'_page_text_after_browser_check',return_value='Cable'), \
                 patch.object(agent,'_identity_card_text',return_value='Cable'), \
                 patch('engine.price_capture.ready_price_snapshot',return_value={}), \
                 patch.object(agent,'_extract_from_page',return_value=data), \
                 patch.object(agent,'wl_requisites_for',return_value=None if refusal=='seller' else Requisites(name='Seller',inn='7842224734')), \
                 patch.object(agent,'fetch_requisites_from_site',return_value=None) as site_req, \
                 patch.object(agent,'fetch_requisites',return_value=None) as inn_req, \
                 patch('engine.price_capture.capture_price',return_value=({},'fixture unit review')), \
                 patch.object(agent,'_save_product_screenshot'), \
                 patch.object(agent,'CACHE_DIR',Path(self.tmp.name)):
                offers,_=agent._collect_offers(browser,[self.url],1,'ABC123','Cable','м')
                site_req.assert_not_called();inn_req.assert_not_called()
            self.assertEqual(2 if refusal in {'seller','review'} else 1,len(callbacks),refusal)
            self.assertEqual([self.url],SourceRegistry(registry.path).load(position)['seller.example']['urls'])
            self.assertEqual([],registry.history(position,'seller.example'))
            if refusal in {'seller','review'}:
                self.assertEqual('ABC123',offers[0].identity_evidence['main_article'])
                self.assertTrue(offers[0].price_capture_review_reason)

    def test_concurrent_callbacks_never_regress_latest_evidence(self):
        from concurrent.futures import ThreadPoolExecutor
        position=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м')
        registry=SourceRegistry(Path(self.tmp.name)/'registry.sqlite3')
        proof=self.capture();proofs=[{**proof,'observed_at':proof['observed_at']-i} for i in range(30)]
        with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(lambda e:registry.remember_identity(position,e),proofs))
        retained=registry.load(position)['seller.example']['mappings'][0]['evidence']
        self.assertEqual(proof,retained);self.assertEqual([],registry.history(position,'seller.example'))

    def test_actual_app_late_wrongcard_rejects_quote_and_invalidates_mapping(self):
        self._late_card_change()

    def test_actual_app_redirected_canonical_mapping_is_invalidated_on_late_wrongcard(self):
        self._late_card_change(self.url+'?canonical=1')

    def test_actual_app_late_challenge_excludes_quote_keeps_mapping_and_records_health(self):
        self._late_card_change(protection=True)

    def _late_card_change(self, canonical=None, protection=False):
        from engine import test_all_sites
        from engine.app import _offer_to_dict
        from engine.models import Requisites
        from engine.search import agent
        position=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м')
        registry=SourceRegistry(Path(self.tmp.name)/'registry.sqlite3')
        browser=MagicMock();browser.page.return_value.__enter__.return_value=self.page
        self.page.locator.return_value.first.inner_text.return_value='Cable'
        canonical=canonical or self.url
        self.page.url=canonical;self.page._kac_document_url=canonical
        self.page.evaluate.return_value['url']=canonical
        def price_capture(*args,**kw):
            if protection:self.page.inner_text.return_value='Checking your browser verify you are human'
            else:self.page.evaluate.return_value=dict(url=canonical,title='Cable',text='Артикул: XYZ999')
            return {'provenance':'stable_visible_main_card'},''
        def research(p):
            with agent.capture_card_diagnostics() as diagnostics:
                offers,_=agent._collect_offers(browser,[self.url],1,'ABC123','Cable','м')
            return dict(status='failed',offers=[],candidates=[_offer_to_dict(o) for o in offers],diagnostics=diagnostics)
        with patch('engine.search.source_registry.SourceRegistry',return_value=registry), \
             patch.object(agent,'_page_text_after_browser_check',return_value='Cable'), \
             patch.object(agent,'_identity_card_text',return_value='Cable'), \
             patch('engine.price_capture.ready_price_snapshot',return_value={}), \
             patch.object(agent,'_extract_from_page',return_value=dict(product_title='Cable ABC123',matches_requested_product=True,
                    listed_price_with_vat=120,price_with_vat=120,price_unit='м')), \
             patch.object(agent,'wl_requisites_for',return_value=Requisites(name='Seller',inn='7842224734')), \
             patch('engine.price_capture.capture_price',side_effect=price_capture), \
             patch.object(agent,'CACHE_DIR',Path(self.tmp.name)), \
             patch('engine.config.CACHE_DIR',Path(self.tmp.name)), \
             patch.object(agent,'transport_observed',wraps=agent.transport_observed) as transport:
            job=test_all_sites.AllSitesTests().run_job([position],['seller.example'],lambda *a,**k:[self.url],research,refresh_first=True)
        row=job['rows'][0]
        self.assertEqual([],row['offers'])
        self.assertTrue(row['candidates'][0]['price_capture_review_reason'])
        self.assertEqual({},row['candidates'][0]['identity_evidence'])
        saved=registry.load(position)['seller.example']
        if protection:
            from engine.search.supplier_health_store import Outcome
            self.assertEqual([canonical],saved['urls']);self.assertEqual('confirmed',saved['mappings'][0]['state'])
            self.assertTrue(any(c.args[0]==Outcome.BOT_PROTECTION for c in transport.call_args_list))
        else:
            self.assertEqual([],saved['urls']);self.assertEqual('invalidated',saved['mappings'][0]['state'])
            self.assertTrue(any(h['outcome']['status']=='identity_invalidated' for h in registry.history(position,'seller.example')))

    def test_real_app_publication_replaces_only_stale_seed_reason_and_keeps_refusal_warning(self):
        from engine import test_all_sites
        from engine.app import _current_research_reason,_offer_to_dict
        from engine.models import PriceOffer,Requisites
        from engine.search.session import observed_quote
        from engine.test_price_capture import fixture_capture
        position=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м')
        old='Подтверждённых цен не найдено; проверьте название, артикул и сайты поставщиков'
        warning='; Регион поставки не подтверждён'
        def research(p):
            from engine.search.session import batch_card_urls
            if not batch_card_urls():return dict(status='failed',offers=[],candidates=[],reason=old+warning)
            offer=fixture_capture(PriceOffer(url=self.url,product_title='Cable ABC123',page_title='Cable ABC123',page_article='ABC123',listed_price_with_vat=100,price_with_vat=100,price_unit='м',requisites=Requisites(name='Seller',inn='7842224734')),expected_mark='ABC123',expected_name='Cable')
            # This adapter replaces _research_row, including its real producer
            # observation/receipt boundary, while intentionally retaining the
            # old failed response to exercise publication reason replacement.
            from engine.search import base_db
            from engine.models import PositionResult
            with base_db.capture_current_observations() as produced:
                observed_quote(offer)
            pos=p.to_result().position
            base_db.upsert(pos,PositionResult(position=pos,offers=[offer],all_offers=[offer]),
                           confirmed_observations=produced)
            return dict(status='failed',offers=[],candidates=[],reason=old+warning)
        registry=SourceRegistry(Path(self.tmp.name)/'publication-registry.sqlite3')
        with patch('engine.config.CACHE_DIR',Path(self.tmp.name)), \
             patch('engine.schemas.CACHE_DIR',Path(self.tmp.name)), \
             patch('engine.search.base_db.BASE_FILE',Path(self.tmp.name)/'publication-base.json'), \
             patch('engine.search.source_registry.SourceRegistry',return_value=registry):
            job=test_all_sites.AllSitesTests().run_job([position],['seller.example'],lambda *a,**k:[self.url],research)
            from engine.search import base_db
            receipts=base_db.refresh_receipt_snapshot()
        row=job['rows'][0]
        self.assertEqual(1,row['found'],row);self.assertEqual(1,len(row['offers']))
        self.assertEqual(['confirmed'],[r['status'] for entry in receipts.values()
            for context in entry['refresh_receipts'].values() for r in context.values()])
        self.assertTrue(row['reason'].startswith('Найдено 1 из '));self.assertTrue(row['reason'].endswith(warning))
        review='Свежий поиск не дал цен; показаны ранее проверенные источники; единица не подтверждена'
        result=_current_research_reason(review,0,3,[{'price_with_vat':100}])
        self.assertEqual('Получены свежие данные карточки; цена требует проверки; единица не подтверждена',result)
        self.assertEqual(review,_current_research_reason(review,0,3,[{'refresh_failed':True}]))
        self.assertEqual('Provider error',_current_research_reason('Provider error',1,3,[{}]))
