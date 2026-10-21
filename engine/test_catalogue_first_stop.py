"""Canonical first-stop finalization on disposable jobs, without transport."""
import copy,time,unittest
from unittest.mock import patch
from engine import app,test_catalogue_unknown as fixtures
from engine.schemas import PositionIn
from engine.search.failure_policy import SearchServiceUnavailable,SearchTransportAttempt
from engine.llm import _REQUEST_GUARD

class FirstStopTests(unittest.TestCase):
 def setUp(self):
  self.f=fixtures.CatalogueUnknownTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.position=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)
 def failure(self):
  return SearchServiceUnavailable('transport_error',transport_attempts=(SearchTransportAttempt('ConnectError',time.time(),6.506,False),),transport_elapsed_seconds=6.506)
 def test_normal_worker_secondary_finalization_error_keeps_persisted_provider_receipt(self):
  failure=self.failure();first=copy.deepcopy(failure.diagnostic());calls=[]
  original=app._publish_catalogue_row
  def search(*args,**kw):
   calls.append(1);_REQUEST_GUARD.get().reject(failure);raise failure
  def publish(job_id,*args,**kw):
   if app.RESEARCH_JOBS[job_id].get('first_stop'):raise RuntimeError('secondary finalization fault')
   return original(job_id,*args,**kw)
  with patch.object(app,'_publish_catalogue_row',side_effect=publish):
   job,public=self.f.job([self.position],search=search)
  self.assertEqual(public['status'],'error');self.assertEqual(public['first_stop'],first)
  self.assertEqual(public['error_code'],first['code']);self.assertEqual(public['error'],first['reason'])
  self.assertEqual(len(calls),1);self.assertEqual(self.f.captures,[])
  self.assertNotIn('secondary finalization fault',str(public))
 def test_later_diagnostic_changes_cannot_overwrite_receipt_or_received_rows(self):
  failure=self.failure();calls=[]
  def search(*args,**kw):
   calls.append(1)
   if len(calls)==1:return self.f.search(*args,**kw)
   _REQUEST_GUARD.get().reject(failure);raise failure
  job,public=self.f.job([self.position],search=search);job_id=job['job_id'];before=copy.deepcopy(public['rows'])
  stop=copy.deepcopy(public['first_stop']);self.assertTrue(before[0]['candidates'])
  job['first_stop']['late_transport_detail']='not part of original receipt'
  job.update(status='done',error='secondary finalization',error_code='internal_error')
  app._save_research_job(job_id)
  after=app._research_store().public(job_id)
  self.assertEqual(after['first_stop'],stop);self.assertEqual(after['status'],'error')
  self.assertEqual(after['error'],stop['reason']);self.assertEqual(after['rows'],before)
  job.update(cancel_requested=True,status='cancelled');app._save_research_job(job_id)
  job.update(cancel_requested=False,status='done');app._save_research_job(job_id)
  cancelled=app._research_store().public(job_id)
  # An earlier fatal remains the canonical status; a later cancel intent is
  # retained and cannot be cleared, without replacing the original receipt.
  self.assertEqual(cancelled['status'],'error');self.assertTrue(cancelled['cancel_requested'])
  self.assertEqual(cancelled['first_stop'],stop);self.assertEqual(cancelled['rows'],before)

if __name__=='__main__':unittest.main()
