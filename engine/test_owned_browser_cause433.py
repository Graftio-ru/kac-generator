"""Native offline browser/CDP failure boundaries and durable first-stop diagnostics."""
import sys,types,contextvars,tempfile,unittest,asyncio,json,time,ast,socket,subprocess
from pathlib import Path
from unittest.mock import patch
sys.dont_write_bytecode=True;sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
TMP=tempfile.TemporaryDirectory();config=types.ModuleType('engine.config');config.CACHE_DIR=Path(TMP.name);config.settings=types.SimpleNamespace(browser_proxy_server='',browser_headed=False);sys.modules['engine.config']=config
llm=types.ModuleType('engine.llm');llm._REQUEST_GUARD=contextvars.ContextVar('offline433',default=None);sys.modules['engine.llm']=llm
from engine import browser,bounded_cdp as cdp,browser_transport_trace as trace
from engine.search import session
from engine.search.failure_policy import ResearchControlError,ResearchInternalFailure
from playwright.sync_api import Error,TimeoutError as NavigationTimeout
BIND={'job_id':'a'*12,'product_id':'b'*32,'generation':4,'position_index':11,'task_id':'c'*32,'operation_id':'d'*32}
class Passive:
 def send(self,*a):return {'frameTree':{'frame':{'id':'main'}}}
 def on(self,*a):pass
 def detach(self):pass
class Page:
 def __init__(self,error=None):self.error=error;self.context=types.SimpleNamespace(new_cdp_session=lambda p:Passive());self.closed=False
 def on(self,*a):pass
 def set_default_timeout(self,*a):pass
 def goto(self,*a,**k):
  self._kac_arrival_trace.native_requests['request']={'generation':1}
  raise self.error
 def wait_for_timeout(self,*a):pass
 def close(self,**k):self.closed=True
class Tests(unittest.TestCase):
 def page_trace(self):
  p=Page()
  with trace.owned_trace_scope(BIND):t=trace.ArrivalTrace('https://supplier.test/private?key=PRIVATE')
  t.attach(p);return p,t
 def bridge(self,p):
  s=object.__new__(cdp.PageCDP);s.page=p;s.deadline=time.monotonic()+1;s.command_deadline=s.deadline-.15;s.session=object();return s
 def test_native_attach_first_identity_and_detach_secondary(self):
  p,t=self.page_trace();s=self.bridge(p);primary=Error('net::ERR_ABORTED PRIVATE');cleanup=Error('net::ERR_CONNECTION_CLOSED PRIVATE')
  with patch.object(s,'_run',side_effect=primary):
   with self.assertRaises(Error)as got:s.__enter__()
  self.assertIs(got.exception,primary);self.assertTrue(p._kac_context_cleanup_required);self.assertEqual(p._kac_owned_browser_failure['phase'],'attach')
  with patch.object(s,'_run',side_effect=cleanup):self.assertFalse(s.__exit__(Error,primary,None))
  d=p._kac_owned_browser_failure;self.assertEqual(d['phase'],'attach');self.assertEqual(d['reason'],'ERR_ABORTED');self.assertEqual(d['cleanup_failure']['phase'],'detach');self.assertNotIn('PRIVATE',json.dumps(d))
 def test_send_and_detach_without_primary(self):
  p,t=self.page_trace();s=self.bridge(p);original=Error('net::ERR_CONNECTION_RESET PRIVATE')
  with patch.object(s,'_run',side_effect=original):
   with self.assertRaises(Error)as got:s.send('Page.getFrameTree',{})
  self.assertIs(got.exception,original);self.assertEqual(p._kac_owned_browser_failure['phase'],'send')
  p,t=self.page_trace();s=self.bridge(p)
  with patch.object(s,'_run',side_effect=original):
   with self.assertRaisesRegex(ValueError,'context_channel_cleanup_unacknowledged')as got:s.__exit__(None,None,None)
  self.assertIs(got.exception.__cause__,original);self.assertEqual(p._kac_owned_browser_failure['phase'],'detach')
 def test_owned_await_failure_retains_primary_before_drain(self):
  p,t=self.page_trace();s=self.bridge(p);loop=asyncio.new_event_loop();p._loop=loop;primary=Error('net::ERR_ABORTED PRIVATE');calls=[]
  def sync(coro):
   coro.close();calls.append(1)
   if len(calls)==1:raise primary
   raise Error('PRIVATE cleanup')
  p._sync=sync
  try:
   with self.assertRaises(Error)as got:s._run(lambda:asyncio.sleep(30),s.deadline,phase='send')
   self.assertIs(got.exception,primary);d=p._kac_owned_browser_failure;self.assertEqual(d['phase'],'send');self.assertTrue(d['owned_await_pending']);self.assertEqual(d['cleanup_failure']['phase'],'owned_await');self.assertEqual(d['reason'],'ERR_ABORTED')
  finally:
   pending=asyncio.all_tasks(loop)
   for task in pending:task.cancel()
   loop.run_until_complete(asyncio.gather(*pending,return_exceptions=True));loop.close()
 def test_full_native_page_original_goto_error_trace_and_persistence(self):
  original=Error('net::ERR_ABORTED PRIVATE');page=Page(original);manager=browser.Browser(headed=False);manager._context_for_url=lambda u:types.SimpleNamespace(new_page=lambda:page)
  bridge=self.bridge(page);bridge._run=lambda *a,**k:(_ for _ in ()).throw(Error('net::ERR_CONNECTION_CLOSED PRIVATE'))
  with tempfile.TemporaryDirectory()as d,patch.object(browser,'CACHE_DIR',Path(d)),patch.object(cdp,'page_cdp',return_value=bridge),trace.owned_trace_scope(BIND):
   with self.assertRaises(Error)as got:
    with manager.page('https://supplier.test/private?key=PRIVATE'):self.fail('no recovered document')
   self.assertIs(got.exception,original);self.assertTrue(page.closed);self.assertTrue(page._kac_context_cleanup_required)
   diagnostic=ResearchInternalFailure('pipeline',original,binding=BIND).diagnostic();self.assertEqual(diagnostic['failure_scope'],'global');self.assertEqual(diagnostic['failure_cause'],'programming_failure');cause=diagnostic['owned_browser_failure'];self.assertEqual(cause['binding'],BIND);self.assertEqual(cause['phase'],'attach');self.assertEqual(cause['reason'],'ERR_CONNECTION_CLOSED');self.assertNotIn('PRIVATE',json.dumps(cause));ref=cause['trace'];raw=(Path(d)/ref['ref']).read_bytes();self.assertEqual(trace.sha256(raw).hexdigest(),ref['sha256']);self.assertEqual(json.loads(raw)['first_owned_failure']['binding'],BIND)
   from engine.research_publication import IntegrationStore,DeltaPublisher
   store=IntegrationStore(Path(d)/'store');key='a'*12;store.create_job(key,[{'number':1,'name':'offline','unit':'шт','qty':1}],['supplier.test']);publisher=DeltaPublisher(store,key);publisher.lifecycle('offline-first-stop-433',status='error',first_stop=diagnostic);store.close();store=IntegrationStore(Path(d)/'store');self.assertEqual(store.public(key)['first_stop']['owned_browser_failure'],cause);store.close()
 def test_recovery_control_precedence_same_exception(self):
  p,t=self.page_trace();t.native_requests={'r':{}};primary=ResearchControlError('PRIVATE authoritative control');s=self.bridge(p)
  with patch.object(s,'_run',side_effect=primary),patch.object(cdp,'page_cdp',return_value=s):
   with self.assertRaises(ResearchControlError)as got:browser._recover_native_document(p,t,'https://supplier.test/')
  self.assertIs(got.exception,primary);self.assertTrue(p._kac_owned_browser_failure['control_error'])
 def test_trace_retention_failure_does_not_replace_original_or_invent_ref(self):
  p,t=self.page_trace();original=Error('original PRIVATE');trace.record_owned_browser_failure(p,'send',Error('net::ERR_ABORTED PRIVATE'),time.monotonic()+.5)
  with patch.object(Path,'mkdir',side_effect=OSError('PRIVATE')):t.finalize(Path(TMP.name),original)
  self.assertEqual(original._kac_owned_browser_failure['reason'],'ERR_ABORTED');self.assertNotIn('trace',original._kac_owned_browser_failure)
 def test_binding_scope_reset_concurrent_isolation_and_closed_values(self):
  self.assertEqual(trace.ArrivalTrace('https://example.test').data['operation_binding'],{})
  async def one(letter):
   value=dict(BIND,operation_id=letter*32)
   with trace.owned_trace_scope(value):await asyncio.sleep(0);return trace.ArrivalTrace('https://example.test').data['operation_binding']
  async def both():return await asyncio.gather(one('e'),one('f'))
  values=asyncio.run(both());self.assertEqual([x['operation_id']for x in values],['e'*32,'f'*32]);self.assertEqual(trace.ArrivalTrace('https://example.test').data['operation_binding'],{})
  d=trace.owned_browser_diagnostic(dict(version=1,phase='send',error_type='PRIVATE',reason='ERR_PRIVATE',binding={'job_id':'PRIVATE','generation':True},trace={'ref':'../PRIVATE','sha256':'a'*64}));self.assertNotIn('PRIVATE',json.dumps(d));self.assertEqual(d['binding'],{});self.assertNotIn('trace',d)
 def test_actual_app_card_context_binding_resets_on_failure(self):
  from contextlib import contextmanager
  tree=ast.parse(Path(__file__).with_name('app.py').read_text());fn=next(n for n in ast.walk(tree)if isinstance(n,ast.FunctionDef)and n.name=='scheduled_operation')
  @contextmanager
  def metrics(*a):yield types.SimpleNamespace(events=[])
  @contextmanager
  def factory():yield object()
  ns={'__package__':'engine','check':lambda:None,'_catalogue_operation_metrics':metrics,'job_id':'a'*12,'indices':[11],'_browser_factory':factory,'Operation':lambda kind,hosts,run,**k:run,'park_slice':lambda:False}
  exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'engine/app.py','exec'),ns)
  original=ValueError('PRIVATE')
  def fail(browser):
   self.assertEqual(trace.ArrivalTrace('https://example.test').data['operation_binding'],BIND);raise original
  run=ns['scheduled_operation']('card',[],fail,diagnostic_binding=BIND)
  with self.assertRaises(ValueError)as got:run()
  self.assertIs(got.exception,original);self.assertEqual(trace.ArrivalTrace('https://example.test').data['operation_binding'],{})
 def test_native_page_control_beats_navigation_and_late_cleanup(self):
  original=Error('net::ERR_ABORTED PRIVATE');stop=ResearchControlError('PRIVATE authoritative');page=Page(original);manager=browser.Browser(headed=False);manager._context_for_url=lambda u:types.SimpleNamespace(new_page=lambda:page);bridge=self.bridge(page);bridge._run=lambda *a,**k:(_ for _ in ()).throw(stop)
  with tempfile.TemporaryDirectory()as d,patch.object(browser,'CACHE_DIR',Path(d)),patch.object(cdp,'page_cdp',return_value=bridge),trace.owned_trace_scope(BIND):
   with self.assertRaises(ResearchControlError)as got:
    with manager.page('https://supplier.test/card'):self.fail('no yield')
  self.assertIs(got.exception,stop);self.assertTrue(stop._kac_owned_browser_failure['control_error']);self.assertEqual(stop._kac_owned_browser_failure['phase'],'attach')
  from engine.search.failure_policy import control_failure_observation
  result=control_failure_observation(stop,global_remaining=12);self.assertEqual(result['error_class'],'ResearchControlError');self.assertEqual(result['owned_browser_failure']['binding'],BIND)
 def test_first_cause_sticky_but_dirty_transition_is_truthful(self):
  p,t=self.page_trace();trace.record_owned_browser_failure(p,'send',Error('net::ERR_ABORTED'),time.monotonic()+1);self.assertFalse(p._kac_owned_browser_failure['cleanup_required']);p._kac_context_cleanup_required=True;trace.record_owned_browser_failure(p,'attach',Error('net::ERR_FAILED'),time.monotonic()+1);d=p._kac_owned_browser_failure;self.assertEqual(d['phase'],'send');self.assertEqual(d['reason'],'ERR_ABORTED');self.assertTrue(d['cleanup_required'])
 def test_successful_recovery_unchanged(self):
  p,t=self.page_trace();bound={'url':'https://supplier.test/card','status':200};t.native_requests={'r':{}};t.native_frame_response=lambda f:bound
  class S:
   command_deadline=time.monotonic()+1
   def __enter__(self):return self
   def __exit__(self,*a):return False
   def send(self,method,params):return {'frameTree':{'frame':{'id':'main'}}}if method=='Page.getFrameTree'else {'result':{'value':{'document_url':bound['url'],'ready_state':'complete','body_length':12,'h1_count':1,'visible_h1_count':1}}}
  with patch.object(cdp,'page_cdp',return_value=S()):self.assertEqual(browser._recover_native_document(p,t,bound['url']),bound)
  self.assertFalse(hasattr(p,'_kac_owned_browser_failure'))
if __name__=='__main__':
 with patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')),patch.object(subprocess,'Popen',side_effect=AssertionError('process forbidden')):unittest.main()
