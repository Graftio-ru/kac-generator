"""Immutable storage integrity and current eligibility are separate authorities."""
import copy,hashlib,json,tempfile,time,unittest,uuid
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
from .models import SpecPosition
from . import config,test_purchase_characteristics335 as makers
from .search import http_card as dom,owned_purchase_row as purchase
from .search.catalogue import CatalogueStore,SourceObservation,ImageEvidence,identity_for_position,CatalogueStoreError
from .search.catalogue_projection import project_product,catalogue_scope,qualify_observation
from .search.catalogue_observation_storage import decode_stored_observation
from .offer_selection import offer_is_comparable

class StorageQualification346Tests(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name);self.cache=self.root/'cache';self.cache.mkdir();self.store=CatalogueStore(self.root/'catalogue.sqlite3');self.position=SpecPosition(number=1,name='Tray',type_mark='ABC123',unit='шт',qty=2);self.identity=identity_for_position(self.position);fixture_start=time.time()-10
  with patch('engine.search.catalogue.time.time',return_value=fixture_start):self.admission=self.store.acquire_unknown(self.identity,['supplier.example'],owner='fixture')
  for module in (config,dom):p=patch.object(module,'CACHE_DIR',self.cache);p.start();self.addCleanup(p.stop)
  maker=makers.PurchaseCharacteristicsTests();maker.cache=self.cache;proof=maker.make(title='Tray ABC123');source=dom.read_source(proof['source'],allow_rendered=True);self.proof=proof;self.text=source[1]['text']
  oid=uuid.uuid4().hex;url=proof['url'];stamp=proof['observed_at']
  self.observation=SourceObservation(oid,self.identity,'supplier.example',url,url,stamp,'supplier_rendered',proof['title'],'ABC123',self.text,hashlib.sha256(self.text.encode()).hexdigest(),100,'RUB','included','шт',1,None,'','in_stock','observed','Seller','7707083893',ImageEvidence(oid,url,stamp,'missing',error='not_captured'),'catalogue-v1',purchase_row=proof)
  self.store.append_observation(self.identity.key,self.observation,acquisition=self.admission,coverage_status='found')
 def row(self):
  with self.store._connection()as db:return dict(db.execute('SELECT * FROM observations WHERE id=?',(self.observation.observation_id,)).fetchone())
 def sibling(self):
  oid=uuid.uuid4().hex;url='https://supplier.example/product/sibling';stamp=time.time();text='Tray ABC123\nАртикул: ABC123\nЦена 200 руб. с НДС\nза 1 шт'
  o=SourceObservation(oid,self.identity,'supplier.example',url,url,stamp,'supplier_rendered','Tray ABC123','ABC123',text,hashlib.sha256(text.encode()).hexdigest(),200,'RUB','included','шт',1,None,'','in_stock','observed','Seller','7707083893',ImageEvidence(oid,url,stamp,'missing',error='not_captured'),'catalogue-v1')
  self.store.append_observation(self.identity.key,o,acquisition=self.admission,coverage_status='found');return o
 def test_current_policy_refusal_visible_and_valid_sibling_available(self):
  sibling=self.sibling();before=self.store.path.read_bytes()
  with patch.object(purchase,'verify',return_value=None),catalogue_scope(self.store):
   product=self.store.lookup(self.identity);self.assertEqual(len(product['heads']),2)
   result=project_product(self.store,self.identity,self.position);old=next(o for o in result.all_offers if o.url==self.observation.url);valid=next(o for o in result.all_offers if o.url==sibling.url)
   self.assertFalse(offer_is_comparable(old,self.position));self.assertTrue(old.unit_review_required);self.assertIn('покупки',old.unit_review_reason);self.assertTrue(offer_is_comparable(valid,self.position))
   q=qualify_observation(next(h['current']for h in product['heads']if h['url']==old.url),self.position);self.assertEqual(q.status,'inadmissible');self.assertEqual(q.code,'current_evidence_unproven')
  self.assertEqual(self.store.path.read_bytes(),before)
 def test_raw_fields_state_omission_and_no_semantic_decoder_calls(self):
  raw=json.loads(self.row()['payload']);raw['state']='observed';raw.pop('primary_source',None)
  with patch.object(purchase,'verify',side_effect=AssertionError('current semantic parser forbidden during decode')):
   decoded=decode_stored_observation(raw)
  self.assertEqual(decoded,raw);self.assertIsNot(decoded,raw);self.assertNotIn('primary_source',decoded)
 def test_decoded_payload_cannot_bypass_strict_writer(self):
  raw=decode_stored_observation(json.loads(self.row()['payload']));before=self.store.path.read_bytes()
  with patch.object(purchase,'verify',return_value=None),self.assertRaises(ValueError):
   self.store.append_observation(self.identity.key,self.observation,acquisition=self.admission,coverage_status='found')
  self.assertEqual(before,self.store.path.read_bytes());self.assertNotIn('storage_valid',raw)
 def test_internal_hash_epoch_price_identity_and_json_corruption_failclosed(self):
  original=self.row()
  for change in (lambda p:p.update(source_sha256='0'*64),lambda p:p['purchase_row'].update(observed_at=p['observed_at']+1),lambda p:p['purchase_row'].update(raw_amount=101),lambda p:p['purchase_row'].update(article='OTHER999'),lambda p:p['identity'].update(code='OTHER999')):
   raw=json.loads(original['payload']);change(raw)
   with self.subTest(change=change),self.assertRaises(CatalogueStoreError):self.store._observation(dict(original,payload=json.dumps(raw)))
  with self.assertRaises(CatalogueStoreError):self.store._observation(dict(original,payload='{invalid'))
 def test_unknown_bounded_version_visible_but_never_admissible(self):
  row=self.row();raw=json.loads(row['payload']);raw['purchase_row']['version']=99
  with self.store._connection(True)as db:db.execute('UPDATE observations SET payload=? WHERE id=?',(json.dumps(raw),row['id']))
  with catalogue_scope(self.store):
   result=project_product(self.store,self.identity,self.position);offer=result.all_offers[0];self.assertFalse(offer_is_comparable(offer,self.position));self.assertIn('stored_purchase_version_unsupported',offer.unit_review_reason)
  bad=copy.deepcopy(raw);bad['purchase_row']['READY']=True
  with self.assertRaises(ValueError):decode_stored_observation(bad)
 def test_cache_tamper_is_not_a_price_or_photo_waiver(self):
  p=self.cache/self.proof['source']['ref'];p.write_bytes(p.read_bytes()+b' ')
  with catalogue_scope(self.store):
   result=project_product(self.store,self.identity,self.position);self.assertFalse(offer_is_comparable(result.all_offers[0],self.position));self.assertTrue(result.all_offers[0].unit_review_required)
 def test_wrong_head_or_native_journal_bindings_failclosed(self):
  other=self.sibling()
  with self.store._connection(True)as db:db.execute('UPDATE heads SET current_id=? WHERE url=?',(other.observation_id,self.observation.url))
  self.assertRaises(CatalogueStoreError,self.store.lookup,self.identity)
 def test_invalid_native_journal_is_not_silently_dropped(self):
  self.store.card_operation(self.identity.key,'supplier.example',self.observation.url,operation_id='a'*32)
  with self.store._connection(True)as db:
   row=db.execute('SELECT id,payload FROM attempts').fetchone();value=json.loads(row['payload']);value['card_operation']['version']=True;db.execute('UPDATE attempts SET payload=? WHERE id=?',(json.dumps(value),row['id']))
  with self.assertRaisesRegex(CatalogueStoreError,'attempt journal'):self.store.lookup(self.identity)

if __name__=='__main__':unittest.main()
