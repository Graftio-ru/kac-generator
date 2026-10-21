"""Unknown foreground routing uses a fresh acquisition namespace exactly once."""
import hashlib,json,tempfile,time,unittest,uuid
from pathlib import Path
from unittest.mock import patch,MagicMock
from contextlib import contextmanager,ExitStack
from engine import app
from engine.schemas import PositionIn
from engine.search.catalogue import CatalogueStore,identity_for_position,SourceObservation,ImageEvidence
from engine.search.search_engine import DiscoveryURLs
from engine.search import session

class CatalogueUnknownTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.store=CatalogueStore(self.root/'catalogue.sqlite3');self.hosts=[f's{i}.test' for i in range(36)];self.calls=[];self.captures=[]
  @contextmanager
  def browser():yield MagicMock()
  self.stack=ExitStack();self.addCleanup(self.stack.close)
  # Acquisition fixtures replace discovery/capture; account status is likewise
  # isolated so they cannot contact an operator's real configured account.
  self.stack.enter_context(patch('engine.search.search_route.resolve_initial_route',return_value=(None,None)))
  for context in (patch.object(app,'RESEARCH_JOB_DIR',self.root/'jobs'),patch.object(app,'_catalogue_store',return_value=self.store),patch.object(app,'_browser_factory',browser),patch.object(app,'_catalogue_image_store',return_value=MagicMock()),patch('engine.search.whitelist.whitelist_hosts',return_value=self.hosts[:29]),patch('engine.search.whitelist.fallback_hosts',return_value=self.hosts[29:]),patch.object(app,'_research_row_impl',side_effect=AssertionError('No legacy prices')),patch('engine.search.base_db.lookup',side_effect=AssertionError('No fuzzy cache')),patch('engine.search.candidate_queue.CandidateQueue',side_effect=AssertionError('No archive')),patch('engine.search.source_registry.SourceRegistry',side_effect=AssertionError('No prior mapping'))):self.stack.enter_context(context)
 def job(self,positions,*,search=None,capture=None,cancel=False):
  key=uuid.uuid4().hex[:12];app.RESEARCH_JOBS[key]=dict(job_id=key,rows=[None]*len(positions),total=len(positions),completed=0,status='pending',configured_hosts=self.hosts,primary_hosts=self.hosts[:29],fallback_hosts=self.hosts[29:],cancel_requested=cancel);app._RESEARCH_INPUTS[key]=positions
  self.addCleanup(app.RESEARCH_JOBS.pop,key,None);self.addCleanup(app._RESEARCH_INPUTS.pop,key,None)
  with patch('engine.search.search_engine.search_on_sites',side_effect=search or self.search),patch('engine.search.catalogue_producer.capture_and_publish',side_effect=capture or self.capture):
   app._save_research_job(key);app._run_research_job(key);public=app._research_store().public(key)
  return app.RESEARCH_JOBS[key],public
 def search(self,q,hosts,**kwargs):
  self.calls.append((q,tuple(hosts),kwargs.get('query_step')));session.transport_started('search_api')
  urls=['https://s0.test/card?variant=one'] if 's0.test' in hosts else []
  metadata={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=len(urls) if h=='s0.test' else 0,candidate_urls=urls if h=='s0.test' else [],diagnostics=[]) for h in hosts}
  return DiscoveryURLs(urls,origin='response',window='below_limit',host_observations=metadata)
 def capture(self,store,identity,position,url,*,browser,image_store,acquisition,supplier_host,observation_id=None,capture=None):
  self.captures.append((identity.key,position.qty,url));stamp=time.time();oid=observation_id or uuid.uuid4().hex;title='Cable '+identity.code;text=title+'\nАртикул: '+identity.code+'\n150 RUB с НДС\nЦена за 1 м'
  observation=SourceObservation(oid,identity,supplier_host,url,url,stamp,'supplier_rendered',title,identity.code,text,hashlib.sha256(text.encode()).hexdigest(),150,'RUB','included','м',1,None,'','in_stock','observed','Seller','7842224734',ImageEvidence(oid,url,stamp,'missing',error='fixture_image_missing'),'catalogue-native-v1')
  store.append_observation(identity.key,observation,acquisition=acquisition,coverage_status='found');return observation
 def test_unknown_duplicates_acquire_once_and_reproject_quantity_current_source(self):
  positions=[PositionIn(number=n,name='Cable',type_mark='ABC123',unit='м',qty=qty) for n,qty in [(1,1),(2,24)]]
  stale=MagicMock();stale.resolve_observation.side_effect=AssertionError('Unscoped stale catalogue authority')
  with patch('engine.search.catalogue_projection._STORE',stale):job,public=self.job(positions)
  self.assertEqual(job['status'],'done',job.get('error'));self.assertEqual(len(self.captures),1);self.assertEqual(len(public['rows']),2);self.assertTrue(all(row['found']==1 for row in job['rows']))
  self.assertEqual(len(job['rows'][0]['source_checks']),36);self.assertEqual(self.store.lookup(identity_for_position(positions[0]))['acquisition']['status'],'done')
  dates=[r['candidates'][0]['catalogue_observed_at'] for r in job['rows']];self.assertEqual(dates[0],dates[1]);self.assertTrue(all(r['candidates'][0]['price_with_vat']==150 for r in job['rows']))
  saved=len(self.calls);captured=len(self.captures);self.job([positions[0].model_copy(update={'qty':50})],search=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('Known searched')),capture=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('Known captured')))
  self.assertEqual(len(self.calls),saved);self.assertEqual(len(self.captures),captured)
 def test_all_fresh_source_tails_captured_even_after_first_eligible_quote(self):
  def discovery(q,hosts,**kwargs):
   value=self.search(q,hosts,**kwargs)
   if 's0.test' in hosts:
    urls=['https://s0.test/card?variant='+s for s in ('one','two','three')];value[:]=urls;value.host_observations['s0.test']['candidate_count']=3;value.host_observations['s0.test']['candidate_urls']=urls
   return value
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1);job,_=self.job([p],search=discovery)
  self.assertEqual(job['status'],'done',job.get('error'));self.assertEqual(len(self.captures),3);self.assertEqual(len(self.store.lookup(identity_for_position(p))['heads']),3);self.assertTrue(any('s35.test' in hosts for q,hosts,step in self.calls))
 def test_known_in_progress_and_failed_zero_never_acquire_again(self):
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1);admission=self.store.acquire_unknown(identity_for_position(p),self.hosts,owner='other_worker')
  for failed in (False,True):
   if failed:self.store.finish_acquisition(admission,error='provider_account')
   job,_=self.job([p],search=lambda *a,**k:(_ for _ in ()).throw(AssertionError('Known zero searched')))
   self.assertEqual(job['status'],'done');self.assertEqual(job['rows'][0]['found'],0);self.assertTrue(job['rows'][0]['catalogue_read'])
  self.assertEqual(self.captures,[])
 def test_failed_initial_empty_known_zero_and_cancellation_no_late_publication(self):
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)
  def empty(q,hosts,**kwargs):return DiscoveryURLs([],origin='response',window='below_limit',host_observations={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=0,diagnostics=[]) for h in hosts})
  job,_=self.job([p],search=empty);self.assertEqual(job['status'],'done');self.assertEqual(job['rows'][0]['found'],0);self.assertIsNotNone(self.store.lookup(identity_for_position(p)))
  self.job([p],search=lambda *a,**k:(_ for _ in ()).throw(AssertionError('No repeated first acquire')))
  other=p.model_copy(update={'type_mark':'OTHER999'})
  def cancelled_capture(store,identity,position,url,**kw):
   for value in app.RESEARCH_JOBS.values():
    if value.get('status')=='running':value['cancel_requested']=True
   kw['acquisition'] # The current session callback revokes durable owner first.
   from engine.search.session import bounded_timeout
   bounded_timeout(1)
   raise AssertionError('No late capture accepted')
  stopped,_=self.job([other],capture=cancelled_capture)
  self.assertEqual(stopped['status'],'cancelled',stopped.get('error'));record=self.store.lookup(identity_for_position(other));self.assertEqual(record['history'],[]);self.assertEqual(record['acquisition']['status'],'cancelled')

 def test_global_provider_stop_keeps_durable_unknown_and_no_card_calls(self):
  from engine.search.search_engine import SearchServiceUnavailable
  from engine.llm import _REQUEST_GUARD
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1);attempts=[]
  def failed(q,hosts,**kw):
   attempts.append(1);failure=SearchServiceUnavailable('account_balance');_REQUEST_GUARD.get().reject(failure);raise failure
  job,_=self.job([p],search=failed)
  self.assertEqual(job['status'],'error');self.assertEqual(len(attempts),1);self.assertEqual(self.captures,[]);saved=self.store.lookup(identity_for_position(p));self.assertEqual(saved['acquisition']['status'],'failed');self.assertTrue(all(c['status']=='deferred' and c['product_presence']=='unknown' for c in saved['coverage'].values()))
  self.job([p],search=lambda *a,**k:(_ for _ in ()).throw(AssertionError('No provider rerun')))
 def test_local_model_review_continues_other_source_and_no_own_replay(self):
  from engine.llm import ModelOutputInvalid
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1);captures=[]
  def discovery(q,hosts,**kw):
   value=self.search(q,hosts,**kw);urls=[f'https://{h}/card' for h in ('s0.test','s1.test') if h in hosts]
   value[:]=urls
   for h in hosts:value.host_observations[h]['candidate_count']=1 if h in ('s0.test','s1.test') else 0
   return value
  def capture(*args,**kw):
   captures.append(kw['supplier_host'])
   if kw['supplier_host']=='s0.test':
    from engine.search.agent import _CARD_DIAGNOSTICS
    _CARD_DIAGNOSTICS.get().append({'code':'bot_protection','reason':'HTTP 403: защита поставщика','http_status':403,'failure_source':'supplier_browser','failure_cause':'bot_protection','observed_at':time.time(),'raw_response':'SECRET'})
    raise ModelOutputInvalid('invalid_schema')
   return self.capture(*args,**kw)
  job,_=self.job([p],search=discovery,capture=capture)
  self.assertEqual(job['status'],'done',job.get('error'));self.assertIn('s1.test',captures);self.assertEqual(captures.count('s0.test'),1);self.assertEqual(job['rows'][0]['found'],1)
  saved=self.store.lookup(identity_for_position(p))['coverage']['s0.test']
  self.assertEqual(saved['reason'],'HTTP 403: защита поставщика');self.assertEqual(saved['diagnostics'][0]['http_status'],403);self.assertNotIn('raw_response',saved['diagnostics'][0])
  check=next(c for c in job['rows'][0]['source_checks'] if c['host']=='s0.test');self.assertEqual(check['code'],'bot_protection');self.assertEqual(check['http_status'],403)
 def test_native_metadata_search_request_known_identity_no_false_unknown(self):
  p=PositionIn(number=1,name='Cable',full_name='Cable complete',type_mark='ABC123',unit='м',qty=24,source_evidence={'reader':'native_pdf','cells':{'name':'Cable','model':'','article':'ABC123','maker':'Maker'}})
  a=self.store.acquire_unknown(identity_for_position(p),self.hosts,owner='previous');self.store.finish_acquisition(a,error='partial')
  with patch.object(app,'_run_catalogue_unknown_job',side_effect=AssertionError('False unknown')):
   result=app.search_position(app.SearchRequest(**p.model_dump(),all_suppliers=False,force_discovery=True))
  self.assertTrue(result['catalogue_read']);self.assertEqual(result['quote_status'],'insufficient');self.assertEqual(result['catalogue_product_id'],identity_for_position(p).key)
 def test_capacity_partial_own_urls_drain_and_other_resources_deferred(self):
  from engine.search.failure_policy import SearchCapacityDeferred
  from engine.llm import _REQUEST_GUARD
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1);calls=[]
  def busy(q,hosts,**kwargs):
   calls.append(1);url='https://s0.test/card';observation=dict(origin='supplier_own_search',window='unknown',observed_at=time.time(),candidate_count=1,candidate_urls=[url],diagnostics=[])
   kwargs['on_host_observation']('s0.test',observation);failure=SearchCapacityDeferred();failure.partial_discovery=DiscoveryURLs([url],origin='supplier_own_search',window='unknown',host_observations={'s0.test':observation});_REQUEST_GUARD.get().defer_search(failure);raise failure
  job,_=self.job([p],search=busy)
  self.assertEqual(job['status'],'running',job.get('error'));self.assertTrue(job['awaiting_provider']);self.assertEqual(len(calls),5);self.assertEqual(len(self.captures),1);record=self.store.lookup(identity_for_position(p));self.assertEqual(record['acquisition']['status'],'running');self.assertEqual(record['coverage']['s0.test']['status'],'found');self.assertTrue(all(record['coverage'][h]['status']=='deferred' for h in self.hosts[1:]));self.assertEqual(job['rows'][0]['found'],1)
 def test_real_neutral_producer_and_injected_images_on_initial_source(self):
  from engine.search.catalogue_producer import capture_and_publish
  from engine.search.catalogue_images import CatalogueImageStore
  from engine.search.card_batch import CardSnapshot
  from engine.search.product_identity import rendered_identity
  from engine.models import PriceOffer,Requisites
  from engine.source_quote import observe_captured_quote
  from PIL import Image
  puts=[];capture_calls=[]
  class Client:
   def put_object(self,**kwargs):puts.append(kwargs['Body']);return {'ResponseMetadata':{'HTTPStatusCode':200}}
  image_store=CatalogueImageStore(self.root,client=Client(),bucket='shots')
  def capture(browser,position,url,**kwargs):
   capture_calls.append(url);stamp=time.time();title='Cable ABC123';text=title+'\nАртикул: ABC123\n150 руб. с НДС\nза 1 м';proof=rendered_identity(url,url,title,'ABC123','ABC123','Cable','s0.test')
   path=self.root/(uuid.uuid4().hex+'.png');Image.new('RGB',(20,10),'white').save(path);data=path.read_bytes();context=dict(version=1,role='review_context',filename=path.name,sha256=hashlib.sha256(data).hexdigest(),url=url,captured_at=time.time(),width=20,height=10,document_width=20,document_height=10,truncated=False)
   facts=dict(url=url,requested_url=url,main_title=title,page_article='ABC123',captured_at=stamp,text=text,identity_evidence=proof,screenshot_context=str(path),context_capture_evidence=context)
   evidence=observe_captured_quote(text,actual_url=url,observed_at=stamp,url=url,title=title,article='ABC123',listed=150,normalized=150,sale_unit='м',requested_unit='м',data={})
   offer=PriceOffer(price_with_vat=150,listed_price_with_vat=150,url=url,price_unit='м',source_quote_evidence=evidence,requisites=Requisites(name='Seller',inn='7842224734'))
   return CardSnapshot.create(facts),offer
  def producer(*args,**kw):return capture_and_publish(*args,**kw,capture=capture)
  with patch.object(app,'_catalogue_image_store',return_value=image_store):
   job,_=self.job([PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)],capture=producer)
  self.assertEqual(job['status'],'done',job.get('error'));self.assertEqual(len(capture_calls),1);self.assertEqual(len(puts),1);product=self.store.lookup(identity_for_position(app._RESEARCH_INPUTS[job['job_id']][0]));self.assertEqual(product['heads'][0]['current']['image']['status'],'captured');self.assertEqual(product['heads'][0]['current']['image']['bucket'],'shots');self.assertEqual(job['rows'][0]['found'],1)

 def test_returned_host_diagnostics_preserved_and_empty_query_does_not_erase_card_failure(self):
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)
  s0_calls=[]
  def discovery(q,hosts,**kw):
   value=self.search(q,hosts,**kw)
   if 's0.test' in hosts:
    s0_calls.append(1)
    if len(s0_calls)>1:
     value[:]=[u for u in value if 's0.test' not in u];value.host_observations['s0.test'].update(candidate_count=0,candidate_urls=[])
   for host in hosts:
    if host=='s2.test':value.host_observations[host].update(window='unknown',diagnostics=[dict(code='search_http_error',reason='HTTP 403: поиск ресурса отклонён',http_status=403,failure_source='search_api',failure_cause='http_403',observed_at=time.time())])
   return value
  def capture(*args,**kwargs):
   from engine.search.agent import _CARD_DIAGNOSTICS
   _CARD_DIAGNOSTICS.get().append(dict(code='captcha',reason='KillBot: защита от ботов требует подтверждения',http_status=403,observed_at=time.time()))
   return None
  job,_=self.job([p],search=discovery,capture=capture)
  checks={v['host']:v for v in job['rows'][0]['source_checks']}
  self.assertEqual(checks['s2.test']['status'],'error');self.assertEqual(checks['s2.test']['code'],'search_http_error');self.assertEqual(checks['s2.test']['http_status'],403);self.assertEqual(checks['s2.test']['failure_source'],'search_api')
  self.assertEqual(checks['s0.test']['status'],'review');self.assertEqual(checks['s0.test']['code'],'captcha');self.assertIn('KillBot',checks['s0.test']['reason'])
  self.assertEqual(checks['s2.test']['product_presence'],'unknown');self.assertEqual(job['rows'][0]['found'],0)
 def test_discovery_child_budget_preserves_partial_owner_and_continues_other_resources(self):
  from engine.search.session import SearchBudgetExceeded
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1);calls=[]
  def discovery(q,hosts,**kw):
   calls.append(tuple(hosts));self.assertIsNone(kw['deadline'])
   if len(calls)==1:
    url='https://s0.test/card';metadata={'s0.test':dict(origin='supplier_own_search',window='unknown',observed_at=time.time(),candidate_count=1,diagnostics=[])}
    failure=SearchBudgetExceeded('finite logical operation exhausted');failure.partial_discovery=DiscoveryURLs([url],origin='supplier_own_search',host_observations=metadata);raise failure
   value=self.search(q,hosts,**kw)
   value[:]=[]
   for h in hosts:value.host_observations[h].update(candidate_count=0,candidate_urls=[],window='unknown')
   return value
  job,_=self.job([p],search=discovery);self.assertEqual(job['status'],'done',job.get('error'));self.assertEqual(job['rows'][0]['found'],1);self.assertTrue(any('s35.test' in hosts for hosts in calls));self.assertEqual(len(self.captures),1)
  checks={v['host']:v for v in job['rows'][0]['source_checks']};attempted=next(v for v in checks.values() if v.get('failure_cause')=='operation_budget');self.assertEqual(attempted['status'],'incomplete');self.assertIsNone(attempted.get('checked_at'));self.assertEqual(attempted['product_presence'],'unknown')
 def test_discovery_budget_cancellation_is_terminal_and_never_continues(self):
  from engine.search.session import SearchBudgetExceeded
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1);calls=[]
  def discovery(*args,**kw):
   calls.append(1);next(j for j in app.RESEARCH_JOBS.values() if j['status']=='running' and j.get('configured_hosts')==self.hosts)['cancel_requested']=True
   raise SearchBudgetExceeded('cancelled operation')
  job,_=self.job([p],search=discovery);self.assertEqual(job['status'],'cancelled');self.assertEqual(len(calls),1);self.assertEqual(self.captures,[])
 def test_terminal_acquisition_control_error_is_public_not_silent_done(self):
  from engine.search.failure_policy import ResearchControlError
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)
  job,_=self.job([p],search=lambda *a,**k:(_ for _ in ()).throw(ResearchControlError('Acquisition explicitly interrupted')))
  self.assertEqual(job['status'],'error');self.assertEqual(job['error_code'],'catalogue_acquisition_failed');self.assertIn('explicitly interrupted',job['rows'][0]['reason']);self.assertTrue(all(c['status']=='incomplete' and c['code']=='catalogue_deferred' for c in job['rows'][0]['source_checks']))

 def test_empty_partial_budget_keeps_actual_failed_host_metadata_not_unattempted_dates(self):
  from engine.search.session import SearchBudgetExceeded
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1);calls=[];stamp=time.time()
  def discovery(q,hosts,**kw):
   calls.append(tuple(hosts))
   if len(calls)==1:
    metadata={'s0.test':dict(origin='supplier_own_search',window='unknown',observed_at=stamp,candidate_count=0,diagnostics=[dict(code='supplier_local_error',reason='ReadTimeout при собственном поиске',observed_at=stamp,failure_source='supplier_http',failure_cause='network_timeout')])}
    error=SearchBudgetExceeded('operation exhausted');error.partial_discovery=DiscoveryURLs([],host_observations=metadata);raise error
   return DiscoveryURLs([],host_observations={h:dict(origin='unknown',window='unknown',candidate_count=0,diagnostics=[]) for h in hosts})
  job,_=self.job([p],search=discovery);self.assertEqual(job['status'],'done',job.get('error'));checks={v['host']:v for v in job['rows'][0]['source_checks']};self.assertEqual(checks['s0.test']['checked_at'],stamp);self.assertEqual(checks['s0.test']['failure_cause'],'network_timeout');self.assertIn('ReadTimeout',checks['s0.test']['reason']);self.assertIsNone(checks['s1.test'].get('checked_at'));self.assertTrue(any('s35.test' in hosts for hosts in calls))
 def test_true_parent_deadline_stops_before_any_discovery(self):
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)
  with session.research_session(deadline=time.monotonic()-1):
   job,_=self.job([p],search=lambda *a,**k:(_ for _ in ()).throw(AssertionError('No expired-parent query')))
  self.assertEqual(job['status'],'error');self.assertEqual(job['error_code'],'catalogue_acquisition_failed');self.assertEqual(self.captures,[])
 def test_publication_error_preserves_authoritative_safe_code_before_passive_trace(self):
  from engine.search.catalogue_producer import CataloguePublicationError
  def capture(*args,**kw):
   from engine.search.agent import _CARD_DIAGNOSTICS
   _CARD_DIAGNOSTICS.get().append(dict(code='render_observation',reason='Passive arrival trace'))
   raise CataloguePublicationError('catalogue_generation_mismatch')
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1);job,_=self.job([p],capture=capture)
  check=next(v for v in job['rows'][0]['source_checks'] if v['host']=='s0.test');self.assertEqual(check['code'],'catalogue_generation_mismatch');self.assertEqual(check['failure_source'],'catalogue_producer');self.assertNotEqual(check['reason'],'Passive arrival trace')
 def test_own_explicit_wrong_candidates_skipped_before_capture_but_ambiguous_tail_kept(self):
  from dataclasses import asdict
  from engine.search.supplier_discovery import parse_candidate_hints
  self.hosts[0]='chipdip.ru';calls=[]
  def card(path,title,fields=''):
   return '<tr class="with-hover"><td class="h_name"><div class="name"><a href="/product/'+path+'">'+title+'</a></div>'+fields+'</td></tr>'
  html='<div id="search_results"><table id="itemlist">'+card('wrong','Cable variant','<span>Артикул производителя: OTHER999</span>')+card('maybe','Cable alternative','<span>Код магазина: 9999999</span>')+'</table></div>'
  hints=parse_candidate_hints(html,'https://www.chipdip.ru/search?searchtext=ABC123','ABC123');urls=list(hints)
  def discovery(q,hosts,**kw):
   metadata={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=0,diagnostics=[]) for h in hosts}
   if 'chipdip.ru' in hosts:metadata['chipdip.ru'].update(origin='supplier_own_search',window='unknown',candidate_count=2,candidate_hints={url:asdict(hint) for url,hint in hints.items()})
   return DiscoveryURLs(urls if 'chipdip.ru' in hosts else [],host_observations=metadata)
  def capture(*args,**kw):calls.append(args[3]);return self.capture(*args,**kw)
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1);job,_=self.job([p],search=discovery,capture=capture)
  self.assertEqual(calls,['https://www.chipdip.ru/product/maybe']);self.assertEqual(job['rows'][0]['found'],1);self.assertEqual(set(job['catalogue_discovered_urls'][identity_for_position(p).key]),set(urls))
