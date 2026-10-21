import hashlib
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from .models import SpecPosition, PriceOffer, Requisites
from .source_quote import observe_captured_quote
from .search.catalogue import CatalogueStore, ProductIdentity
from .search.catalogue_images import CatalogueImageStore
from .search.card_batch import CardSnapshot
from .search.product_identity import rendered_identity
from .search.catalogue_producer import publish_snapshot, capture_and_publish, refresh_recorded
from .search.catalogue_projection import project_product

class CatalogueProducerTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.store=CatalogueStore(self.root/'catalogue.sqlite3')
        self.identity=ProductIdentity('model','ABC123','','Изделие')
        self.pos=SpecPosition(number=1,name='Изделие',type_mark='ABC123',unit='шт',qty=7)
        self.store.acquire_unknown(self.identity,['shop.test'],owner='test')
        self.images=CatalogueImageStore(self.root)
        self.url='https://shop.test/product/ABC123?id=7'

    def capture(self,price=100,text=None,stamp=None):
        stamp=stamp or time.time()
        text=text or f'Изделие ABC123\nАртикул: ABC123\nЦена {price} руб. с НДС\nза 1 шт\nВ наличии'
        proof=rendered_identity(self.url,self.url,'Изделие ABC123','ABC123','ABC123','Изделие','shop.test')
        facts=dict(url=self.url,requested_url=self.url,main_title='Изделие ABC123',page_article='ABC123',
            captured_at=stamp,text=text,identity_evidence=proof,screenshot_context='',context_capture_evidence={})
        evidence=observe_captured_quote(text,actual_url=self.url,observed_at=stamp,url=self.url,title='Изделие ABC123',
            article='ABC123',listed=price,normalized=price,sale_unit='шт',requested_unit='шт',data={})
        offer=PriceOffer(price_with_vat=price,listed_price_with_vat=price,url=self.url,price_unit='шт',
            source_quote_evidence=evidence,requisites=Requisites(name='Сбербанк',inn='7707083893'))
        return CardSnapshot.create(facts),offer

    def publish(self,**kwargs):
        snapshot,offer=self.capture(**kwargs)
        return publish_snapshot(self.store,self.identity,self.pos,snapshot,image_store=self.images,offer=offer)

    def test_publication_uses_original_raw_facts_and_dated_coverage(self):
        observed=self.publish()
        record=self.store.lookup(self.identity)
        self.assertEqual(record['heads'][0]['current']['raw_amount'],100)
        self.assertEqual(record['coverage']['shop.test']['status'],'found')
        self.assertEqual(record['heads'][0]['current']['url'],self.url)
        self.assertNotIn('requested_unit',record['heads'][0]['current'])
        self.assertEqual(observed.availability,'in_stock')
        self.assertEqual(project_product(self.store,self.identity,self.pos).offers[0].catalogue_observed_at,observed.observed_at)

    def test_no_explicit_vat_keeps_raw_price_with_tax_uncertainty(self):
        self.publish(text='Изделие ABC123\nАртикул: ABC123\n100 руб.\nза 1 шт')
        record=self.store.lookup(self.identity)
        self.assertEqual(record['heads'][0]['current']['vat'],'unknown')
        self.assertEqual(record['heads'][0]['current']['state'],'observed')
        offer=project_product(self.store,self.identity,self.pos).offers[0]
        self.assertEqual(offer.price_with_vat,100)
        self.assertEqual(offer.catalogue_vat_status,'unknown')
        self.assertIn('НДС не подтверждён',offer.catalogue_vat_notice)
        self.publish(text='Изделие ABC123\nАртикул: ABC123\n100 руб. без НДС\nза 1 шт')
        self.assertEqual(self.store.lookup(self.identity)['heads'][0]['current']['vat'],'excluded')
        self.assertEqual(project_product(self.store,self.identity,self.pos).offers,[])

    def test_actual_native_image_upload_and_rebased_locator(self):
        from PIL import Image
        from . import config
        snapshot,offer=self.capture();facts=snapshot.facts()
        path=self.root/'original.png';Image.new('RGB',(20,10),'white').save(path);data=path.read_bytes()
        context=dict(version=1,role='review_context',filename=path.name,sha256=hashlib.sha256(data).hexdigest(),url=self.url,
            captured_at=time.time(),width=20,height=10,document_width=20,document_height=10,truncated=False)
        facts.update(screenshot_context=str(path),context_capture_evidence=context)
        class Client:
            def put_object(self,**kwargs):
                self.data=kwargs['Body'];return {'ResponseMetadata':{'HTTPStatusCode':200}}
        client=Client();images=CatalogueImageStore(self.root,client=client,bucket='shots')
        observed=publish_snapshot(self.store,self.identity,self.pos,CardSnapshot.create(facts),image_store=images,offer=offer)
        self.assertEqual(client.data,data)
        self.assertNotEqual(observed.image.local_path,path.name)
        self.assertEqual(observed.image.context_evidence['sha256'],context['sha256'])
        self.assertEqual(observed.image.context_evidence['captured_at'],context['captured_at'])
        with patch.object(config,'CACHE_DIR',self.root):
            self.assertTrue(project_product(self.store,self.identity,self.pos).offers[0].screenshot_context)

    def test_cancellation_after_upload_cannot_publish_quote(self):
        from PIL import Image
        from .search.session import research_session
        from .search.failure_policy import ResearchControlError
        snapshot,offer=self.capture();facts=snapshot.facts()
        path=self.root/'cancel.png';Image.new('RGB',(20,10),'white').save(path);data=path.read_bytes()
        context=dict(version=1,role='review_context',filename=path.name,sha256=hashlib.sha256(data).hexdigest(),url=self.url,
            captured_at=time.time(),width=20,height=10,document_width=20,document_height=10,truncated=False)
        facts.update(screenshot_context=str(path),context_capture_evidence=context)
        cancelled=[False]
        class Client:
            def put_object(self,**kwargs):
                cancelled[0]=True
                return {'ResponseMetadata':{'HTTPStatusCode':200}}
        images=CatalogueImageStore(self.root,client=Client(),bucket='shots')
        with research_session(cancelled=lambda:cancelled[0]),self.assertRaises(ResearchControlError):
            publish_snapshot(self.store,self.identity,self.pos,CardSnapshot.create(facts),image_store=images,offer=offer)
        self.assertEqual(self.store.lookup(self.identity)['heads'],[])

    def test_capture_unknown_path_calls_every_recorded_card_once_and_no_discovery(self):
        self.publish(stamp=time.time()-10)
        calls=[]
        def capture(browser,pos,url,**kwargs):
            calls.append(url);return self.capture(price=90)
        with patch('engine.search.agent.research_position',side_effect=AssertionError('discovery')):
            result=refresh_recorded(self.store,browser=object(),image_store=self.images,capture=capture)
        self.assertEqual(calls,[self.url]);self.assertEqual(result[0]['outcome'],'observed')
        self.assertEqual(self.store.lookup(self.identity)['heads'][0]['current']['raw_amount'],90)

    def test_refresh_no_new_facts_preserves_lastgood_date(self):
        original=self.publish(stamp=time.time()-100)
        result=refresh_recorded(self.store,browser=object(),image_store=self.images,capture=lambda *a,**k:(None,None))
        self.assertEqual(result[0]['outcome'],'parse_error')
        record=self.store.lookup(self.identity)
        self.assertEqual(record['heads'][0]['current']['observed_at'],original.observed_at)
        self.assertEqual(record['refresh_attempts'][0]['reason'],'no_new_owned_facts')

    def test_actual_redirect_replaces_current_lower_price_not_new_duplicate_seller(self):
        original=self.publish(stamp=time.time()-100)
        actual=self.url.replace('id=7','id=redirected')
        def capture(browser,pos,url,**kwargs):
            snapshot,offer=self.capture(price=75)
            facts=snapshot.facts()
            facts.update(url=actual,requested_url=url,identity_evidence=rendered_identity(url,actual,'Изделие ABC123','ABC123','ABC123','Изделие','shop.test'))
            offer.url=actual;offer.source_quote_evidence['url']=actual
            return CardSnapshot.create(facts),offer
        result=refresh_recorded(self.store,browser=object(),image_store=self.images,capture=capture)
        self.assertEqual(result[0]['outcome'],'observed')
        record=self.store.lookup(self.identity)
        self.assertEqual(len(record['heads']),1)
        self.assertEqual(record['heads'][0]['url'],actual)
        self.assertEqual(project_product(self.store,self.identity,self.pos).offers[0].price_with_vat,75)
        self.assertEqual(record['refresh_attempts'][0]['url'],self.url)
        self.assertEqual(record['refresh_attempts'][0]['source_url'],actual)
        self.assertTrue(any(o['observation_id']==original.observation_id for o in self.store.lookup(self.identity,include_history=True)['history']))

    def test_account_provider_internal_controls_propagate_distinct_safe_reasons(self):
        from .llm import LLMAccountError,LLMUnavailable
        from .search.failure_policy import ResearchInternalFailure
        original=self.publish(stamp=time.time()-100)
        cases=((LLMAccountError(402),'account_error','llm_account_error'),
               (LLMUnavailable('SECRET_RESPONSE'),'unknown','llm_provider_unavailable'),
               (ResearchInternalFailure('pipeline',ValueError('SECRET_RESPONSE')),'unknown','research_internal_error'))
        for error,outcome,reason in cases:
            def capture(*a,**k):raise error
            with self.assertRaises(type(error)):
                refresh_recorded(self.store,browser=object(),image_store=self.images,capture=capture)
            attempt=self.store.lookup(self.identity)['refresh_attempts'][-1]
            self.assertEqual(attempt['outcome'],outcome)
            self.assertEqual(attempt['reason'],reason)
            self.assertNotIn('SECRET_RESPONSE',str(attempt))
        self.assertEqual(self.store.lookup(self.identity)['heads'][0]['current']['observed_at'],original.observed_at)

    def test_owned_neutral_request_price_supersedes_firm_without_old_image(self):
        original=self.publish(stamp=time.time()-100)
        coherent=dict(owner_bound=True,owner_truncated=False,main_title='Изделие ABC123',article='ABC123',
            owner_text='Изделие ABC123\nАртикул: ABC123\nЦена по запросу\nПод заказ',observed_at=time.time(),document_url=self.url)
        folder=self.root/'main_facts';folder.mkdir()
        path=folder/(uuid.uuid4().hex+'.json');data=json.dumps(dict(coherent,version=1,role='main_card_facts',requested_url=self.url.split('?')[0])).encode();path.write_bytes(data)
        record=dict(coherent=coherent,requested_url=self.url,source=dict(ref=str(path.relative_to(self.root)),sha256=hashlib.sha256(data).hexdigest(),observed_at=coherent['observed_at']),
            identity_observation=dict(main_title=coherent['main_title'],main_article=coherent['article'],canonical_url=self.url,observed_at=coherent['observed_at']))
        def capture(browser,pos,url,neutral_sink):
            neutral_sink(record);return None,None
        with patch('engine.search.agent.CACHE_DIR',self.root):
            observation=capture_and_publish(self.store,self.identity,self.pos,self.url,browser=object(),image_store=self.images,capture=capture)
        self.assertEqual(observation.state,'unpriced')
        self.assertEqual(observation.image.status,'missing')
        current=self.store.lookup(self.identity)['heads'][0]
        self.assertEqual(current['current']['raw_amount'],None)
        self.assertEqual(current['last_good']['observation_id'],original.observation_id)
        self.assertEqual(project_product(self.store,self.identity,self.pos).offers,[])
        # Exact same retained body hash cannot certify another query-ID card.
        record['coherent']=dict(coherent,document_url=self.url.replace('id=7','id=999'))
        with patch('engine.search.agent.CACHE_DIR',self.root),self.assertRaises(ValueError):
            capture_and_publish(self.store,self.identity,self.pos,self.url,browser=object(),image_store=self.images,capture=capture)
        # An actual new verified wrong owner supersedes the former firm head.
        wrong=dict(coherent,main_title='Изделие OTHER999',article='OTHER999',owner_text='Изделие OTHER999\nАртикул: OTHER999\n200 руб. с НДС\nза 1 шт',observed_at=time.time())
        data=json.dumps(dict(wrong,version=1,role='main_card_facts',requested_url=self.url.split('?')[0])).encode();path.write_bytes(data)
        record.update(coherent=wrong,source=dict(ref=str(path.relative_to(self.root)),sha256=hashlib.sha256(data).hexdigest(),observed_at=wrong['observed_at']),
            identity_observation=dict(main_title=wrong['main_title'],main_article=wrong['article'],canonical_url=self.url,observed_at=wrong['observed_at']))
        with patch('engine.search.agent.CACHE_DIR',self.root):
            conflict=capture_and_publish(self.store,self.identity,self.pos,self.url,browser=object(),image_store=self.images,capture=capture)
        self.assertEqual(conflict.state,'identity_conflict')
        self.assertEqual(project_product(self.store,self.identity,self.pos).offers,[])

    def test_tampered_capture_and_other_generation_are_refused(self):
        snapshot,offer=self.capture()
        from dataclasses import replace
        snapshot=replace(snapshot,sha256='0'*64)
        with self.assertRaises(ValueError):publish_snapshot(self.store,self.identity,self.pos,snapshot,image_store=self.images,offer=offer)

    def test_native_request_price_emits_owned_facts_but_never_offer_admission(self):
        from unittest.mock import MagicMock
        from .search import agent
        page=MagicMock();page.url=page._kac_document_url=self.url;page._kac_response_status=200
        page.inner_text.return_value='Изделие ABC123\nАртикул: ABC123\nЦена по запросу'
        page.locator.return_value.count.return_value=1
        page.locator.return_value.first.inner_text.return_value='Изделие ABC123'
        browser=MagicMock();browser.page.return_value.__enter__.return_value=page
        coherent=dict(kind='explicit_price_on_request',owner_bound=True,body=page.inner_text.return_value,
            owner_text=page.inner_text.return_value,main_title='Изделие ABC123',article='ABC123',document_title='',
            document_url=self.url,observed_at=time.time())
        records=[]
        with patch.object(agent,'_render_observation',return_value=({'observation_status':'observed'},{})), \
             patch.object(agent,'_wait_main_card_facts',return_value=(coherent,{})), \
             patch.object(agent,'_admit_card_text',side_effect=AssertionError('Price admission')):
            result=agent._capture_rendered_card(browser,self.url,self.url,'shop.test','ABC123','Изделие','шт',7,'',set(),time.monotonic()+10,neutral_sink=records.append)
        self.assertIsNone(result)
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]['coherent']['kind'],'explicit_price_on_request')
        from .search.card_attachments import attachment_capture
        records=[]
        with patch.object(agent,'_render_observation',return_value=({'observation_status':'observed'},{})), \
             patch.object(agent,'_wait_main_card_facts',return_value=(coherent,{})), \
             patch.object(agent,'CACHE_DIR',self.root), \
             patch('engine.review_context.capture_context',return_value=({},'TimeoutError')) as photo,attachment_capture():
            result=agent._capture_rendered_card(browser,self.url,self.url,'shop.test','ABC123','Изделие','шт',7,'',set(),time.monotonic()+10,neutral_sink=records.append)
        self.assertIsNone(result)
        self.assertEqual(photo.call_count,1)
        self.assertEqual(records[0]['context_capture_review_reason'],'TimeoutError')

    def test_native_success_reuses_early_owned_screenshot_once(self):
        from unittest.mock import MagicMock
        from .search import agent
        from .search.card_attachments import attachment_capture
        page=MagicMock();page.url=page._kac_document_url=self.url;page._kac_response_status=200
        text='Изделие ABC123\nАртикул: ABC123\n100 руб. с НДС\nза 1 шт'
        page.inner_text.return_value=text;page.locator.return_value.count.return_value=1
        page.locator.return_value.first.inner_text.return_value='Изделие ABC123'
        browser=MagicMock();browser.page.return_value.__enter__.return_value=page
        coherent=dict(kind='ready',owner_bound=True,body=text,owner_text=text,main_title='Изделие ABC123',
            article='ABC123',document_title='',document_url=self.url,observed_at=time.time(),missing_fields=[])
        records=[];evidence={'fixture':'native capture context'}
        proof=rendered_identity(self.url,self.url,'Изделие ABC123','ABC123','ABC123','Изделие','shop.test')
        with patch.object(agent,'_render_observation',return_value=({'observation_status':'observed'},{})), \
             patch.object(agent,'_wait_main_card_facts',return_value=(coherent,{})), \
             patch.object(agent,'_coherent_identity',return_value=proof), \
             patch.object(agent,'_admit_card_text',return_value={'roll_length':None,'page_article':'ABC123'}), \
             patch.object(agent,'CACHE_DIR',self.root), \
             patch('engine.review_context.capture_context',return_value=(evidence,'')) as photo,attachment_capture():
            snapshot=agent._capture_rendered_card(browser,self.url,self.url,'shop.test','ABC123','Изделие','шт',7,'',set(),time.monotonic()+10,neutral_sink=records.append)
        self.assertIsNotNone(snapshot)
        self.assertEqual(photo.call_count,1)
        self.assertEqual(snapshot.facts()['context_capture_evidence'],records[0]['context_capture_evidence'])
        snapshot,offer=self.capture();offer.source_quote_evidence['observed_at']-=1
        with self.assertRaises(ValueError):publish_snapshot(self.store,self.identity,self.pos,snapshot,image_store=self.images,offer=offer)


    def test_primary_purchase_stock_is_independent_of_other_store_availability(self):
        text='Изделие ABC123\nАртикул: ABC123\nВ наличии\nЦена 100 руб. с НДС\nза 1 шт\nДобавить в корзину\nЦена и наличие в магазинах\nНет в наличии'
        observation=self.publish(text=text)
        self.assertEqual(observation.availability,'in_stock')
        later='Изделие ABC123\nАртикул: ABC123\nНет в наличии\nЦена 100 руб. с НДС\nза 1 шт\nДобавить в корзину\nЦена и наличие в магазинах\nВ наличии'
        observation=self.publish(text=later)
        self.assertEqual(observation.availability,'out_of_stock')
        self.assertEqual(self.store.lookup(self.identity)['heads'][0]['current']['availability'],'out_of_stock')
        # Availability observation does not introduce a new financial hard block.
        self.assertEqual(project_product(self.store,self.identity,self.pos).offers[0].price_with_vat,100)

    def test_direct_priced_lot_compact_and_spaced_producer_store_projection(self):
        for label in ('за24м','за 24 м'):
            snapshot,offer=self.capture(price=120,text='Изделие ABC123\nАртикул: ABC123\n120 руб. '+label)
            facts=snapshot.facts()
            offer.price_unit='24 м';offer.price_with_vat=5
            offer.source_quote_evidence=observe_captured_quote(facts['text'],actual_url=self.url,url=self.url,
                observed_at=facts['captured_at'],title=facts['main_title'],article='ABC123',listed=120,normalized=5,
                sale_unit='24 м',requested_unit='м',data={})
            published=publish_snapshot(self.store,self.identity,self.pos,snapshot,image_store=self.images,offer=offer)
            self.assertEqual(published.lot_quantity,24)
            row=self.pos.model_copy(update={'unit':'м'})
            self.assertEqual(project_product(self.store,self.identity,row).offers[0].price_with_vat,5)
        snapshot,offer=self.capture(price=120,text='Изделие ABC123\nАртикул: ABC123\n120 руб.\nКратность заказа24м')
        offer.price_unit='24 м';offer.price_with_vat=5
        publish_snapshot(self.store,self.identity,self.pos,snapshot,image_store=self.images,offer=offer)
        self.assertEqual(project_product(self.store,self.identity,self.pos.model_copy(update={'unit':'м'})).offers,[])

    def test_same_epoch_actual_offer_cash_and_preview_publish_per_displayed_unit(self):
        from dataclasses import replace
        text='Изделие ABC123\nАртикул: ABC123\n100 руб.\nКратность заказа 3 м\n−+\n3 м\nна сумму 300 руб.\nДобавить в корзину'
        snapshot,offer=self.capture(text=text)
        observed=publish_snapshot(self.store,self.identity,self.pos,snapshot,image_store=self.images,offer=offer)
        self.assertEqual(observed.raw_amount,100)
        self.assertEqual(observed.sale_unit,'м')
        self.assertEqual(observed.lot_quantity,1)
        self.assertEqual(observed.unit_basis['selected_quantity'],3)
        self.assertEqual(observed.unit_basis['subtotal'],300)
        self.assertEqual(observed.source_text,text)
        row=self.pos.model_copy(update={'unit':'м'})
        self.assertEqual(project_product(self.store,self.identity,row).offers[0].price_with_vat,100)
        self.assertEqual(project_product(self.store,self.identity,self.pos).offers,[])
        restarted=CatalogueStore(self.store.path)
        self.assertEqual(restarted.lookup(self.identity)['heads'][0]['current']['unit_basis'],observed.unit_basis)
        with self.assertRaises(ValueError):
            replace(observed,unit_basis=dict(observed.unit_basis,price_quantity=3)).payload()
        with self.assertRaises(ValueError):replace(observed,sale_unit='шт').payload()
        with self.assertRaises(ValueError):replace(observed,lot_quantity=3).payload()
        # No final independently selected cash proof: preview cannot pick100 by300/3.
        later,_=self.capture(text=text)
        missing=publish_snapshot(self.store,self.identity,row,later,image_store=self.images)
        self.assertIsNone(missing.unit_basis)
        self.assertEqual(missing.state,'unpriced')
        self.assertEqual(missing.unit_basis_reason,'preview_selected_cash_missing')
        self.assertIn('Независимо выбранная цена',self.store.lookup(self.identity)['coverage']['shop.test']['reason'])

    def test_retained_actual_chipdip_empty_article_uses_owned_epoch_and_native_image(self):
        source=Path(__file__).resolve().parents[2]/'sources/kac-generator/catalogue-live-2026-10-13/initial-2/cache'
        raw=(source/'captured_inputs/8fa1b1625a974c2d922e20d53d0fa4bb.json').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'19ee824d5acfdebceb32c68f6b0fcdce66085a998ba8df77203742d8296cf2c3')
        record=json.loads(raw);facts=json.loads(record['payload'])
        self.assertEqual(hashlib.sha256(record['payload'].encode()).hexdigest(),record['sha256'])
        self.assertEqual(facts['page_article'],'')
        self.identity=ProductIdentity('article','3526210','DKC')
        self.pos=SpecPosition(number=23,name=facts['expected_name'],type_mark='3526210',unit='м',qty=1)
        self.store.acquire_unknown(self.identity,['chipdip.ru'],owner='offline-history')
        main=source/facts['main_facts']['ref'];target=self.root/facts['main_facts']['ref']
        target.parent.mkdir();target.write_bytes(main.read_bytes())
        image=source/facts['context_capture_evidence']['filename']
        (self.root/image.name).write_bytes(image.read_bytes())
        facts['screenshot_context']=image.name
        observed=publish_snapshot(self.store,self.identity,self.pos,CardSnapshot.create(facts),image_store=self.images)
        self.assertEqual(observed.manufacturer_code,'3526210')
        self.assertNotEqual(observed.state,'identity_conflict')
        self.assertEqual(observed.observed_at,facts['captured_at'])
        self.assertEqual(observed.image.status,'captured')
        self.assertEqual(observed.image.sha256,facts['context_capture_evidence']['sha256'])
        self.assertEqual(observed.image.captured_at,facts['context_capture_evidence']['captured_at'])
        from .search.catalogue_producer import CataloguePublicationError
        for patch_facts,code in (
            ({'page_article':'3526210ZL'},'catalogue_article_conflict'),
            ({'captured_at':facts['captured_at']+1},'catalogue_source_binding_invalid'),
            ({'identity_evidence':{}},'catalogue_owner_unconfirmed'),
            ({'main_facts':{}},'catalogue_source_binding_invalid')):
            with self.assertRaises(CataloguePublicationError) as caught:
                publish_snapshot(self.store,self.identity,self.pos,CardSnapshot.create(dict(facts,**patch_facts)),image_store=self.images)
            self.assertEqual(caught.exception.code,code)
        # Even matching bytes through an external directory are not owned source.
        with tempfile.TemporaryDirectory() as outside:
            external=Path(outside)
            external.joinpath(target.name).write_bytes(target.read_bytes())
            target.unlink();target.parent.rmdir();target.parent.symlink_to(external,target_is_directory=True)
            with self.assertRaises(CataloguePublicationError) as caught:
                publish_snapshot(self.store,self.identity,self.pos,CardSnapshot.create(facts),image_store=self.images)
            self.assertEqual(caught.exception.code,'catalogue_source_binding_invalid')
            from .search.catalogue_producer import _neutral_facts
            owned=json.loads(external.joinpath(target.name).read_text())
            record=dict(coherent=owned,source=facts['main_facts'])
            with self.assertRaises(CataloguePublicationError):
                _neutral_facts(record,facts['requested_url'],self.root)
            target.parent.unlink();target.parent.mkdir();target.write_bytes(main.read_bytes())
        mismatch=dict(facts,main_title=facts['main_title'].replace('3526210','3526210ZL'))
        mismatch['identity_evidence']=dict(facts['identity_evidence'],main_title=mismatch['main_title'],main_article='3526210ZL')
        mismatch['page_article']='3526210ZL'
        with self.assertRaises(CataloguePublicationError):
            publish_snapshot(self.store,self.identity,self.pos,CardSnapshot.create(mismatch),image_store=self.images)
