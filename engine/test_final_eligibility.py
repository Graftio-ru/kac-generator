"""Real final API/pipeline boundaries and context-bound saved current captures."""
import copy
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from . import config
from .models import SpecPosition, PriceOffer, PositionResult, Requisites
from .price_capture import unit_capture_review_reason
from .offer_selection import select_top_offers
from .final_eligibility import prepare_final_results
from .search.product_identity import rendered_identity


class FinalEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.cache=Path(self.tmp.name)
        self.patch=patch.object(config,'CACHE_DIR',self.cache);self.patch.start()
        self.pos=SpecPosition(number=1,name='Лоток 200х50',type_mark='3526410',unit='м')
    def tearDown(self):
        self.patch.stop();self.tmp.cleanup()
    def captured(self, sale='м',quantity=1,title='Лоток 200х50 3526410 (Упаковка 12 м)',unit_text='цена за 1 м',price=100):
        url='https://supplier.ru/product/3526410'
        stamp=time.time()-2
        identity=rendered_identity(url,url,title,'3526410','3526410',self.pos.name,'supplier.ru')
        self.assertTrue(identity)
        capture_index=getattr(self,'capture_index',0);self.capture_index=capture_index+1
        price_file='price.png' if not capture_index else f'price_{capture_index}.png'
        identity_file='identity.png' if not capture_index else f'identity_{capture_index}.png'
        for name in (price_file,identity_file):(self.cache/name).write_bytes(b'actual fixture '+name.encode())
        identity.update(observed_at=stamp+1,identity_screenshot=identity_file,identity_sha256=hashlib.sha256((self.cache/identity_file).read_bytes()).hexdigest())
        e=dict(version=1,provenance='stable_visible_main_card',url=url,title=title,article='3526410',listed_price=price*quantity,normalized_price=price,sale_unit=sale,requested_unit=__import__('engine.search.agent',fromlist=['_sale_unit'])._sale_unit(self.pos.unit),quantity=quantity,basis=title if sale!=self.pos.unit else unit_text,unit_text=unit_text,price_text=f'{price*quantity:.2f} ₽',screenshot=price_file,sha256=hashlib.sha256((self.cache/price_file).read_bytes()).hexdigest(),captured_at=stamp)
        from .test_price_capture import fixture_visibility_attestation
        fixture_visibility_attestation(e,self.cache/price_file)
        identity['observed_at']=e['captured_at']+.001
        return PriceOffer(price_with_vat=price,url=url,product_title=title,page_article='3526410',identity_evidence=identity,price_capture_evidence=e,price_unit=sale,normalized_price_unit=self.pos.unit,listed_price_with_vat=price*quantity,screenshot_product=str(self.cache/price_file),requisites=Requisites(inn='1'))
    def test_current_meter_pack_and_direct_piece_ignore_title_pack_as_sale_unit(self):
        o=self.captured('уп',12,unit_text='цена за упаковку');self.assertEqual(unit_capture_review_reason(o,self.pos),'')
        self.pos.unit='шт'
        o=self.captured(title='Лоток 3526410 (Упаковка 70 шт)',sale='шт',unit_text='цена за 1 шт')
        self.assertEqual(unit_capture_review_reason(o,self.pos),'')
    def test_current_proof_bound_to_spec_and_all_guard_fields(self):
        o=self.captured()
        for changes in ({'unit':'шт'},{'type_mark':'3526411'},{'name':'другой товар'}):
            with self.subTest(changes=changes):self.assertTrue(unit_capture_review_reason(o,self.pos.model_copy(update=changes)))
        for field,value in [('price_with_vat',101),('price_unit','шт'),('url','https://supplier.ru/product/3526411')]:
            with self.subTest(field=field):self.assertTrue(unit_capture_review_reason(o.model_copy(update={field:value}),self.pos))
        for key,value in [('quantity',2),('requested_unit','шт'),('basis','неизвестно'),('unit_text','цена за 1 шт'),('price_text','500 ₽')]:
            bad=o.model_copy(deep=True);bad.price_capture_evidence[key]=value
            if key=='basis': bad=self.captured('уп',12,unit_text='цена за упаковку');bad.price_capture_evidence[key]=value
            with self.subTest(key=key):self.assertTrue(unit_capture_review_reason(bad,self.pos))
        (self.cache/'identity.png').write_bytes(b'changed')
        self.assertTrue(unit_capture_review_reason(o,self.pos))
    def test_missing_invalid_capture_never_becomes_manual_fallback(self):
        o=self.captured();o.price_capture_evidence={}
        self.assertTrue(unit_capture_review_reason(o,self.pos))
        o.listed_price_with_vat=None;o.price_capture_evidence={'version':1}
        self.assertTrue(unit_capture_review_reason(o,self.pos))
    def test_complete_kit_direct_piece_only_with_explicit_composition(self):
        self.pos.unit='компл'
        o=self.captured(sale='шт',title='Монтажный комплект 3526410 в комплекте с крепежом',unit_text='цена за 1 шт')
        self.assertEqual(unit_capture_review_reason(o,self.pos),'')
        bad=o.model_copy(deep=True);bad.price_capture_evidence['title']='Винт 3526410';bad.identity_evidence['main_title']='Винт 3526410'
        self.assertTrue(unit_capture_review_reason(bad,self.pos))
    def manual(self,price,inn,host=None):
        return PriceOffer(price_with_vat=price,url=f'https://{host or inn}.ru/p',requisites=Requisites(inn=inn))
    def test_independent_most_expensive_three_over_all_candidates(self):
        offers=[self.manual(10,'1'),self.manual(40,'1','other'),self.manual(30,'2'),self.manual(20,'3'),self.manual(50,'4')]
        r=PositionResult(position=self.pos,offers=offers[:1],all_offers=offers)
        prepare_final_results([r]);self.assertEqual([o.price_with_vat for o in r.offers],[30,40,50])
    def test_standard_manual_and_unique_nonletter_still_require_three(self):
        for unique in (False,True):
            r=PositionResult(position=self.pos.model_copy(update={'is_unique':unique}),offers=[self.manual(10,'1')])
            with self.assertRaisesRegex(ValueError,'1 из 3'):prepare_final_results([r])
        r.position.is_unique=True;r.offers[0].from_letter=True
        prepare_final_results([r]);self.assertEqual(len(r.offers),1)
    def test_real_api_refuses_before_final_spawn_and_selects_highest_three(self):
        from . import app
        from .schemas import BuildRequest,PositionIn,OfferIn
        from fastapi.testclient import TestClient
        client=TestClient(app.app)
        def request(prices):
            return BuildRequest(object_name='Test',skip_search=True,positions=[PositionIn(number=1,name='Manual',unit='шт',offers=[OfferIn(price_with_vat=p,inn=str(i+1),org_name='Seller') for i,p in enumerate(prices)])]).model_dump()
        with patch.object(app,'run_pipeline_async') as spawn:
            response=client.post('/build',json=request([10,20]));self.assertEqual(response.status_code,422);spawn.assert_not_called()
            response=client.post('/build',json=request([10,40,20,30]));self.assertEqual(response.status_code,200)
            results=spawn.call_args.args[1].spec_results;self.assertEqual([o.price_with_vat for o in results[0].offers],[20,30,40])
    def test_actual_pipeline_blocks_post_search_before_builders(self):
        from . import pipeline
        from .search import agent,base_db
        from contextlib import nullcontext
        r=PositionResult(position=self.pos)
        found=PositionResult(position=self.pos,offers=[self.manual(10,'1')])
        job=pipeline.JobState('test','Test')
        with patch.object(base_db,'lookup',return_value=None),patch.object(base_db,'upsert'),patch.object(agent,'research_position',return_value=found),patch.object(pipeline,'build_tkp') as tkp,patch.object(pipeline,'build_kac') as kac:
            pipeline.run_pipeline(job,pipeline.JobInputs('Test',[r],use_tsn=False),lambda:nullcontext(None))
        self.assertEqual(job.status,'error');self.assertIn('1 из 3',job.error);tkp.assert_not_called();kac.assert_not_called()
    def test_api_current_proof_reaches_count_guard_and_changed_spec_is_blocked(self):
        from . import app,schemas
        from .schemas import BuildRequest,PositionIn,OfferIn
        from fastapi.testclient import TestClient
        c=self.captured('уп',12,unit_text='цена за упаковку')
        fields=c.model_dump();fields.pop('requisites');fields['screenshot_requisites']='';fields.update(inn='1',org_name='Seller')
        row=PositionIn(number=1,name=self.pos.name,type_mark=self.pos.type_mark,unit=self.pos.unit,offers=[OfferIn(**fields)])
        with patch.object(schemas,'CACHE_DIR',self.cache),patch.object(app,'run_pipeline_async') as spawn:
            response=TestClient(app.app).post('/build',json=BuildRequest(object_name='Test',skip_search=True,positions=[row]).model_dump())
            self.assertEqual(response.status_code,422);self.assertIn('1 из 3',response.text);spawn.assert_not_called()
            row.offers.extend([OfferIn(price_with_vat=90,inn='2',org_name='Seller2'),OfferIn(price_with_vat=110,inn='3',org_name='Seller3')])
            response=TestClient(app.app).post('/build',json=BuildRequest(object_name='Test',skip_search=True,positions=[row]).model_dump())
            self.assertEqual(response.status_code,200)
            row.unit='шт';spawn.reset_mock()
            response=TestClient(app.app).post('/build',json=BuildRequest(object_name='Test',skip_search=True,positions=[row]).model_dump())
            self.assertEqual(response.status_code,422);spawn.assert_not_called()
    def test_api_unsearched_standard_row_can_enter_search_stage(self):
        from . import app
        from .schemas import BuildRequest,PositionIn
        from fastapi.testclient import TestClient
        with patch.object(app,'run_pipeline_async') as spawn:
            response=TestClient(app.app).post('/build',json=BuildRequest(object_name='Test',skip_search=False,positions=[PositionIn(number=1,name='Item')]).model_dump())
            self.assertEqual(response.status_code,200);spawn.assert_called_once()
    def test_pipeline_valid_search_selects_highest_three_and_builds(self):
        from . import pipeline
        from .search import agent,base_db
        from contextlib import nullcontext
        r=PositionResult(position=self.pos)
        all_offers=[self.manual(10,'1'),self.manual(20,'2'),self.manual(30,'3'),self.manual(50,'4')]
        found=PositionResult(position=self.pos,offers=all_offers[:1],all_offers=all_offers)
        job=pipeline.JobState('test','Test')
        with patch.object(base_db,'lookup',return_value=None),patch.object(base_db,'upsert'),patch.object(agent,'research_position',return_value=found),patch.object(pipeline,'build_tkp',return_value=(Path('TKP.pdf'),{})) as tkp,patch.object(pipeline,'build_kac',return_value=Path('KAC.xlsx')) as kac:
            pipeline.run_pipeline(job,pipeline.JobInputs('Test',[r],use_tsn=False),lambda:nullcontext(None))
        self.assertEqual(job.status,'done');self.assertEqual([o.price_with_vat for o in r.offers],[20,30,50]);tkp.assert_called_once();kac.assert_called_once()

    def test_unique_letter_preserves_issuer_lookup_pending_without_standard_exemption(self):
        r=PositionResult(position=self.pos.model_copy(update={'is_unique':True}),offers=[PriceOffer(price_with_vat=10,from_letter=True)])
        prepare_final_results([r]);self.assertEqual(len(r.offers),1)
        r.position.is_unique=False
        with self.assertRaises(ValueError):prepare_final_results([r])
    def test_literal_direct_mass_volume_and_aliases(self):
        for requested,sale,label in [('килограмм','кг','цена за 1 кг'),('литр','л','цена за 1 л'),('метр','м','цена за 1 м')]:
            self.pos.unit=requested
            o=self.captured(sale=sale,unit_text=label)
            o.price_capture_evidence['requested_unit']=sale
            self.assertEqual(unit_capture_review_reason(o,self.pos),'')
            o.price_capture_evidence['unit_text']='цена за 1 шт'
            self.assertTrue(unit_capture_review_reason(o,self.pos))

    def test_anonymous_host_quote_cannot_create_extra_independent_seller(self):
        offers=[self.manual(10,'123','same'),self.manual(20,'','same'),self.manual(30,'456','other')]
        r=PositionResult(position=self.pos,offers=offers)
        with self.assertRaisesRegex(ValueError,'2 из 3'):prepare_final_results([r])
        self.assertEqual([20,30],[o.price_with_vat for o in select_top_offers(offers,3,self.pos)])
    def test_marketplace_identified_sellers_remain_distinct_anonymous_is_uncertain(self):
        from .offer_selection import seller_review_reason
        offers=[self.manual(10,'123','market'),self.manual(20,'456','market'),self.manual(50,'','market'),self.manual(30,'789','other')]
        r=PositionResult(position=self.pos,offers=offers)
        prepare_final_results([r]);self.assertEqual([10,20,30],[o.price_with_vat for o in r.offers])
        self.assertIn('ИНН',seller_review_reason(offers[2],offers))
        self.assertEqual(len(offers),4)

    def test_highest_anonymous_quote_retains_original_group_context_without_fabricated_inn(self):
        offers=[self.manual(10,'123','same'),self.manual(50,'','same'),self.manual(20,'123','alias'),self.manual(40,'456','other'),self.manual(30,'789','third')]
        r=PositionResult(position=self.pos,offers=offers)
        prepare_final_results([r]);prepare_final_results([r])
        self.assertEqual([30,40,50],[o.price_with_vat for o in r.offers])
        self.assertEqual(r.offers[-1].requisites.inn,'')
        self.assertEqual(len(r.all_offers),5)
