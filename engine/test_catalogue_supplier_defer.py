"""Real health denials are host-local and keep original pending work."""
import json,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from engine import app,test_catalogue_unknown as fixtures
from engine.schemas import PositionIn
from engine.search import session,search_engine as search,supplier_discovery as own,catalogue
from engine.search.supplier_health_store import SupplierHealthStore,Outcome
from engine.search.catalogue_producer import capture_and_publish
from engine.search.article_query_plan import QueryStep

class CatalogueSupplierDeferTests(unittest.TestCase):
 def setUp(self):
  self.f=fixtures.CatalogueUnknownTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.health=SupplierHealthStore(self.f.root/'health.sqlite3');self.position=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)
  self.query_dates={}
  self.health_patch=patch('engine.search.supplier_health_store.SupplierHealthStore',return_value=self.health);self.health_patch.start();self.addCleanup(self.health_patch.stop)
 def cooldown(self,host):
  admission=self.health.acquire(host);self.health.finish(admission,Outcome.BOT_PROTECTION);return self.health.acquire(host).pair_check()
 def test_actionable_captcha_survives_trailing_passive_trace_in_public_and_xlsx(self):
  cause='KillBot: защита от ботов требует подтверждения'
  def blocked(*args,**kwargs):
   from engine.search.agent import _reject_card,_CARD_DIAGNOSTICS
   _reject_card(args[3],code='captcha',reason=cause)
   _CARD_DIAGNOSTICS.get().append(dict(code='browser_transport_trace',reason='Passive arrival metadata retained'))
   return None
  job,public=self.f.job([self.position],capture=blocked)
  self.assertEqual(public['status'],'done',public.get('error'))
  check=next(c for c in public['rows'][0]['source_checks'] if c['host']=='s0.test')
  self.assertIn(cause,check['reason']);self.assertEqual(check['status'],'review')
  saved=self.f.store.lookup(catalogue.identity_for_position(self.position))
  self.assertEqual(saved['heads'],[]);self.assertEqual(public['rows'][0]['found'],0)
  self.assertEqual([d['code'] for d in saved['coverage']['s0.test']['diagnostics']],['captcha','browser_transport_trace'])
  self.assertEqual(saved['coverage']['s0.test']['reason'],cause)
  import asyncio
  from io import BytesIO
  from openpyxl import load_workbook
  from engine.schemas import ResearchReportRequest
  response=app.research_report(ResearchReportRequest(object_name='Card refusal',positions=[self.position.model_dump()]))
  async def content():return b''.join([part async for part in response.body_iterator])
  book=load_workbook(BytesIO(asyncio.run(content())),data_only=True)
  values=[str(cell) for sheet in book for line in sheet.iter_rows(values_only=True) for cell in line]
  self.assertTrue(any(cause in value for value in values))
  for code in ('render_observation','main_facts_readiness','capture_snapshot_observation'):
   self.assertEqual(app._catalogue_card_review_reason([dict(code=code,reason='Passive')],'Actual failure'),'Actual failure')
 def discovery(self,q,hosts,**kw):
  urls=['https://'+h+'/card' for h in hosts if h in {'s0.test','s8.test'}];stamp=time.time();self.query_dates.update({h:stamp for h in hosts})
  return search.DiscoveryURLs(urls,origin='response',window='below_limit',host_observations={h:dict(origin='response',window='below_limit',observed_at=stamp,candidate_count=int(h in {'s0.test','s8.test'}),diagnostics=[]) for h in hosts})
 def test_real_cooldown_capture_boundary_keeps_card_pending_and_other_host_continues(self):
  denial=self.cooldown('s0.test');admitted=[]
  with patch('engine.search.agent._capture_rendered_card',side_effect=lambda *a,**kw:admitted.append(a[1])):
   def grouped(q,hosts,**kw):
    value=self.discovery(q,hosts,**kw)
    if 's1.test' in hosts:value.append('https://s1.test/card');value.host_observations['s1.test']['candidate_count']=1
    return value
   job,public=self.f.job([self.position],search=grouped,capture=capture_and_publish)
  self.assertEqual(public['status'],'running',public.get('error'));self.assertTrue(public['awaiting_supplier']);self.assertFalse(public['awaiting_provider']);self.assertNotIn('first_stop',public)
  self.assertEqual(admitted,['https://s8.test/card']);self.assertIn('защиту от ботов',public['awaiting_supplier_reason'])
  from engine.search.catalogue import identity_for_position
  record=self.f.store.lookup(identity_for_position(self.position));check=record['coverage']['s0.test'];self.assertEqual(check['status'],'deferred');self.assertEqual(check['observed_at'],self.query_dates['s0.test']);self.assertEqual(check['deferred_check']['checked_at'],denial['checked_at']);self.assertEqual(check['deferred_check']['retry_at'],denial['retry_at'])
  frontier=app._research_store().load_acquisition_frontier(job['job_id'],0)['frontier'];pending=next(q for q in frontier['queries'] if q['current_card']);self.assertEqual(pending['cursor'],0);self.assertEqual(pending['current_card']['url'],'https://s0.test/card');self.assertFalse(frontier['frontier_complete'])
  with patch.object(app,'_launch_research_job',side_effect=AssertionError('No automatic supplier resume')):
   self.assertTrue(app.research_status(job['job_id'])['awaiting_supplier'])
  offset=901;self.health.clock=lambda:time.time()+offset
  def exhausted(q,hosts,**kw):return search.DiscoveryURLs([],origin='response',window='below_limit',host_observations={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=0,diagnostics=[]) for h in hosts})
  with patch.object(catalogue,'time',SimpleNamespace(time=lambda:time.time()+offset)),patch.object(app,'time',SimpleNamespace(time=lambda:time.time()+offset,monotonic=time.monotonic)),patch.object(search,'search_on_sites',side_effect=exhausted),patch('engine.search.catalogue_producer.capture_and_publish',side_effect=self.f.capture):
   resumed=app.resume_catalogue_acquisition(job['job_id'],store=self.f.store)
  self.assertEqual(resumed['status'],'done',resumed.get('error'));self.assertFalse(resumed['awaiting_supplier']);self.assertEqual(len(self.f.captures),2);self.assertEqual([v[2] for v in self.f.captures],['https://s0.test/card','https://s1.test/card'])
 def test_probe_contention_waits_then_admits_without_paid_retry(self):
  active=self.health.acquire('s0.test');waits=[];admitted=[]
  def released(seconds):waits.append(seconds);self.health.finish(active,Outcome.SUCCESS)
  def one(q,hosts,**kw):
   value=self.discovery(q,hosts,**kw);value[:]=[u for u in value if 's0.test' in u];return value
  with patch.object(app,'_supplier_contention_wait',side_effect=released),patch('engine.search.agent._capture_rendered_card',side_effect=lambda *a,**kw:admitted.append(a[1])):
   job,public=self.f.job([self.position],search=one,capture=capture_and_publish)
  self.assertEqual(public['status'],'done',public.get('error'));self.assertEqual(waits,[1]);self.assertEqual(admitted,['https://s0.test/card']);self.assertFalse(public['awaiting_supplier'])
 def test_probe_queue_120s_exhaustion_retains_unattempted_card(self):
  self.health.acquire('s0.test');clock=[time.monotonic()];waits=[]
  def advance(seconds):waits.append(seconds);clock[0]+=seconds
  with patch.object(app,'time',SimpleNamespace(time=time.time,monotonic=lambda:clock[0])),patch.object(app,'_supplier_contention_wait',side_effect=advance),patch('engine.search.agent._capture_rendered_card',side_effect=lambda *a,**kw:None):
   job,public=self.f.job([self.position],search=self.discovery,capture=capture_and_publish)
  self.assertEqual(sum(waits),120);self.assertTrue(all(0<s<=1 for s in waits));self.assertEqual(public['status'],'running');self.assertTrue(public['awaiting_supplier'])
  check=next(c for c in public['rows'][0]['source_checks'] if c['host']=='s0.test');self.assertEqual(check['observed_at'],self.query_dates['s0.test']);self.assertEqual(check['deferred_check']['cards'],0)
 def test_cancellation_during_contention_stops_before_admitted_capture(self):
  self.health.acquire('s0.test')
  def cancel(seconds):
   for job in app.RESEARCH_JOBS.values():job['cancel_requested']=True
  with patch.object(app,'_supplier_contention_wait',side_effect=cancel),patch('engine.search.agent._capture_rendered_card',side_effect=AssertionError('No capture after cancellation')):
   job,public=self.f.job([self.position],search=self.discovery,capture=capture_and_publish)
  self.assertEqual(public['status'],'cancelled');self.assertNotIn('first_stop',public)
 def test_real_own_health_denial_stays_pending_own_only_and_no_serp_replay(self):
  self.f.hosts=['chipdip.ru']+[f's{i}.test' for i in range(35)];check=self.cooldown('chipdip.ru');serp=[]
  def empty(q,hosts,**kw):
   serp.append(tuple(hosts));return search.DiscoveryURLs([],origin='response',window='below_limit',host_observations={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=0,diagnostics=[]) for h in hosts})
  actual=search.search_on_sites
  with patch.object(search,'_serp_on_sites',side_effect=empty),patch('engine.browser.supplier_http_get',side_effect=AssertionError('No HTTP in cooldown')):
   job,public=self.f.job([self.position],search=actual,capture=capture_and_publish)
  self.assertEqual(public['status'],'running',public.get('error'));self.assertTrue(public['awaiting_supplier'])
  frontier=app._research_store().load_acquisition_frontier(job['job_id'],0)['frontier'];retry=[q for q in frontier['queries'] if q['strategy']=='own_only'];self.assertEqual(len(retry),1);self.assertEqual(retry[0]['state'],'pending');self.assertFalse(frontier['frontier_complete'])
  key=job['job_id'];offset=121
  with patch.object(catalogue,'time',SimpleNamespace(time=lambda:time.time()+offset)),patch.object(app,'time',SimpleNamespace(time=lambda:time.time()+offset,monotonic=time.monotonic)),patch.object(search,'_serp_on_sites',side_effect=AssertionError('No fulfilled SERP replay')),patch('engine.browser.supplier_http_get',side_effect=AssertionError('No HTTP while still in cooldown')):
   resumed=app.resume_catalogue_acquisition(key,store=self.f.store)
  self.assertEqual(resumed['status'],'running',resumed.get('error'));self.assertTrue(resumed['awaiting_supplier'])
  final=app._research_store().load_acquisition_frontier(key,0)['frontier'];pending=next(q for q in final['queries'] if q['strategy']=='own_only');self.assertEqual(pending['state'],'running');self.assertIsNone(pending['response'])
  saved=self.f.store.lookup(catalogue.identity_for_position(self.position))['coverage']['chipdip.ru'];self.assertIsNone(saved['observed_at']);self.assertEqual(saved['deferred_check']['retry_at'],check['retry_at'])
 def test_current_quote_head_date_unchanged_by_validated_health_notice(self):
  identity=catalogue.identity_for_position(self.position);admission=self.f.store.acquire_unknown(identity,self.f.hosts,owner='owner')
  source=self.f.capture(self.f.store,identity,self.position.to_result().position,'https://s0.test/card',browser=None,image_store=None,acquisition=admission,supplier_host='s0.test');before=self.f.store.lookup(identity)['heads']
  check=self.cooldown('s0.test')
  self.f.store.publish_coverage(identity.key,'s0.test',status='found',observed_at=source.observed_at,product_presence='observed',observation_id=source.observation_id,reason=catalogue.supplier_defer_reason(check),deferred_check=check,acquisition=admission)
  after=self.f.store.lookup(identity);self.assertEqual(after['heads'],before);self.assertEqual(after['coverage']['s0.test']['observed_at'],source.observed_at)
  row=app._catalogue_known_row(self.position,store=self.f.store,product=after);self.assertEqual(row['found'],1);self.assertEqual(row['source_checks'][0]['deferred_check'],check)
  self.assertIn('защиту от ботов',row['source_checks'][0]['reason'])
  import asyncio
  from io import BytesIO
  from openpyxl import load_workbook
  from engine.schemas import ResearchReportRequest
  response=app.research_report(ResearchReportRequest(object_name='Deferred source',positions=[self.position.model_dump()]))
  async def content():return b''.join([part async for part in response.body_iterator])
  book=load_workbook(BytesIO(asyncio.run(content())),data_only=True)
  values=[cell for sheet in book for line in sheet.iter_rows(values_only=True) for cell in line]
  self.assertTrue(any('защиту от ботов' in str(value) for value in values));self.assertIn(150,values)
  for field,value in [('cards',1),('retry_at',float('inf')),('reason','untrusted SDK value')]:
   corrupted=dict(check,**{field:value})
   with self.subTest(field=field),self.assertRaises(ValueError):self.f.store.publish_coverage(identity.key,'s0.test',status='deferred',deferred_check=corrupted,acquisition=admission)
 def test_own_only_saved_positive_response_never_dispatches_serp(self):
  import httpx
  root=Path(__file__).resolve().parents[2];body=(root/'sources/kac-generator/mpo-own-search-20261013/response.html').read_text();health=session.PersistentSupplierHealth(store=self.health,hosts=['electro-mpo.ru','s0.test']);step=QueryStep('"3526210" цена','exact_article','3526210');calls=[]
  def delivered(url,**kw):
   calls.append(url);return httpx.Response(200,headers={'content-type':'text/html'},text=body,request=httpx.Request('GET',url))
  with session.research_session(health),patch('engine.browser.supplier_http_get',side_effect=delivered),patch.object(search,'_serp_on_sites',side_effect=AssertionError('Own-only is never SERP')):
   result=search.search_on_sites(step.query,['electro-mpo.ru','s0.test'],query_step=step,own_only=True)
  self.assertEqual(len(calls),1);self.assertEqual(len(result),3);self.assertEqual(result.host_observations['electro-mpo.ru']['window'],'unknown')
 def test_deferred_check_roundtrip_and_corrupt_storage_is_not_unknown(self):
  import copy
  from engine.search.acquisition_frontier import new_frontier,NeutralRecords,FrontierInvalid
  check=self.cooldown('s0.test');identity=catalogue.identity_for_position(self.position);a=self.f.store.acquire_unknown(identity,self.f.hosts,owner='owner')
  self.f.store.publish_coverage(identity.key,'s0.test',status='deferred',deferred_check=check,acquisition=a)
  value=new_frontier(job_id='a'*12,input_sha256='b'*64,positions=[self.position.model_dump()],configured_hosts=self.f.hosts,index=0,product_id=identity.key,route_intent_sha256='c'*64,acquisition_generation=1);records=NeutralRecords(self.f.root.resolve()/'records',value)
  host=dict(host='s0.test',origin='supplier_own_search',window='unknown',observed_at=None,empty_code='',success=False,candidate_count=0,deferred_check=check)
  reference=records.write_response('d'*32,[],[host]);self.assertEqual(records.response(reference,'d'*32)['host_observations'][0]['deferred_check'],check)
  with self.assertRaises(FrontierInvalid):records.write_response('e'*32,[],[dict(host,success=True)])
  corrupted=copy.deepcopy(check);corrupted['headers']={'private':'not allowed'}
  with self.f.store._connection(True) as db:
   payload=json.loads(db.execute('SELECT payload FROM coverage WHERE product_id=? AND host=?',(identity.key,'s0.test')).fetchone()[0]);payload['deferred_check']=corrupted;db.execute('UPDATE coverage SET payload=? WHERE product_id=? AND host=?',(json.dumps(payload),identity.key,'s0.test'))
  with self.assertRaises(catalogue.CatalogueStoreError):self.f.store.lookup(identity)
