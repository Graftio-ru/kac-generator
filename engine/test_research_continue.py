"""Explicit HTTP continuation preserves native CAS and supersession cancellation."""
import time, types, unittest
from contextlib import ExitStack
from unittest.mock import patch
from fastapi.testclient import TestClient
from engine import app, test_catalogue_unknown as fixtures
from engine.schemas import PositionIn
from engine.search import catalogue, session
from engine.search.catalogue import identity_for_position
from engine.search.article_query_plan import build_article_query_plan
from engine.search.discovery_plan import StagedDiscoveryPlan, expand_discovery_tasks

REAL_SEND = None
class ResearchContinueTests(unittest.TestCase):
 def setUp(self):
  self.f=fixtures.CatalogueUnknownTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.stack=ExitStack();self.addCleanup(self.stack.close)
  for attr,value in [('RESEARCH_JOBS',{}),('_RESEARCH_INPUTS',{}),('_RESEARCH_THREADS',set()),('_RESEARCH_CONTINUATIONS',{}),('_ACTIVE_RESEARCH_ID',None)]:self.stack.enter_context(patch.object(app,attr,value))
  self.stack.enter_context(patch.object(app.settings,'research_job_seconds',30))
  self.p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=2)
  with patch.object(app,'_launch_research_job'),patch('engine.search.whitelist.whitelist_hosts',return_value=self.f.hosts):self.jid=app._start_research_job([self.p])
  job=app.RESEARCH_JOBS[self.jid];job['_indexed_acquisition']=True
  self.identity=identity_for_position(self.p);admission=self.f.store.acquire_unknown(self.identity,self.f.hosts,owner=self.jid)
  plan=build_article_query_plan(self.p.name,self.p.type_mark,article=self.identity.code,manufacturer=self.identity.manufacturer,unit=self.p.unit)
  planner=StagedDiscoveryPlan(plan.queries,self.f.hosts,[],strong_article=True)
  tasks=expand_discovery_tasks(planner.initial(),{s.query:s for s in plan.steps})
  controller,_,_=app._catalogue_checkpoint_controller(self.jid,0,admission,self.f.store,planner,tasks)
  self.assertEqual(controller.value['version'],5)
  job.update(status='running',awaiting_supplier=True,awaiting_supplier_retry_at=time.time()-1)
  app._save_research_job(self.jid,force=True)
  self.real_time=time.time;self.virtual=types.SimpleNamespace(time=lambda:self.real_time()+121,monotonic=time.monotonic)
  self.stack.enter_context(patch.object(catalogue,'time',self.virtual));self.stack.enter_context(patch.object(app,'time',self.virtual))
  import httpx
  def send(client,request,**kw):
   if not isinstance(client._transport,__import__('starlette.testclient',fromlist=['_TestClientTransport'])._TestClientTransport):raise AssertionError('Only in-process HTTP')
   return REAL_SEND(client,request,**kw)
  self.stack.enter_context(patch.object(httpx.Client,'send',send));self.client=TestClient(app.app,raise_server_exceptions=False)
 def post(self,**kw):return self.client.post('/research/'+self.jid+'/continue',**kw)
 def test_real_native_cas_context_and_same_job(self):
  seen=[]
  def consumer(jid,store,*,resumed):seen.append((jid,session.research_deadline(),resumed[self.identity.key][0].generation));session.bounded_timeout(1)
  before=time.monotonic()
  with patch.object(app,'_run_catalogue_unknown_job',side_effect=consumer),patch.object(self.f.store,'claim_acquisition',wraps=self.f.store.claim_acquisition) as claim:r=self.post(json={})
  self.assertEqual(r.status_code,200,r.text);self.assertEqual(seen[0][0],self.jid);self.assertEqual(seen[0][2],2)
  self.assertGreaterEqual(seen[0][1],before+29);self.assertLessEqual(seen[0][1],time.monotonic()+30)
  self.assertEqual(claim.call_args.kwargs['expected_owner'],self.jid);self.assertNotIn(self.jid,app._RESEARCH_THREADS);self.assertEqual(app._RESEARCH_CONTINUATIONS,{})
  self.assertIsNone(session.research_deadline());self.assertIsNone(app._ACTIVE_RESEARCH_ID)
 def test_native_live_lease_refusal_and_no_claim(self):
  with patch.object(catalogue,'time',types.SimpleNamespace(time=self.real_time)),patch.object(app,'time',types.SimpleNamespace(time=self.real_time,monotonic=time.monotonic)),patch.object(self.f.store,'claim_acquisition',wraps=self.f.store.claim_acquisition) as claim:r=self.post()
  self.assertEqual(r.status_code,409);claim.assert_not_called();self.assertEqual(self.f.store.lookup(self.identity)['acquisition']['generation'],1)
 def test_body_and_pause_controls_refuse_before_native(self):
  with patch.object(app,'resume_catalogue_acquisition') as native:
   self.assertEqual(self.post(json={'budget':999}).status_code,422)
   for field,value in [('awaiting_provider',True),('awaiting_supplier',False),('awaiting_supplier_retry_at',self.real_time()+999),('awaiting_supplier_retry_at',None),('cancel_requested',True)]:
    job=app.RESEARCH_JOBS[self.jid];old=job.get(field);job[field]=value;app._save_research_job(self.jid,force=True)
    self.assertEqual(self.post().status_code,409,(field,value));job[field]=old
    # Durable cancellation/status is intentionally sticky; use last cases only.
    if field in ('cancel_requested','status','error_code'):break
   native.assert_not_called()
 def test_invalid_budget_unknown_id_and_no_legacy_adoption(self):
  with patch.object(app,'resume_catalogue_acquisition') as native:
   for budget in (0,-1,float('inf'),float('nan')):
    with patch.object(app.settings,'research_job_seconds',budget):self.assertEqual(self.post().status_code,409)
   with patch.object(app,'_load_research_job',side_effect=AssertionError('No legacy adoption')):self.assertEqual(self.client.post('/research/000000000000/continue').status_code,404)
   native.assert_not_called()
 def test_pending_reservation_rejects_duplicate_and_other_job(self):
  def native(*a,**kw):
   self.assertEqual(app._ACTIVE_RESEARCH_ID,self.jid)
   self.assertEqual(self.post().status_code,409)
   return {'job_id':self.jid}
  with patch.object(app,'resume_catalogue_acquisition',side_effect=native):self.assertEqual(self.post().status_code,200)
  app._RESEARCH_THREADS.add('other')
  with patch.object(app,'resume_catalogue_acquisition') as native:self.assertEqual(self.post().status_code,409);native.assert_not_called()
 def test_durable_cancel_checker_control_and_cleanup(self):
  def native(*a,**kw):
   app.RESEARCH_JOBS[self.jid]['cancel_requested']=True;app._save_research_job(self.jid,force=True)
   session.bounded_timeout(1);raise AssertionError('Cancelled work continued')
  with patch.object(app,'resume_catalogue_acquisition',side_effect=native):self.assertEqual(self.post().status_code,409)
  self.assertTrue(app._research_store().runtime_snapshot(self.jid)['cancel_requested']);self.assertEqual(app._RESEARCH_CONTINUATIONS,{})
 def test_new_parse_pre_native_and_replacement_cancel_fence(self):
  for replacement in (False,True):
   # Each race gets a fresh real native job/header because cancellation is sticky.
   if replacement:
    self.doCleanups();self.setUp()
   new=[]
   def native(*a,**kw):
    old=dict(app.RESEARCH_JOBS[self.jid])
    def launch(key):
     app._RESEARCH_THREADS.add(key);app._ACTIVE_RESEARCH_ID=key
    with patch.object(app,'_launch_research_job',side_effect=launch),patch('engine.search.whitelist.whitelist_hosts',return_value=self.f.hosts):new.append(app._start_research_job([self.p]))
    if replacement:app.RESEARCH_JOBS[self.jid]=old;app._save_research_job(self.jid,force=True)
    session.bounded_timeout(1);raise AssertionError('Superseded work continued')
   with patch.object(app,'resume_catalogue_acquisition',side_effect=native):self.assertEqual(self.post().status_code,409)
   self.assertTrue(app._research_store().runtime_snapshot(self.jid)['cancel_requested']);self.assertEqual(app._ACTIVE_RESEARCH_ID,new[0]);self.assertEqual(app._RESEARCH_CONTINUATIONS,{})
 def test_error_terminal_firststop_and_active_worker_refuse(self):
  for field,value in [('status','error'),('error_code','search_service_unavailable'),('first_stop',{'code':'search_service_unavailable','reason':'Fixed control'})]:
   with self.subTest(field=field):
    if field!='status':self.doCleanups();self.setUp()
    job=app.RESEARCH_JOBS[self.jid];job[field]=value;app._save_research_job(self.jid,force=True)
    with patch.object(app,'resume_catalogue_acquisition') as native:self.assertEqual(self.post().status_code,409);native.assert_not_called()
  self.doCleanups();self.setUp();app._RESEARCH_THREADS.add(self.jid)
  with patch.object(app,'resume_catalogue_acquisition') as native:self.assertEqual(self.post().status_code,409);native.assert_not_called()
 def test_native_wrong_owner_refused_without_reclaim(self):
  with self.f.store._connection(write=True) as db:db.execute('UPDATE acquisitions SET owner=? WHERE product_id=?',('another-owner',self.identity.key))
  with patch.object(self.f.store,'claim_acquisition',wraps=self.f.store.claim_acquisition) as claim:r=self.post()
  self.assertEqual(r.status_code,409);claim.assert_not_called();self.assertTrue(self.f.store.acquisition_owner_matches(self.identity.key,owner='another-owner',expected_identity=self.identity))
 def test_deadline_control_stops_request_and_restores_context(self):
  def native(*a,**kw):
   deadline=session.research_deadline()
   with patch.object(session,'time',types.SimpleNamespace(monotonic=lambda:deadline+1)):
    session.bounded_timeout(1)
   raise AssertionError('Expired work continued')
  with patch.object(app,'resume_catalogue_acquisition',side_effect=native):self.assertEqual(self.post().status_code,409)
  self.assertIsNone(session.research_deadline());self.assertEqual(app._RESEARCH_CONTINUATIONS,{})
 def test_native_source_intent_refusal_does_not_claim(self):
  from engine.research_store import Conflict
  with patch.object(app,'_verify_catalogue_source_intents',side_effect=Conflict('PRIVATE SOURCE')),patch.object(self.f.store,'claim_acquisition',wraps=self.f.store.claim_acquisition) as claim:r=self.post()
  self.assertEqual(r.status_code,409);self.assertNotIn('PRIVATE',r.text);claim.assert_not_called()
 def test_unexpected_failure_not_claimed_as_native_refusal(self):
  with patch.object(app,'resume_catalogue_acquisition',side_effect=RuntimeError('PRIVATE')):r=self.post()
  self.assertEqual(r.status_code,500);self.assertNotIn('PRIVATE',r.text);self.assertEqual(app._RESEARCH_CONTINUATIONS,{})

 def test_inherited_shorter_deadline_through_http(self):
  ancestor=time.monotonic()+5;seen=[]
  def consumer(*a,**kw):seen.append(session.research_deadline())
  with session.research_session(deadline=ancestor):
   with patch.object(app,'_run_catalogue_unknown_job',side_effect=consumer):r=self.post(json={})
   self.assertEqual(session.research_deadline(),ancestor)
  self.assertEqual(r.status_code,200,r.text);self.assertEqual(seen,[ancestor]);self.assertIsNone(session.research_deadline())
 def test_configured_shorter_window_through_http(self):
  start=time.monotonic();ancestor=start+60;seen=[]
  with session.research_session(deadline=ancestor):
   with patch.object(app,'_run_catalogue_unknown_job',side_effect=lambda *a,**kw:seen.append(session.research_deadline())):r=self.post()
   self.assertEqual(session.research_deadline(),ancestor)
  self.assertEqual(r.status_code,200,r.text);self.assertGreaterEqual(seen[0],start+29);self.assertLessEqual(seen[0],time.monotonic()+30);self.assertLess(seen[0],ancestor)
 def test_expired_ancestor_refuses_before_native_claim(self):
  ancestor=time.monotonic()-1
  with session.research_session(deadline=ancestor):
   with patch.object(app,'resume_catalogue_acquisition',wraps=app.resume_catalogue_acquisition) as native,patch.object(self.f.store,'claim_acquisition',wraps=self.f.store.claim_acquisition) as claim:r=self.post()
   self.assertEqual(session.research_deadline(),ancestor)
  self.assertEqual(r.status_code,409,r.text);native.assert_not_called();claim.assert_not_called();self.assertEqual(app._RESEARCH_CONTINUATIONS,{});self.assertIsNone(app._ACTIVE_RESEARCH_ID);self.assertNotIn(self.jid,app._RESEARCH_THREADS)
