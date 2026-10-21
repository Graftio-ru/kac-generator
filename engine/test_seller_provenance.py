"""Common source-bound seller rules. No photographs, remote calls or jobs."""
import copy,hashlib,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from . import config
from .models import Requisites,PriceOffer,SpecPosition
from .schemas import OfferIn,PositionIn
from .requisites import supplier as policy
from .search import whitelist,http_card
from .search.catalogue import CatalogueStore,ProductIdentity,SourceObservation,ImageEvidence
from .search.catalogue_projection import project_product,catalogue_scope,catalogue_review_reason
from .offer_selection import seller_review_reason,offer_is_comparable

class SellerProvenanceTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  for module in [config,http_card]:
   p=patch.object(module,'CACHE_DIR',self.root);p.start();self.addCleanup(p.stop)
  self.reference_file=self.root/'suppliers.md';self.reference_file.write_text('')
  p=patch.object(whitelist,'SUPPLIERS_FILE',self.reference_file);p.start();self.addCleanup(p.stop)
  self.now=time.time();self.url='https://supplier.example/card';self.hash='a'*64
 def source(self,inn='7610131367',origin='approved_supplier_reference',role='seller'):
  extras={'source_ref':'approved-supplier-list','row_sha256':self.hash} if origin=='approved_supplier_reference' else {'record_sha256':self.hash,'body_sha256':self.hash,'scope_sha256':self.hash} if origin=='scoped_offer' else {}
  return policy.seller_source(origin,role,'ООО Seller',inn,source_url=self.url,source_sha256=self.hash,source_observed_at=self.now,role_text='Продавец ООО Seller',binding=dict(product_url=self.url,captured_at=self.now,**extras))
 def evidence(self,*sources):return policy.make_seller_evidence(self.url,self.now,list(sources))
 def test_common_decisions_and_raw_identity_aliases(self):
  one=self.source();same=self.source('7610 131367','scoped_offer');raw=copy.deepcopy([one,same]);e=self.evidence(one,same)
  self.assertEqual(e['decision']['status'],'role_confirmed');self.assertEqual([one,same],raw)
  self.assertEqual(self.evidence(one)['decision']['status'],'reference_accepted')
  conflict=self.evidence(one,self.source('7701677700','scoped_offer'));self.assertEqual(conflict['decision']['status'],'conflict');self.assertTrue(policy.seller_evidence_reason(conflict))
  for origin,role in [('identity_enrichment','seller'),('identity_enrichment','unknown'),('scoped_offer','manufacturer'),('scoped_offer','marketplace_operator')]:
   self.assertEqual(self.evidence(self.source(origin=origin,role=role))['decision']['status'],'unknown')
  for inn in ['', '123','0000000000']:self.assertEqual(self.evidence(self.source(inn))['decision']['status'],'unknown')
 def test_strict_shape_bounds_and_tamper(self):
  e=self.evidence(self.source())
  mutations=[{'version':'future'},{'product_url':'https://foreign.test/card'},{'captured_at':True},{'sources':e['sources']*9},{'decision':{'status':'role_confirmed'}},{'extra':1}]
  for change in mutations:
   bad=copy.deepcopy(e);bad.update(change)
   with self.subTest(change=change),self.assertRaises((ValueError,TypeError)):policy.validate_seller_evidence(bad)
  for key,value in [('raw_inn','7701677700'),('source_sha256','b'*64),('role','manufacturer'),('role_text','x'*17000)]:
   bad=copy.deepcopy(e);bad['sources'][0][key]=value
   self.assertTrue(policy.seller_evidence_reason(bad),key)
  with self.assertRaises(ValueError):policy.validate_seller_evidence(e,product_url='https://foreign.test/card')
  with self.assertRaises(ValueError):policy.validate_seller_evidence(e,captured_at=self.now+1)
 def test_approved_reference_capture_snapshot_is_immutable(self):
  from .search.card_batch import CardSnapshot
  path=self.reference_file;path.write_text('| ООО Seller | 123 | 7610131367 | https://supplier.example |\n')
  facts=CardSnapshot.create({'url':self.url,'captured_at':self.now}).facts()
  e=policy.captured_seller_evidence(facts)
  self.assertEqual(e['decision']['status'],'reference_accepted');before=copy.deepcopy(e)
  path.write_text('removed')
  new=CardSnapshot.create({'url':self.url,'captured_at':self.now+1}).facts()
  self.assertEqual(policy.captured_seller_evidence(new)['decision']['status'],'unknown')
  self.assertEqual(policy.captured_seller_evidence(facts),before)
  repeated=CardSnapshot.create({'url':self.url,'captured_at':self.now}).facts()
  self.assertEqual(policy.captured_seller_evidence(repeated),before)
  self.assertEqual(policy.captured_seller_evidence({'url':self.url,'captured_at':self.now}),before)
  self.assertEqual(policy.captured_seller_evidence({'url':self.url,'captured_at':self.now-1})['decision']['status'],'unknown')
  self.assertEqual(policy.validate_seller_evidence(e),before)
 def test_retained_http_offer_is_rederived_not_caller_claim(self):
  import httpx
  from .test_http_card import html,URL
  record,reference=http_card.save_response(httpx.Response(200,text=html(),headers={'content-type':'text/html'},request=httpx.Request('GET',URL)),URL)
  facts=dict(url=URL,captured_at=record['observed_at'],http_source=reference,merchant_requisites={'name':'forged','inn':'7610131367'})
  from .search.card_batch import CardSnapshot
  facts=CardSnapshot.create(facts).facts()
  with patch.object(whitelist,'_BY_HOST',{}):e=policy.captured_seller_evidence(facts)
  self.assertEqual(e['decision']['status'],'role_confirmed');self.assertEqual(e['sources'][0]['raw_inn'],'7842224734')
  for change in [{'url':'https://foreign.test/card'},{'captured_at':record['observed_at']+1}]:
   with self.assertRaises(ValueError):policy.captured_seller_evidence({**facts,**change})
 def test_terms_explicit_product_context_and_cache_binding(self):
  body=f'Продавец товара {self.url}: ООО «Seller», ИНН 7610131367'
  self.assertIsNone(policy.retain_contextual_seller_terms('Производитель ООО Seller ИНН 7610131367',self.url,self.url))
  self.assertIsNone(policy.retain_contextual_seller_terms('Продавец: ООО Seller, ИНН 7610131367',self.url,self.url))
  self.assertIsNone(policy.retain_contextual_seller_terms(body,'https://foreign.test/terms',self.url))
  ref=policy.retain_contextual_seller_terms(body,'https://supplier.example/terms',self.url,capture_context=dict(product_url=self.url,captured_at=self.now));source=policy.read_contextual_seller_terms(ref,self.url,self.now)
  self.assertEqual(self.evidence(source)['decision']['status'],'role_confirmed')
  with self.assertRaises(ValueError):policy.read_contextual_seller_terms(ref,'https://supplier.example/other',self.now)
  with self.assertRaisesRegex(ValueError,'generation'):policy.read_contextual_seller_terms(ref,self.url,self.now+1)
  self.assertIsNone(policy.retain_contextual_seller_terms(body,'https://supplier.example/terms',self.url))
  with patch.object(whitelist,'_BY_HOST',{}):self.assertEqual(policy.captured_seller_evidence({'url':self.url,'captured_at':self.now+1},ref)['decision']['status'],'unknown')
  path=self.root/ref['ref'];path.write_text(path.read_text()+' ')
  with self.assertRaises(ValueError):policy.read_contextual_seller_terms(ref,self.url,self.now)
 def observation(self,evidence=None,include=False):
  identity=ProductIdentity('model','ABC123','','Изделие ABC123');text='Изделие ABC123\nАртикул: ABC123\n100 руб. / шт';oid='a'*32
  obs=SourceObservation(observation_id=oid,identity=identity,supplier_host='supplier.example',requested_url=self.url,url=self.url,observed_at=self.now,issuer='approved_import',title='Изделие ABC123',manufacturer_code='ABC123',source_text=text,source_sha256=hashlib.sha256(text.encode()).hexdigest(),raw_amount=100,currency='RUB',vat='unknown',sale_unit='шт',lot_quantity=None,package_quantity=None,purchase_terms='',availability='unknown',state='observed',seller_name='ООО Seller',seller_inn='7610131367',image=ImageEvidence(oid,self.url,self.now,'missing',error='not_selected_price_observation'),validation_version='catalogue-native-v1',seller_evidence=evidence)
  return obs
 def test_legacy_payload_omission_and_new_immutable_roundtrip(self):
  legacy=self.observation();before=legacy.payload();self.assertNotIn('seller_evidence',before)
  def restore(value):
   value=copy.deepcopy(value);value['identity']=ProductIdentity(**{**value['identity'],'features':tuple(tuple(v) for v in value['identity']['features'])});value['image']=ImageEvidence(**value['image']);return SourceObservation(**value).payload()
  self.assertEqual(restore(before),before)
  modern=self.observation(self.evidence(self.source()));self.assertEqual(restore(modern.payload()),modern.payload())
  store=CatalogueStore(self.root/'catalogue.sqlite3');store.acquire_unknown(legacy.identity,['supplier.example'],owner='test');store.append_observation(legacy.identity.key,modern,coverage_status='found')
  self.assertEqual(store.resolve_observation(legacy.identity.key,legacy.observation_id)['observation'],modern.payload())
 def test_automatic_conflict_unknown_manual_letter_policy(self):
  conflict=self.evidence(self.source(),self.source('7701677700','scoped_offer'))
  for e in [conflict,self.evidence()]:
   offer=PriceOffer(price_with_vat=100,url=self.url,seller_evidence=e,requisites=Requisites(name='Seller',inn='7610131367'))
   self.assertTrue(seller_review_reason(offer,[offer]));self.assertFalse(offer_is_comparable(offer))
   offer.from_letter=True;self.assertEqual(seller_review_reason(offer,[offer]),'');self.assertTrue(offer_is_comparable(offer))
  offer.seller_evidence=None;offer.from_letter=False;self.assertTrue(offer_is_comparable(offer))
 def test_schema_public_and_projection_field_survives_without_client_upgrade(self):
  from . import app
  e=self.evidence(self.source());offer=PriceOffer(price_with_vat=100,url=self.url,seller_evidence=e,requisites=Requisites(name='Seller',inn='7610131367'))
  public=app._offer_to_dict(offer);self.assertEqual(public['seller_evidence'],e);self.assertIn('справочника',public['seller_origin_notice'])
  row=PositionIn(number=1,name='Manual',offers=[OfferIn.model_validate(public)])
  self.assertEqual(row.to_result().offers[0].seller_evidence,e)

 def test_publication_rederives_reference_and_rejects_forged_envelope(self):
  from .test_catalogue_producer import CatalogueProducerTests
  from .search.catalogue_producer import publish_snapshot,CataloguePublicationError
  helper=CatalogueProducerTests();helper.setUp();self.addCleanup(helper.doCleanups)
  self.reference_file.write_text('| ООО Seller | 123 | 7610131367 | https://shop.test |\n')
  snapshot,offer=helper.capture()
  self.reference_file.write_text('removed before publication')
  observed=publish_snapshot(helper.store,helper.identity,helper.pos,snapshot,image_store=helper.images,offer=offer)
  self.assertEqual(observed.seller_inn,'7610131367');self.assertEqual(observed.seller_evidence['decision']['status'],'reference_accepted')
  original=helper.store.all_snapshot()
  other,forged=helper.capture(price=101);forged.seller_evidence=self.evidence(self.source())
  with self.assertRaisesRegex(CataloguePublicationError,'Происхождение'):publish_snapshot(helper.store,helper.identity,helper.pos,other,image_store=helper.images,offer=forged)
  self.assertEqual(helper.store.all_snapshot(),original)
 def test_projection_binds_stored_envelope_and_keeps_raw_review_money(self):
  from .test_catalogue_projection import CatalogueProjectionTests
  fixture=CatalogueProjectionTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
  observation=fixture.observe()
  self.assertIsNone(observation.seller_evidence)
  # Construct a NEW source observation; the old fixture is never rewritten.
  stamp=observation.observed_at;url=observation.url
  source=policy.seller_source('approved_supplier_reference','seller',observation.seller_name,observation.seller_inn,
    source_url=url,source_sha256=self.hash,source_observed_at=stamp,role_text='approved reference',
    binding=dict(product_url=url,captured_at=stamp,source_ref='approved-supplier-list',row_sha256=self.hash))
  newstamp=stamp+1
  source=policy.seller_source('approved_supplier_reference','seller',observation.seller_name,observation.seller_inn,
    source_url=url,source_sha256=self.hash,source_observed_at=stamp,role_text='approved reference',
    binding=dict(product_url=url,captured_at=newstamp,source_ref='approved-supplier-list',row_sha256=self.hash))
  modern=fixture.observe(stamp=newstamp,seller_evidence=policy.make_seller_evidence(url,newstamp,[source]))
  with catalogue_scope(fixture.store):
   result=fixture.project();offer=result.all_offers[0]
   self.assertEqual(catalogue_review_reason(offer,fixture.position),'')
   for changed in [None,{},self.evidence()]:
    self.assertTrue(catalogue_review_reason(offer.model_copy(update={'seller_evidence':changed}),fixture.position))
  unknown=fixture.observe(stamp=newstamp+1,seller_evidence=policy.make_seller_evidence(url,newstamp+1,[]))
  with catalogue_scope(fixture.store):
   result=fixture.project();self.assertEqual(result.offers,[]);self.assertEqual(result.all_offers[0].listed_price_with_vat,100)
   self.assertTrue(catalogue_review_reason(result.all_offers[0],fixture.position))

 def test_refresh_fingerprint_preserves_legacy_and_binds_new_seller_origin(self):
  from .search.base_db import _observation_fingerprint,_refresh_context,_refresh_view
  from .search.product_identity import canonical_card_url
  pos=SpecPosition(number=1,name='Manual',unit='шт')
  offer=PriceOffer(price_with_vat=100,url=self.url,requisites=Requisites(name='Seller',inn='7610131367'))
  expected=json.dumps(dict(context=_refresh_context(pos),url=canonical_card_url(offer.url),price=offer.price_with_vat,listed=offer.listed_price_with_vat,unit=offer.price_unit,normalized_unit=offer.normalized_price_unit,capture=offer.source_quote_evidence or offer.price_capture_evidence,identity=offer.identity_evidence),sort_keys=True,ensure_ascii=False)
  self.assertEqual(_observation_fingerprint(offer,pos),expected)
  offer.seller_evidence=self.evidence(self.source());fingerprint=_observation_fingerprint(offer,pos)
  rebuilt=PriceOffer.model_validate_json(offer.model_dump_json());self.assertEqual(_observation_fingerprint(rebuilt,pos),fingerprint)
  changed=offer.model_copy(update={'seller_evidence':self.evidence()})
  entry={'refresh_receipts':{_refresh_context(pos):{canonical_card_url(offer.url):{'status':'confirmed','proof':fingerprint,'observed_at':self.now}}}}
  self.assertFalse(_refresh_view([offer],entry,pos)[0].refresh_failed)
  self.assertTrue(_refresh_view([changed],entry,pos)[0].refresh_failed)

 def test_xlsx_keeps_price_and_source_warning_for_conflicting_seller(self):
  from io import BytesIO
  from openpyxl import load_workbook
  from .schemas import ResearchReportRequest,ResearchReportPositionIn
  from .builders.research_report_xlsx import build_research_report
  e=self.evidence(self.source(),self.source('7701677700','scoped_offer'))
  offer=OfferIn(price_with_vat=100,url=self.url,org_name='Seller',inn='7610131367',seller_evidence=e,price_unit='шт')
  request=ResearchReportRequest(object_name='Offline conflict',positions=[ResearchReportPositionIn(number=1,name='Manual',unit='шт',qty=1,candidates=[offer],research_status='partial')])
  wb=load_workbook(BytesIO(build_research_report(request)))
  cells=[c.value for sheet in wb for row in sheet for c in row if c.value is not None]
  self.assertIn(100,cells);self.assertTrue(any(isinstance(v,str) and 'противоречат' in v for v in cells))
  self.assertEqual(request.positions[0].candidates[0].seller_evidence,e)

 def test_actual_collector_retains_raw_current_reference_conflict_without_remote_enrichment(self):
  import httpx
  from unittest.mock import Mock
  from .test_http_card import html,URL
  from .search import card_attachments,agent,session
  self.reference_file.write_text('| ООО Reference | 123 | 7610131367 | https://supplier.example |\n')
  response=httpx.Response(200,text=html(),headers={'content-type':'text/html'},request=httpx.Request('GET',URL))
  pos=SpecPosition(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)
  with patch.object(agent,'CACHE_DIR',self.root),patch.object(agent,'supplier_http_get',return_value=response),patch.object(agent,'fetch_requisites',side_effect=AssertionError('No lookup')) as remote,patch.object(agent,'fetch_requisites_from_site',side_effect=AssertionError('No lookup')) as site,session.research_session(deadline=time.monotonic()+30):
   snapshot,offer=card_attachments.collect_current(Mock(),pos,URL)
   self.assertIsNotNone(snapshot);self.assertIsNotNone(offer);self.assertEqual(offer.listed_price_with_vat,120)
   self.assertEqual(offer.seller_evidence['decision']['status'],'conflict');self.assertFalse(offer_is_comparable(offer,pos));remote.assert_not_called();site.assert_not_called()
   from .search.catalogue import identity_for_position
   from .search.catalogue_images import CatalogueImageStore
   from .search.catalogue_producer import publish_snapshot
   identity=identity_for_position(pos);store=CatalogueStore(self.root/'conflict.sqlite3');store.acquire_unknown(identity,['supplier.example'],owner='test')
   offer.seller_evidence=None  # Stripping the envelope cannot upgrade trusted publication.
   observed=publish_snapshot(store,identity,pos,snapshot,image_store=CatalogueImageStore(self.root),offer=offer)
   self.assertEqual(observed.seller_evidence['decision']['status'],'conflict')
   with catalogue_scope(store):
    result=project_product(store,identity,pos);self.assertEqual(result.offers,[]);self.assertEqual(result.all_offers[0].listed_price_with_vat,120)
 def test_seller_stage_typed_control_keeps_review_candidate_and_original_cause(self):
  import httpx
  from unittest.mock import Mock
  from .test_http_card import html,URL
  from .search import card_attachments,agent,session
  from .search.failure_policy import ResearchControlError
  body=html().replace('<div itemprop="seller" itemscope itemtype="https://schema.org/Organization"><span itemprop="name">ООО Supplier</span><span itemprop="taxID">7842224734</span></div>','')
  response=httpx.Response(200,text=body,headers={'content-type':'text/html'},request=httpx.Request('GET',URL))
  pos=SpecPosition(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)
  with patch.object(whitelist,'_BY_HOST',{}),patch.object(agent,'CACHE_DIR',self.root),patch.object(agent,'supplier_http_get',return_value=response),patch.object(agent,'fetch_requisites_from_site',side_effect=ResearchControlError('typed control')) as site,session.research_session(deadline=time.monotonic()+30):
   with self.assertRaisesRegex(ResearchControlError,'typed control'):card_attachments.collect_current(Mock(),pos,URL)
   site.assert_called_once()

 def test_unbounded_shapes_reject_before_any_serialization_or_digest(self):
  e=self.evidence(self.source())
  cases=[]
  bad=copy.deepcopy(e);bad['sources']=bad['sources']*9;cases.append(bad)
  bad=copy.deepcopy(e);bad['decision']['resolved_source_ids']=['a'*64]*9;cases.append(bad)
  bad=copy.deepcopy(e);bad['decision']['status']=['nested']*100;cases.append(bad)
  bad=copy.deepcopy(e);bad['sources'][0]['origin']={};cases.append(bad)
  bad=copy.deepcopy(e);bad['sources'][0]['binding']['extra']={'nested':['x']*100};cases.append(bad)
  bad=copy.deepcopy(e);bad['sources'][0]['raw_name']='x'*2049;cases.append(bad)
  bad=copy.deepcopy(e);bad['decision']['foreign']={'nested':['x']*100};cases.append(bad)
  with patch.object(policy,'_seller_json',side_effect=AssertionError('Must reject before encoding')),patch.object(policy,'_seller_digest',side_effect=AssertionError('Must reject before digest')):
   for value in cases:
    with self.subTest(case=cases.index(value)),self.assertRaises(ValueError):policy.validate_seller_evidence(value)

 def test_two_explicit_sellers_in_one_retained_offer_cannot_borrow_reference_identity(self):
  import httpx
  from .test_http_card import html,URL
  seller='<div itemprop="seller" itemscope itemtype="https://schema.org/Organization"><span itemprop="name">ООО Other</span><span itemprop="taxID">7610131367</span></div>'
  body=html().replace('</section>',seller+'</section>')
  record,reference=http_card.save_response(httpx.Response(200,text=body,headers={'content-type':'text/html'},request=httpx.Request('GET',URL)),URL)
  with patch.object(whitelist,'_BY_HOST',{}):
   evidence=policy.captured_seller_evidence(__import__('engine.search.card_batch',fromlist=['CardSnapshot']).CardSnapshot.create(dict(url=URL,captured_at=record['observed_at'],http_source=reference)).facts())
  self.assertEqual(len(evidence['sources']),2);self.assertEqual(evidence['decision']['status'],'conflict')
  body=html().replace('</section>',seller*8+'</section>')
  record,reference=http_card.save_response(httpx.Response(200,text=body,headers={'content-type':'text/html'},request=httpx.Request('GET',URL)),URL)
  with patch.object(whitelist,'_BY_HOST',{}):
   overflow=policy.captured_seller_evidence(__import__('engine.search.card_batch',fromlist=['CardSnapshot']).CardSnapshot.create(dict(url=URL,captured_at=record['observed_at'],http_source=reference)).facts())
  self.assertEqual(overflow['decision']['status'],'unknown');self.assertEqual(overflow['sources'],[])

 def test_reference_absence_stays_frozen_when_operator_adds_seller_later(self):
  from .search.card_batch import CardSnapshot
  empty=CardSnapshot.create({'url':self.url,'captured_at':self.now}).facts()
  self.reference_file.write_text('| ООО Seller | 123 | 7610131367 | https://supplier.example |\n')
  self.assertEqual(policy.captured_seller_evidence(empty)['decision']['status'],'unknown')
  stripped={k:v for k,v in empty.items() if k!='seller_reference_snapshot'}
  self.assertEqual(policy.captured_seller_evidence(stripped)['decision']['status'],'unknown')
  fresh=CardSnapshot.create({'url':self.url,'captured_at':self.now+1}).facts()
  self.assertEqual(policy.captured_seller_evidence(fresh)['decision']['status'],'reference_accepted')
  old=dict(empty);old['captured_at']+=1
  with self.assertRaises(ValueError):policy.captured_seller_evidence(old)
  path=self.root/empty['seller_reference_snapshot']['ref'];path.unlink()
  with self.assertRaises((ValueError,OSError)):policy.captured_seller_evidence(empty)

 def test_reviewer_terms_contradictions_are_not_seller_role_proof(self):
  prefix=f'Продавец товара {self.url}: ООО «Seller», ИНН 7610131367'
  for tail in [' (производитель, не продавец)','; сведения устарели, договор продажи заключает ООО Other',' / ИНН7701677700',' не является продавцом',' (предположительно)','\nне является продавцом','\nИНН 7701677700']:
   self.assertIsNone(policy._contextual_terms_seller(prefix+tail,self.url),tail)
  self.assertIsNotNone(policy._contextual_terms_seller(prefix+'.',self.url))
 def test_constructor_rejects_unbounded_inputs_before_decision(self):
  source=self.source();cases=[[source]*9]
  for key,value in [('origin',{}),('raw_name','x'*2049),('binding',{'foreign':{'nested':['x']*100}})]:
   bad=copy.deepcopy(source);bad[key]=value;cases.append([bad])
  with patch.object(policy,'_seller_decision',side_effect=AssertionError('No decision before bounds')):
   for sources in cases:
    with self.assertRaises(ValueError):policy.make_seller_evidence(self.url,self.now,sources)
 def test_missing_context_or_explicit_none_cannot_hide_current_reference_conflict(self):
  import httpx
  from .test_http_card import html,URL
  from .search.card_batch import CardSnapshot
  record,reference=http_card.save_response(httpx.Response(200,text=html(),headers={'content-type':'text/html'},request=httpx.Request('GET',URL)),URL)
  self.reference_file.write_text('| ООО Reference | 123 | 7610131367 | https://supplier.example |\n')
  facts=dict(url=URL,captured_at=record['observed_at'],http_source=reference,seller_reference_snapshot=None)
  self.assertEqual(policy.captured_seller_evidence(facts)['decision']['status'],'unknown')
  frozen=CardSnapshot.create(facts).facts()
  self.assertIsNotNone(frozen['seller_reference_snapshot'])
  self.assertEqual(policy.captured_seller_evidence(frozen)['decision']['status'],'conflict')
  stripped={k:v for k,v in frozen.items() if k!='seller_reference_snapshot'}
  self.assertEqual(policy.captured_seller_evidence(stripped)['decision']['status'],'conflict')
  for claimed in [None,{},dict(ref='seller_references/'+'b'*64+'.json',record_sha256='b'*64)]:
   supplied={**facts,'seller_reference_snapshot':claimed}
   rebuilt=CardSnapshot.create(supplied).facts()
   self.assertEqual(rebuilt['seller_reference_snapshot'],frozen['seller_reference_snapshot'])
   self.assertEqual(policy.captured_seller_evidence(rebuilt)['decision']['status'],'conflict')
  with self.assertRaises(ValueError):policy.captured_seller_evidence({**frozen,'seller_reference_snapshot':{'ref':'forged'}})

 def test_reviewer_legalname_role_qualifiers_never_become_retained_seller_proof(self):
  from .search.card_batch import CardSnapshot
  facts=CardSnapshot.create({'url':self.url,'captured_at':self.now}).facts()
  names=['ООО Тест (не продавец)','ООО Тест; договор продажи заключает ООО Другой','ООО Тест',
         'ООО «Тест» (не продавец)','ООО «Тест» ООО «Другой»','ООО «Тест; договор продажи заключает Другой»',
         'ООО «Тест (не продавец)»','ООО «Тест» «Другой»','ООО « »']
  context=dict(product_url=self.url,captured_at=self.now)
  for name in names:
   body=f'Продавец товара {self.url}: {name}, ИНН7610131367'
   self.assertIsNone(policy._contextual_terms_seller(body,self.url),name)
   ref=policy.retain_contextual_seller_terms(body,'https://supplier.example/terms',self.url,capture_context=context)
   self.assertIsNone(ref,name)
   self.assertEqual(policy.captured_seller_evidence(facts,ref)['decision']['status'],'unknown',name)
  for name in ['ООО «Тест»','ПАО "Тест-2"','АО «Компания Тест»','ЗАО "Тест"']:
   body=f'Продавец товара {self.url}: {name}, ИНН7610131367'
   ref=policy.retain_contextual_seller_terms(body,'https://supplier.example/terms',self.url,capture_context=context)
   self.assertIsNotNone(ref,name)
   self.assertEqual(policy.captured_seller_evidence(facts,ref)['decision']['status'],'role_confirmed',name)
