"""Bounded producer observations and value-free schema diagnostics, offline."""
import json,tempfile,time,unittest,hashlib
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch,MagicMock
import httpx
from engine import llm
from engine.search import agent,session,card_batch as batch
from engine.test_card_batch import snapshot
from engine.test_http_card import URL,html

class CaptureDiagnostics(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.addCleanup(patch.stopall);patch.object(agent,'CACHE_DIR',self.root).start()
 def fields(self,body='Cable ABC123\nАртикул: ABC123\n120 ₽\nЦена за 1 м',main='Cable ABC123',count=1):
  return dict(document_url=URL,title='Cable ABC123',body=body,body_truncated=False,title_truncated=False,headings_truncated=False,main_title=main,all_h1=count,visible_h1=1 if main else 0,main_h1=1 if main else 0)
 def page(self,fields):
  page=MagicMock();page.url=page._kac_document_url=URL;page._kac_response_status=200;page._kac_retry_after=None
  page.title.return_value=fields['title'];page.inner_text.return_value=fields['body'];page.locator.return_value.count.return_value=fields['all_h1'];page.locator.return_value.first.inner_text.return_value=fields['main_title']
  from engine.test_http_card import bind_main_facts
  bind_main_facts(page)
  old_send=page.context.new_cdp_session.return_value.send.side_effect
  page.context.new_cdp_session.return_value.send.side_effect=lambda method,params:old_send(method,params) if params.get('awaitPromise') else {'result':{'value':fields}}
  return page
 def test_partial_and_protection_retained_before_close(self):
  for body in ('Cloudflare verify you are human','Visible catalog loading'):
   page=self.page(self.fields(body,main='',count=2));closed=[]
   class Owned:
    @contextmanager
    def page(inner,url):
     try:yield page
     finally:closed.append(bool(list((self.root/'main_facts').glob('*.json'))))
   with patch('engine.review_context.capture_context',return_value=({},'Fixture unavailable')),session.research_session(deadline=time.monotonic()+10),agent.capture_card_diagnostics() as diagnostics:
    result=agent._capture_rendered_card(Owned(),URL,URL,'supplier.example','ABC123','Cable','м',1,None,set(),None)
   self.assertEqual(closed,[True]);ref=next(d['main_facts'] for d in diagnostics if d.get('main_facts'));raw=(self.root/ref['ref']).read_bytes();self.assertEqual(hashlib.sha256(raw).hexdigest(),ref['sha256']);self.assertEqual(json.loads(raw)['body'],body)
   if 'Cloudflare' in body:self.assertIsNone(result);self.assertTrue(any(d['code'] in {'http_blocked','captcha'} for d in diagnostics))
   else:self.assertIsNotNone(result);self.assertEqual(result.facts()['identity_evidence'],{})
 def test_bounded_observation_control_and_unique_observed_main_title(self):
  fields=self.fields(count=2);page=self.page(fields)
  with session.research_session(deadline=time.monotonic()+2):record,ref=agent._render_observation(page,URL,'fixture')
  self.assertEqual(agent._observed_main_title(page,record),'Cable ABC123');self.assertFalse(page.locator.called)
  params=page.context.new_cdp_session.return_value.send.call_args.args[1];self.assertLessEqual(params['timeout'],1000);self.assertTrue(params['returnByValue']);self.assertIn('modal',params['expression'])
  ambiguous=dict(record,main_title='',main_h1=2);self.assertEqual(agent._observed_main_title(page,ambiguous),'')
  for options in ({'cancelled':lambda:True},{'deadline':time.monotonic()-1}):
   page.reset_mock()
   with session.research_session(**options):
    with self.assertRaises(session.SearchBudgetExceeded):agent._render_observation(page,URL,'stopped')
   page.context.new_cdp_session.assert_not_called()
 def test_navigation_during_observation_does_not_bind_body_to_old_status(self):
  page=self.page(self.fields());channel=page.context.new_cdp_session.return_value
  def moved(*args,**kwargs):
   page.url=URL+'/moved';return {'result':{'value':self.fields()}}
  channel.send.side_effect=moved
  with session.research_session(deadline=time.monotonic()+10):record,ref=agent._render_observation(page,URL,'fixture')
  self.assertEqual(record['observation_status'],'navigation_changed');self.assertFalse(record['status_url_bound']);self.assertEqual(record['document_url'],URL);self.assertEqual(record['url'],URL+'/moved');self.assertEqual(agent._observed_main_title(page,record),'')
 def test_unbound_marker_never_cools_requested_supplier_and_url_queries_redacted(self):
  for changed in (False,True):
   actual='https://foreign.example/item?token=PRIVATE_QUERY';fields=self.fields('Cloudflare verify you are human',main='');fields['document_url']=actual;page=self.page(fields);page.url=page._kac_document_url=actual
   with session.research_session(deadline=time.monotonic()+10),agent.capture_card_diagnostics() as diagnostics:
    record,ref=agent._render_observation(page,URL,'fixture')
    if changed:record['observation_status']='navigation_changed'
    agent._readiness_failure(URL,'supplier.example',record)
   refusal=diagnostics[-1];self.assertEqual(refusal['code'],'redirect_unconfirmed');self.assertNotEqual(refusal.get('failure_cause'),'bot_protection');self.assertNotIn('PRIVATE_QUERY',(self.root/ref['ref']).read_text())
 def test_ambiguous_main_headings_do_not_start_semantic_matching(self):
  fields=self.fields();fields.update(main_title='',main_h1=2,visible_h1=2,all_h1=2);page=self.page(fields)
  class Owned:
   @contextmanager
   def page(inner,url):yield page
  with patch.object(llm,'complete_json',side_effect=AssertionError('No model for ambiguous main heading')),session.research_session(deadline=time.monotonic()+10),agent.capture_card_diagnostics() as diagnostics:
   result=agent._capture_rendered_card(Owned(),URL,URL,'supplier.example','ABC123','Cable','м',1,None,set(),None)
  self.assertIsNotNone(result);self.assertEqual(result.facts()['identity_evidence'],{})
 def test_generic_main_sku_contradiction_before_paid_matching(self):
  title='Анкер латунный забивной М8 CLP1M-AL-8 IEK';body=title+'\nАртикул: CLP1M-AL-8\n69.55 ₽ /шт\nРекомендации: CM401040'
  fields=self.fields(body,main=title);fields['title']=title;page=self.page(fields)
  class Owned:
   @contextmanager
   def page(inner,url):yield page
  with patch.object(llm,'complete_json',side_effect=AssertionError('Conflicting main product must not call model')),session.research_session(deadline=time.monotonic()+10),agent.capture_card_diagnostics() as diagnostics:
   result=agent._capture_rendered_card(Owned(),URL,URL,'supplier.example','CM401040','Забивной анкер М10','шт',1,None,set(),None)
  self.assertIsNone(result);self.assertTrue(any(d['code']=='article_mismatch' for d in diagnostics),diagnostics)
 def test_multiple_headings_related_label_never_confirms_main_identity(self):
  title='Анкер латунный забивной М8 IEK';fields=self.fields(title+'\nRelated offer\nАртикул: CM401040\n69.55 ₽ /шт',main=title,count=2);fields['title']=title;page=self.page(fields);observed=[]
  class Owned:
   @contextmanager
   def page(inner,url):yield page
  with patch('engine.review_context.capture_context',return_value=({},'Fixture unavailable')),session.research_session(deadline=time.monotonic()+10,on_identity=observed.append):
   captured=agent._capture_rendered_card(Owned(),URL,URL,'supplier.example','CM401040','Забивной анкер М10','шт',1,None,set(),None)
  self.assertIsNotNone(captured);self.assertEqual(captured.facts()['identity_evidence'],{});self.assertEqual(observed,[])
 def test_main_facts_clipped_wait_and_stop_before_and_after_rpc(self):
  page=self.page(self.fields())
  with session.research_session(deadline=time.monotonic()+2):facts,ref=agent._wait_main_card_facts(page,URL)
  params=next(c.args[1] for c in page.context.new_cdp_session.return_value.send.call_args_list if c.args[1].get('awaitPromise'))
  self.assertLessEqual(params['timeout'],2000);self.assertTrue(params['awaitPromise']);self.assertEqual(facts['article'],'ABC123');self.assertEqual(ref['observed_at'],facts['observed_at']);self.assertNotIn('ABC123',params['expression']);self.assertNotIn(URL,params['expression'])
  for options in ({'cancelled':lambda:True},{'deadline':time.monotonic()-1}):
   page.reset_mock()
   with session.research_session(**options):
    with self.assertRaises(session.SearchBudgetExceeded):agent._wait_main_card_facts(page,URL)
   page.context.new_cdp_session.assert_not_called()
  stopped=[False];page=self.page(self.fields());previous=page.context.new_cdp_session.return_value.send.side_effect;before=set((self.root/'main_facts').glob('*'))
  def stop(method,params):
   value=previous(method,params);stopped[0]=True;return value
  page.context.new_cdp_session.return_value.send.side_effect=stop
  with session.research_session(deadline=time.monotonic()+10,cancelled=lambda:stopped[0]):
   with self.assertRaises(session.SearchBudgetExceeded):agent._wait_main_card_facts(page,URL)
  self.assertEqual(set((self.root/'main_facts').glob('*')),before);page.context.new_cdp_session.return_value.detach.assert_called_once()
 def test_partial_exact_title_uses_original_gates_and_atomic_owner_epoch(self):
  from engine.models import Requisites
  from engine.offer_selection import offer_is_comparable
  owner='Cable ABC123\n120 ₽\nЦена за 1 м';fields=self.fields(owner+'\nFooter\n999 ₽ /шт');page=self.page(fields);observed_at=time.time()
  original=page.context.new_cdp_session.return_value.send.side_effect
  def read(method,params):
   value=original(method,params)
   if params.get('awaitPromise'):value['result']['value'].update(kind='partial',article='',missing_fields=['article'],owner_text=owner,observed_at=observed_at)
   else:value['result']['value'].update(main_title='',visible_h1=0,main_h1=0)
   return value
  page.context.new_cdp_session.return_value.send.side_effect=read
  class Owned:
   @contextmanager
   def page(inner,url):yield page
  shell=httpx.Response(200,text='<main>JS shell</main>',request=httpx.Request('GET',URL))
  with patch.object(agent,'supplier_http_get',return_value=shell),patch.object(agent,'wl_requisites_for',return_value=Requisites(name='Supplier',inn='7842224734')),patch('engine.review_context.capture_context',return_value=({},'No attachment')),patch.object(llm,'complete_json',side_effect=AssertionError('Literal must not call model')),session.research_session(deadline=time.monotonic()+10):
   captured=agent._capture_rendered_card(Owned(),URL,URL,'supplier.example','ABC123','Cable','м',1,None,set(),None)
   self.assertEqual(captured.facts()['text'],owner);self.assertEqual(captured.facts()['document_body'],fields['body']);self.assertEqual(captured.facts()['captured_at'],observed_at)
   literal=agent._literal_card_fields(captured);self.assertIsNotNone(literal)
   offer=agent._finish_captured_card(Owned(),captured,literal)
  self.assertTrue(offer_is_comparable(offer,{'unit':'м','type_mark':'ABC123','name':'Cable'}));self.assertEqual(offer.price_with_vat,120);self.assertEqual(offer.source_quote_evidence['observed_at'],observed_at)
 def test_main_owner_without_price_cannot_borrow_footer_or_related_quote(self):
  for outside in ('Footer\n100 ₽ /шт','Related products\nАртикул: ABC123\n100 ₽ /шт'):
   owner='Cable ABC123';fields=self.fields(owner+'\n'+outside);page=self.page(fields);previous=page.context.new_cdp_session.return_value.send.side_effect
   def read(method,params):
    value=previous(method,params)
    if params.get('awaitPromise'):value['result']['value'].update(kind='partial',article='',owner_text=owner,missing_fields=['article','price','sale_unit'])
    return value
   page.context.new_cdp_session.return_value.send.side_effect=read
   class Owned:
    @contextmanager
    def page(inner,url):yield page
   with patch('engine.review_context.capture_context',return_value=({},'No attachment')),session.research_session(deadline=time.monotonic()+10):captured=agent._capture_rendered_card(Owned(),URL,URL,'supplier.example','ABC123','Cable','шт',1,None,set(),None)
   self.assertIsNotNone(captured);self.assertEqual(captured.facts()['text'],owner);self.assertIsNone(agent._deterministic_card_data(captured));self.assertEqual(captured.facts()['document_body'],fields['body'])
 def test_snapshot_identifier_path_and_unchanged_original_deadline(self):
  captured=snapshot();ref=captured.retain(self.root);raw=(self.root/ref['ref']).read_bytes();self.assertEqual(json.loads(raw)['deadline'],captured.deadline);self.assertEqual(captured.retain(self.root),ref)
  bad=batch.CardSnapshot('../../outside',captured.payload,captured.sha256,captured.deadline)
  with self.assertRaises(ValueError):bad.retain(self.root)
  outside=self.root/'outside';outside.mkdir();folder=self.root/'escaped';folder.mkdir();(folder/'captured_inputs').symlink_to(outside,target_is_directory=True)
  with self.assertRaises(ValueError):captured.retain(folder)
 def test_schema_stages_reveal_no_values_unknown_keys_or_ids(self):
  source=snapshot();private='PRIVATE_MODEL_VALUE'
  cases=[('wrapper',[private]),('cards',{'cards':private}),('item',{'cards':[private]}),('id',{'cards':[{'id':private,'extraction':{}}]}),('extraction',{'cards':[{'id':source.card_id,'extraction':private}]})]
  for stage,response in cases:
   with patch.object(llm,'complete_json',return_value=response) as calls:
    try:out=batch.extract_batch([source]);error=out[source.card_id][1]
    except llm.ModelOutputInvalid as e:error=e
   diag=error.diagnostic();self.assertEqual(diag['batch_schema_stage'],stage);self.assertNotIn(private,json.dumps(diag));self.assertEqual(calls.call_count,1)
  for stage,cards in (('duplicate_id',[{'id':source.card_id,'extraction':{}},{'id':source.card_id,'extraction':{}}]),('missing_id',[])):
   with patch.object(llm,'complete_json',return_value={'cards':cards}):error=batch.extract_batch([source])[source.card_id][1]
   self.assertEqual(error.diagnostic()['batch_schema_stage'],stage)
  for field,value in (('matches_requested_product',private),('price_with_vat',private),('region_status',private)):
   with patch.object(llm,'complete_json',return_value={'cards':[{'id':source.card_id,'extraction':{field:value}}]}):error=batch.extract_batch([source])[source.card_id][1]
   self.assertEqual(error.diagnostic()['batch_schema_field'],field);self.assertNotIn(private,json.dumps(error.diagnostic()))
  for supplied in ({'product_title':private},{'PRIVATE_UNKNOWN_FIELD':private}):
   with patch.object(llm,'complete_json',return_value={'cards':[{'id':source.card_id,'extraction':supplied}]}):error=batch.extract_batch([source])[source.card_id][1]
   self.assertEqual(error.diagnostic()['batch_schema_stage'],'readonly_mismatch');self.assertNotIn(private,json.dumps(error.diagnostic()));self.assertNotIn('PRIVATE_UNKNOWN_FIELD',json.dumps(error.diagnostic()))
 def test_direct_collector_failed_extraction_retains_current_snapshot_no_replay(self):
  body='Cable ABC123\nАртикул: ABC123\n120 ₽\nЦена за 1 м\nВторая цена\n125 ₽\nЦена за 1 м';page=self.page(self.fields(body));closed=[]
  class Owned:
   @contextmanager
   def page(inner,url):
    try:yield page
    finally:closed.append(url)
  shell=httpx.Response(200,headers={'content-type':'text/html'},text='<main>JS shell</main>',request=httpx.Request('GET',URL))
  # Own actual source response cache as well as producer capture files.
  from engine.search import http_card
  with patch.object(http_card,'CACHE_DIR',self.root),patch.object(agent,'supplier_http_get',return_value=shell),patch('engine.review_context.capture_context',return_value=({},'Fixture unavailable')),patch.object(llm,'complete_json',return_value={'wrong': 'PRIVATE'}) as calls,session.research_session(deadline=time.monotonic()+10),agent.capture_card_diagnostics() as diagnostics:
   offers,checked=agent._collect_offers(Owned(),[URL],1,'ABC123','Cable','м',supplier_hosts={'supplier.example'})
  self.assertEqual(offers,[]);self.assertEqual(checked,1);self.assertEqual(closed,[URL]);self.assertEqual(calls.call_count,1)
  diag=next(d for d in diagnostics if d['code']=='model_output_invalid');ref=diag['capture_snapshot'];raw=(self.root/ref['ref']).read_bytes();self.assertEqual(hashlib.sha256(raw).hexdigest(),ref['record_sha256']);stored=json.loads(raw);self.assertEqual(hashlib.sha256(stored['payload'].encode()).hexdigest(),ref['snapshot_sha256']);facts=json.loads(stored['payload']);self.assertEqual(facts['text'],body);self.assertEqual(facts['requested_url'],URL);self.assertEqual(stored['id'],ref['card_id']);self.assertNotIn('PRIVATE',raw.decode())
 def test_failed_second_capture_never_borrows_prior_snapshot_ref(self):
  page=self.page(self.fields('Cable ABC123\nАртикул: ABC123\n120 ₽\nЦена за 1 м\nВторая цена\n125 ₽\nЦена за 1 м'));second=URL+'/next';opened=[]
  class Owned:
   @contextmanager
   def page(inner,url):
    opened.append(url)
    if url==second:raise llm.ModelOutputInvalid('invalid_schema')
    yield page
  from engine.search import http_card
  def shell(url,**kwargs):return httpx.Response(200,headers={'content-type':'text/html'},text='<main>JS shell</main>',request=httpx.Request('GET',url))
  with patch.object(http_card,'CACHE_DIR',self.root),patch.object(agent,'supplier_http_get',shell),patch('engine.review_context.capture_context',return_value=({},'Fixture unavailable')),patch.object(llm,'complete_json',return_value={'wrong':'PRIVATE'}) as calls,session.research_session(deadline=time.monotonic()+10),agent.capture_card_diagnostics() as diagnostics:
   offers,checked=agent._collect_offers(Owned(),[URL,second],2,'ABC123','Cable','м',supplier_hosts={'supplier.example'})
  errors=[d for d in diagnostics if d['code']=='model_output_invalid'];self.assertEqual(len(errors),2);self.assertIn('capture_snapshot',errors[0]);self.assertNotIn('capture_snapshot',errors[1]);self.assertEqual(errors[1]['url'],second);self.assertEqual(calls.call_count,1)
if __name__=='__main__':unittest.main()
