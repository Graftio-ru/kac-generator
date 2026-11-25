"""Native report journal disclosure; no supplier or model calls."""
import json,sqlite3,tempfile,time,unittest,uuid
from pathlib import Path
from io import BytesIO
from zipfile import ZipFile
from unittest.mock import patch
from openpyxl import load_workbook
from .models import SpecPosition
from .schemas import ResearchReportRequest
from .search.catalogue import CatalogueStore,identity_for_position
from .search.catalogue_projection import catalogue_scope
from .builders.research_report_xlsx import build_research_report
from .builders.research_package import build_research_package

class CatalogueJournalTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.position=SpecPosition(number=1,name='Изделие',type_mark='ABC123',unit='шт',qty=1)
  self.identity=identity_for_position(self.position);self.store=CatalogueStore(self.root/'catalogue.sqlite3');self.store.acquire_unknown(self.identity,['shop.test'],owner='fixture')
 def request(self):
  product=self.store.lookup(self.identity)
  return ResearchReportRequest(positions=[dict(self.position.model_dump(),catalogue_product_id=self.identity.key,catalogue_revision=product['revision'],catalogue_refresh_attempts=product['refresh_attempts'],source_checks=[dict(host='shop.test',**product['coverage']['shop.test'])])])
 def journal(self,request):
  with catalogue_scope(self.store,snapshot=self.store.read_snapshot([self.identity])):
   return load_workbook(BytesIO(build_research_report(request)))['Журнал источников']
 def test_all_urls_and_errors_native_order_resolution_clock_rollback_and_zip(self):
  first,second=uuid.uuid4().hex,uuid.uuid4().hex
  self.store.card_operation(self.identity.key,'shop.test','https://shop.test/first',operation_id=first)
  self.store.card_operation(self.identity.key,'shop.test','https://shop.test/second',operation_id=second)
  now=time.time();event=dict(stage='provider_transport',seconds=.1,ok=False,label=json.dumps(dict(logical_id=uuid.uuid4().hex,provider='cloudru',ordinal=1,phase='tcp_connect',outcome='error',exception_type='ConnectTimeout',observed_at=now,elapsed_seconds=.1,endpoint_send_started=False,proven_presend=True,attempt_started_at=now-.1)))
  self.store.card_operation(self.identity.key,'shop.test','https://shop.test/first',operation_id=first,resolve=True,evidence={'basis':'proven_presend','events':[event]})

  self.store.record_refresh_attempt(self.identity.key,'shop.test','https://shop.test/third',attempted_at=time.time(),outcome='network_error',reason='=HYPERLINK("unsafe")')
  # Native append order remains authoritative despite regressed wall-clock time.
  with self.store._connection(True)as db:db.execute('UPDATE attempts SET attempted_at=1 WHERE id=3')
  request=self.request();sheet=self.journal(request);rows=list(sheet.values)[1:]
  self.assertEqual(len(rows),4);self.assertEqual([r[5]for r in rows],sorted(r[5]for r in rows))
  first_rows=[r for r in rows if r[10]==first];self.assertEqual(len(first_rows),2)
  self.assertTrue(all(r[12]=='resolved'for r in first_rows));self.assertEqual(first_rows[0][13],'Нет');self.assertEqual(first_rows[1][13],'Да')
  pending=[r for r in rows if r[12]=='pending'and r[13]=='Да'];self.assertEqual([r[4]for r in pending],['https://shop.test/second'])
  error=next(r for r in rows if r[8]=='network_error');self.assertEqual(error[12],'Не установлено');self.assertEqual(error[9],'=HYPERLINK("unsafe")')
  cell=sheet.cell(next(i for i in range(2,6)if sheet.cell(i,9).value=='network_error'),10);self.assertEqual(cell.data_type,'s')
  with catalogue_scope(self.store,snapshot=self.store.read_snapshot([self.identity])):
   package=build_research_package(request)
  with ZipFile(BytesIO(package))as archive:
   embedded=load_workbook(BytesIO(archive.read('supplier-research-summary.xlsx')))['Журнал источников']
   self.assertEqual(list(embedded.values),list(sheet.values))
 def test_native_metadata_cannot_be_overridden_by_payload(self):
  self.store.record_refresh_attempt(self.identity.key,'shop.test','https://shop.test/card',attempted_at=time.time(),outcome='unknown',reason='known unknown')
  with self.store._connection(True)as db:
   row=db.execute('SELECT * FROM attempts').fetchone();data=json.loads(row['payload']);data.update(catalogue_attempt_id=999,catalogue_attempt_revision=999)
   db.execute('UPDATE attempts SET payload=? WHERE id=?',(json.dumps(data),row['id']))
  item=self.request().positions[0].catalogue_refresh_attempts[0]
  self.assertEqual(item['catalogue_attempt_id'],row['id']);self.assertEqual(item['catalogue_attempt_revision'],row['revision'])
 def test_missing_or_duplicate_native_order_does_not_invent_current_state(self):
  self.store.card_operation(self.identity.key,'shop.test','https://shop.test/card',operation_id=uuid.uuid4().hex)
  request=self.request();request.positions[0].catalogue_refresh_attempts[0].pop('catalogue_attempt_id')
  row=list(self.journal(request).values)[1];self.assertEqual(row[11],'pending');self.assertEqual(row[12],'Не установлено')
 def test_overlong_reason_refused_instead_of_silently_truncated(self):
  self.store.record_refresh_attempt(self.identity.key,'shop.test','https://shop.test/card',attempted_at=time.time(),outcome='unknown',reason='known unknown')
  request=self.request();request.positions[0].catalogue_refresh_attempts[0]['reason']='x'*32768
  with self.assertRaisesRegex(ValueError,'лимит Excel'):build_research_report(request)
 def test_empty_journal_keeps_legacy_report_without_new_sheet(self):
  workbook=load_workbook(BytesIO(build_research_report(self.request())))
  self.assertNotIn('Журнал источников',workbook.sheetnames)

 def test_client_only_forged_native_order_does_not_establish_current_state(self):
  self.store.card_operation(self.identity.key,'shop.test','https://shop.test/card',operation_id=uuid.uuid4().hex)
  request=self.request()
  book=load_workbook(BytesIO(build_research_report(request)))
  self.assertEqual(book['Журнал источников']['M2'].value,'Не установлено')
  request.positions[0].catalogue_refresh_attempts[0]['reason']='forged current data'
  with catalogue_scope(self.store,snapshot=self.store.read_snapshot([self.identity])):
   changed=load_workbook(BytesIO(build_research_report(request)))
  self.assertEqual(changed['Журнал источников']['M2'].value,'Не установлено')
