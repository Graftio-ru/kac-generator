import hashlib
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from .models import SpecPosition
from .schemas import OfferIn, PositionIn
from .source_quote import quote_review_reason
from .offer_selection import offer_is_comparable
from .search.catalogue import CatalogueStore, ProductIdentity, SourceObservation, ImageEvidence
from .search.catalogue_projection import project_product, catalogue_scope, configure_store


class CatalogueProjectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = CatalogueStore(Path(self.directory.name)/'catalogue.sqlite3')
        self.identity = ProductIdentity('model', 'ABC123', '', 'Изделие')
        self.position = SpecPosition(number=1, name='Изделие', type_mark='ABC123', unit='шт', qty=10)
        self.store.acquire_unknown(self.identity, ['shop.test', 'other.test'], owner='test')
        self.addCleanup(configure_store, None)

    def observe(self, *, price=100, text=None, sale='шт', pack=None, lot=None, state='observed', stamp=None, host='shop.test', **changes):
        stamp = stamp or time.time()-10
        oid = uuid.uuid4().hex
        url = f'https://{host}/card?id=7'
        text = text or f'Изделие ABC123\nЦена {price} руб.\nза 1 шт'
        values = dict(observation_id=oid, identity=self.identity, supplier_host=host,
            requested_url=url, url=url, observed_at=stamp, issuer='supplier_rendered',
            title='Изделие ABC123', manufacturer_code='ABC123', source_text=text,
            source_sha256=hashlib.sha256(text.encode()).hexdigest(), raw_amount=price,
            currency='RUB', vat='included', sale_unit=sale, lot_quantity=lot,
            package_quantity=pack, purchase_terms='', availability='unknown', state=state,
            seller_name='Сбербанк', seller_inn='7707083893',
            image=ImageEvidence(oid, url, stamp, 'missing', error='fixture missing image'),
            validation_version='catalogue-v1')
        values.update(changes)
        observation = SourceObservation(**values)
        self.store.append_observation(self.identity.key, observation)
        return observation

    def project(self, pos=None):
        return project_product(self.store, self.identity, pos or self.position)

    def test_known_empty_and_absent_are_distinct_without_acquisition(self):
        with patch('engine.search.agent.research_position', side_effect=AssertionError('network')):
            self.assertEqual(self.project().all_offers, [])
            self.assertEqual(len(self.project().catalogue_coverage), 2)
            self.assertIsNone(project_product(self.store, ProductIdentity('article','OTHER','Maker'),self.position))

    def test_roundtrip_trusted_resolver_and_quantity_independence(self):
        observed = self.observe()
        result = self.project()
        self.assertEqual(len(result.offers), 1)
        offer = result.offers[0]
        self.assertEqual(offer.catalogue_observed_at, observed.observed_at)
        self.assertEqual(offer.source_quote_evidence, {})
        configure_store(self.store)
        self.assertTrue(offer_is_comparable(offer, self.position))
        pos = self.position.model_copy(update={'qty':150})
        self.assertEqual(self.project(pos).offers[0].price_with_vat, 100)
        dto = OfferIn(**dict(offer.model_dump(),org_name=offer.requisites.name,inn=offer.requisites.inn,screenshot_product='',screenshot_requisites=''))
        rebuilt = PositionIn(number=1,name='Изделие',type_mark='ABC123',unit='шт',qty=150,offers=[dto]).to_result()
        self.assertTrue(offer_is_comparable(rebuilt.offers[0],rebuilt.position))

    def test_reference_price_product_and_source_tampering_fail_closed(self):
        self.observe()
        offer = self.project().offers[0]
        self.assertTrue(quote_review_reason(offer, self.position)) # unconfigured server
        with catalogue_scope(self.store):
            for changes in ({'price_with_vat':999},{'listed_price_with_vat':999},
                            {'catalogue_ref':dict(product_id='wrong',observation_id=offer.catalogue_ref['observation_id'])},
                            {'catalogue_observed_at':time.time()},{'catalogue_source_sha256':'0'*64},
                            {'catalogue_ref':{}}, {'catalogue_image':{}},
                            {'catalogue_vat_status':'unknown'}, {'catalogue_vat_notice':'forged'},
                            {'url':'https://shop.test/card?id=8'}):
                self.assertTrue(quote_review_reason(offer.model_copy(update=changes),self.position),changes)
            self.assertTrue(quote_review_reason(offer,self.position.model_copy(update={'type_mark':'WRONG'})))

    def test_related_price_and_wrong_owner_do_not_become_current_quote(self):
        self.observe(text='Изделие ABC123\nЦена по запросу\nПохожие товары\n100 руб.\nза 1 шт')
        self.assertEqual(self.project().offers,[])
        self.observe(text='Изделие OTHER\nЦена 100 руб.\nза 1 шт',stamp=time.time())
        self.assertEqual(self.project().offers,[])

    def test_failure_keeps_dated_price_new_conditional_replaces_current(self):
        original = self.observe(stamp=time.time()-100)
        self.store.record_refresh_attempt(self.identity.key,'shop.test',original.url,
            attempted_at=time.time(),outcome='network_error',reason='timeout')
        self.assertEqual(self.project().offers[0].catalogue_observed_at,original.observed_at)
        with catalogue_scope(self.store):
            self.assertTrue(offer_is_comparable(self.project().offers[0],self.position))
        self.observe(price=90,text='Изделие ABC123\nот 90 руб.\nза 1 шт',state='review')
        self.assertEqual(self.project().offers,[])
        self.assertEqual(self.project().all_offers[0].listed_price_with_vat,90)

    def test_both_price_directions_and_old_ref_invalidated(self):
        self.observe(stamp=time.time()-30)
        old = self.project().offers[0]
        for price, age in ((80,20),(120,10)):
            self.observe(price=price,stamp=time.time()-age)
            self.assertEqual(self.project().offers[0].price_with_vat,price)
        with catalogue_scope(self.store):
            self.assertTrue(quote_review_reason(old,self.position))

    def test_conditional_roles_review_unknown_vat_preserves_raw_price(self):
        for wording in ('от 100 руб.', 'Ориентировочная цена 100 руб.', '100 руб.\nОкончательная цена по запросу'):
            self.observe(text='Изделие ABC123\n'+wording+'\nза 1 шт',stamp=time.time())
            self.assertEqual(self.project().offers,[],wording)
        self.observe(vat='unknown',stamp=time.time())
        offer=self.project().offers[0]
        self.assertEqual(offer.price_with_vat,100)
        self.assertEqual(offer.catalogue_vat_status,'unknown')
        self.assertIn('без налогового пересчёта',offer.catalogue_vat_notice)
        self.assertFalse(offer.unit_review_reason)
        self.assertFalse(offer.match_review_required)
        dto=OfferIn(**dict(offer.model_dump(),org_name=offer.requisites.name,inn=offer.requisites.inn,screenshot_product='',screenshot_requisites=''))
        rebuilt=PositionIn(number=1,name='Изделие',type_mark='ABC123',unit='шт',qty=10,offers=[dto]).to_result()
        self.assertEqual(rebuilt.offers[0].catalogue_vat_status,'unknown')
        self.assertEqual(rebuilt.offers[0].catalogue_vat_notice,offer.catalogue_vat_notice)
        self.observe(vat='excluded',stamp=time.time())
        self.assertEqual(self.project().offers,[])

    def test_explicit_lot_and_package_conversion_ambiguous_units_review(self):
        self.observe(price=1000,sale='10 шт',lot=10,text='Изделие ABC123\nЦена 1000 руб.\nза 10 шт')
        self.assertEqual(self.project().offers[0].price_with_vat,100)
        self.observe(price=500,sale='уп',pack=5,text='Изделие ABC123\nЦена 500 руб.\nза упаковку\nВ 1 упаковке 5 шт',stamp=time.time())
        self.assertEqual(self.project().offers[0].price_with_vat,100)
        self.assertEqual(self.project(self.position.model_copy(update={'unit':'м'})).offers,[])

    def test_restart_and_independent_seller_rule(self):
        self.observe()
        self.observe(host='other.test')
        self.store = CatalogueStore(self.store.path)
        self.assertEqual(self.project().sources_found,1)
        self.assertEqual(len(self.project().all_offers),2)

    def test_server_snapshot_survives_concurrent_refresh_without_restamping(self):
        observed = self.observe(stamp=time.time()-30)
        frozen = self.store.read_snapshot([self.identity])
        record = frozen['products'][self.identity.key]
        self.observe(price=200,stamp=time.time()-10)
        projected = project_product(self.store,self.identity,self.position,snapshot=record)
        self.assertEqual(projected.offers[0].price_with_vat,100)
        self.assertEqual(projected.offers[0].catalogue_observed_at,observed.observed_at)
        with catalogue_scope(self.store,snapshot=frozen):
            self.assertTrue(offer_is_comparable(projected.offers[0],self.position))
        with catalogue_scope(self.store):
            self.assertFalse(offer_is_comparable(projected.offers[0],self.position))

    def test_native_combined_model_article_uses_bound_identity(self):
        self.identity=ProductIdentity('article','ABC123','Maker')
        self.store.acquire_unknown(self.identity,['shop.test'],owner='native')
        self.observe(title='Изделие Maker MODEL1 ABC123',text='Изделие Maker MODEL1 ABC123\nЦена 100 руб.\nза 1 шт')
        native=self.position.model_copy(update={'type_mark':'MODEL1 / ABC123','source_evidence':{
            'reader':'native_pdf','cells':{'name':'Изделие','model':'MODEL1','article':'ABC123','maker':'Maker'}}})
        result=self.project(native)
        self.assertEqual(len(result.offers),1)
        self.observe(text='Изделие MODEL1 ABC123\nЦена 100 руб.\nза 1 шт',title='Изделие MODEL1 ABC123',stamp=time.time())
        self.assertEqual(self.project(native).offers,[])
        self.observe(text='Изделие MODEL1 ABC123\nПроизводитель: Maker\nЦена 100 руб.\nза 1 шт',title='Изделие MODEL1 ABC123',stamp=time.time())
        self.assertEqual(len(self.project(native).offers),1)
        self.observe(text='Изделие Maker MODEL1 ABC123\nПроизводитель: OTHER\nЦена 100 руб.\nза 1 шт',title='Изделие Maker MODEL1 ABC123',stamp=time.time())
        self.assertEqual(self.project(native).offers,[])

    def test_verified_maker_alias_preserves_article_and_contradiction_guards(self):
        self.identity=ProductIdentity('article','ABC123','DKC')
        self.store.acquire_unknown(self.identity,['shop.test'],owner='alias')
        pos=self.position.model_copy(update={'source_evidence':{
            'reader':'native_pdf','cells':{'name':'Изделие','model':'','article':'ABC123','maker':'DKC'}}})
        self.observe(title='Изделие DKC ABC123',text='Изделие DKC ABC123\nБренд\nДКС\nЦена 100 руб.\nза 1 шт')
        self.assertEqual(self.project(pos).offers[0].price_with_vat,100)
        self.observe(title='Изделие DKC ABC123',text='Изделие DKC ABC123\nБренд\nIEK\nЦена 100 руб.\nза 1 шт',stamp=time.time())
        self.assertEqual(self.project(pos).offers,[])
        self.observe(title='Изделие ДКС ABC123ZL',manufacturer_code='ABC123ZL',text='Изделие ДКС ABC123ZL\nБренд\nДКС\nЦена 100 руб.\nза 1 шт',stamp=time.time())
        self.assertEqual(self.project(pos).offers,[])

    def test_snapshot_image_is_verified_local_bytes_without_supplier(self):
        from PIL import Image
        from . import config
        root=Path(self.directory.name)
        path=root/'snapshot.png'
        Image.new('RGB',(10,10),'white').save(path)
        data=path.read_bytes();stamp=time.time()-10;captured=stamp+1;oid=uuid.uuid4().hex
        url='https://shop.test/card?id=7'
        context=dict(version=1,role='review_context',filename=path.name,sha256=hashlib.sha256(data).hexdigest(),
            url=url,captured_at=captured,width=10,height=10,document_width=10,document_height=10,truncated=False)
        image=ImageEvidence(oid,url,stamp,'captured',sha256=context['sha256'],local_path=path.name,
            size=len(data),captured_at=captured,context_evidence=context)
        self.observe(observation_id=oid,stamp=stamp,image=image)
        with patch.object(config,'CACHE_DIR',root):
            offer=self.project().offers[0]
            self.assertEqual(offer.screenshot_context,str(path.resolve()))
            self.assertEqual(offer.context_capture_evidence,context)
            self.assertEqual(offer.screenshot_product,str(path.resolve()))
            with catalogue_scope(self.store):
                changed=offer.model_copy(update={'context_capture_evidence':dict(context,sha256='0'*64)})
                self.assertFalse(offer_is_comparable(changed,self.position))
                for reference in ('/tmp/arbitrary/snapshot.png','subdir/snapshot.png','../snapshot.png'):
                    changed=offer.model_copy(update={'screenshot_context':reference})
                    self.assertFalse(offer_is_comparable(changed,self.position),reference)
                    changed=offer.model_copy(update={'screenshot_context':'','screenshot_product':reference})
                    self.assertFalse(offer_is_comparable(changed,self.position),reference)
                self.assertTrue(offer_is_comparable(offer.model_copy(update={'screenshot_context':path.name}),self.position))
            path.write_bytes(b'corrupt')
            broken=self.project().offers[0]
            self.assertEqual(broken.screenshot_context,'')
            self.assertTrue(broken.context_capture_review_reason)
            self.assertEqual(broken.price_with_vat,100)

if __name__ == '__main__':
    unittest.main()
