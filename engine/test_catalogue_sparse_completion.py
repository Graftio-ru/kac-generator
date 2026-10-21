"""Sparse server snapshots finish READ jobs; own acquisition never becomes done."""
import json,sqlite3,tempfile,unittest,uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from engine import app
from engine.schemas import PositionIn
from engine.search.catalogue import CatalogueStore,identity_for_position
from engine.equipment_output_policy import is_graftio_equipment
SOURCE=Path(__file__).resolve().parents[2]/'sources/kac-generator'
class SparseCompletionTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.store=CatalogueStore(self.root/'catalogue.sqlite3');self.hosts=['s'+str(i)+'.test' for i in range(36)]
  self.stack=ExitStack();self.addCleanup(self.stack.close)
  for p in (patch.object(app,'_catalogue_store',return_value=self.store),patch.object(app,'RESEARCH_JOB_DIR',self.root/'jobs'),patch.object(app,'_browser_factory',side_effect=AssertionError('No browser')),patch.object(app,'_run_catalogue_unknown_job',side_effect=AssertionError('No acquisition')),patch('engine.search.search_route.resolve_initial_route',side_effect=AssertionError('No account API')),patch('engine.search.search_engine.search_on_sites',side_effect=AssertionError('No search'))):self.stack.enter_context(p)
 def prepare(self,positions,key=None):
  key=key or uuid.uuid4().hex[:12];app.RESEARCH_JOBS[key]=dict(job_id=key,rows=[None]*len(positions),total=len(positions),completed=0,status='pending',configured_hosts=self.hosts,primary_hosts=self.hosts[:29],fallback_hosts=self.hosts[29:]);app._RESEARCH_INPUTS[key]=positions;self.addCleanup(app.RESEARCH_JOBS.pop,key,None);self.addCleanup(app._RESEARCH_INPUTS.pop,key,None);app._save_research_job(key);return key
 def run_job(self,key):
  before=self.store.all_snapshot(include_history=True);app._run_research_job(key);public=app._research_store().public(key);self.assertEqual(self.store.all_snapshot(include_history=True),before);return public
 def test_actual_sparse_card_native96_known_upload_finishes_local96_without_io(self):
  source=SOURCE/'owned-card-native-handoff-live-2026-10-14/catalogue.sqlite3'
  # Original source untouched; replay exact immutable source heads/images/dates.
  self.store=None
  with sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True) as origin,sqlite3.connect(self.root/'copied.sqlite3') as target:origin.backup(target)
  self.store=CatalogueStore(self.root/'copied.sqlite3');self.stack.enter_context(patch.object(app,'_catalogue_store',return_value=self.store))
  data=json.loads((SOURCE/'full-service-2026-10-14-typed-supplier-boundary-slice-1/parsed.json').read_text());positions=[PositionIn.model_validate(p) for p in data['positions']]
  product=self.store.lookup(identity_for_position(positions[9]));self.assertIsNotNone(product);self.assertEqual(len(product['coverage']),1)
  # Job uses real immutable accepted native configured36, including supplier root.
  import sqlite3 as sql
  original=SOURCE/'full-service-2026-10-14-typed-supplier-boundary-slice-1/jobs/research.sqlite3'
  with sql.connect('file:'+str(original)+'?mode=ro',uri=True) as db:self.hosts=json.loads(db.execute('SELECT payload FROM inputs WHERE job=?',('3dbf9ce0fb6a',)).fetchone()[0])['configured_hosts']
  for position in positions:
   if not position.is_unique and not is_graftio_equipment(position):self.store.acquire_unknown(identity_for_position(position),self.hosts,owner='other-job')
  key=self.prepare(positions);public=self.run_job(key)
  self.assertEqual((public['status'],public['completed'],public['total']),('done',96,96));self.assertEqual(sum(r['status']=='skipped' for r in public['rows']),10);self.assertEqual(sum(len(r['source_checks']) for r in public['rows']),3096)
  row=public['rows'][9];missing=[c for c in row['source_checks'] if c['host'] not in product['coverage']];self.assertEqual(len(missing),35);self.assertTrue(all(c['catalogue_snapshot'] is True and c['not_attempted'] is True and c['observed_at'] is None and c.get('checked_at') is None and c['product_presence']=='unknown' for c in missing))
 def test_other_owner_running_snapshot_done_but_original_owned_running_stays_parked(self):
  position=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1);key=self.prepare([position]);a=self.store.acquire_unknown(identity_for_position(position),self.hosts[:1],owner=key)
  app.RESEARCH_JOBS[key].update(status='running',awaiting_slice=True,acquisition_resume_only=True);app._save_research_job(key)
  public=self.run_job(key);self.assertEqual(public['status'],'running');self.assertEqual(public['completed'],0);self.assertTrue(public['awaiting_slice']);self.assertTrue(public['acquisition_resume_only']);self.assertTrue(public['rows'][0]['catalogue_acquisition_running'])
  new=self.prepare([position]);warm=self.run_job(new);self.assertEqual(warm['status'],'done');self.assertEqual(warm['completed'],1)
 def test_actual_original85_acquisitions_remain_parked_while_warm_read_can_finish(self):
  original=SOURCE/'full-service-2026-10-14-typed-supplier-boundary-slice-1';key='3dbf9ce0fb6a'
  for source,target in ((original/'catalogue/catalogue.sqlite3',self.root/'original-catalogue.sqlite3'),(original/'jobs/research.sqlite3',self.root/'jobs/research.sqlite3')):
   target.parent.mkdir(parents=True,exist_ok=True)
   with sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True) as origin,sqlite3.connect(target) as destination:origin.backup(destination)
  self.store=CatalogueStore(self.root/'original-catalogue.sqlite3');self.stack.enter_context(patch.object(app,'_catalogue_store',return_value=self.store))
  job,positions=app._load_research_job(key);app.RESEARCH_JOBS[key]=job;app._RESEARCH_INPUTS[key]=positions;self.addCleanup(app.RESEARCH_JOBS.pop,key,None);self.addCleanup(app._RESEARCH_INPUTS.pop,key,None);self.hosts=job['configured_hosts']
  public=self.run_job(key);self.assertEqual(public['status'],'running');self.assertTrue(public['awaiting_slice']);self.assertTrue(public['acquisition_resume_only']);self.assertEqual(sum(bool(r.get('catalogue_acquisition_running')) for r in public['rows']),85);self.assertEqual(public['completed'],11)
 def test_late_cancellation_provider_stop_and_inflight_owner_prevent_done(self):
  from engine.search.failure_policy import SearchServiceUnavailable
  for kind in ('cancel','provider','owner'):
   with self.subTest(kind=kind):
    p=PositionIn(number=1,name='Cable',type_mark='LATE'+kind,unit='м',qty=1);admission=self.store.acquire_unknown(identity_for_position(p),self.hosts[:1],owner='other');key=self.prepare([p]);publish=app._publish_catalogue_row;trigger=[False]
    def after_projection(*a,**kw):
     result=publish(*a,**kw)
     if not trigger[0]:
      trigger[0]=True
      if kind=='cancel':app.RESEARCH_JOBS[key]['cancel_requested']=True
      elif kind=='provider':app.RESEARCH_JOBS[key]['first_stop']=SearchServiceUnavailable('account_balance').diagnostic()
      else:
       with self.store._connection(True) as db:db.execute('UPDATE acquisitions SET owner=? WHERE product_id=?',(key,admission.product_id))
     return result
    with patch.object(app,'_publish_catalogue_row',side_effect=after_projection):
     if kind=='owner':
      # Only fixture race mutates ownership; final READ makes no further catalogue write.
      app._run_research_job(key);public=app._research_store().public(key)
     else:public=self.run_job(key)
    self.assertEqual(public['status'],'cancelled' if kind=='cancel' else 'error' if kind=='provider' else 'running')
    if kind=='owner':self.assertEqual(public['completed'],0);self.assertTrue(public['rows'][0]['catalogue_acquisition_running'])
 def test_lifecycle_lost_ack_replays_immutable_payload_after_later_list_mutation(self):
  from engine.research_store import StoreError
  p=PositionIn(number=1,name='Cable',type_mark='ACK123',unit='м',qty=1);key=self.prepare([p]);job=app.RESEARCH_JOBS[key];job['catalogue_awaiting_products']=['a'*64];publisher=app._research_publisher(key);original=publisher.lifecycle
  def lost_ack(*a,**kw):
   result=original(*a,**kw);job['catalogue_awaiting_products'].append('b'*64);raise StoreError('Injected acknowledgement loss')
  with patch.object(publisher,'lifecycle',side_effect=lost_ack):
   with self.assertRaises(StoreError):app._save_research_job(key)
  retry=app._RESEARCH_LIFECYCLE_RETRIES[(app.RESEARCH_JOB_DIR.resolve(),key)][1]
  self.assertEqual(retry['catalogue_awaiting_products'],['a'*64])
  app._save_research_job(key);public=app._research_store().public(key);self.assertEqual(public['catalogue_awaiting_products'],['a'*64,'b'*64])
 def test_cancel_and_terminal_provider_error_never_become_done(self):
  for kind in ('cancel','provider'):
   with self.subTest(kind=kind):
    position=PositionIn(number=1,name='Cable',type_mark='ABC'+kind,unit='м',qty=1);self.store.acquire_unknown(identity_for_position(position),self.hosts[:1],owner='other')
    key=self.prepare([position]);job=app.RESEARCH_JOBS[key]
    if kind=='cancel':job['cancel_requested']=True;app._save_research_job(key)
    else:job.update(status='error',error_code='search_account_error',error='Provider failure');app._save_research_job(key)
    public=self.run_job(key);self.assertEqual(public['status'],'cancelled' if kind=='cancel' else 'error')
