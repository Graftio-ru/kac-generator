"""Native scheduler early-proof regression; owned stores and inert captures only."""
import json,time,unittest
from unittest.mock import patch
from engine import app
from engine.schemas import PositionIn
from engine.test_catalogue_unknown import CatalogueUnknownTests
from engine.test_selected_catalogue_proofs import SelectedProofTests
from engine.search.catalogue import identity_for_position
from engine.search.search_engine import DiscoveryURLs
from engine.search.catalogue_projection import catalogue_scope,project_product
from engine.search.selected_proofs import price_candidates,proof_ready

class EarlySelectedProofTests(unittest.TestCase):
 def setUp(self):
  self.f=CatalogueUnknownTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.f.hosts=[f's{i}.test' for i in range(4)]
  self.p=PositionIn(number=1,name='Изделие',type_mark='ABC123',unit='шт',qty=2)
  self.identity=identity_for_position(self.p);self.store=self.f.store;self.root=self.f.root;self.hosts=self.f.hosts;self.sources={}
  cache=patch('engine.config.CACHE_DIR',self.root);cache.start();self.addCleanup(cache.stop)
  self.events=[];self.photos=[];self.photo_failure=False;self.limit=4;self.cached=False;self.stop=None
 def source(self,index,price,photo=False):
  return SelectedProofTests.source(self,index,price,photo=photo)
 def search(self,q,hosts,**kwargs):
  urls=[f'https://{h}/product/ABC123' for h in hosts if self.hosts.index(h)<self.limit]
  return DiscoveryURLs(urls,host_observations={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=int(self.hosts.index(h)<self.limit),diagnostics=[]) for h in hosts})
 def capture(self,store,identity,p,url,**kwargs):
  self.admission=kwargs['acquisition'];index=self.hosts.index(kwargs['supplier_host']);photo=kwargs.get('photo_only',False)
  self.events.append(('photo' if photo else 'price',index))
  if photo:
   self.photos.append(index)
   job=next(j for j in app.RESEARCH_JOBS.values() if j.get('status')=='running' and j.get('configured_hosts')==self.hosts)
   saved=app._research_store().load_acquisition_frontier(job['job_id'],0)['frontier']
   self.assertFalse(saved['frontier_complete']);self.assertNotEqual(saved['planning_phase'],'complete')
   self.assertIn(url,job['catalogue_proof_attempts'][identity.key])
   if self.stop:self.stop(job)
   if self.photo_failure:return None
  return self.source(index,100*(index+1),photo=photo or self.cached)
 def run_job(self):return self.f.job([self.p],search=self.search,capture=self.capture)
 def test_three_prices_proved_before_unsettled_fourth_card_then_new_selection(self):
  job,public=self.run_job()
  self.assertEqual(public['status'],'done',public.get('error'))
  self.assertLess(self.events.index(('photo',0)),self.events.index(('price',3)))
  self.assertEqual(sorted(self.photos),[0,1,2,3]);self.assertEqual(len(self.photos),len(set(self.photos)))
  with catalogue_scope(self.store):
   selected=price_candidates(project_product(self.store,self.identity,self.p.to_result().position))
   self.assertEqual(len(selected),3);self.assertTrue(all(proof_ready(o,self.p.to_result().position) for o in selected))
  self.assertEqual(public['rows'][0]['proof_ready_sources'],3)
 def test_two_prices_do_not_trigger_early_proof(self):
  self.limit=2
  # The existing final pass for a shortfall is retained; no early pass occurs.
  def capture(store,identity,p,url,**kwargs):
   if kwargs.get('photo_only'):
    job=next(j for j in app.RESEARCH_JOBS.values() if j.get('status')=='running' and j.get('configured_hosts')==self.hosts)
    frontier=app._research_store().load_acquisition_frontier(job['job_id'],0)['frontier']
    self.assertTrue(all(q['state']=='done' for q in frontier['queries']))
    self.photos.append(self.hosts.index(kwargs['supplier_host']));return None
   return self.capture(store,identity,p,url,**kwargs)
  job,public=self.f.job([self.p],search=self.search,capture=capture)
  self.assertEqual(public['status'],'done',public.get('error'));self.assertEqual(public['rows'][0]['comparable_sources'],2);self.assertEqual(len(self.photos),2)
 def test_ambiguous_photo_never_repeated_across_candidates_and_final_pass(self):
  self.photo_failure=True
  job,public=self.run_job()
  self.assertEqual(public['status'],'done',public.get('error'));self.assertEqual(sorted(self.photos),[0,1,2,3]);self.assertEqual(public['rows'][0]['proof_ready_sources'],0)
  self.assertEqual(len(job['catalogue_proof_attempts'][self.identity.key]),4)
 def test_current_cached_images_are_not_recaptured(self):
  self.cached=True
  job,public=self.run_job();self.assertEqual(public['status'],'done',public.get('error'));self.assertEqual(self.photos,[]);self.assertEqual(public['rows'][0]['proof_ready_sources'],3)
 def test_cancellation_during_photo_prevents_following_capture_and_publication(self):
  from engine.search.session import bounded_timeout
  def stop(job):job['cancel_requested']=True;bounded_timeout(0)
  self.stop=stop
  job,public=self.run_job();self.assertEqual(public['status'],'cancelled',public.get('error'));self.assertEqual(len(self.photos),1)
  with catalogue_scope(self.store):self.assertFalse(any(proof_ready(o,self.p.to_result().position) for o in project_product(self.store,self.identity,self.p.to_result().position).all_offers))
 def test_global_stop_during_photo_prevents_following_capture(self):
  from engine.llm import _REQUEST_GUARD
  from engine.search.failure_policy import SearchServiceUnavailable
  def stop(job):failure=SearchServiceUnavailable('account_balance');_REQUEST_GUARD.get().reject(failure);raise failure
  self.stop=stop
  job,public=self.run_job();self.assertEqual(public['status'],'error');self.assertEqual(len(self.photos),1);self.assertIsNotNone(public.get('first_stop'))

 def test_queued_proof_slice_boundary_does_not_consume_attempt(self):
  from types import SimpleNamespace
  from engine.search import acquisition_scheduler
  clock=[time.monotonic()];operation=acquisition_scheduler.Operation
  class BoundaryOperation(operation):
   def __init__(self,kind,hosts,call,before=lambda:True):
    super().__init__(kind,hosts,call,before)
    if kind=='proof':clock[0]+=2
  with patch.object(app,'time',SimpleNamespace(time=time.time,monotonic=lambda:clock[0])),patch.object(app,'_acquisition_slice_seconds',return_value=1),patch.object(acquisition_scheduler,'Operation',BoundaryOperation):
   job,public=self.run_job()
  self.assertTrue(public.get('awaiting_slice'),public);self.assertEqual(self.photos,[]);self.assertFalse(job.get('catalogue_proof_attempts'))
  self.assertFalse(app._research_store().load_acquisition_frontier(job['job_id'],0)['frontier']['frontier_complete'])
 def test_lost_owner_during_photo_has_no_image_publication(self):
  def stop(job):
   self.store.cancel_acquisition(self.admission,reason='synthetic lost owner')
   from engine.search.session import bounded_timeout
   bounded_timeout(0)
  self.stop=stop
  job,public=self.run_job();self.assertEqual(len(self.photos),1);self.assertNotEqual(public['status'],'done')
  self.assertTrue(all(h['current']['image']['status']=='missing' for h in self.store.lookup(self.identity)['heads']))
 def test_deadline_during_photo_has_no_following_request(self):
  from engine.search import session
  def stop(job):session._DEADLINE.set(time.monotonic()-1);session.bounded_timeout(0)
  self.stop=stop
  job,public=self.run_job();self.assertEqual(len(self.photos),1);self.assertTrue(all(h['current']['image']['status']=='missing' for h in self.store.lookup(self.identity)['heads']))
 def test_changed_financial_fact_rejected_by_current_projection(self):
  from dataclasses import replace
  native=self.source;append=self.store.append_observation
  def source(index,price,photo=False):
   def publish(product,value,**kwargs):
    if index==2:value=replace(value,sale_unit='уп')
    return append(product,value,**kwargs)
   with patch.object(self.store,'append_observation',side_effect=publish):
    return native(index,price,photo)
  self.source=source;self.limit=3
  # No third admissible quote means photos only at the historical final pass.
  def capture(*args,**kwargs):
   if kwargs.get('photo_only'):
    job=next(j for j in app.RESEARCH_JOBS.values() if j.get('status')=='running' and j.get('configured_hosts')==self.hosts)
    self.assertTrue(all(q['state']=='done' for q in app._research_store().load_acquisition_frontier(job['job_id'],0)['frontier']['queries']))
    return None
   return self.capture(*args,**kwargs)
  job,public=self.f.job([self.p],search=self.search,capture=capture)
  self.assertEqual(public['rows'][0]['comparable_sources'],2)

 def test_saved_attempt_ledger_survives_fresh_generator(self):
  from types import SimpleNamespace
  from engine.search import search_engine,catalogue_producer
  clock=[time.monotonic()];capture=self.capture;self.photo_failure=True
  def stop_after_old_selected(*args,**kwargs):
   value=capture(*args,**kwargs)
   if not kwargs.get('photo_only') and kwargs['supplier_host']=='s3.test':clock[0]+=2
   return value
  with patch.object(app,'time',SimpleNamespace(time=time.time,monotonic=lambda:clock[0])),patch.object(app,'_acquisition_slice_seconds',return_value=1):
   job,public=self.f.job([self.p],search=self.search,capture=stop_after_old_selected)
  self.assertTrue(public.get('awaiting_slice'));self.assertEqual(sorted(self.photos),[0,1,2])
  key=job['job_id'];saved=app._research_store().runtime_snapshot(key,include_events=False)
  frontier=app._research_store().load_acquisition_frontier(key,0)['frontier']
  self.assertEqual(len(saved['catalogue_proof_attempts'][self.identity.key]),3)
  # Discard the process-local job object; reconstruct from native saved bytes.
  app.RESEARCH_JOBS[key]=saved
  with patch.object(search_engine,'search_on_sites',side_effect=self.search),patch.object(catalogue_producer,'capture_and_publish',side_effect=capture):
   app._run_catalogue_unknown_job(key,self.store,resumed={self.identity.key:(self.admission,frontier)})
  self.assertEqual(sorted(self.photos),[0,1,2,3]);self.assertEqual(app.RESEARCH_JOBS[key]['status'],'done')

 def test_total_six_attempt_budget_is_not_reset_for_new_ranked_source(self):
  saved=app._save_research_job
  exhausted=[f'https://old{i}.test/product/ABC123' for i in range(6)]
  def save(key,*args,**kwargs):
   app.RESEARCH_JOBS[key].setdefault('catalogue_proof_attempts',{self.identity.key:exhausted})
   return saved(key,*args,**kwargs)
  with patch.object(app,'_save_research_job',side_effect=save):job,public=self.run_job()
  self.assertEqual(public['status'],'done');self.assertEqual(self.photos,[]);self.assertEqual(job['catalogue_proof_attempts'][self.identity.key],exhausted)
