"""Real local API, immutable source history and durable estimator authority."""
import copy,hashlib,json,tempfile,time,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from . import app,config
from .models import SpecPosition
from .schemas import PositionIn,ResearchReportRequest
from .search.catalogue import CatalogueStore,identity_for_position,SourceObservation,ImageEvidence
from .search.catalogue_projection import catalogue_scope,project_product,configure_store
from .search import estimator_approval as approval
from .requisites.supplier import seller_source,make_seller_evidence

class EstimatorApproval375Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.cache=self.root/'cache';self.cache.mkdir()
  self.addCleanup(patch.stopall);patch.object(config,'CACHE_DIR',self.cache).start();patch.object(config,'BASE_DIR',self.root).start()
  self.position=PositionIn(number=7,name='Изделие',type_mark='ABC123',unit='шт',qty=14);self.pos=self.position.to_result().position;self.identity=identity_for_position(self.pos);self.store=CatalogueStore(self.root/'catalogue.sqlite3');self.store.acquire_unknown(self.identity,['one.test','two.test','three.test'],owner='fixture');self.refs=[]
  for i in range(3):self.observation(i)
  configure_store(self.store);self.addCleanup(configure_store,None);patch.object(app,'_catalogue_store',lambda:self.store).start();self.client=TestClient(app.app)
  from .search import base_db
  patch.object(base_db,'BASE_FILE',self.root/'index.json').start()
  self.before=self.store.lookup(self.identity);self.ref=self.refs[0]
 def observation(self,i,price=None,sale=None,photo=True,seller=True):
  from PIL import Image
  inn=['7707083893','7701677700','7842224734'][i];host=['one.test','two.test','three.test'][i];url='https://'+host+'/card';stamp=time.time()-1;oid=uuid.uuid4().hex;raw=price or 100+i;unit=sale or('уп'if i==0 else'шт');text=f'Изделие ABC123\nЦена {raw} руб.\nза 1 {unit}'
  path=self.cache/(oid+'.png');Image.new('RGB',(100,100),'white').save(path);body=path.read_bytes();sha=hashlib.sha256(body).hexdigest();ctx=dict(version=1,role='review_context',filename=path.name,sha256=sha,url=url,captured_at=stamp,width=100,height=100,document_width=100,document_height=100,truncated=False)
  source=seller_source('approved_supplier_reference','seller','Seller '+str(i),inn,source_url=url,source_sha256='a'*64,source_observed_at=stamp,role_text='Продавец',binding=dict(product_url=url,captured_at=stamp,source_ref='approved-supplier-list',row_sha256='a'*64))
  image=ImageEvidence(oid,url,stamp,'captured',sha256=sha,local_path=path.name,size=len(body),captured_at=stamp,context_evidence=ctx)if photo else ImageEvidence(oid,url,stamp,'missing',error='not_available')
  obs=SourceObservation(oid,self.identity,host,url,url,stamp,'supplier_rendered','Изделие ABC123','ABC123',text,hashlib.sha256(text.encode()).hexdigest(),raw,'RUB','included',unit,None,None,'','unknown','observed','Seller '+str(i)if seller else'',inn if seller else'',image,'catalogue-v1',seller_evidence=make_seller_evidence(url,stamp,[source])if seller else None)
  self.store.append_observation(self.identity.key,obs);ref={'product_id':self.identity.key,'observation_id':oid};self.refs.append(ref);return ref
 def request(self):return {'position':self.position.model_dump(mode='json'),'catalogue_ref':self.ref}
 def confirm(self,factor=2):
  req=self.request();r=self.client.post('/candidate-approvals/review',json=req);self.assertEqual(r.status_code,200,r.text);c=r.json()['candidate'];req.update(expected_material_sha256=c['material_sha256'],confirmed_product_title=c['product']['title'],confirmed_article=c['product']['article'],confirmed_raw_amount=c['raw_amount'],confirmed_sale_unit=c['sale_unit'],requested_unit='шт',units_per_sale_unit=factor,reason='Сметчик проверил упаковку и текущую цену');r=self.client.post('/candidate-approvals/confirm',json=req);self.assertEqual(r.status_code,200,r.text);return r.json()
 def project(self):
  with catalogue_scope(self.store):return project_product(self.store,self.identity,self.pos)
 def test_native_api_confirm_cold_reload_selection_report_and_save_roundtrip(self):
  from .offer_selection import offer_is_comparable
  from .final_eligibility import prepare_final_results
  self.assertEqual(self.project().sources_found,2)
  value=self.confirm();self.assertEqual(value['offer']['price_with_vat'],50);self.assertFalse(value['offer']['unit_review_required']);self.assertTrue(value['approval']['original_machine_reasons'])
  self.store=CatalogueStore(self.store.path);configure_store(self.store)
  loaded=self.client.post('/base/fill',json={'positions':[self.position.model_dump(mode='json')]});self.assertEqual(loaded.status_code,200,loaded.text);row=loaded.json()['results'][0];self.assertEqual(len(row['offers']),3)
  saved=PositionIn.model_validate({**self.position.model_dump(),'offers':row['offers']});restored=PositionIn.model_validate_json(saved.model_dump_json());self.assertTrue(restored.offers[0].estimator_approval_ref)
  saved_reply=self.client.post('/base/save',json={'positions':[saved.model_dump(mode='json')]});self.assertEqual(saved_reply.status_code,200,saved_reply.text);self.assertEqual(saved_reply.json()['saved'],1)
  result=restored.to_result()
  with catalogue_scope(self.store):
   self.assertTrue(all(offer_is_comparable(o,self.pos)for o in result.offers));prepare_final_results([result])
  report=self.client.post('/research/report',json={'positions':[saved.model_dump(mode='json')]});self.assertEqual(report.status_code,200,report.text[:100]if report.headers.get('content-type','').startswith('application/json')else'');self.assertGreater(len(report.content),1000)
  after=self.store.lookup(self.identity);self.assertEqual(after['heads'],self.before['heads']);self.assertEqual(after['refresh_attempts'],self.before['refresh_attempts'])
 def test_revoke_and_forged_boolean_or_reference_cannot_qualify(self):
  from .offer_selection import offer_is_comparable
  value=self.confirm();offer=PositionIn.model_validate({**self.position.model_dump(),'offers':[value['offer']]}).to_result().offers[0]
  r=self.client.post('/candidate-approvals/revoke',json={'approval_id':value['approval']['approval_id'],'expected_revision':1,'reason':'Отозвано сметчиком'});self.assertEqual(r.status_code,200)
  with catalogue_scope(self.store):self.assertFalse(offer_is_comparable(offer,self.pos))
  self.assertEqual(self.project().sources_found,2)
  fake=offer.model_copy(update={'unit_review_required':False,'model_review_accepted':True,'estimator_approval_ref':{'approval_id':'f'*32,'revision':1}})
  with catalogue_scope(self.store):self.assertFalse(offer_is_comparable(fake,self.pos))
 def test_changed_material_price_unit_product_or_conversion_reject(self):
  value=self.confirm();self.observation(0,price=200);self.assertIsNone(approval.active_approval(self.store,self.store.resolve_observation(**{'product_id':self.identity.key,'observation_id':self.refs[-1]['observation_id']})['observation'],self.pos))
  req=self.request();r=self.client.post('/candidate-approvals/review',json=req);self.assertEqual(r.status_code,409)
  current=self.project().all_offers[0];tampered=current.model_copy(update={'estimator_approval_ref':value['estimator_approval_ref'],'estimator_approval':value['approval'],'price_with_vat':50})
  from .offer_selection import offer_is_comparable
  with catalogue_scope(self.store):self.assertFalse(offer_is_comparable(tampered,self.pos))
 def test_invalid_numbers_stale_review_and_other_product_deny(self):
  req=self.request();c=self.client.post('/candidate-approvals/review',json=req).json()['candidate'];base={**req,'expected_material_sha256':c['material_sha256'],'confirmed_product_title':c['product']['title'],'confirmed_article':c['product']['article'],'confirmed_raw_amount':100,'confirmed_sale_unit':'уп','requested_unit':'шт','units_per_sale_unit':2,'reason':'Проверено'}
  for change in [{'units_per_sale_unit':True},{'units_per_sale_unit':0},{'units_per_sale_unit':1e-320},{'confirmed_raw_amount':101},{'expected_material_sha256':'0'*64},{'confirmed_article':'OTHER'}]:self.assertNotEqual(self.client.post('/candidate-approvals/confirm',json={**base,**change}).status_code,200)
 def test_missing_photo_or_seller_independent_guards_remain(self):
  self.confirm();self.observation(0,photo=False)
  from .search.selected_proofs import proof_ready
  with catalogue_scope(self.store):
   result=self.project();offer=next(o for o in result.all_offers if 'one.test'in o.url);self.assertTrue(offer.estimator_approval_ref);self.assertFalse(proof_ready(offer,self.pos))
 def test_more_than512_unrelated_records_do_not_hide_approval(self):
  value=self.confirm()
  with self.store._connection(True)as db:
   for i in range(520):
    v={**value['approval'],'approval_id':uuid.uuid4().hex,'material_sha256':'f'*64,'material':{'unrelated':i}};db.execute('INSERT INTO estimator_approval_events VALUES(?,?,?,?)',(v['approval_id'],1,approval._json(v),approval._hash(v)))
  self.assertEqual(self.project().sources_found,3)

 def test_current_policy_pins_new_authority_exact_prior_history_readonly(self):
  from .test_factory_policy_closure353 import FactoryPolicyClosure353Tests
  from .search import acquisition_source_intent as intent
  from .search.document_context import current_document_policy
  fixture=FactoryPolicyClosure353Tests();fixture.setUp()
  try:
   old=fixture.historical(intent.PRE_ESTIMATOR_SOURCE_FILES);self.assertEqual(intent.validate(old),old)
   with self.assertRaises(intent.IntentInvalid):intent.attest(fixture.runtime,old['components'])
   before=intent.attest(fixture.runtime,intent.components(fixture.root));policy=current_document_policy(engine_root=fixture.root,supplier_file=fixture.suppliers);path=fixture.root/'search/estimator_approval.py';self.assertIn(str(path),policy);path.write_bytes(path.read_bytes()+b'\n# inert policy mutation\n');self.assertNotEqual(before['sha256'],intent.attest(fixture.runtime,intent.components(fixture.root))['sha256']);self.assertNotEqual(policy,current_document_policy(engine_root=fixture.root,supplier_file=fixture.suppliers));fixture.test_policy_upgrade_never_reopens_old_pending_supplier_url()
  finally:fixture.doCleanups()

 def test_conversion_update_has_new_revision_and_revoke_never_resurrects(self):
  first=self.confirm(2);second=self.confirm(4);self.assertEqual(first['approval']['approval_id'],second['approval']['approval_id']);self.assertEqual(second['approval']['revision'],2)
  from .offer_selection import offer_is_comparable
  bad=PositionIn.model_validate({**self.position.model_dump(),'offers':[first['offer']]}).to_result().offers[0]
  with catalogue_scope(self.store):self.assertFalse(offer_is_comparable(bad,self.pos))
  self.assertEqual(self.client.post('/candidate-approvals/revoke',json={'approval_id':second['approval']['approval_id'],'expected_revision':2,'reason':'Отмена'}).status_code,200);self.assertEqual(self.project().sources_found,2)
 def test_boolean_revision_and_independent_seller_failure_are_not_accepted(self):
  value=self.confirm();off=PositionIn.model_validate({**self.position.model_dump(),'offers':[value['offer']]}).to_result().offers[0];off.estimator_approval_ref['revision']=True
  from .offer_selection import offer_is_comparable
  with catalogue_scope(self.store):self.assertFalse(offer_is_comparable(off,self.pos))
  self.ref=self.observation(0,seller=False);confirmed=self.confirm();projected=PositionIn.model_validate({**self.position.model_dump(),'offers':[confirmed['offer']]}).to_result().offers[0]
  with catalogue_scope(self.store):self.assertFalse(offer_is_comparable(projected,self.pos))

 def test_position_material_and_normalized_price_tamper_refuse_but_order_is_not_identity(self):
  value=self.confirm();from .offer_selection import offer_is_comparable
  offer=PositionIn.model_validate({**self.position.model_dump(),'offers':[value['offer']]}).to_result().offers[0]
  with catalogue_scope(self.store):
   self.assertFalse(offer_is_comparable(offer.model_copy(update={'price_with_vat':51}),self.pos))
   self.assertTrue(offer_is_comparable(offer,self.pos.model_copy(update={'number':88,'qty':99})))
   for change in [{'unit':'м'},{'name':'Другой товар'},{'type_mark':'OTHER999'}]:self.assertFalse(offer_is_comparable(offer,self.pos.model_copy(update=change)))
 def test_stored_receipt_tamper_fails_closed(self):
  value=self.confirm()
  with self.store._connection(True)as db:
   row=db.execute('SELECT * FROM estimator_approval_events').fetchone();v=json.loads(row['payload']);v['normalized_price']=1;db.execute('UPDATE estimator_approval_events SET payload=? WHERE id=?',(approval._json(v),v['approval_id']))
  with self.assertRaises(approval.ApprovalInvalid):self.project()

 def _with_price_disclosure(self,text):
  from . import test_estimator_approval375 as fixturemodule
  original=fixturemodule.SourceObservation
  def changed(*args,**kwargs):
   args=list(args);args[9]+='\n'+text;args[10]=hashlib.sha256(args[9].encode()).hexdigest();return original(*args,**kwargs)
  with patch.object(fixturemodule,'SourceObservation',changed):return self.observation(0)
 def _confirmation_response(self):
  req=self.request();review=self.client.post('/candidate-approvals/review',json=req);self.assertEqual(review.status_code,200,review.text);c=review.json()['candidate'];req.update(expected_material_sha256=c['material_sha256'],confirmed_product_title=c['product']['title'],confirmed_article=c['product']['article'],confirmed_raw_amount=c['raw_amount'],confirmed_sale_unit=c['sale_unit'],requested_unit='шт',units_per_sale_unit=2,reason='Проверено сметчиком');return self.client.post('/candidate-approvals/confirm',json=req)
 def test_conditional_club_starting_and_old_price_cannot_be_confirmed(self):
  from . import test_estimator_approval375 as fixturemodule
  for disclosure in ['Цена действительна только при покупке от 100 упаковок','Клубная цена 100 руб.','Старая цена 100 руб.','Цена от 100 руб.','Цена по карте 100 руб.']:
   with self.subTest(disclosure=disclosure):self.ref=self._with_price_disclosure(disclosure);self.assertNotEqual(self._confirmation_response().status_code,200)
 def test_approved_reuse_rechecks_source_terms_even_with_empty_structured_terms(self):
  self.confirm();self.ref=self._with_price_disclosure('Цена действительна только при покупке от 100 упаковок')
  self.assertEqual(self.project().sources_found,2);self.assertFalse(any(o.estimator_approval_ref for o in self.project().all_offers if 'one.test'in o.url))
 def test_firm_same_unit_price_can_still_be_human_confirmed(self):
  self.ref=self.observation(0,sale='шт');value=self.confirm(1);self.assertEqual(value['offer']['price_with_vat'],100);self.assertEqual(self.project().sources_found,3)
