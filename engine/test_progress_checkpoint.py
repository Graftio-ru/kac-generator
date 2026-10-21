"""Transient progress coalescing never delays meaningful proof or stop boundaries."""
import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import engine.app as api
from engine.schemas import PositionIn

class ProgressCheckpointTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.jid='abcdef123456'
  api.RESEARCH_JOBS[self.jid]=dict(job_id=self.jid,status='running',rows=[None],total=1,completed=0,configured_hosts=['a.ru'],primary_hosts=['a.ru'],fallback_hosts=[])
  api._RESEARCH_INPUTS[self.jid]=[PositionIn(number=1,name='Item',type_mark='ABC',unit='шт',qty=1)]
  self.paths=patch.object(api,'RESEARCH_JOB_DIR',self.root);self.paths.start()
 def tearDown(self):
  store=api._RESEARCH_STORES.pop(self.root.resolve(),None)
  if store:store.close()
  for mapping in (api._RESEARCH_PUBLISHERS,api._RESEARCH_LIFECYCLES,api._RESEARCH_LIFECYCLE_RETRIES):
   for key in list(mapping):
    if key[0]==self.root.resolve():del mapping[key]
  self.paths.stop();api.RESEARCH_JOBS.pop(self.jid,None);api._RESEARCH_INPUTS.pop(self.jid,None);api._RESEARCH_CHECKPOINT_AT.pop(self.jid,None);self.temp.cleanup()
 def test_hundred_transient_updates_coalesce_and_cancel_drains_complete_trace(self):
  with patch.object(api.time,'monotonic',return_value=10):
   api._save_research_job(self.jid);publisher=api._research_publisher(self.jid)
   for n in range(100):
    publisher.publish(0,row_patch={'status':'running','cards_attempted':n},events=[{'stage':'search_api','seconds':.1,'ok':True,'label':'xmlriver'}])
   self.assertEqual(1,len(api._research_store().public(self.jid)['rows'][0]['metrics']['events']))
   api.RESEARCH_JOBS[self.jid].update(status='cancelled',cancel_requested=True);api._save_research_job(self.jid)
  saved=api._research_store().public(self.jid)
  self.assertEqual('cancelled',saved['status']);self.assertEqual(100,len(saved['rows'][0]['metrics']['events']));self.assertEqual(99,saved['rows'][0]['cards_attempted']);self.assertEqual(0,saved['completed'])
  self.assertLess(api._research_store().diagnostics()['commits'],10)
 def test_cadence_and_explicit_error_preserve_last_committed_state(self):
  with patch.object(api.time,'monotonic',return_value=10):
   api._save_research_job(self.jid);publisher=api._research_publisher(self.jid)
   publisher.publish(0,row_patch={'cards_attempted':1})
  with patch.object(api.time,'monotonic',return_value=10.99):self.assertIsNone(publisher.publish(0,row_patch={'cards_attempted':2}))
  self.assertEqual(1,api._research_store().public(self.jid)['rows'][0]['cards_attempted'])
  with patch.object(api.time,'monotonic',return_value=11):publisher.publish(0,row_patch={'cards_attempted':3})
  api.RESEARCH_JOBS[self.jid].update(status='error',error_code='llm_unavailable',error='Stopped');api._save_research_job(self.jid)
  saved=api.research_status(self.jid);self.assertEqual('error',saved['status']);self.assertEqual(3,saved['rows'][0]['cards_attempted'])
 def test_failed_commit_does_not_advance_durable_revision_exact_retry_deduplicates(self):
  api._save_research_job(self.jid);publisher=api._research_publisher(self.jid);store=api._research_store()
  store.fault=lambda point:(_ for _ in ()).throw(OSError('disk')) if point=='before_commit' else None
  from engine.research_publication import BufferedPublicationError
  with self.assertRaises(BufferedPublicationError):publisher.publish(0,row_patch={'cards_attempted':1})
  self.assertIsNone(store.public(self.jid)['rows'][0])
  store.fault=lambda _:None;publisher.flush(0)
  self.assertEqual(1,store.public(self.jid)['rows'][0]['revision'])
 def test_generic_coordinator_error_is_forced_to_disk_and_poll_does_not_restart(self):
  from contextlib import ExitStack
  cache=self.root/'cache';cache.mkdir()
  with ExitStack() as stack:
   for ctx in [patch('engine.config.CACHE_DIR',cache),patch('engine.search.source_registry.CACHE_DIR',cache),patch('engine.search.candidate_queue.CACHE_DIR',cache),patch('engine.search.shared_pipeline.run_shared_pipeline',side_effect=RuntimeError('callback failure'))]:stack.enter_context(ctx)
   api._run_research_job(self.jid)
  disk=api._load_research_job(self.jid)[0]
  self.assertEqual('error',disk['status']);self.assertIn('RuntimeError',disk['error'])
  api.RESEARCH_JOBS.pop(self.jid);api._RESEARCH_INPUTS.pop(self.jid)
  with patch.object(api,'_launch_research_job') as launch:
   self.assertEqual('error',api.research_status(self.jid)['status']);launch.assert_not_called()
 def test_generic_error_disk_failure_retains_original_cause(self):
  from contextlib import ExitStack
  cache=self.root/'cache';cache.mkdir();api._save_research_job(self.jid)
  store=api._research_store()
  def broken_pipeline(*args,**kwargs):
   store.fault=lambda point:(_ for _ in ()).throw(OSError('disk')) if point=='before_commit' else None
   raise RuntimeError('callback failure')
  with ExitStack() as stack:
   for ctx in [patch('engine.config.CACHE_DIR',cache),patch('engine.search.source_registry.CACHE_DIR',cache),patch('engine.search.candidate_queue.CACHE_DIR',cache),patch('engine.search.shared_pipeline.run_shared_pipeline',side_effect=broken_pipeline)]:stack.enter_context(ctx)
   api._run_research_job(self.jid)
  error=api.RESEARCH_JOBS[self.jid]['error'];self.assertIn('RuntimeError',error);self.assertIn('не удалось сохранить',error)
  self.assertEqual('error',api.research_status(self.jid)['status'])

class ProgressWorkerPublicationTests(unittest.TestCase):
 def test_actual_worker_callback_price_changes_and_failed_refresh_are_immediately_durable(self):
  from contextlib import ExitStack
  import time
  from engine.models import PriceOffer,Requisites
  from engine.test_price_capture import fixture_capture
  from engine.search.session import batch_card_urls,observed_quote,transport_started
  with tempfile.TemporaryDirectory() as temporary,ExitStack() as stack:
   root=Path(temporary);cache=root/'cache';cache.mkdir();jid='abcde7654321';position=PositionIn(number=1,name='Item',type_mark='ABC',unit='шт',qty=1)
   callbacks=[];cost=[]
   from engine.research_publication import DeltaPublisher
   original_publish=DeltaPublisher.publish
   def measured_publish(publisher,*args,**kwargs):
    start=time.perf_counter();result=original_publish(publisher,*args,**kwargs)
    cost.append((result is not None,time.perf_counter()-start))
    return result
   def research(pos):
    if not batch_card_urls():return dict(status='failed',found=0,offers=[],candidates=[],diagnostics=[])
    url=batch_card_urls()[0];transport_started('supplier_navigation',url)
    offer=fixture_capture(PriceOffer(url=url,product_title='Item ABC',page_article='ABC',price_with_vat=100,listed_price_with_vat=100,price_unit='шт',requisites=Requisites()),expected_mark='ABC',expected_name='Item')
    for price,failed in [(100,False),(90,False),(110,False),(110,True)]:
     value=offer.model_copy(deep=True);value.price_with_vat=price;value.listed_price_with_vat=price
     value=fixture_capture(value,expected_mark='ABC',expected_name='Item')
     # Reuse identical descriptive identity: subsequent callbacks cannot force via identity.
     value.identity_evidence=copy.deepcopy(offer.identity_evidence)
     value.refresh_failed=failed
     observed_quote(value)
     disk=api._research_store().public(jid)['rows'][0]
     candidate=next(c for c in disk['candidates'] if c['url']==url)
     callbacks.append((candidate['price_with_vat'],candidate.get('refresh_failed'),disk['found']))
    return dict(status='partial',found=0,offers=[],candidates=[value.model_dump()],diagnostics=[])
   def search(query,hosts,**kwargs):
    transport_started('search_api');return ['https://a.ru/ABC']
   for ctx in [patch.object(api,'RESEARCH_JOB_DIR',root/'jobs'),patch('engine.config.CACHE_DIR',cache),patch('engine.search.source_registry.CACHE_DIR',cache),patch('engine.search.candidate_queue.CACHE_DIR',cache),patch.object(DeltaPublisher,'publish',new=measured_publish),patch.object(api,'_research_row',side_effect=research),patch('engine.search.agent.build_queries',return_value=['literal']),patch('engine.search.search_engine.search_on_sites',side_effect=search),patch.object(api.time,'monotonic',return_value=10)]:stack.enter_context(ctx)
   api.RESEARCH_JOBS[jid]=dict(job_id=jid,status='running',rows=[None],total=1,completed=0,refresh_first=True,configured_hosts=['a.ru'],primary_hosts=['a.ru'],fallback_hosts=[])
   api._RESEARCH_INPUTS[jid]=[position]
   try:
    api._run_research_job(jid)
    self.assertEqual([100,90,110,110],[x[0] for x in callbacks]);self.assertFalse(callbacks[2][1]);self.assertTrue(callbacks[3][1]);self.assertEqual(0,callbacks[3][2])
    self.assertEqual('done',api.RESEARCH_JOBS[jid]['status']);self.assertGreaterEqual(sum(x[0] for x in cost),5)
    self.assertTrue(any(not x[0] for x in cost),'Ordinary transport publication should coalesce')
    type(self).last_measurement=dict(scope='Actual isolated coordinator callbacks, four synthetic quote transitions',calls=len(cost),writes=sum(bool(x[0]) for x in cost),suppressed=sum(not x[0] for x in cost),logical_serialized_bytes=api._research_store().diagnostics()['serialized_bytes'],publish_seconds=sum(x[1] for x in cost))
   finally:
    api.RESEARCH_JOBS.pop(jid,None);api._RESEARCH_INPUTS.pop(jid,None);api._RESEARCH_CHECKPOINT_AT.pop(jid,None)

 def test_failed_capture_postprocessing_without_identity_callback_flushes_stale_selection(self):
  from contextlib import ExitStack
  from engine.models import PriceOffer,Requisites
  from engine.test_price_capture import fixture_capture
  from engine.search.session import batch_card_urls,transport_started
  with tempfile.TemporaryDirectory() as temporary,ExitStack() as stack:
   root=Path(temporary);cache=root/'cache';cache.mkdir();jid='abcde7654322';position=PositionIn(number=1,name='Item',type_mark='ABC',unit='шт',qty=1)
   for ctx in [patch.object(api,'RESEARCH_JOB_DIR',root/'jobs'),patch('engine.config.CACHE_DIR',cache),patch('engine.search.source_registry.CACHE_DIR',cache),patch('engine.search.candidate_queue.CACHE_DIR',cache),patch('engine.search.agent.build_queries',return_value=['literal']),patch('engine.search.search_engine.search_on_sites',return_value=[]),patch.object(api.time,'monotonic',return_value=10)]:stack.enter_context(ctx)
   candidate=api._offer_to_dict(fixture_capture(PriceOffer(url='https://a.ru/ABC',product_title='Item ABC',page_article='ABC',price_with_vat=100,listed_price_with_vat=100,price_unit='шт',requisites=Requisites()),expected_mark='ABC',expected_name='Item'),'ABC')
   def research(pos):
    # Exact same nested candidate object survives from seed into failed refresh.
    if batch_card_urls():
     transport_started('supplier_navigation',batch_card_urls()[0]);candidate['unit_review_required']=True
    return dict(status='partial',found=1,offers=[candidate],candidates=[candidate],diagnostics=[])
   from engine.research_publication import DeltaPublisher
   original=DeltaPublisher.publish;forced_failure=[]
   def measured_publish(publisher,*args,**kwargs):
    result=original(publisher,*args,**kwargs)
    disk=publisher.store.public(publisher.job)['rows'][0]
    if disk and disk.get('candidates') and disk['candidates'][0].get('refresh_failed'):
     forced_failure.append((result is not None,disk['found'],disk['candidates'][0]['refresh_failed']))
    return result
   stack.enter_context(patch.object(api,'_research_row',side_effect=research));stack.enter_context(patch.object(DeltaPublisher,'publish',new=measured_publish))
   api.RESEARCH_JOBS[jid]=dict(job_id=jid,status='running',rows=[None],total=1,completed=0,refresh_first=True,configured_hosts=['a.ru'],primary_hosts=['a.ru'],fallback_hosts=[]);api._RESEARCH_INPUTS[jid]=[position]
   try:
    api._run_research_job(jid)
    self.assertEqual('done',api.RESEARCH_JOBS[jid]['status'],api.RESEARCH_JOBS[jid].get('error'))
    self.assertIn((True,0,True),forced_failure,'Failed capture must force transient public state before terminal save')
   finally:
    api.RESEARCH_JOBS.pop(jid,None);api._RESEARCH_INPUTS.pop(jid,None);api._RESEARCH_CHECKPOINT_AT.pop(jid,None)
