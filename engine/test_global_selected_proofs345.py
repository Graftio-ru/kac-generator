"""Global selected-photo grants use real native journal and bounded wire doubles."""
import hashlib,time,uuid,tempfile,unittest
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch
from engine.models import SpecPosition
from engine.search.catalogue import CatalogueStore,SourceObservation,ImageEvidence,identity_for_position
from engine.search.catalogue_projection import catalogue_scope,project_product
from engine.search.selected_proofs import capture_selected_proofs,price_candidates
class GlobalSelectedProofTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.store=CatalogueStore(self.root/'catalogue.sqlite3');self.p=SpecPosition(number=1,name='Изделие',type_mark='ABC123',unit='шт',qty=12);self.identity=identity_for_position(self.p);self.hosts=['one.test','two.test','three.test'];self.admission=self.store.acquire_unknown(self.identity,self.hosts,owner='fixture')
  self.patch=patch('engine.config.CACHE_DIR',self.root);self.patch.start();self.addCleanup(self.patch.stop);self.browsers=[];self.calls=[]
  for i in range(3):self.source(i)
 def source(self,i,oid=None,photo=False):
  oid=oid or uuid.uuid4().hex;url=f'https://{self.hosts[i]}/product/ABC123';stamp=time.time();title='Изделие ABC123';text=f'{title}\nАртикул: ABC123\nЦена {100+i} руб. с НДС\nза 1 шт'
  image=ImageEvidence(oid,url,stamp,'missing',error='not_selected_price_observation')
  if photo:
   from PIL import Image
   path=self.root/(oid+'.png');Image.new('RGB',(20,10),'white').save(path);raw=path.read_bytes();sha=hashlib.sha256(raw).hexdigest();context=dict(version=1,role='review_context',filename=path.name,sha256=sha,url=url,captured_at=stamp,width=20,height=10,document_width=20,document_height=10,truncated=False)
   image=ImageEvidence(oid,url,stamp,'captured',local_path=path.name,sha256=sha,size=len(raw),captured_at=stamp,context_evidence=context)
  value=SourceObservation(oid,self.identity,self.hosts[i],url,url,stamp,'supplier_rendered',title,'ABC123',text,hashlib.sha256(text.encode()).hexdigest(),100+i,'RUB','included','шт',1,None,'','in_stock','observed',f'Seller {i}',['7707083893','7842224734','7708503727'][i],image,'catalogue-v1')
  self.store.append_observation(self.identity.key,value,acquisition=self.admission,coverage_status='found');return value
 @contextmanager
 def browser(self):self.browsers.append(1);yield object()
 def invoke(self,**kwargs):
  return capture_selected_proofs(self.store,[self.p],browser=None,browser_factory=self.browser,image_store=object(),acquisition=self.admission,**kwargs)
 def pending(self,url):
  oid=uuid.uuid4().hex;self.store.card_operation(self.identity.key,url.split('/')[2],url,operation_id=oid,acquisition=self.admission);return oid
 def states(self):
  out={}
  for a in self.store.lookup(self.identity)['refresh_attempts']:
   if a.get('card_operation'):out[a['card_operation']['operation_id']]=a['card_operation']['state']
  return out
 def test_pending_url_has_no_browser_or_capture_and_history_survives(self):
  url='https://three.test/product/ABC123';oid=self.pending(url);before=self.store.lookup(self.identity)['refresh_attempts']
  with patch('engine.search.catalogue_producer.capture_and_publish',side_effect=AssertionError('no capture')):report=self.invoke(selected_url=url)
  self.assertEqual(self.browsers,[]);self.assertEqual(self.states()[oid],'pending');self.assertEqual(self.store.lookup(self.identity)['refresh_attempts'],before);self.assertTrue(report['positions'][0]['failures'])
 def test_one_fixed_url_one_native_operation_same_observation_and_resolve(self):
  def capture(store,identity,p,url,**kw):
   self.calls.append(url);self.assertEqual(self.states()[kw['observation_id']],'pending');return self.source(self.hosts.index(kw['supplier_host']),kw['observation_id'])
  with patch('engine.search.catalogue_producer.capture_and_publish',side_effect=capture):self.invoke(selected_url='https://two.test/product/ABC123')
  self.assertEqual(self.calls,['https://two.test/product/ABC123']);self.assertEqual(len(self.browsers),1);self.assertEqual(list(self.states().values()),['resolved'])
 def test_no_observation_stays_pending_and_fresh_attempt_set_cannot_retry(self):
  url='https://three.test/product/ABC123'
  with patch('engine.search.catalogue_producer.capture_and_publish',return_value=None)as capture:self.invoke(selected_url=url);self.invoke(selected_url=url)
  self.assertEqual(capture.call_count,1);self.assertEqual(len(self.browsers),1);self.assertEqual(list(self.states().values()),['pending'])
 def test_stale_fixed_selection_no_other_url_or_browser(self):
  with patch('engine.search.catalogue_producer.capture_and_publish',side_effect=AssertionError('no capture')):self.invoke(selected_url='https://one.test/foreign')
  self.assertEqual(self.browsers,[]);self.assertEqual(self.states(),{})
 def test_control_before_admission_wins_and_writes_nothing(self):
  from engine.search.failure_policy import ResearchControlError
  before=self.store.lookup(self.identity)
  with self.assertRaises(ResearchControlError):self.invoke(control=lambda:(_ for _ in()).throw(ResearchControlError()))
  self.assertEqual(self.store.lookup(self.identity),before);self.assertEqual(self.browsers,[])
 def test_lost_lease_before_attempt_denies_browser(self):
  from engine.search.catalogue import CatalogueConflict
  self.store.cancel_acquisition(self.admission,reason='synthetic owner lost')
  with self.assertRaises(CatalogueConflict):self.invoke(selected_url='https://three.test/product/ABC123')
  self.assertEqual(self.browsers,[])
 def test_supplier_deferral_is_local_and_unknown_untouched(self):
  from engine.search.session import SupplierDeferred
  failure=SupplierDeferred({'retry_at':time.time()+60,'reason':'synthetic defer'})
  with patch('engine.search.catalogue_producer.capture_and_publish',side_effect=failure)as capture:report=self.invoke()
  self.assertEqual(capture.call_count,3);self.assertFalse(report['paused']);self.assertEqual(set(self.states().values()),{'pending'})
 def test_confirmed_wrong_observation_id_cannot_resolve(self):
  from engine.search.catalogue_producer import CataloguePublicationError
  with patch('engine.search.catalogue_producer.capture_and_publish',side_effect=lambda *a,**kw:self.source(2)):
   with self.assertRaises(CataloguePublicationError):self.invoke(selected_url='https://three.test/product/ABC123')
  self.assertEqual(set(self.states().values()),{'pending'})

 def test_ready_photos_are_reused_without_journal_browser_or_wire(self):
  for i in range(3):self.source(i,photo=True)
  before=self.store.lookup(self.identity)
  with patch('engine.search.catalogue_producer.capture_and_publish',side_effect=AssertionError('ready capture forbidden')):report=self.invoke()
  self.assertEqual(report['positions'][0]['attachments'],3);self.assertEqual(self.browsers,[]);self.assertEqual(self.store.lookup(self.identity),before)
 def test_global_control_from_capture_stops_following_supplier(self):
  from engine.search.failure_policy import ResearchControlError
  with patch('engine.search.catalogue_producer.capture_and_publish',side_effect=ResearchControlError())as capture:
   with self.assertRaises(ResearchControlError):self.invoke()
  self.assertEqual(capture.call_count,1);self.assertEqual(len(self.browsers),1);self.assertEqual(list(self.states().values()),['pending'])
 def test_shared_six_attempt_limit_not_reset_by_single_url_grant(self):
  before=self.store.lookup(self.identity);attempted={f'https://old{i}.test/card'for i in range(6)}
  with patch('engine.search.catalogue_producer.capture_and_publish',side_effect=AssertionError('budget exhausted')):self.invoke(selected_url='https://three.test/product/ABC123',attempted=attempted)
  self.assertEqual(self.browsers,[]);self.assertEqual(self.store.lookup(self.identity),before)

class WholeInputSchedulerTests(unittest.TestCase):
 def test_native_scheduler_all86_opportunity_and_same_host_exclusion(self):
  import threading
  from engine.search.acquisition_scheduler import run_products,Operation
  active=set();overlap=[];progress=[];lock=threading.Lock();first=threading.Event();unrelated=threading.Event()
  def call(i,host):
   with lock:
    if host in active:overlap.append(host)
    active.add(host)
   if i==0:first.set();self.assertTrue(unrelated.wait(1))
   elif i==1:self.assertTrue(first.wait(1));unrelated.set()
   time.sleep(.001)
   with lock:active.remove(host);progress.append(i)
  def product(i):
   host='photo.test'if i in(0,2)else'other.test'if i==1 else f'host{i%12}.test'
   yield Operation('proof'if i==0 else'card',(host,),lambda:call(i,host));return True
  result=run_products({i:product(i)for i in range(86)},control=lambda:None,workers=3)
  self.assertEqual(len(result),86);self.assertEqual(len(set(progress)),86);self.assertEqual(overlap,[])
 def test_actual_original96_native_entry_all86_search_10own_skipped(self):
  from engine import app
  from engine.schemas import PositionIn
  from engine.search.search_engine import DiscoveryURLs
  from engine.equipment_output_policy import is_graftio_equipment
  from contextlib import ExitStack
  import json
  # Runner sets a public immutable fixture path; portable test skips without it.
  fixture=getattr(app,'_TEST345_ORIGINAL96',None)
  if fixture is None:self.skipTest('Original96 public fixture not supplied')
  positions=[PositionIn.model_validate(v)for v in json.loads(Path(fixture).read_bytes())['positions']]
  self.assertEqual(len(positions),96);self.assertEqual(sum(is_graftio_equipment(p)for p in positions),10)
  with tempfile.TemporaryDirectory()as tmp,ExitStack()as patches:
   root=Path(tmp);store=CatalogueStore(root/'catalogue.sqlite3');hosts=[f's{i}.test'for i in range(36)];queries=[]
   def search(q,selected,**kw):
    queries.append((q,tuple(selected)));return DiscoveryURLs([],host_observations={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=0,candidate_urls=[],diagnostics=[])for h in selected})
   for context in (patch.object(app,'RESEARCH_JOB_DIR',root/'jobs'),patch.object(app,'_catalogue_store',return_value=store),patch.object(app,'_catalogue_image_store',return_value=object()),patch('engine.search.search_route.resolve_initial_route',return_value=(None,None)),patch('engine.search.whitelist.whitelist_hosts',return_value=hosts),patch('engine.search.whitelist.fallback_hosts',return_value=[]),patch('engine.search.search_engine.search_on_sites',side_effect=search),patch.object(app,'_browser_factory',side_effect=AssertionError('empty discovery has no card')),patch('engine.search.catalogue_producer.capture_and_publish',side_effect=AssertionError('empty discovery has no capture'))):patches.enter_context(context)
   key=uuid.uuid4().hex[:12];app.RESEARCH_JOBS[key]=dict(job_id=key,rows=[None]*96,total=96,completed=0,status='pending',configured_hosts=hosts,primary_hosts=hosts,fallback_hosts=[],cancel_requested=False);app._RESEARCH_INPUTS[key]=positions
   try:
    app._save_research_job(key);app._run_research_job(key);job=app.RESEARCH_JOBS[key]
    self.assertEqual(job['status'],'done',job.get('error'));self.assertEqual(len(job['rows']),96);self.assertEqual(sum(r['status']=='skipped'for r in job['rows']),10)
    ordinary=[(p,r)for p,r in zip(positions,job['rows'])if not is_graftio_equipment(p)]
    self.assertEqual(len(ordinary),86);self.assertTrue(all(len(r['source_checks'])==36 for p,r in ordinary));self.assertTrue(queries)
    for p,row in ordinary:
     product=store.lookup(identity_for_position(p));self.assertEqual(product['acquisition']['status'],'done')
   finally:app.RESEARCH_JOBS.pop(key,None);app._RESEARCH_INPUTS.pop(key,None)

class NativeAppProofGrantTests(unittest.TestCase):
 def test_real_app_proof_grants_single_host_requeue_and_unrelated_progress(self):
  from engine import app
  from engine.schemas import PositionIn
  from engine.search import acquisition_scheduler
  from engine.search.search_engine import DiscoveryURLs
  from contextlib import ExitStack,contextmanager
  import threading
  from PIL import Image
  positions=[PositionIn(number=i+1,name='Изделие',type_mark=f'ABC12{i}',unit='шт',qty=2)for i in range(3)]
  with tempfile.TemporaryDirectory()as tmp,ExitStack()as patches:
   root=Path(tmp);store=CatalogueStore(root/'catalogue.sqlite3');hosts=[f's{i}.test'for i in range(36)];events=[];proof_hosts=[];started=threading.Event();advanced=threading.Event();lock=threading.Lock();captured=set();blocked=[]
   @contextmanager
   def browser():yield object()
   def search(q,selected,**kw):
    offset=next((i*3 for i in range(3)if f'ABC12{i}'in q),0);wanted=hosts[offset:offset+3]
    urls=[f'https://{h}/product'for h in selected if h in wanted]
    return DiscoveryURLs(urls,host_observations={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=int(h in wanted),candidate_urls=[u for u in urls if u.split('/')[2]==h],diagnostics=[])for h in selected})
   def capture(store,identity,p,url,**kw):
    host=kw['supplier_host'];photo=kw.get('photo_only',False);oid=kw['observation_id'];stamp=time.time();i=hosts.index(host);title=f'Изделие {identity.code}';text=f'{title}\nАртикул: {identity.code}\nЦена {100+i} руб. с НДС\nза 1 шт'
    with lock:events.append((p.number,host,photo))
    if photo and not started.is_set():blocked.append(host);started.set();self.assertTrue(advanced.wait(2),'unrelated supplier work starved by proof grant')
    elif started.is_set()and host!=blocked[0]:advanced.set()
    image=ImageEvidence(oid,url,stamp,'missing',error='not_selected_price_observation')
    if photo:
     path=root/(oid+'.png');Image.new('RGB',(20,10),'white').save(path);raw=path.read_bytes();sha=hashlib.sha256(raw).hexdigest();context=dict(version=1,role='review_context',filename=path.name,sha256=sha,url=url,captured_at=stamp,width=20,height=10,document_width=20,document_height=10,truncated=False)
     image=ImageEvidence(oid,url,stamp,'captured',local_path=path.name,sha256=sha,size=len(raw),captured_at=stamp,context_evidence=context)
    value=SourceObservation(oid,identity,host,url,url,stamp,'supplier_rendered',title,identity.code,text,hashlib.sha256(text.encode()).hexdigest(),100+i,'RUB','included','шт',1,None,'','in_stock','observed',f'Seller {i}',['7707083893','7842224734','7708503727'][i%3],image,'catalogue-v1')
    store.append_observation(identity.key,value,acquisition=kw['acquisition'],coverage_status='found');return value
   operation=acquisition_scheduler.Operation
   class CheckedOperation(operation):
    def __init__(self,kind,selected,call,before=lambda:True):
     if kind=='proof':self_test.assertEqual(len(selected),1);proof_hosts.append(tuple(selected))
     super().__init__(kind,selected,call,before)
   self_test=self
   for context in (patch.object(app,'RESEARCH_JOB_DIR',root/'jobs'),patch.object(app,'_catalogue_store',return_value=store),patch.object(app,'_catalogue_image_store',return_value=object()),patch.object(app,'_browser_factory',browser),patch('engine.config.CACHE_DIR',root),patch('engine.search.search_route.resolve_initial_route',return_value=(None,None)),patch('engine.search.whitelist.whitelist_hosts',return_value=hosts),patch('engine.search.whitelist.fallback_hosts',return_value=[]),patch('engine.search.search_engine.search_on_sites',side_effect=search),patch('engine.search.catalogue_producer.capture_and_publish',side_effect=capture),patch.object(acquisition_scheduler,'Operation',CheckedOperation)):patches.enter_context(context)
   key=uuid.uuid4().hex[:12];app.RESEARCH_JOBS[key]=dict(job_id=key,rows=[None]*3,total=3,completed=0,status='pending',configured_hosts=hosts,primary_hosts=hosts,fallback_hosts=[],cancel_requested=False);app._RESEARCH_INPUTS[key]=positions
   try:
    app._save_research_job(key);app._run_research_job(key);job=app.RESEARCH_JOBS[key]
    self.assertEqual(job['status'],'done',job.get('error'));self.assertTrue(advanced.is_set());self.assertEqual(len(proof_hosts),9);self.assertTrue(all(len(v)<=6 for v in job['catalogue_proof_attempts'].values()));self.assertEqual([r['proof_ready_sources']for r in job['rows']],[3,3,3])
   finally:app.RESEARCH_JOBS.pop(key,None);app._RESEARCH_INPUTS.pop(key,None)
