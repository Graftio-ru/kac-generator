"""Current explicit alternative permission; immutable facts and operation keys stay original."""
import copy,hashlib,json,tempfile,time,unittest,uuid,httpx
from pathlib import Path
from unittest.mock import patch
from engine.models import SpecPosition,PriceOffer,Requisites
from engine.schemas import PositionIn
from engine.search import http_card as dom
from engine.search.catalogue import CatalogueStore,identity_for_position
from engine.search.catalogue_producer import publish_facts
from engine.search.catalogue_projection import project_product,catalogue_scope,_identity_reason,_facts
from engine.search.product_identity import alternative_policy,_original_native_model_role
from engine.search.article_query_plan import build_article_query_plan

class PositionAlternativeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.patch=patch.object(dom,'CACHE_DIR',self.root);self.patch.start();self.addCleanup(self.patch.stop);dom._CHAR_INDEX.clear();from engine import config;self.wp=patch.object(config,'CACHE_DIR',self.root);self.wp.start();self.addCleanup(self.wp.stop)
  self.input=dict(number=1,name='Tray',type_mark='ABC123',unit='м',qty=14,source_evidence=dict(reader='native_pdf',cells=dict(name='Tray',model='',article='ABC123',maker='DKC',unit='м',qty='14')))
  self.position=PositionIn(**self.input).to_result().position;self.identity=identity_for_position(self.position);self.store=CatalogueStore(self.root/'catalogue.sqlite3');self.store.acquire_unknown(self.identity,['shop.test'],owner='offline302')
 def source(self,article='ALT999',maker='DKC'):
  self.url='https://shop.test/product/'+article;title='Tray '+maker+' '+article
  html='<main itemscope itemtype="https://schema.org/Product"><h1>'+title+'</h1><div>Артикул: <span itemprop="mpn">'+article+'</span></div><p>Производитель '+maker+'</p><section itemprop="offers" itemscope itemtype="https://schema.org/Offer"><meta itemprop="priceCurrency" content="RUB"><span itemprop="price">100</span> ₽<p>Цена за 1 м с НДС</p><div itemprop="seller" itemscope itemtype="https://schema.org/Organization"><span itemprop="name">ООО Supplier</span><span itemprop="taxID">7842224734</span></div></section></main>'
  owned=dom.main_product(html,self.url,check_controls=False,admission_only=True);self.assertIsNotNone(owned)
  record,http_source=dom.save_response(httpx.Response(200,text=html,headers={'content-type':'text/html; charset=utf-8'},request=httpx.Request('GET',self.url)),self.url);stamp=record['observed_at'];frame=dict(version=1,role='main_card_facts',document_url=self.url,requested_url=self.url,main_title=title,article=owned['article'],owner_text=owned['text'],purchase_text='',owner_bound=True,owner_truncated=False,observed_at=stamp)
  rendered=dom.save_rendered_dom(html,frame,self.url);frame['rendered_source']=rendered;path=self.root/'main_facts'/(uuid.uuid4().hex+'.json');path.parent.mkdir(exist_ok=True);path.write_text(json.dumps(frame));ref=dict(ref='main_facts/'+path.name,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),observed_at=stamp)
  from engine.search.whitelist import capture_seller_reference
  capture_seller_reference(self.url,stamp)
  facts=dict(url=self.url,requested_url=self.url,main_title=title,page_article=article,text=owned['text'],captured_at=stamp,main_facts=ref,http_source=http_source,issuer='supplier_http')
  return facts,{**rendered,'main_facts':ref}
 def publish(self,position=None,article='ALT999',maker='DKC',sale='м'):
  position=position or self.position;facts,source=self.source(article,maker);offer=PriceOffer(price_with_vat=100,listed_price_with_vat=100,url=self.url,price_unit=sale,requisites=Requisites(name='Сбербанк',inn='7707083893'))
  from types import SimpleNamespace
  with _original_native_model_role(self.store,self.identity,position):obs=publish_facts(self.store,self.identity,position,facts,image_store=SimpleNamespace(root=self.root),offer=offer,verified_owner=True)
  return obs.payload(),source
 def allowed(self,*articles,names=None):return self.position.model_copy(update=dict(alternative_articles=list(articles),alternative_names=names or []))
 def test_schema_strict_roundtrip_and_original_identity(self):
  value=PositionIn(**self.input,alternative_articles=[' ALT999 ','alt999'],alternative_names=[' Other ','other']);self.assertEqual(value.alternative_articles,['ALT999']);self.assertEqual(value.to_result().position.alternative_names,['Other']);self.assertEqual(identity_for_position(value).key,self.identity.key);self.assertEqual(value.source_evidence,self.input['source_evidence'])
  for bad in [None,'ABC123',[1],[''],['x'*129],['a\nb'],['x']*21]:
   with self.subTest(bad=bad):self.assertRaises(ValueError,PositionIn,**self.input,alternative_articles=bad)
 def test_combined_three_queries_priority_and_unplanned(self):
  plan=build_article_query_plan('Tray','ABC123',article='ABC123',manufacturer='DKC',alternative_articles=['ALT999','ALT888','ALT777'],alternative_names=['Synonym'])
  self.assertEqual(len(plan.steps),3);self.assertEqual([s.identity_hint for s in plan.steps],['ABC123','ALT999','ALT888']);self.assertEqual(plan.unplanned_hints,({'kind':'alternative_article','value':'ALT777'},{'kind':'alternative_name','value':'Synonym'}));self.assertTrue(plan.diagnostic()['unplanned_hints_optional'])
 def test_historical_conflict_current_permission_only_and_removal(self):
  observed,source=self.publish();self.assertEqual(observed['state'],'identity_conflict');before=self.store.resolve_observation(self.identity.key,observed['observation_id'])['observation']
  self.assertTrue(_identity_reason(observed,self.position));allowed=self.allowed('ALT999');self.assertEqual(_identity_reason(observed,allowed),'')
  facts,reason=_facts(observed,allowed);self.assertFalse(reason);self.assertEqual(facts['price'],100)
  projected=project_product(self.store,self.identity,allowed);self.assertEqual(projected.all_offers[0].article_match_evidence['match_basis'],'estimator_alternative');self.assertFalse(_facts(observed,self.position)[0]);self.assertTrue(_identity_reason(observed,self.allowed(names=['ALT999'])))
  self.assertEqual(self.store.resolve_observation(self.identity.key,observed['observation_id'])['observation'],before)
  path=self.root/source['ref'];path.write_bytes(path.read_bytes()+b' ');self.assertTrue(_identity_reason(observed,allowed));self.assertFalse(_facts(observed,allowed)[0])
 def test_new_producer_v2_reopen_and_forged_display_ignored(self):
  allowed=self.allowed('ALT999');observed,_=self.publish(position=allowed);self.assertEqual(observed['native_request_contract']['version'],'native-source-constraints-v2');self.assertEqual(observed['state'],'observed')
  reopened=CatalogueStore(self.store.path);result=project_product(reopened,self.identity,allowed);offer=result.all_offers[0]
  from engine.app import _offer_to_dict
  with catalogue_scope(reopened):
   outgoing=_offer_to_dict(offer,allowed.type_mark,position=allowed);self.assertEqual(outgoing['article_match_evidence']['matched_article'],'ALT999')
   incoming=PositionIn(**self.input,alternative_articles=['ALT999'],offers=[{**outgoing,'article_match_evidence':{'match_basis':'FORGED'}}]);native=incoming.to_result();self.assertEqual(native.all_offers or native.offers,native.offers);self.assertEqual(native.offers[0].article_match_evidence['matched_article'],'ALT999')
   removed=PositionIn(**self.input,offers=[{**outgoing,'article_match_evidence':outgoing['article_match_evidence']}]).to_result();self.assertEqual(removed.offers[0].article_match_evidence,{})
 def test_wrong_maker_unit_and_unapproved_full_suffix_still_fail(self):
  observed,_=self.publish(maker='Other');self.assertTrue(_identity_reason(observed,self.allowed('ALT999')))
  observed,_=self.publish(article='ALT998');self.assertTrue(_identity_reason(observed,self.allowed('ALT999')))
  observed,_=self.publish(sale='unknown');self.assertFalse(_facts(observed,self.allowed('ALT999'))[0])
 def test_46_pending_operations_survive_policy_edits(self):
  for i in range(46):self.store.card_operation(self.identity.key,'shop.test','https://shop.test/card/'+str(i),operation_id=f'{i:032x}')
  for position in [self.position,self.allowed('ALT999'),self.allowed('ALT888',names=['Synonym']),self.allowed()]:
   self.assertEqual(identity_for_position(position).key,self.identity.key)
   for i in range(46):self.assertFalse(self.store.card_operation(identity_for_position(position).key,'shop.test','https://shop.test/card/'+str(i),operation_id='f'*32)['allowed'])
 def test_frontier_policy_change_stale_and_legacy_compatible(self):
  from engine.search.acquisition_frontier import new_frontier,validate_frontier
  def frontier(position):return new_frontier(job_id='a'*12,input_sha256='b'*64,positions=[position.model_dump()],configured_hosts=['shop.test'],index=0,product_id=self.identity.key,route_intent_sha256='c'*64,acquisition_generation=1)
  legacy=frontier(self.position);self.assertNotIn('alternatives_policy_sha256',legacy);allowed=self.allowed('ALT999');value=frontier(allowed);self.assertEqual(value['alternatives_policy_sha256'],alternative_policy(allowed)['sha256'])
  with self.assertRaises(ValueError):validate_frontier(value,job_id='a'*12,input_sha256='b'*64,positions=[self.allowed('ALT999',names=['changed']).model_dump()],configured_hosts=['shop.test'],index=0)
 def test_distinct_native_model_is_not_overridden_by_allowed_article(self):
  observed,_=self.publish();cells={**self.input['source_evidence']['cells'],'model':'MODEL7'}
  position=PositionIn(**{**self.input,'type_mark':'MODEL7 / ABC123','source_evidence':{'reader':'native_pdf','cells':cells}},alternative_articles=['ALT999']).to_result().position
  self.assertTrue(_identity_reason(observed,position));self.assertFalse(_facts(observed,position)[0])
 def test_direct_request_schemas_forward_and_cache_hit_echo(self):
  from engine import app as api
  from fastapi.testclient import TestClient
  allowed=self.allowed('ALT999');self.publish(position=allowed)
  request=api.SearchRequest(**self.input,alternative_articles=[' ALT999 '],alternative_names=['Name hint']);self.assertEqual(request.alternative_articles,['ALT999'])
  self.assertRaises(ValueError,api.CaptureRequest,url='https://unused.test',alternative_articles='ALT999')
  original=api._catalogue_known_row
  def known(position):return original(position,store=self.store,hosts=['shop.test'])
  with patch.object(api,'_catalogue_known_row',side_effect=known),TestClient(api.app)as client:
   response=client.post('/search_position',json=request.model_dump());self.assertEqual(response.status_code,200);row=response.json();self.assertEqual(row['alternative_articles'],['ALT999']);self.assertEqual(row['alternative_names'],['Name hint']);self.assertEqual(row['candidates'][0]['article_match_evidence']['matched_article'],'ALT999')
 def test_base_persistence_load_and_automatic_lookup_never_imports_permission(self):
  from engine.search import base_db
  allowed=self.allowed('ALT999');self.publish(position=allowed);result=project_product(self.store,self.identity,allowed)
  with patch.object(base_db,'BASE_FILE',self.root/'base.json'),catalogue_scope(self.store):
   base_db.upsert(allowed,result);data=json.loads((self.root/'base.json').read_text());entry=next(iter(data['entries'].values()));restored=base_db._position_from_entry(entry);self.assertEqual(restored.alternative_articles,['ALT999']);self.assertEqual(identity_for_position(restored).key,self.identity.key);self.assertEqual(base_db.entries()[0]['alternative_articles'],['ALT999'])
   hit=base_db.lookup(allowed,'2026','4',exact_only=True);self.assertTrue(hit.offers,[(o.unit_review_required,o.unit_review_reason,o.match_review_required,o.model_review_accepted,o.requisites.name,o.seller_evidence)for o in hit.all_offers])
   without=base_db.lookup(self.position,'2026','4',exact_only=True);self.assertFalse(without.offers);self.assertEqual(without.position.alternative_articles,[])
 def test_immutable_input_persists_arrays_and_policy_change_invalidates_certificate(self):
  from engine.research_store import ResearchStore
  allowed=self.allowed('ALT999',names=['Hint']);store=ResearchStore(self.root/'research.sqlite3');store.create_job('a'*12,[allowed.model_dump()],['shop.test']);snapshot,digest=store._acquisition_input('a'*12);self.assertEqual(snapshot['positions'][0]['alternative_articles'],['ALT999']);self.assertEqual(SpecPosition.model_validate(snapshot['positions'][0]).alternative_names,['Hint'])
  from engine.search.catalogue import require_catalogue_completion,CatalogueConflict
  from engine.search.acquisition_frontier import _hash
  receipt=dict(version='acquisition-completion-v1',product_id=self.identity.key,job_id='a'*12,input_sha256=digest,requested_identity_sha256=self.identity.key,generation=1,configured_hosts=['shop.test'],primary_hosts=['shop.test'],planner_version='native-acquisition-indexed-v5',source_intent_sha256='b'*64,runtime_sha256='c'*64,search_route=None,components_sha256='d'*64,frontier_sha256='e'*64,records_sha256='f'*64,query_count=1,card_count=0,completed_at=time.time(),alternatives_policy_sha256=alternative_policy(allowed)['sha256']);receipt['sha256']=_hash(receipt)
  product=dict(product_id=self.identity.key,acquisition_completion=receipt,acquisition=dict(status='done',error='',generation=1));snap={'products':{self.identity.key:product}}
  require_catalogue_completion(snap,[allowed],[0],configured_hosts=['shop.test'],primary_hosts=['shop.test'],components_sha256='d'*64,runtime_sha256='c'*64)
  with self.assertRaises(CatalogueConflict):require_catalogue_completion(snap,[self.allowed('ALT999',names=['Other'])],[0],configured_hosts=['shop.test'],primary_hosts=['shop.test'],components_sha256='d'*64,runtime_sha256='c'*64)
 def test_exact301_retained_card_identity_permission_does_not_grant_price(self):
  A=Path('/Users/MmM/WebstormProjects/graftio/sources/kac-generator/atomic-simplification-2026-10-14');B=A/'EPIC-0001-TASK-0301';pins=json.loads((B/'provenance-pins.json').read_text())['files']
  for name in ['observation-raw.json','rendered-card.json','one-card-facts.json']:self.assertEqual(hashlib.sha256((B/name).read_bytes()).hexdigest(),pins[name])
  observed=json.loads(json.loads((B/'observation-raw.json').read_text())['payload']);render=json.loads((B/'rendered-card.json').read_text());facts=json.loads((B/'one-card-facts.json').read_text())
  original_cache=Path(facts['rendered_card_source']).parents[1]
  matches=[]
  for path in (original_cache/'main_facts').glob('*.json'):
   value=json.loads(path.read_text())
   if value.get('rendered_source',{}).get('record_sha256')==facts['rendered_card_sha256']:matches.append(path)
  self.assertEqual(len(matches),1);original=matches[0];frame=json.loads(original.read_text());folder=self.root/'main_facts';folder.mkdir(exist_ok=True);(folder/original.name).write_bytes(original.read_bytes())
  rendered_ref=frame['rendered_source']['ref'];target=self.root/rendered_ref;target.parent.mkdir(exist_ok=True);target.write_bytes((B/'rendered-card.json').read_bytes())
  native_input=json.loads((A/'EPIC-0001-TASK-0300/original-position.json').read_text());pos=PositionIn.model_validate(native_input).to_result().position;allowed=pos.model_copy(update={'alternative_articles':['3526210ZL']})
  self.assertTrue(_identity_reason(observed,pos));self.assertEqual(_identity_reason(observed,allowed),'');self.assertFalse(_facts(observed,allowed)[0]);self.assertEqual(observed['state'],'identity_conflict');self.assertEqual(observed['sale_unit'],'unknown')

 def test_acquisition_prefilters_use_only_current_exact_full_article(self):
  from engine.search import agent
  from engine.search.product_identity import request_alternatives_scope,current_discovery_mark
  allowed=self.allowed('ALT999')
  self.assertTrue(agent._conflicting_page_article('ABC123','ALT999'))
  with request_alternatives_scope(allowed),_original_native_model_role(self.store,self.identity,allowed):
   self.assertFalse(agent._conflicting_page_article('ABC123','ALT999'));self.assertIs(agent._type_mark_match_status('ABC123','Tray ALT999'),True)
   self.assertFalse(agent._conflicting_model_code_in_title('ABC123','Tray ALT999'));self.assertTrue(agent._conflicting_page_article('ABC123','ALT998'))
  with request_alternatives_scope(self.allowed(names=['ALT999'])):self.assertTrue(agent._conflicting_page_article('ABC123','ALT999'))
  cells={**self.input['source_evidence']['cells'],'model':'MODEL7'}
  pos=PositionIn(**{**self.input,'type_mark':'MODEL7 / ABC123','source_evidence':{'reader':'native_pdf','cells':cells}},alternative_articles=['ALT999']).to_result().position
  identity=identity_for_position(pos);self.store.acquire_unknown(identity,['shop.test'],owner='offline302-model')
  with request_alternatives_scope(pos),_original_native_model_role(self.store,identity,pos):
   self.assertEqual(current_discovery_mark(pos.type_mark,'MODEL8 ALT999'),'MODEL7 / ALT999');from engine.search.product_identity import native_article_reason
   self.assertTrue(native_article_reason(pos,'MODEL8 ALT999','ALT999'))
 def test_native_snapshot_persist_reopen_and_fresh_report_current_policy(self):
  from engine.search.card_batch import CardSnapshot
  from engine.search.catalogue_producer import publish_snapshot
  from engine.search.product_identity import rendered_identity,request_alternatives_scope
  from engine.schemas import ResearchReportRequest
  from engine.builders.research_report_xlsx import build_research_report
  from engine.app import _offer_to_dict
  from types import SimpleNamespace
  import io,openpyxl
  allowed=self.allowed('ALT999','ALT888','ALT777',names=['Unplanned name']);facts,_=self.source()
  offer=PriceOffer(price_with_vat=100,listed_price_with_vat=100,url=self.url,price_unit='м',source_quote_evidence=dict(observed_at=facts['captured_at'],url=self.url,title=facts['main_title']))
  with request_alternatives_scope(allowed),_original_native_model_role(self.store,self.identity,allowed):
   proof=rendered_identity(self.url,self.url,facts['main_title'],'ALT999',allowed.type_mark,allowed.name,'shop.test',observed_at=facts['captured_at']);self.assertTrue(proof)
   snapshot=CardSnapshot.create({**facts,'identity_evidence':proof});observed=publish_snapshot(self.store,self.identity,allowed,snapshot,image_store=SimpleNamespace(root=self.root),offer=offer)
  reopened=CatalogueStore(self.store.path)
  with catalogue_scope(reopened):
   projected=project_product(reopened,self.identity,allowed);self.assertEqual(projected.all_offers[0].article_match_evidence['matched_article'],'ALT999')
   outgoing=_offer_to_dict(projected.all_offers[0],allowed.type_mark,position=allowed)
   def report(pos):
    req=ResearchReportRequest(positions=[{**pos.model_dump(),'offers':[outgoing],'candidates':[outgoing]}]);data=build_research_report(req,screenshot_links={});wb=openpyxl.load_workbook(io.BytesIO(data));return '\n'.join(str(c.value)for ws in wb for row in ws for c in row if c.value is not None)
   allowed_report=report(allowed);self.assertIn('Разрешённый артикул ALT999',allowed_report);self.assertIn('Не запланированы (не проверялись): ALT777, Unplanned name',allowed_report)
   removed_report=report(self.position);self.assertNotIn('Разрешённый артикул ALT999',removed_report)
  self.assertEqual(reopened.resolve_observation(self.identity.key,observed.observation_id)['observation']['state'],'observed')

 def test_actual_capture_dto_preserves_native_model_and_reduced_payload_fails_closed(self):
  from engine import app as api
  from engine.search import card_attachments
  from engine.search.product_identity import rendered_identity
  from fastapi.testclient import TestClient
  from contextlib import contextmanager
  cells={**self.input['source_evidence']['cells'],'model':'MODEL7'}
  payload={**self.input,'type_mark':'MODEL7 / ABC123','full_name':'Original PDF full label','source_evidence':{'reader':'native_pdf','cells':cells},'alternative_articles':['ALT999'],'alternative_names':['Search hint'],'url':'https://shop.test/product/ALT999'}
  observed=[]
  @contextmanager
  def browser():yield object()
  def capture(browser,position,url,extract=True):
   self.assertEqual(position.source_evidence,payload['source_evidence']);self.assertEqual(position.full_name,payload['full_name']);self.assertEqual(position.review_fields,{})
   def identity(model):return rendered_identity(url,url,'Tray '+model+' ALT999','ALT999',position.type_mark,position.name,'shop.test',observed_at=time.time())
   self.assertFalse(identity('MODEL8'));observed.append(bool(identity('MODEL7')))
   return None,None
  with patch.object(api,'_browser_factory',browser),patch.object(card_attachments,'capture_current',side_effect=capture),TestClient(api.app)as client:
   response=client.post('/capture',json=payload);self.assertEqual(response.status_code,200);self.assertEqual(observed,[True]);self.assertEqual(response.json()['alternative_articles'],['ALT999'])
   without={**payload,'alternative_articles':[]};self.assertEqual(client.post('/capture',json=without).status_code,200);self.assertEqual(observed,[True,False])
   reduced={k:v for k,v in payload.items()if k not in {'source_evidence','review_fields','full_name'}};self.assertEqual(client.post('/capture',json=reduced).status_code,422);self.assertEqual(observed,[True,False])
   reviewed={**payload,'review_fields':{'article':'Unverified original'}};self.assertEqual(client.post('/capture',json=reviewed).status_code,422)
  reduced_pos=SpecPosition(number=1,name='Tray',type_mark='MODEL7 / ABC123',unit='м',qty=14,alternative_articles=['ALT999'])
  from engine.search.product_identity import request_alternatives_scope
  with request_alternatives_scope(reduced_pos):self.assertFalse(rendered_identity(payload['url'],payload['url'],'Tray MODEL8 ALT999','ALT999',reduced_pos.type_mark,reduced_pos.name,'shop.test',observed_at=time.time()))

 def test_capture_legacy_literal_article_supported_but_native_model_role_preserved(self):
  from engine import app as api
  from engine.search import card_attachments
  from engine.search.product_identity import rendered_identity,request_alternatives_scope
  from fastapi.testclient import TestClient
  from contextlib import contextmanager
  @contextmanager
  def browser():yield object()
  def capture(browser,position,url,extract=True):
   self.assertTrue(rendered_identity(url,url,'Tray ALT999','ALT999',position.type_mark,position.name,'shop.test',observed_at=time.time()));return None,None
  with patch.object(api,'_browser_factory',browser),patch.object(card_attachments,'capture_current',side_effect=capture),TestClient(api.app)as client:
   response=client.post('/capture',json=dict(name='Tray',type_mark='ABC123',unit='м',qty=14,url='https://shop.test/product/ALT999',alternative_articles=['ALT999']));self.assertEqual(response.status_code,200)
  model=SpecPosition(number=1,name='Tray',type_mark='MODEL7',unit='м',qty=14,alternative_articles=['ALT999'],source_evidence={'reader':'native_pdf','cells':dict(name='Tray',model='MODEL7',article='',maker='DKC')})
  for current in [model,model.model_copy(update={'alternative_articles':[]})]:
   with request_alternatives_scope(current):
    self.assertTrue(rendered_identity('https://shop.test/product/MODEL7','https://shop.test/product/MODEL7','Tray MODEL7 ALT999','ALT999',current.type_mark,current.name,'shop.test',observed_at=time.time()))
    self.assertFalse(rendered_identity('https://shop.test/product/ALT999','https://shop.test/product/ALT999','Tray MODEL8 ALT999','ALT999',current.type_mark,current.name,'shop.test',observed_at=time.time()))
