"""Offline decisions exercised through the production catalogue cursor loop."""
from dataclasses import asdict
import time
import json
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch
from engine import app, test_catalogue_unknown as fixtures
from engine.schemas import PositionIn
from engine.search.candidate_admission import admit_with_hint
from engine.search.catalogue import identity_for_position
from engine.search.search_engine import DiscoveryURLs
from engine.search.supplier_discovery import CandidateHint
from engine.search.supplier_health_store import SupplierHealthStore
from engine.search.session import PersistentSupplierHealth


def saved_candidate_routes():
    """Load public routing inputs without the historical captures or workspace tree."""
    fixture = Path(__file__).parent / 'testdata/candidate-admission/routes.json'
    return json.loads(fixture.read_text(encoding='utf-8'))


def native(model='ABC123', article='', maker='Maker', name='Cable'):
    return PositionIn(number=1, name=name, type_mark=' / '.join(v for v in (model, article) if v), unit='м', qty=1,
        source_evidence={'reader':'native_pdf','cells':{'name':name,'model':model,'article':article,'maker':maker,'unit':'м'}}).to_result().position


def hint(url, title='Cable ABC123', article='', **changes):
    value=asdict(CandidateHint(url,title,article,'',(),0,'search_result_owner',
        'https://www.chipdip.ru/search?searchtext=ABC123','a'*64,time.time()))
    value.update(changes)
    return value


class CandidateAdmissionTests(unittest.TestCase):
    def test_exact_observed_etm_category_only(self):
        from engine.search.product_identity import card_host
        path='/catalog/1635_aksessuary_dlja_kabelnyh_lotkov-138_dkc'
        for origin in ('https://etm.ru','https://www.etm.ru'):
            for suffix in ('','/','?page=680','/?page=680','#products'):
                decision=admit_with_hint(origin+path+suffix,'etm.ru',native())
                self.assertEqual(decision.reason,'candidate_known_listing')
                self.assertEqual(dict(decision.evidence),{'route':'etm_dkc_cable_tray_accessories_category'})
        for url in ('https://etm.ru/cat/nn/123456','https://etm.ru/cat/123456',
                    'https://etm.ru/catalog/other','https://etm.ru/catalog/',
                    'https://etm.ru'+path+'-product','https://etm.ru'+path+'/product',
                    'https://etm.ru/prefix'+path,'https://evil-etm.ru'+path,
                    'https://etm.ru.evil.test'+path,'https://other.test'+path):
            self.assertFalse(admit_with_hint(url,card_host(url),native()).rejected,url)

    def test_exact_observed_ledpremium_filter_only(self):
        base='https://ledpremium.ru/catalog/silovoy_kabel/filter/zhily-4'
        for suffix in ('','/','/?sort=price','#products','/?sort=price#products'):
            decision=admit_with_hint(base+suffix,'ledpremium.ru',native())
            self.assertEqual(decision.reason,'candidate_known_listing')
            self.assertEqual(dict(decision.evidence),{'route':'ledpremium_four_core_cable_filter'})
        for url in ('https://ledpremium.ru/catalog/silovoy_kabel/filter/zhily-5/',
                    base+'-product/',base+'/product/',
                    'https://ledpremium.ru/catalog/silovoy_kabel/product/',
                    'https://ledpremium.ru/catalog/other/filter/zhily-4/',
                    'https://unknown.test/catalog/silovoy_kabel/filter/zhily-4/',
                    'https://evil-ledpremium.ru/catalog/silovoy_kabel/filter/zhily-4/',
                    'https://ledpremium.ru.evil.test/catalog/silovoy_kabel/filter/zhily-4/'):
            from engine.search.product_identity import card_host
            self.assertFalse(admit_with_hint(url,card_host(url),native()).rejected,url)

    def test_known_routes_only_and_catalog_children_retained(self):
        for url in ('https://www.chipdip.ru/catalog-show/cables','https://minimaks.ru/catalog/?q=ABC123','https://thermopat.com/products'):
            host=url.split('/')[2].removeprefix('www.')
            self.assertTrue(admit_with_hint(url,host,native()).rejected,url)
        for url in ('https://minimaks.ru/catalog/cable/ABC123','https://elektro.ru/catalog/cable/ABC123','https://electro-mpo.ru/catalog/cable/ABC123','https://unknown.test/catalog/','https://chipdip.ru/product/ABC123','https://chipdip.ru/catalog-showcase/ABC123'):
            self.assertFalse(admit_with_hint(url,url.split('/')[2],native()).rejected,url)

    def test_role_bound_mpn_and_full_model_conflicts(self):
        url='https://www.chipdip.ru/product/card'
        self.assertEqual(admit_with_hint(url,'chipdip.ru',native(article='SKU123'),hint(url,article='OTHER999')).reason,'discovery_article_conflict')
        self.assertFalse(admit_with_hint(url,'chipdip.ru',native(),hint(url,article='SHOP999')).rejected)
        p=native(model='ABC1-10-24')
        self.assertEqual(admit_with_hint(url,'chipdip.ru',p,hint(url,title='Cable ABC1-20-24')).reason,'discovery_identity_conflict')
        self.assertFalse(admit_with_hint(url,'chipdip.ru',p,hint(url,title='Cable ABC1-10-24')).rejected)
        self.assertFalse(admit_with_hint(url,'chipdip.ru',p,hint(url,title='ABC1-10-24 or ABC1-20-24')).rejected)

    def test_typed_power_conflicts_only_when_unambiguous_and_bound(self):
        url='https://www.chipdip.ru/product/card'
        p=native(model='CT30',article='SKU123',name='Трансформатор тока, мощность 5 ВА')
        for title,expected in [('CT30 3 VA',True),('CT30 5 VA',False),('CT30 5,0 ВА',False),('CT30',False),('CT30 3 VA / 5 VA',False),('CT30 3-5VA',False),('CT30-3VA',False)]:
            self.assertEqual(admit_with_hint(url,'chipdip.ru',p,hint(url,title=title,article='SKU123')).reason=='discovery_power_conflict',expected,title)
        p3=native(model='CT30',name='Transformer 3 VA')
        for title in ('CT30 3 - 5 VA','CT30 3–5 VA','CT30 3—5 VA','CT30 3 / 5 VA','CT30 3 VA to 5 VA','CT30 <=5 VA','CT30 до 5 ВА','CT30 5 VA max'):
            self.assertFalse(admit_with_hint(url,'chipdip.ru',p3,hint(url,title=title)).rejected,title)
        p.review_fields={'model':'review'}
        self.assertFalse(admit_with_hint(url,'chipdip.ru',p,hint(url,title='CT30 3 VA',article='SKU123')).rejected)

    def test_ambiguous_native_and_untrusted_hints_preserved(self):
        url='https://www.chipdip.ru/product/card';p=native(article='SKU123')
        for changes in ({'source_role':'ambiguous_result_owner'},{'source_sha256':'invalid'},{'source_url':'https://foreign.test/search'},{'source_url':'https://chipdip.ru/product/other'},{'observed_at':float('inf')},{'observed_at':True},{'url':'https://chipdip.ru/product/other'}):
            self.assertFalse(admit_with_hint(url,'chipdip.ru',p,{**hint(url,article='OTHER999'),**changes}).rejected,changes)
        p.review_fields={'article':'review'}
        self.assertFalse(admit_with_hint(url,'chipdip.ru',p,hint(url,article='OTHER999')).rejected)
        legacy=PositionIn(number=1,name='Cable',type_mark='SKU123',unit='м',qty=1).to_result().position
        self.assertEqual(admit_with_hint(url,'chipdip.ru',legacy,hint(url,article='OTHER999')).reason,'discovery_article_conflict')


class CandidateProductionTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.CatalogueUnknownTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.hosts[:2]=['chipdip.ru','minimaks.ru']
        self.health=SupplierHealthStore(self.f.root/'health.sqlite3')
        self.f.stack.enter_context(patch('engine.search.supplier_health_store.SupplierHealthStore',return_value=self.health))
        # Only discovery and card-boundary replay are allowed. Any accidental
        # deeper HTTP, browser launch, model or external publication must fail.
        for target in ('httpx.get','httpx.Client.send','engine.browser.supplier_http_get','engine.browser.Browser.__enter__','engine.llm.complete_json'):
            self.f.stack.enter_context(patch(target,side_effect=AssertionError('Offline: transport denied')))
        self.captured=[];self.decisions=[]
        from engine.search.candidate_admission import admit_with_hint as decide
        def traced(*args,**kw):
            value=decide(*args,**kw);self.decisions.append((args[0],value));return value
        self.f.stack.enter_context(patch('engine.search.candidate_admission.admit_with_hint',side_effect=traced))

    def run_candidates(self, candidates, *, model='ABC123', article='SKU123', hints=None, native_position=None, forbid_capture=False):
        def discovery(q,hosts,**kw):
            urls=[u for u in candidates if u.split('/')[2].removeprefix('www.') in hosts]
            meta={h:dict(origin='supplier_own_search',window='unknown',observed_at=time.time(),candidate_count=sum(u.split('/')[2].removeprefix('www.')==h for u in urls),diagnostics=[],candidate_hints={u:v for u,v in (hints or {}).items() if u in urls}) for h in hosts}
            return DiscoveryURLs(urls,host_observations=meta)
        def capture(*args,**kw):
            self.assertFalse(forbid_capture,'Listing reached native card-capture callback')
            from engine.search.session import _HEALTH
            self.assertIsInstance(_HEALTH.get(),PersistentSupplierHealth)
            self.assertIs(_HEALTH.get().store,self.health)
            self.captured.append(args[3]);return None
        position=PositionIn(number=1,name='Cable',type_mark=' / '.join(v for v in (model,article) if v),unit='м',qty=1,source_evidence={'reader':'native_pdf','cells':{'name':'Cable','model':model,'article':article,'maker':'Maker','unit':'м'}})
        job,public=self.f.job([native_position or position],search=discovery,capture=capture)
        self.assertEqual(job['status'],'done',job.get('error'))
        return job,public,self.f.store.lookup(identity_for_position(position))

    def test_production_rejects_before_capture_retains_cards_and_settles_once(self):
        urls=['https://www.chipdip.ru/catalog-show/cables','https://www.chipdip.ru/product/wrong','https://www.chipdip.ru/product/exact','https://minimaks.ru/catalog/cable/ABC123']
        hints={urls[1]:hint(urls[1],article='OTHER999'),urls[2]:hint(urls[2],article='SKU123')}
        job,public,product=self.run_candidates(urls,hints=hints)
        self.assertEqual(self.captured,urls[2:])
        self.assertEqual([v.reason for _,v in self.decisions if v.rejected],['candidate_known_listing','discovery_article_conflict'])
        self.assertEqual(len(self.decisions),4) # Repeated query results never replay settled cards.
        self.assertEqual(set(job['catalogue_discovered_urls'][product['product_id']]),set(urls))
        self.assertTrue(all(c['product_presence']=='unknown' for c in product['coverage'].values()))
        self.assertTrue(all(product['coverage'][h]['status']=='review' for h in self.f.hosts[:2]))
        payload=app._research_store().db.execute('SELECT payload FROM rows WHERE job=? AND idx=0',(job['job_id'],)).fetchone()[0]
        frontier=json.loads(payload)['_catalogue_acquisition_frontier']
        self.assertTrue(frontier['frontier_complete'])
        self.assertTrue(all(q['cursor']==q['response']['count'] for q in frontier['queries'] if q['response']))

    def test_trusted_typed_power_conflict_never_reaches_capture(self):
        url='https://www.chipdip.ru/product/power'
        p=PositionIn(number=1,name='Transformer 5 VA',type_mark='CT30 / SKU123',unit='шт',qty=1,
            source_evidence={'reader':'native_pdf','cells':{'name':'Transformer 5 VA','model':'CT30','article':'SKU123','maker':'Maker','unit':'шт'}})
        job,public,product=self.run_candidates([url],hints={url:hint(url,title='CT30 3 VA',article='SKU123')},native_position=p)
        self.assertEqual(self.captured,[])
        self.assertEqual([d.reason for _,d in self.decisions],['discovery_power_conflict'])
        self.assertEqual(product['coverage']['chipdip.ru']['product_presence'],'unknown')

    def test_range_power_remains_normal_capture_in_production(self):
        url='https://www.chipdip.ru/product/range'
        p=PositionIn(number=1,name='Transformer 3 VA',type_mark='CT30',unit='шт',qty=1,
            source_evidence={'reader':'native_pdf','cells':{'name':'Transformer 3 VA','model':'CT30','article':'','maker':'Maker','unit':'шт'}})
        self.run_candidates([url],hints={url:hint(url,title='CT30 3 – 5 VA')},native_position=p)
        self.assertEqual(self.captured,[url])

    def test_saved_routes_through_actual_production_loop(self):
        """Exercise retained routes through the native scheduler with stubbed transport."""
        routes = saved_candidate_routes()
        positions = {v['number']: PositionIn.model_validate(v) for v in routes['positions']}
        self.f.hosts[:] = list(dict.fromkeys(c['url'].split('/')[2].removeprefix('www.') for c in routes['cases']))
        expected = []
        for number in (11, 12, 29):
            cases = [case for case in routes['cases'] if case['position_number'] == number]
            self.run_candidates([case['url'] for case in cases], native_position=positions[number])
            expected.extend(case['url'] for case in cases if case['expected'] != 'recognised_listing')
        # The scheduler can prioritize suppliers independently of discovery order.
        self.assertCountEqual(self.captured, expected)
        self.assertEqual(len(self.captured), 21)
        rejected = {url for url, decision in self.decisions if decision.rejected}
        self.assertEqual(rejected, {case['url'] for case in routes['cases']
                                    if case['expected'] == 'recognised_listing'})

    def test_all_rejected_never_becomes_supplier_absence(self):
        url='https://www.chipdip.ru/catalog-show/cables'
        job,public,product=self.run_candidates([url])
        self.assertEqual(self.captured,[])
        cover=product['coverage']['chipdip.ru']
        self.assertEqual(cover['status'],'review');self.assertEqual(cover['product_presence'],'unknown')
        self.assertEqual(cover['diagnostics'][0]['code'],'candidate_known_listing')
        self.assertEqual(public['rows'][0]['found'],0)

    def test_observed_etm_category_native_rejection_and_completion_evidence(self):
        self.f.hosts[:]=['etm.ru']
        url='https://www.etm.ru/catalog/1635_aksessuary_dlja_kabelnyh_lotkov-138_dkc?page=680'
        job,public,product=self.run_candidates([url],forbid_capture=True)
        self.assertEqual(self.captured,[])
        cover=product['coverage']['etm.ru']
        self.assertEqual((cover['status'],cover['product_presence']),('review','unknown'))
        diagnostic=next(d for d in cover['diagnostics'] if d['code']=='candidate_known_listing')
        self.assertEqual(diagnostic['failure_cause'],'listing')
        self.assertIn('etm_dkc_cable_tray_accessories_category',diagnostic['reason'])
        self.assertNotIn('deferred_check',cover);self.assertEqual(public['rows'][0]['found'],0)
        payload=app._research_store().db.execute('SELECT payload FROM rows WHERE job=? AND idx=0',(job['job_id'],)).fetchone()[0]
        frontier=json.loads(payload)['_catalogue_acquisition_frontier']
        self.assertTrue(frontier['frontier_complete'])
        folder=app.RESEARCH_JOB_DIR/'acquisition'/job['job_id']/product['product_id']
        from engine.search.acquisition_frontier import NeutralRecords,FrontierController
        records=NeutralRecords(folder,frontier)
        outcomes=[records._read(item['outcome']) for item in FrontierController(frontier,records).completed_cards() if item['outcome']]
        self.assertTrue(outcomes)
        definitive=[o for o in outcomes if o['completion']['disposition']=='deterministic_rejection']
        self.assertEqual(len(definitive),1)
        self.assertEqual((definitive[0]['state'],definitive[0]['observation_id']),('review',None))
        self.assertEqual(definitive[0]['completion']['evidence']['code'],'candidate_known_listing')
        self.assertTrue(all(o in definitive or o['completion']=={'version':1,'disposition':'duplicate','evidence':{}} for o in outcomes))
        # This new synthetic job alone proves the existing verifier accepts
        # its route-bound outcome; frozen real outcomes are never rewritten.
        self.assertIsNotNone(product['acquisition_completion'])
        from engine.search.acquisition_frontier import validate_completion_receipt
        self.assertEqual(validate_completion_receipt(product['acquisition_completion'])['job_id'],job['job_id'])

    def test_observed_ledpremium_listing_never_captured_or_cooled_down(self):
        self.f.hosts[:]=['ledpremium.ru']
        url='https://ledpremium.ru/catalog/silovoy_kabel/filter/zhily-4/'
        # The callback passed into the native indexed fixture must never run.
        job,public,product=self.run_candidates([url],forbid_capture=True)
        self.assertEqual(self.captured,[])
        cover=product['coverage']['ledpremium.ru']
        self.assertEqual((cover['status'],cover['product_presence']),('review','unknown'))
        diagnostic=next(d for d in cover['diagnostics'] if d['code']=='candidate_known_listing')
        self.assertEqual(diagnostic['failure_cause'],'listing')
        self.assertIn('ledpremium_four_core_cable_filter',diagnostic['reason'])
        self.assertNotIn('deferred_check',cover)
        self.assertEqual(public['rows'][0]['found'],0)
        payload=app._research_store().db.execute('SELECT payload FROM rows WHERE job=? AND idx=0',(job['job_id'],)).fetchone()[0]
        frontier=json.loads(payload)['_catalogue_acquisition_frontier']
        self.assertTrue(frontier['frontier_complete'])
        self.assertTrue(all(q['cursor']==q['response']['count'] for q in frontier['queries'] if q['response']))


class FrozenCorpusAdmissionTests(unittest.TestCase):
    def test_saved_routes_retain_every_non_listing_candidate(self):
        """Discovery may reject known listings but must leave card facts for later checks."""
        routes = saved_candidate_routes()
        positions = {v['number']: PositionIn.model_validate(v).to_result().position
                     for v in routes['positions']}
        rejected = []
        retained = []
        for case in routes['cases']:
            url = case['url']
            host = url.split('/')[2].removeprefix('www.')
            decision = admit_with_hint(url, host, positions[case['position_number']])
            self.assertEqual(decision.rejected, case['expected'] == 'recognised_listing', url)
            (rejected if decision.rejected else retained).append(url)
        self.assertEqual((len(rejected), len(retained)), (3, 21))
        for card in routes['cards']:
            url = card['url']
            host = url.split('/')[2].removeprefix('www.')
            self.assertFalse(admit_with_hint(url, host, positions[card['position_number']]).rejected, url)

    def test_new_admission_source_is_bound_to_resume_runtime(self):
        runtime=app._catalogue_frontier_runtime()
        path=Path(__file__).parent/'search/candidate_admission.py'
        self.assertEqual(runtime['candidate_admission_sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
