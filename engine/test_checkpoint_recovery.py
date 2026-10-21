"""Checkpoint completeness is pair coverage; explicit stops never restart on polling."""
import tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack
import engine.app as api
from engine.schemas import PositionIn

class CheckpointRecoveryTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.jid='abcde1234567'
  self.p=PositionIn(number=1,name='Item',type_mark='ABC',unit='шт',qty=1)
  self.check={'host':'a.ru','status':'no_match','reason':'Dated completed query; presence unknown','checked_at':123456.,'queries':2,'cards':0,'product_presence':'unknown'}
 def tearDown(self):
  api.RESEARCH_JOBS.pop(self.jid,None);api._RESEARCH_INPUTS.pop(self.jid,None);self.temp.cleanup()
 def row(self):return {'status':'failed','number':1,'found':0,'offers':[],'candidates':[],'diagnostics':[{'code':'not_found','reason':'Historical query','url':'https://a.ru/'}],'source_checks':[self.check,{'host':'b.ru','status':'incomplete','reason':'No request','product_presence':'unknown'}]}
 def job(self,status='running'):return dict(job_id=self.jid,status=status,rows=[self.row()],total=1,completed=1,refresh_first=True,configured_hosts=['a.ru','b.ru'],primary_hosts=['a.ru'],fallback_hosts=['b.ru'])
 def test_non_none_incomplete_row_is_not_completed_and_duplicate_checks_fail(self):
  j=self.job();self.assertEqual(0,api._research_completed(j))
  j['rows'][0]['source_checks'][1]={**self.check,'host':'b.ru'};self.assertEqual(1,api._research_completed(j))
  j['rows'][0]['source_checks'].append(self.check);self.assertEqual(0,api._research_completed(j))
 def test_explicit_cancel_and_provider_error_polls_do_not_launch_or_clear(self):
  with patch.object(api,'RESEARCH_JOB_DIR',self.root),patch.object(api,'_launch_research_job') as launch:
   for ordinal,status in enumerate(('cancelled','error','running')):
    self.jid=f'abcde123456{ordinal}'
    j=self.job(status);j.update(error_code='llm_unavailable',error='Provider blocked',cancel_requested=True)
    api.RESEARCH_JOBS[self.jid]=j;api._RESEARCH_INPUTS[self.jid]=[self.p];api._save_research_job(self.jid)
    api.RESEARCH_JOBS.pop(self.jid);api._RESEARCH_INPUTS.pop(self.jid)
    public=api.research_status(self.jid)
    self.assertEqual(status,public['status']);self.assertEqual(0,public['completed']);self.assertEqual('Provider blocked',public['error']);self.assertTrue(public['cancel_requested'])
   launch.assert_not_called()
 def test_running_crash_reopens_only_unfinished_host_with_fixed_tier_snapshot(self):
  calls=[]
  def search(query,hosts,**kwargs):
   from engine.search.session import transport_started
   transport_started('search_api');calls.extend(hosts);return []
  with ExitStack() as stack:
   for ctx in [patch.object(api,'RESEARCH_JOB_DIR',self.root/'jobs'),patch('engine.config.CACHE_DIR',self.root/'cache'),patch('engine.search.source_registry.CACHE_DIR',self.root/'cache'),patch('engine.search.candidate_queue.CACHE_DIR',self.root/'cache'),patch.object(api,'_research_row',return_value={'status':'failed','found':0,'candidates':[],'offers':[]}),patch('engine.search.agent.build_queries',return_value=['literal']),patch('engine.search.search_engine.search_on_sites',side_effect=search),patch('engine.search.whitelist.whitelist_hosts',return_value=['new.ru']),patch('engine.search.whitelist.fallback_hosts',return_value=[])]:stack.enter_context(ctx)
   api.RESEARCH_JOBS[self.jid]=self.job();api._RESEARCH_INPUTS[self.jid]=[self.p];api._save_research_job(self.jid)
   api.RESEARCH_JOBS.pop(self.jid);api._RESEARCH_INPUTS.pop(self.jid)
   api.research_status(self.jid)
   until=time.monotonic()+5
   while self.jid in api._RESEARCH_THREADS and time.monotonic()<until:time.sleep(.01)
   self.assertNotIn(self.jid,api._RESEARCH_THREADS)
   j=api.RESEARCH_JOBS[self.jid];self.assertEqual('done',j['status'],j);self.assertEqual(1,j['completed']);self.assertEqual(['b.ru'],calls)
   checks={c['host']:c for c in j['rows'][0]['source_checks']};self.assertEqual(123456.,checks['a.ru']['checked_at']);self.assertTrue(checks['a.ru']['reused']);self.assertEqual('no_match',checks['b.ru']['status']);self.assertIn('Historical query',str(j['rows'][0]['diagnostics']))
 def test_actual_cancelled_worker_keeps_non_none_unfinished_count_zero_everywhere(self):
  import json
  def search(query,hosts,**kwargs):
   from engine.search.session import transport_started
   transport_started('search_api');api.RESEARCH_JOBS[self.jid]['cancel_requested']=True;return []
  with ExitStack() as stack:
   for ctx in [patch.object(api,'RESEARCH_JOB_DIR',self.root/'jobs'),patch('engine.config.CACHE_DIR',self.root/'cache'),patch('engine.search.source_registry.CACHE_DIR',self.root/'cache'),patch('engine.search.candidate_queue.CACHE_DIR',self.root/'cache'),patch.object(api,'_research_row',return_value={'status':'failed','found':0,'candidates':[],'offers':[]}),patch('engine.search.agent.build_queries',return_value=['literal']),patch('engine.search.search_engine.search_on_sites',side_effect=search)]:stack.enter_context(ctx)
   api.RESEARCH_JOBS[self.jid]=self.job();api._RESEARCH_INPUTS[self.jid]=[self.p]
   api._run_research_job(self.jid)
   j=api.RESEARCH_JOBS[self.jid];self.assertEqual('cancelled',j['status']);self.assertIsNotNone(j['rows'][0]);self.assertEqual(0,j['completed'])
   self.assertTrue(any(c['status']=='incomplete' for c in j['rows'][0]['source_checks']))
   raw=api._load_research_job(self.jid);self.assertEqual(0,raw[0]['completed'])
   with patch.object(api,'_launch_research_job') as launch:
    public=api.research_status(self.jid);self.assertEqual(0,public['completed']);launch.assert_not_called()
 def test_force_discovery_recovery_does_not_revisit_finished_persisted_queue_card(self):
  from engine.search.candidate_queue import CandidateQueue
  from engine.models import SpecPosition
  cards=[];queries=[]
  def research(position):
   from engine.search.session import batch_card_urls,transport_started
   for url in batch_card_urls() or []:cards.append(url);transport_started('supplier_navigation',url)
   return {'status':'failed','found':0,'offers':[],'candidates':[]}
  def search(query,hosts,**kw):
   from engine.search.session import transport_started
   transport_started('search_api');queries.extend(hosts);return []
  with ExitStack() as stack:
   for ctx in [patch.object(api,'RESEARCH_JOB_DIR',self.root/'jobs'),patch('engine.config.CACHE_DIR',self.root/'cache'),patch('engine.search.source_registry.CACHE_DIR',self.root/'cache'),patch('engine.search.candidate_queue.CACHE_DIR',self.root/'cache'),patch.object(api,'_research_row',side_effect=research),patch('engine.search.agent.build_queries',return_value=['literal']),patch('engine.search.search_engine.search_on_sites',side_effect=search)]:stack.enter_context(ctx)
   queue=CandidateQueue(SpecPosition(number=1,name='Item',type_mark='ABC',unit='шт',qty=1));queue.add(['https://a.ru/item/ABC','https://b.ru/item/ABC'])
   job=self.job();job['force_discovery']=True;api.RESEARCH_JOBS[self.jid]=job;api._RESEARCH_INPUTS[self.jid]=[self.p];api._run_research_job(self.jid)
   self.assertEqual(['https://b.ru/item/ABC'],cards);self.assertEqual(['b.ru'],queries);self.assertEqual('done',job['status'])
 def test_legacy_done_report_stays_compatible_without_new_host_claim(self):
  j=dict(status='done',rows=[{'status':'failed'}]);self.assertEqual(1,api._research_completed(j));self.assertNotIn('configured_hosts',j)
