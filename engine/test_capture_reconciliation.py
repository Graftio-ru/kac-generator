"""Synthetic offline regressions; no historical quote promotion or network."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch
from engine.price_capture import capture_price, reconcile_capture_identity, qualify_snapshot
from engine.search.product_identity import rendered_identity

class CaptureReconciliationTests(unittest.TestCase):
    def setUp(self):
        from engine.test_price_capture import PriceCaptureTests
        fixture=PriceCaptureTests()
        self.snapshot=fixture.snapshot()
        self.fields=fixture.fields()
        self.proof,_=qualify_snapshot(self.snapshot,**self.fields)
        self.identity=rendered_identity(self.fields['url'],self.fields['url'],self.snapshot['title'],
            'ABC123','ABC123','Item','seller.example')

    def test_hydrated_heading_uses_capture_own_identity_not_early_heading(self):
        # Both headings identify the same SKU. Only current purchase heading is
        # compared; an earlier card state is not a price-capture signature.
        early=rendered_identity(self.fields['url'],self.fields['url'],'Item ABC123 old heading',
            'ABC123','ABC123','Item','seller.example')
        self.assertTrue(early)
        self.assertNotEqual(early['main_title'],self.identity['main_title'])
        proof,reason=reconcile_capture_identity(self.proof,self.identity,self.snapshot)
        self.assertEqual(self.proof,proof);self.assertEqual('',reason)

    def test_wrong_sku_redirect_price_unit_and_pack_changes_remain_refused(self):
        for field,value in [('main_article','XYZ999'),('canonical_url','https://other.example/product/ABC123'),
                            ('main_title','Other XYZ999')]:
            self.assertFalse(reconcile_capture_identity(self.proof,{**self.identity,field:value},self.snapshot)[0])
        for text in ['240 ₽ / уп','120 ₽ / шт','Упаковка 24 м']:
            snapshot=deepcopy(self.snapshot)
            if text.startswith('Упаковка'):
                snapshot['text']=snapshot['text'].replace('Упаковка 12 м',text)
            else:
                snapshot['text']=snapshot['text'].replace('120 ₽ / уп',text)
                snapshot['prices'][0]['text']=text
            self.assertFalse(reconcile_capture_identity(self.proof,self.identity,snapshot)[0])

    def test_capture_refusal_reports_stage_without_exposing_exception(self):
        from engine.price_capture import FONT_READY_SCRIPT, FONT_RUN_OBSERVATION
        page=MagicMock()
        def settled_then_unreadable(script,arg=None):
            if script==FONT_READY_SCRIPT:return dict(loaded=True,status='loaded',render_frames=2)
            if script==FONT_RUN_OBSERVATION:return dict(observational_only=True,runs=[])
            raise RuntimeError('sensitive exception text')
        page.evaluate.side_effect=settled_then_unreadable
        stages=[]
        proof,reason=capture_price(page,Path('unused.png'),on_refusal=stages.append,**self.fields)
        self.assertFalse(proof);self.assertEqual(['price_dom_unreadable'],stages)
        self.assertNotIn('sensitive',reason)
        with TemporaryDirectory() as tmp:
            from engine.test_capture_visibility import Page
            snapshot=deepcopy(self.snapshot);snapshot['heading']['y']=200
            for f in snapshot['fragments']:f['y']+=200
            snapshot['prices'][0]['y']+=200
            page=Page(snapshot,fail=RuntimeError('sensitive'));stages=[]
            proof,reason=capture_price(page,Path(tmp)/'shot.png',on_refusal=stages.append,**self.fields)
            self.assertFalse(proof);self.assertEqual(['price_screenshot_failed'],stages)

    def test_actual_collector_retains_current_identity_after_capture_failure_and_hydration(self):
        self._collector_hydration(False)

    def test_actual_collector_hydrated_purchase_heading_preserves_coherent_capture(self):
        self._collector_hydration(True)

    def _collector_hydration(self, success):
        from engine.search import agent
        from engine.models import Requisites
        from engine.search.session import research_session
        browser=MagicMock();page=MagicMock()
        browser.page.return_value.__enter__.return_value=page
        page.url=self.fields['url'];page.locator.return_value.first.inner_text.return_value='Item ABC123 old heading'
        page._kac_document_url=page.url;page._kac_response_status=200
        page.title.return_value='Item ABC123';page.inner_text.return_value='Item ABC123\nАртикул: ABC123'
        old=rendered_identity(page.url,page.url,'Item ABC123 old heading','ABC123','ABC123','Item','seller.example')
        late=self.identity
        observed=[]
        current=deepcopy(self.snapshot)
        current['text']=current['text'].replace('120 ₽ / уп','120 ₽ / м')
        current['prices'][0]['text']='120 ₽ / м'
        from engine.test_purchase_scope import fixture_purchase_snapshot
        current=fixture_purchase_snapshot(current)
        proof,_=qualify_snapshot(current,**{**self.fields,'sale_unit':'м','normalized':120})
        with TemporaryDirectory() as tmp, research_session(on_identity=observed.append), \
            patch.object(agent,'_page_text_after_browser_check',return_value='Item ABC123\nАртикул: ABC123'), \
            patch.object(agent,'_identity_card_text',return_value='Item ABC123\nАртикул: ABC123'), \
            patch('engine.price_capture.ready_price_snapshot',return_value=current if success else {}), \
            patch('engine.search.product_identity.rendered_identity',return_value=old), \
            patch('engine.search.product_identity.capture_current_identity',return_value=late) as identity_capture, \
            patch.object(agent,'_extract_from_page',return_value=dict(product_title='Item ABC123',matches_requested_product=True,
                listed_price_with_vat=120,price_with_vat=120,price_unit='м')), \
            patch.object(agent,'wl_requisites_for',return_value=Requisites(name='Seller',inn='7842224734')), \
            patch('engine.price_capture.capture_price',return_value=(proof,'') if success else ({},'price capture failed')) as price_capture, \
            patch('engine.price_capture._snapshot',return_value=current), \
            patch.object(agent,'_save_product_screenshot'),patch.object(agent,'CACHE_DIR',Path(tmp)):
            offers,_=agent._collect_offers(browser,[page.url],1,'ABC123','Item','м')
        self.assertEqual(late,offers[0].identity_evidence)
        self.assertEqual(bool(success),bool(offers[0].price_capture_evidence))
        self.assertEqual(not success,bool(offers[0].price_capture_review_reason))
        self.assertEqual('',identity_capture.call_args.args[6])
        if success:
            self.assertEqual(current['title'],price_capture.call_args.kwargs['title'])
            self.assertEqual('ABC123',price_capture.call_args.kwargs['article'])
        self.assertIn(late,observed)

    def test_actual_admitted_transport_progress_precedes_terminal_row(self):
        from engine import app, test_all_sites
        from engine.schemas import PositionIn
        from engine.search.session import transport_started
        progress=[]
        def search(query, hosts, **kwargs):
            transport_started('search_api')
            row=app.RESEARCH_JOBS['a11517e12345']['progress_rows']['0']
            progress.append(deepcopy(row))
            return ['https://seller.example/product/ABC123']
        def research(position):
            row=app.RESEARCH_JOBS['a11517e12345'].get('progress_rows',{}).get('0')
            if row:
                progress.append(deepcopy(row))
            return dict(status='failed',candidates=[],found=0)
        job=test_all_sites.AllSitesTests().run_job(
            [PositionIn(number=1,name='Item',type_mark='ABC123',unit='м')],
            ['seller.example'],search,research)
        self.assertTrue(any(p['queries_attempted']==1 and p['sites_attempted']==1
                            and p['sites_finished']==0 for p in progress))
        self.assertTrue(any(p['cards_attempted']==1 and p['sites_finished']==0 for p in progress))
        self.assertEqual('done',job['status'])

    def test_valid_identity_refresh_reuses_card_even_with_failed_price(self):
        from engine import test_all_sites
        from engine.schemas import PositionIn
        from engine.search.source_registry import SourceRegistry
        from engine.search.session import batch_card_urls, observed_identity
        position=PositionIn(number=1,name='Item',type_mark='ABC123',unit='м')
        with TemporaryDirectory() as tmp:
            registry=SourceRegistry(Path(tmp)/'registry.sqlite3')
            registry.remember_identity(position,self.identity)
            cards=[]
            def research(p):
                if batch_card_urls():
                    cards.extend(batch_card_urls())
                    observed_identity(self.identity)
                return dict(status='failed',found=0,candidates=[])
            def search(*a,**kw):
                self.fail('Confirmed current SKU must reuse known card without alias rediscovery')
            with patch('engine.search.source_registry.SourceRegistry',return_value=registry):
                job=test_all_sites.AllSitesTests().run_job([position],['seller.example'],search,
                    research,refresh_first=True)
            self.assertEqual([self.fields['url']],cards)
            self.assertEqual(0,job['rows'][0]['found'])
            self.assertEqual(0,job['rows'][0]['source_checks'][0]['queries'])

    def test_grouped_search_is_one_wire_query_and_two_attributed_sites(self):
        from engine import app, test_all_sites
        from engine.schemas import PositionIn
        from engine.search.session import transport_started
        snapshots=[]
        def search(q,hosts,**kw):
            transport_started('search_api')
            snapshots.append(deepcopy(app.RESEARCH_JOBS['a11517e12345']['progress_rows']['0']))
            return []
        with patch('engine.search.discovery_plan.query_sweeps',return_value=[('literal',['a.ru','b.ru'])]):
            test_all_sites.AllSitesTests().run_job([PositionIn(number=1,name='Item',unit='м')],
                ['a.ru','b.ru'],search,lambda p:dict(status='failed',found=0,candidates=[]))
        self.assertTrue(snapshots)
        self.assertEqual(1,snapshots[0]['queries_attempted'])
        self.assertEqual(2,snapshots[0]['sites_attempted'])
        self.assertEqual(0,snapshots[0]['sites_finished'])

    def test_two_cards_same_host_keep_finished_host_when_later_cancelled(self):
        from engine import app, test_all_sites
        from engine.schemas import PositionIn
        from engine.search.session import batch_card_urls
        def search(q,hosts,**kw):
            if hosts==['b.ru']:
                app.RESEARCH_JOBS['a11517e12345']['cancel_requested']=True
                return []
            return ['https://a.ru/product/ABC123','https://a.ru/goods/ABC123']
        def research(p):
            urls=batch_card_urls() or []
            return dict(status='failed',found=0,candidates=[],
                diagnostics=[dict(code='site_error',url=u,reason='observed local failure') for u in urls])
        def ordered_worker(indices,*,seed,discover,cards,publish,**kw):
            row,_=seed(0)
            urls,diagnostics,_=discover(0,('literal',['a.ru']))
            row=cards(0,urls,row)
            _,later_diagnostics,_=discover(0,('literal',['b.ru']))
            publish(0,row,diagnostics+later_diagnostics,terminal=True)
        with patch('engine.search.shared_pipeline.run_shared_pipeline',side_effect=ordered_worker):
            job=test_all_sites.AllSitesTests().run_job([PositionIn(number=1,name='Item',type_mark='ABC123',unit='м')],
                ['a.ru','b.ru'],search,research)
        byhost={c['host']:c for c in job['rows'][0]['source_checks']}
        self.assertEqual('cancelled',job['status'])
        self.assertEqual(2,byhost['a.ru']['cards'])
        self.assertEqual('error',byhost['a.ru']['status'])
        self.assertEqual('incomplete',byhost['b.ru']['status'])

    def test_transient_transport_publish_preserves_prior_diagnostics(self):
        from engine import app, test_all_sites
        from engine.schemas import PositionIn
        from engine.search.session import transport_started
        seen=[]
        diagnostic=dict(code='price_capture_review',url='https://a.ru/product/ABC123',
                        reason='actual capture refusal',capture_stage='price_screenshot_failed')
        def search(q,hosts,**kw):
            prior=app.RESEARCH_JOBS['a11517e12345']['progress_rows']['0']
            prior['diagnostics']=[diagnostic]
            transport_started('search_api')
            seen.append(deepcopy(app.RESEARCH_JOBS['a11517e12345']['progress_rows']['0']))
            return []
        job=test_all_sites.AllSitesTests().run_job([PositionIn(number=1,name='Item',unit='м')],
            ['a.ru'],search,lambda p:dict(status='failed',found=0,candidates=[]))
        self.assertIn(diagnostic,seen[0]['diagnostics'])
        self.assertIn(diagnostic,job['rows'][0]['diagnostics'])

    def test_finished_card_failure_with_cooldown_retains_error_outcome(self):
        from engine import test_all_sites
        from engine.schemas import PositionIn
        from engine.search.session import PersistentSupplierHealth, batch_card_urls
        from engine.search.supplier_health_store import SupplierHealthStore, Outcome
        with TemporaryDirectory() as tmp:
            store=SupplierHealthStore(Path(tmp)/'health.sqlite3')
            health=PersistentSupplierHealth(store=store,hosts=['a.ru'])
            url='https://a.ru/product/ABC123'
            def research(p):
                if batch_card_urls():
                    store.finish(store.acquire('a.ru'),Outcome.HTTP_FORBIDDEN,cooldown_seconds=120)
                    return dict(status='failed',found=0,candidates=[],
                        diagnostics=[dict(code='http_blocked',url=url,reason='observed HTTP403')])
                return dict(status='failed',found=0,candidates=[])
            def ordered_worker(indices,*,seed,discover,cards,publish,**kw):
                row,_=seed(0)
                urls,diagnostics,_=discover(0,('literal',['a.ru']))
                row=cards(0,urls,row)
                publish(0,row,diagnostics,terminal=True)
            with patch('engine.search.session.PersistentSupplierHealth',return_value=health), \
                 patch('engine.search.shared_pipeline.run_shared_pipeline',side_effect=ordered_worker):
                job=test_all_sites.AllSitesTests().run_job([PositionIn(number=1,name='Item',type_mark='ABC123',unit='м')],
                    ['a.ru'],lambda *a,**kw:[url],research)
            check=job['rows'][0]['source_checks'][0]
            self.assertEqual('error',check['status'])
            self.assertEqual('unknown',check['product_presence'])
