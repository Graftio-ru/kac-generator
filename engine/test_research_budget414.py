"""Global27+3 policy through the native runner, without provider operations."""
import time,unittest
from unittest.mock import patch
from .test_runner_finalization403 import RunnerFinalization403Tests
from .runner_executor import RESEARCH_FINALIZATION_SECONDS,RESEARCH_MIN_WORK_SECONDS
from .runner_control import DispatchError

class ResearchBudget414Tests(unittest.TestCase):
 setup_job=RunnerFinalization403Tests.setup_job
 downloads=RunnerFinalization403Tests.downloads
 def setUp(self):RunnerFinalization403Tests.setUp(self)
 def test_native_collection_stops_at_1620_without_extending_1800_claim(self):
  from .search.session import research_deadline,bounded_timeout,SearchBudgetExceeded
  initial=self.now[0];job,claim,runner=self.setup_job();original=self.native._run_research_job;seen=[]
  def at_boundary(j):
   seen.append(research_deadline()-time.monotonic())
   self.now[0]=claim['deadline']-180-1
   self.assertEqual(bounded_timeout(0),0)
   self.now[0]=claim['deadline']-180
   with self.assertRaises(SearchBudgetExceeded):bounded_timeout(1)
   original(j)
  with patch.object(self.native,'_run_research_job',side_effect=at_boundary):runner.execute(claim,self.seal)
  self.assertEqual(RESEARCH_FINALIZATION_SECONDS,180);self.assertEqual(claim['deadline'],initial+1800)
  self.assertGreater(seen[0],1618);self.assertLessEqual(seen[0],1620)
  value=self.control.public(job);self.assertEqual(value['dispatch_state'],'cancelled');self.assertTrue(value['partial']);self.assertFalse(value['business_complete'])
  self.assertEqual(value['finalization_reason'],'research_time_reserved');self.assertTrue({'supplier-research-summary.xlsx','supplier-research-package.zip'}<={e['name']for e in value['downloads']})
 def test_minimum_native_work_remains_available_at_exact_admission_boundary(self):
  seconds=RESEARCH_FINALIZATION_SECONDS+RESEARCH_MIN_WORK_SECONDS
  initial=self.now[0];job,claim,runner=self.setup_job(seconds);runner.execute(claim,self.seal)
  self.assertEqual(claim['deadline'],initial+seconds);self.assertEqual(self.control.public(job)['dispatch_state'],'done');self.assertTrue({'supplier-research-summary.xlsx','supplier-research-package.zip'}<={e['name']for e in self.downloads(job)})
 def test_control_hard_limit_does_not_expand_for_the_new_policy(self):
  req={'positions':[self.fixture.position.model_dump(mode='json')],'acquisition_mode':'catalogue_read'}
  with self.assertRaisesRegex(DispatchError,'runner_deadline_invalid'):
   self.control.enqueue(req,'budget414_hardlimit_001','server-basic:fixture',work_seconds=1801)
if __name__=='__main__':unittest.main()
