"""New native uncertainty evidence; historical unknowns never receive invented causes."""
import json,threading,unittest,time
from unittest.mock import patch
from engine import app,test_catalogue_unknown as fixture
from engine.schemas import PositionIn
from engine.models import SpecPosition
from engine.search.acquisition_frontier import NeutralRecords,FrontierController
from engine.search.catalogue import identity_for_position
METRICS={}
class UncertainCardReasonTests(unittest.TestCase):
 def run_native(self,capture,*,deadline=None):
  f=fixture.CatalogueUnknownTests();f.setUp();self.addCleanup(f.doCleanups);f.hosts[:]=['s0.test'];threads=[]
  def operation(*args,**kw):threads.append(threading.get_ident());return capture(f,*args,**kw)
  from engine.search.session import research_session
  with research_session(deadline=deadline):job,public=f.job([PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)],capture=operation)
  saved=json.loads(app._research_store().db.execute('SELECT payload FROM rows WHERE job=? AND idx=0',(job['job_id'],)).fetchone()[0])['_catalogue_acquisition_frontier'];product=f.store.lookup(identity_for_position(SpecPosition(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)))
  records=NeutralRecords(app.RESEARCH_JOB_DIR/'acquisition'/job['job_id']/product['product_id'],saved)
  outcomes=[records._read(card['outcome'])for card in FrontierController(saved,records).completed_cards()]
  # Reopen a NEW records reader using durable frontier, not a result dictionary.
  reopened=NeutralRecords(records.root,saved)
  for card in FrontierController(saved,reopened).completed_cards():self.assertEqual(reopened._read(card['outcome']),records._read(card['outcome']))
  self.assertTrue(threads);self.assertTrue(all(t!=threading.get_ident()for t in threads),'Real native scheduler worker required')
  self.assertIsNone(product.get('acquisition_completion'))
  METRICS.setdefault('native',[]).append(dict(status=job['status'],outcomes=outcomes,worker_threads=len(set(threads)),diagnostics=public['rows'][0]['diagnostics']))
  return job,public,outcomes
 def test_native_worker_empty_capture_persists_typed_unknown_after_reopen(self):
  job,public,outcomes=self.run_native(lambda *a,**kw:None)
  uncertain=[o for o in outcomes if o.get('completion',{}).get('disposition')=='uncertain'];self.assertTrue(uncertain)
  for o in uncertain:
   self.assertEqual(o['reason'],'card_no_observation');self.assertEqual(o['completion']['evidence']['code'],'card_no_observation');self.assertEqual(o['completion']['evidence']['stage'],'capture_operation');self.assertEqual(o['completion']['evidence']['role'],'card_uncertainty')
   self.assertTrue(o['operation_id']);self.assertEqual(o['host'],'s0.test');self.assertTrue(o['binding']['requested_identity_sha256'])
  self.assertTrue(any(d['code']=='completion_uncertain_card'for d in public['rows'][0]['diagnostics']))

 def test_native_decision_and_unknown_source_codes_keep_no_secret_prose(self):
  from engine.search import agent
  for code,expected in [('price_on_request','price_on_request'),('SYNTHETIC_SECRET_URL https://hidden.invalid/key','card_source_reason_unknown')]:
   def capture(f,*args,**kw):
    agent._CARD_DIAGNOSTICS.get().append(dict(code=code,reason='SYNTHETIC_PRIVATE_MESSAGE',url=args[3],failure_cause='source_decision'))
    return None
   job,public,outcomes=self.run_native(capture)
   uncertain=[o for o in outcomes if o.get('completion',{}).get('disposition')=='uncertain'];self.assertTrue(uncertain)
   for o in uncertain:self.assertEqual(o['reason'],expected);self.assertNotIn('SYNTHETIC',json.dumps(o));self.assertNotIn('hidden.invalid',json.dumps(o))
 def test_native_caught_local_exception_classes_remain_uncertain_and_bounded(self):
  from engine.search.catalogue_producer import CataloguePublicationError
  from engine.llm import LLMOperationFailed
  from engine.search.session import SearchBudgetExceeded
  for error,expected in [(CataloguePublicationError('catalogue_source_binding_invalid'),'catalogue_source_binding_invalid'),(LLMOperationFailed('invalid_response_envelope'),'llm_operation_failed'),(SearchBudgetExceeded('SYNTHETIC_PRIVATE_MESSAGE'),'card_operation_budget'),(ValueError('SYNTHETIC_PRIVATE_MESSAGE'),'catalogue_card_validation_failed')]:
   def capture(*a,**kw):raise error
   job,public,outcomes=self.run_native(capture)
   uncertain=[o for o in outcomes if o.get('completion',{}).get('disposition')=='uncertain'];self.assertTrue(uncertain)
   for o in uncertain:
    self.assertEqual(o['reason'],expected);self.assertEqual(o['completion']['evidence']['error_type'],type(error).__name__);self.assertNotIn('SYNTHETIC',json.dumps(o))
 def test_write_time_fallback_is_safe_and_old_empty_record_reads_unchanged(self):
  from engine.test_acquisition_completion import AcquisitionCompletionTests
  from engine.search.acquisition_frontier import uncertain_card_completion
  case=AcquisitionCompletionTests();case.setUp();self.addCleanup(case.doCleanups);controller,records=case.controller();url='https://s0.test/card';operation='a'*32
  ref=records.write_outcome(operation,url,'s0.test',state='review',completion=dict(version=1,disposition='uncertain',evidence={'raw':'SYNTHETIC_PRIVATE_MESSAGE','url':'https://hidden.invalid/key'}));saved=records._read(ref)
  self.assertEqual(saved['reason'],'card_source_reason_unknown');self.assertEqual(saved['completion']['evidence'],dict(role='card_uncertainty',code='card_source_reason_unknown',stage='outcome_write'));self.assertNotIn('SYNTHETIC',json.dumps(saved))
  recovery=records.write_outcome('b'*32,url,'s0.test',state='review',reason='card_link_parent_unsettled',completion=dict(version=1,disposition='uncertain',evidence={}));self.assertEqual(records._read(recovery)['completion']['evidence']['stage'],'outcome_recovery')
  # Synthetic pre-change record. Reading never enriches immutable missing reasons.
  legacy=dict(binding=records.binding,kind='outcome',operation_id='c'*32,url=url,host='s0.test',state='review',observation_id=None,reason='',completion=dict(version=1,disposition='uncertain',evidence={}))
  oldref=records._write(legacy);self.assertEqual(records._read(oldref),legacy)
  class Hostile(str):
   def __str__(self):raise AssertionError('Do not stringify')
  for value in ['SECRET https://hidden.invalid/key','x'*100000,Hostile('price_on_request'),True,{}]:self.assertEqual(uncertain_card_completion(value)['evidence']['code'],'card_source_reason_unknown')
  for value in [True,{},'x'*100000,Hostile('price_on_request')]:self.assertEqual(app._catalogue_uncertain_card_completion([dict(code=value,reason='SYNTHETIC_PRIVATE_MESSAGE')])['evidence']['code'],'card_source_reason_unknown')
 def test_native_cancel_global_internal_and_ownership_never_settle_as_local_uncertain(self):
  from engine.search.session import SearchBudgetExceeded
  from engine.search.failure_policy import ResearchInternalFailure
  from engine.search.acquisition_lease import AcquisitionOwnershipLost
  from engine.llm import _REQUEST_GUARD
  for mode in ['cancel','global','internal','ownership']:
   def capture(f,*args,**kw):
    if mode=='cancel':
     job=next(j for j in app.RESEARCH_JOBS.values()if j['status']=='running'and j['configured_hosts']==f.hosts);job['cancel_requested']=True;raise SearchBudgetExceeded('SYNTHETIC_PRIVATE_MESSAGE')
    if mode in ('global','internal'):
     error=ResearchInternalFailure('card_adapter',ValueError('SYNTHETIC_PRIVATE_MESSAGE'))
     if mode=='global':_REQUEST_GUARD.get().reject(error)
     raise error
    raise AcquisitionOwnershipLost()
   job,public,outcomes=self.run_native(capture)
   self.assertFalse(any(o.get('completion',{}).get('disposition')=='uncertain'for o in outcomes),mode)
   self.assertIn(job['status'],{'error','cancelled'});self.assertNotIn('SYNTHETIC',json.dumps(outcomes))
 def test_parent_deadline_never_becomes_local_uncertain_completion(self):
  from engine.search.session import SearchBudgetExceeded
  deadline=time.monotonic()+.25
  def capture(*a,**kw):
   while time.monotonic()<deadline:time.sleep(.001)
   raise SearchBudgetExceeded('SYNTHETIC_PRIVATE_MESSAGE')
  job,public,outcomes=self.run_native(capture,deadline=deadline)
  self.assertFalse(any(o.get('completion',{}).get('disposition')=='uncertain'for o in outcomes));self.assertIn(job['status'],{'error','cancelled'})
