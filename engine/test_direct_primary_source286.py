"""Present direct references never downgrade to the bounded legacy locator."""
import copy,json,unittest
from dataclasses import asdict
from unittest.mock import patch
from engine.test_owned_length285 import OwnedLengthTests
from engine.search import http_card as dom
from engine.search.catalogue import validate_primary_source,_PrimarySourceAbsent,SourceObservation,ProductIdentity,ImageEvidence
from engine.source_quote import owned_product_length

class DirectPrimarySourceTests(unittest.TestCase):
 setUp=OwnedLengthTests.setUp
 tearDown=OwnedLengthTests.tearDown
 html=OwnedLengthTests.html
 store=OwnedLengthTests.store
 def test_direct_source_bypasses_saturated_locator(self):
  obs,source=self.store();obs['primary_source']=source
  with patch.object(dom,'_CHAR_MAX_FILES',0),patch.object(dom,'locate_primary_characteristics',side_effect=AssertionError('no fallback')) as locate:
   witness,reason=owned_product_length(obs);self.assertEqual(reason,'');self.assertEqual(witness['length']['metres'],3);locate.assert_not_called()
 def test_invalid_present_reference_never_downgrades(self):
  obs,source=self.store()
  bad=[None,{},[],{**source,'role':'http_main_product'},{**source,'ref':'../foreign.json'},{**source,'record_sha256':'0'*64},{**source,'body_sha256':'0'*64},{**source,'main_facts':{**source['main_facts'],'sha256':'0'*64}}]
  with patch.object(dom,'locate_primary_characteristics',side_effect=AssertionError('no fallback')) as locate:
   for value in bad:
    with self.subTest(value=value):self.assertIsNone(owned_product_length({**obs,'primary_source':value})[0])
   locate.assert_not_called()
 def test_direct_source_still_binds_observation(self):
  obs,source=self.store();obs['primary_source']=source
  with patch.object(dom,'locate_primary_characteristics',side_effect=AssertionError('no fallback')):
   for key,value in [('url','https://supplier.test/other'),('observed_at',self.stamp+1),('title','Other'),('manufacturer_code','OTHER99'),('source_text',obs['source_text']+' altered')]:
    other=copy.deepcopy(obs);other[key]=value
    with self.subTest(key=key):self.assertIsNone(owned_product_length(other)[0])
 def test_typed_reference_shape_and_absence_sentinel(self):
  obs,source=self.store();self.assertEqual(validate_primary_source(copy.deepcopy(source)),source)
  for value in [None,{},[],{**source,'foreign':1},{**source,'record_sha256':True},{**source,'main_facts':None}]:
   with self.subTest(value=value):self.assertRaises((ValueError,TypeError),validate_primary_source,value)
  self.assertIs(copy.deepcopy(_PrimarySourceAbsent.VALUE),_PrimarySourceAbsent.VALUE)
 def test_absent_reference_preserves_legacy_lookup(self):
  obs,source=self.store()
  with patch.object(dom,'locate_primary_characteristics',wraps=dom.locate_primary_characteristics) as locate:
   self.assertIsNotNone(owned_product_length(obs)[0]);self.assertEqual(locate.call_count,1)
 def test_observation_payload_distinguishes_absent_and_present_null(self):
  obs,source=self.store();oid='a'*32
  values={**obs,'observation_id':oid,'identity':ProductIdentity('article','ABC123','Maker'),'supplier_host':'supplier.test','requested_url':self.url,'issuer':'supplier_rendered','purchase_terms':'','availability':'unknown','seller_name':'','seller_inn':'','image':ImageEvidence(oid,self.url,self.stamp,'missing',error='native_capture_missing'),'validation_version':'catalogue-native-v1'}
  legacy=SourceObservation(**values);payload=legacy.payload();self.assertNotIn('primary_source',payload)
  self.assertIs(asdict(copy.deepcopy(legacy))['primary_source'],_PrimarySourceAbsent.VALUE)
  self.assertEqual(legacy.payload(),SourceObservation(**values).payload())
  with self.assertRaises(ValueError):SourceObservation(**{**values,'primary_source':None}).payload()
  direct=SourceObservation(**{**values,'primary_source':source}).payload();self.assertEqual(direct['primary_source'],source)
 def test_producer_skips_characteristics_when_conversion_not_needed(self):
  from engine.models import SpecPosition,PriceOffer,Requisites
  from engine.search.catalogue import CatalogueStore
  from engine.search.catalogue_producer import publish_facts
  from types import SimpleNamespace
  obs,source=self.store();identity=ProductIdentity('model','ABC123','','Лоток')
  store=CatalogueStore(self.root/'catalogue.sqlite3');store.acquire_unknown(identity,['supplier.test'],owner='offline286')
  facts=dict(url=self.url,requested_url=self.url,text=obs['source_text'],main_title=self.title,page_article='ABC123',captured_at=self.stamp,main_facts=source['main_facts'])
  for unit,cash in [('шт',True),('компл',True),('уп',True),('кг',True),('м',False)]:
   pos=SpecPosition(number=1,name='Лоток',type_mark='ABC123',unit=unit,qty=1)
   offer=PriceOffer(price_with_vat=100,listed_price_with_vat=100,url=self.url,price_unit='шт',source_quote_evidence=obs['unit_basis']['selected_cash'] if cash else {},requisites=Requisites(name='Сбербанк',inn='7707083893'))
   with self.subTest(unit=unit,cash=cash),patch('engine.search.catalogue_producer._primary_characteristics_reference',side_effect=AssertionError('unnecessary frame read')) as helper:
    published=publish_facts(store,identity,pos,facts,image_store=SimpleNamespace(root=self.root),offer=offer,verified_owner=True)
    self.assertNotIn('primary_source',published.payload());helper.assert_not_called()
