"""Real96 identities/native catalogue stop plus actual report/ACK executor cases."""
import json,sqlite3,tempfile,time,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from .schemas import PositionIn
from .equipment_output_policy import is_graftio_equipment
from .search.catalogue import CatalogueStore,identity_for_position
from .search.acquisition_lease import AcquisitionLeaseKeeper
from .test_runner_finalization403 import RunnerFinalization403Tests

class GlobalStop407Tests(unittest.TestCase):
 def test_real96_native_stop_preserves_unknown_coverage_and_card_intent(self):
  # Exact approved input source; no embedded offers/retimestamp or external work.
  root=Path(__file__).resolve()
  base=next(p for p in root.parents if p.name=='atomic-simplification-2026-10-14')
  raw=(base/'EPIC-0001-TASK-0279/quality/verified-public/controls/approved-full96.json').read_bytes()
  import hashlib
  self.assertEqual(hashlib.sha256(raw).hexdigest(),'28de220b3c3c91635870ec4c2a2545e920770f51cefb4dd2798233b289e91a88')
  positions=[PositionIn.model_validate(p)for p in json.loads(raw)['positions']]
  identities={identity_for_position(p).key:identity_for_position(p)for p in positions if not p.is_unique and not is_graftio_equipment(p)}
  hosts=['supplier%d.example'%i for i in range(36)] # Load shape only; never site calls/config changes.
  with tempfile.TemporaryDirectory()as folder:
   store=CatalogueStore(Path(folder)/'catalogue.sqlite3');keeper=AcquisitionLeaseKeeper(store,lambda:None,owner='offline407')
   for identity in identities.values():keeper.add(store.acquire_unknown(identity,hosts,owner='offline407'),identity=identity)
   first=next(iter(keeper._entries.values())).admission
   store.card_operation(first.product_id,hosts[0],'https://'+hosts[0]+'/card',operation_id=uuid.uuid4().hex,acquisition=first)
   with store._connection()as db:
    before=[tuple(r)for r in db.execute('SELECT * FROM coverage ORDER BY product_id,host')];attempts=[tuple(r)for r in db.execute('SELECT * FROM attempts ORDER BY id')]
   start=time.monotonic()
   with patch.object(store,'publish_coverage',side_effect=AssertionError('no_eager_coverage_rewrite')),patch.object(store,'lookup',side_effect=AssertionError('no_expensive_projection_on_stop')):
    keeper.stop(revoke=True,reason='Global time budget ended',cancelled=False)
   elapsed=time.monotonic()-start
   with store._connection()as db:
    self.assertEqual(before,[tuple(r)for r in db.execute('SELECT * FROM coverage ORDER BY product_id,host')]);self.assertEqual(attempts,[tuple(r)for r in db.execute('SELECT * FROM attempts ORDER BY id')]);states=[r[0]for r in db.execute('SELECT status FROM acquisitions')]
   self.assertEqual(len(positions),96);self.assertTrue(all(s=='failed'for s in states));self.assertLess(elapsed,5)
   print(json.dumps({'native96_stop_seconds':round(elapsed,3),'products':len(identities),'hosts_per_product':36,'coverage_records_preserved':len(before),'card_intents_preserved':len(attempts),'coverage_diagnostic_writes':0}))

class NativePublication407Tests(RunnerFinalization403Tests):
 def test_real96_native_current_projection_builders_and_partial_ack(self):
  from .runner_executor import NativeResearchRunner,DirectControl
  from .search.session import bounded_timeout,SearchBudgetExceeded
  from io import BytesIO
  from zipfile import ZipFile
  from openpyxl import load_workbook
  base=next(p for p in Path(__file__).resolve().parents if p.name=='atomic-simplification-2026-10-14')
  positions=[PositionIn.model_validate(p)for p in json.loads((base/'EPIC-0001-TASK-0279/quality/verified-public/controls/approved-full96.json').read_bytes())['positions']]
  store=self.fixture.store;hosts=['supplier%d.example'%i for i in range(36)];keeper=AcquisitionLeaseKeeper(store,lambda:None,owner='offline407')
  for p in positions:
   if p.is_unique or is_graftio_equipment(p):continue
   identity=identity_for_position(p);admission=store.acquire_unknown(identity,hosts,owner='offline407')
   if admission.allowed:keeper.add(admission,identity=identity)
  keeper.stop(revoke=True,reason='Global time budget ended',cancelled=False)
  job=self.control.enqueue({'positions':[p.model_dump(mode='json')for p in positions],'acquisition_mode':'catalogue_read'},'native96_fixture407_once','server-basic:fixture',work_seconds=1800)
  claim=self.control.claim('fixture-runner',self.seal);runner=NativeResearchRunner(self.root/'runner96',DirectControl(self.control),self.native,clock=lambda:self.now[0],heartbeat_seconds=10000);original=self.native._run_research_job
  def reserved(j):
   self.now[0]=claim['deadline']-60
   with self.assertRaises(SearchBudgetExceeded):bounded_timeout(1)
   original(j)
  with patch.object(self.native,'_run_research_job',side_effect=reserved):runner.execute(claim,self.seal)
  public=self.control.public(job);self.assertEqual(public['dispatch_state'],'cancelled');self.assertTrue(public['partial']);self.assertEqual(len(public['rows']),96);self.assertFalse(public['business_complete'])
  entries={e['name']:e for e in public['downloads']};self.assertTrue({'supplier-research-summary.xlsx','supplier-research-package.zip'}<=set(entries))
  for e in entries.values():
   raw=self.control.artifact(job,e['sha256'])[0].read_bytes()
   with ZipFile(BytesIO(raw))as z:self.assertIsNone(z.testzip())
   if e['name'].endswith('.xlsx'):self.assertEqual(load_workbook(BytesIO(raw),read_only=True)['Позиции'].max_row,97)
  self.assertTrue(all(not row.get('offers')for row in public['rows']))
  print(json.dumps({'native96_partial_publication':'PASS','rows':96,'financially_promoted':0,'artifacts':len(entries)}))
 def test_typed_first_failure_stage_survives_outer_uncertainty(self):
  from .runner_executor import RunnerExecutionError
  job,claim,runner=self.setup_job();original=self.native._run_research_job
  def elapsed(j):original(j);self.now[0]=claim['deadline']
  with patch.object(self.native,'_run_research_job',side_effect=elapsed),self.assertRaises(RunnerExecutionError):runner.execute(claim,self.seal)
  with sqlite3.connect(runner.path)as db:state,ack=db.execute('SELECT state,ack FROM executions WHERE job=?',(job,)).fetchone()
  self.assertEqual(state,'uncertain');self.assertEqual(json.loads(ack)['first_finalization_error']['phase'],'freeze_snapshot');self.assertEqual(self.downloads(job),[])
