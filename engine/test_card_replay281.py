"""Cross-job native uncertain card admission; inert stores and captures only."""
import hashlib,json,time,unittest,uuid
from unittest.mock import patch
from engine import app
from engine.llm import LLMOperationFailed
from engine.schemas import PositionIn
from engine.telemetry import record_stage
from engine.search.catalogue import identity_for_position,CatalogueConflict
from engine.search.catalogue import definitely_presend,import_retained_uncertainty
from engine.search.search_engine import DiscoveryURLs
from engine.test_catalogue_unknown import CatalogueUnknownTests

def event(*,sent=True,presend=False):
 now=time.time()
 value=dict(logical_id=uuid.uuid4().hex,provider='cloudru',ordinal=1,phase='endpoint_response' if sent else 'tcp_connect',outcome='error',exception_type='ReadTimeout' if sent else 'ConnectTimeout',observed_at=now,elapsed_seconds=.1,endpoint_send_started=sent,proven_presend=presend,attempt_started_at=now-.1)
 return dict(stage='provider_transport',seconds=.1,ok=False,label=json.dumps(value))

class CardReplayTests(unittest.TestCase):
 def setUp(self):
  self.f=CatalogueUnknownTests();self.f.setUp();self.addCleanup(self.f.doCleanups);self.f.hosts=['s0.test','s1.test','s2.test'];self.allowed={0,1};self.calls=[];self.presend=False
  self.p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=2)
 def search(self,q,hosts,**kwargs):
  urls=[f'https://{h}/card' for h in hosts if self.f.hosts.index(h) in self.allowed]
  return DiscoveryURLs(urls,host_observations={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=int(self.f.hosts.index(h) in self.allowed),diagnostics=[]) for h in hosts})
 def capture(self,*args,**kwargs):
  if kwargs.get('photo_only'):return None
  self.calls.append(args[3])
  if kwargs['supplier_host']=='s0.test':
   value=event(sent=not self.presend,presend=self.presend);record_stage('provider_transport',.1,ok=False,label=value['label']);raise LLMOperationFailed('transport_error')
  return self.f.capture(*args,**kwargs)
 def job(self,position=None):
  save=app._save_research_job
  def missing(key,*args,**kwargs):
   if app.RESEARCH_JOBS[key]['status']=='pending':app.RESEARCH_JOBS[key]['acquisition_mode']='missing_data'
   return save(key,*args,**kwargs)
  with patch.object(app,'_save_research_job',side_effect=missing):return self.f.job([position or self.p],search=self.search,capture=self.capture)
 def test_new_job_skips_same_unknown_but_reuses_price_and_processes_new_url(self):
  first,public=self.job();self.assertEqual(public['status'],'done',public.get('error'));self.assertIn('https://s0.test/card',self.calls)
  self.allowed={0,1,2};self.calls=[]
  second,public=self.job();self.assertEqual(public['status'],'done',public.get('error'));self.assertEqual(self.calls,['https://s2.test/card'])
  check=next(c for c in public['rows'][0]['source_checks'] if c['host']=='s0.test');self.assertEqual(check['product_presence'],'unknown');self.assertNotEqual(check['status'],'no_match')
  product=self.f.store.lookup(identity_for_position(self.p));self.assertEqual(len(product['heads']),2)
 def test_proven_presend_all_attempts_allows_later_job(self):
  self.presend=True;self.job();self.calls=[];self.job();self.assertIn('https://s0.test/card',self.calls)
 def test_mixed_postsend_and_presend_does_not_resolve(self):
  self.assertFalse(definitely_presend([event(),event(sent=False,presend=True)]));self.assertTrue(definitely_presend([event(sent=False,presend=True)]));self.assertFalse(definitely_presend([]))
 def test_supplier_admission_refusal_does_not_reserve_owned_card(self):
  """A cooldown and a held probe defer their cards while a healthy supplier still runs."""
  from engine.search.session import supplier_transport
  from engine.search.supplier_health_store import Outcome, SupplierHealthStore

  wall = time.time()
  health = SupplierHealthStore(self.f.root/'preflight-health.sqlite3', cooldown_seconds=3600,
                               clock=lambda: wall)
  self.assertTrue(health.finish(health.acquire('s0.test'),Outcome.HTTP_RATE_LIMITED))
  held = health.acquire('s1.test')
  self.assertTrue(held.allowed)
  self.allowed = {0,1,2}
  capture_entries = []

  def capture_available(*args, **kwargs):
   """Place the fixture capture behind the same reentrant health gate as the native adapter."""
   capture_entries.append(kwargs['supplier_host'])
   with supplier_transport(args[3]):
    return self.f.capture(*args, **kwargs)

  self.capture = capture_available
  positions = (self.p,self.p.model_copy(update={'number':2,'name':'Other cable','type_mark':'DEF456'}))
  with patch('engine.search.supplier_health_store.SupplierHealthStore',return_value=health), \
       patch.object(health,'acquire',wraps=health.acquire) as acquire:
   for position in positions:
    _, public = self.job(position)
    product = self.f.store.lookup(identity_for_position(position))
    self.assertEqual({head['host'] for head in product['heads']},{'s2.test'})
    for host in ('s0.test','s1.test'):
     self.assertFalse(any(row.get('card_operation') and row['host']==host
                          for row in product['refresh_attempts']))
     sources = [check for check in public['rows'][0]['source_checks'] if check['host']==host]
     self.assertEqual(sources[0]['product_presence'],'unknown')
     self.assertNotEqual(sources[0]['status'],'no_match')
  self.assertEqual(capture_entries,['s2.test','s2.test'])
  self.assertEqual(sum(call.args==('s2.test',) for call in acquire.call_args_list),2)
  self.assertTrue(health.finish(held,Outcome.UNKNOWN))
 def test_lost_card_reservation_ack_preserves_pending(self):
  """A committed reservation with a lost reply cannot be resolved or captured automatically."""
  reserve = self.f.store.card_operation
  calls_before = []

  def lose_ack(*args, **kwargs):
   """Commit the real journal record, then interrupt its acknowledgement."""
   result = reserve(*args, **kwargs)
   if kwargs.get('host') == 's0.test' and result['allowed'] and not kwargs.get('resolve'):
    calls_before.append(kwargs['operation_id'])
    raise RuntimeError('Fixture lost journal acknowledgement')
   return result

  self.allowed={0}
  with patch.object(self.f.store,'card_operation',side_effect=lose_ack):
   self.job()
  self.assertEqual(self.calls,[])
  self.assertEqual(len(calls_before),1)
  product = self.f.store.lookup(identity_for_position(self.p))
  operations = [row['card_operation'] for row in product['refresh_attempts']
                if row.get('card_operation',{}).get('operation_id') in calls_before]
  self.assertEqual([operation['state'] for operation in operations],['pending'])
 def test_different_product_is_not_blanket_blocked(self):
  self.job();self.calls=[];self.job(self.p.model_copy(update={'type_mark':'DEF456'}));self.assertIn('https://s0.test/card',self.calls)
 def test_legacy_import_uses_native_bound_outcome_without_transport_attribution(self):
  native=self.f.store.card_operation
  with patch.object(self.f.store,'card_operation',return_value={'allowed':True,'pending':[]}):first,public=self.job()
  audit=import_retained_uncertainty(self.f.store,app._research_store(),job_ids=[first['job_id']])
  self.assertTrue(any(v['url']=='https://s0.test/card' and v['evidence']['basis']=='retained_card_outcome_unknown' for v in audit),audit)
  self.calls=[];self.job();self.assertNotIn('https://s0.test/card',self.calls)
 def test_atomic_pending_admission_and_invalid_resolution(self):
  identity=identity_for_position(self.p);a=self.f.store.acquire_unknown(identity,self.f.hosts,owner='fixture');url='https://s0.test/card'
  first=self.f.store.card_operation(identity.key,'s0.test',url,operation_id='1'*32,acquisition=a)
  second=self.f.store.card_operation(identity.key,'s0.test',url,operation_id='2'*32,acquisition=a)
  self.assertTrue(first['allowed']);self.assertFalse(second['allowed'])
  with self.assertRaises(CatalogueConflict):self.f.store.card_operation(identity.key,'s0.test',url,operation_id='1'*32,acquisition=a,resolve=True,evidence={'basis':'observation'})
  self.f.store.cancel_acquisition(a,reason='fixture')
  with self.assertRaises(CatalogueConflict):self.f.store.card_operation(identity.key,'s1.test','https://s1.test/new',operation_id='3'*32,acquisition=a)
 def retained_current(self,committed=False):
  from engine.test_catalogue_acquisition_resume import ResumeTests
  fixture=ResumeTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
  task=fixture.responses();fixture.checkpoint(fixture.controller.start_card(task));card=fixture.controller._task(task)['current_card']
  if committed:fixture.f.capture(fixture.f.store,fixture.identity,fixture.position.to_result().position,card['url'],browser=None,image_store=None,acquisition=fixture.admission,supplier_host=card['host'],observation_id=card['operation_id'])
  receipt={}
  for role,data in [('parent',{'child_reaped':True,'cleanup_verified':True}),('child',{'job_id':fixture.job})]:
   path=fixture.f.root/(role+'.json');raw=json.dumps(data).encode();path.write_bytes(raw);receipt[role+'_path']=str(path);receipt[role+'_sha256']=hashlib.sha256(raw).hexdigest()
  return fixture,card,{fixture.job:receipt}
 def test_terminal_current_unknown_and_committed_observation_distinguished(self):
  fixture,card,receipts=self.retained_current()
  report=import_retained_uncertainty(fixture.f.store,app._research_store(),job_ids=[fixture.job],terminal_receipts=receipts)
  self.assertEqual(len(report),1);self.assertEqual(report[0]['evidence']['basis'],'terminal_current_card')
  fixture,card,receipts=self.retained_current(committed=True)
  report=import_retained_uncertainty(fixture.f.store,app._research_store(),job_ids=[fixture.job],terminal_receipts=receipts)
  self.assertEqual(report[0]['state'],'already_observed');self.assertFalse(any('card_operation' in a for a in fixture.f.store.lookup(fixture.identity)['refresh_attempts']))
 def test_changed_retained_terminal_receipt_refused(self):
  fixture,card,receipts=self.retained_current();receipts[fixture.job]['child_sha256']='0'*64
  with self.assertRaises(ValueError):import_retained_uncertainty(fixture.f.store,app._research_store(),job_ids=[fixture.job],terminal_receipts=receipts)
 def test_atomic_competing_admission_one_pending_only(self):
  from concurrent.futures import ThreadPoolExecutor
  identity=identity_for_position(self.p);a=self.f.store.acquire_unknown(identity,self.f.hosts,owner='fixture')
  def admit(i):return self.f.store.card_operation(identity.key,'s0.test','https://s0.test/race',operation_id=f'{i:032x}',acquisition=a)['allowed']
  with ThreadPoolExecutor(max_workers=4) as pool:answers=list(pool.map(admit,range(1,9)))
  self.assertEqual(sum(answers),1)
 def test_unrelated_older_transport_not_attributed_to_budget_card(self):
  native=self.capture
  def capture(*args,**kwargs):
   if kwargs.get('photo_only'):return None
   self.calls.append(args[3])
   if kwargs['supplier_host']=='s0.test':
    from engine.search.session import SearchBudgetExceeded
    raise SearchBudgetExceeded('synthetic pre-model local budget')
   return self.f.capture(*args,**kwargs)
  self.capture=capture
  with patch.object(self.f.store,'card_operation',return_value={'allowed':True,'pending':[]}):first,public=self.job()
  # A previous unrelated provider event in this same row is not a source URL binding.
  store=app._research_store();raw=json.dumps(event()).encode()
  with store.lock:store.db.execute('INSERT INTO trace VALUES(?,?,?,?,?,?)',(first['job_id'],0,999,'f'*32,0,raw.decode()))
  report=import_retained_uncertainty(self.f.store,store,job_ids=[first['job_id']])
  barrier=next(v for v in report if v['url']=='https://s0.test/card')
  self.assertEqual(barrier['evidence']['basis'],'retained_card_outcome_unknown');self.assertEqual(barrier['evidence']['submission_stage'],'unknown');self.assertNotIn('trace_batch_id',barrier['evidence'])

 def test_saturated_metric_batch_never_proves_presend(self):
  """Keep uncertain card operations closed to replay when evidence is incomplete or navigation occurred."""
  presend=event(sent=False,presend=True)
  self.assertFalse(definitely_presend([presend]*1024))
  self.assertFalse(definitely_presend([presend,{'stage':'telemetry_rejected','reason_code':'invalid_stage'}]))
  for success in (True,False):
   self.assertFalse(definitely_presend([presend,dict(stage='navigation',seconds=.5,ok=success,label='')]))
  self.assertFalse(definitely_presend([dict(presend,omitted_fields=1)]))
