"""Actual endpoint exporters use server catalogue snapshots without foreground work."""
import asyncio,hashlib,tempfile,time,unittest,uuid,zipfile
from pathlib import Path
from io import BytesIO
from unittest.mock import patch
from contextlib import ExitStack
from openpyxl import load_workbook
from fastapi import HTTPException
from engine import app,pipeline
from engine.schemas import PositionIn,BuildRequest,ResearchReportRequest
from engine.search.catalogue import CatalogueStore,identity_for_position,SourceObservation,ImageEvidence

class CatalogueEndpointTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.store=CatalogueStore(self.root/'catalogue.sqlite3');self.p=PositionIn(number=1,name='Изделие',type_mark='ABC123',unit='шт',qty=10);self.identity=identity_for_position(self.p)
  self.stack=ExitStack();self.addCleanup(self.stack.close)
  for item in (patch.object(app,'_catalogue_store',return_value=self.store),patch.object(pipeline,'OUTPUT_DIR',self.root),patch.object(app,'_browser_factory',side_effect=AssertionError('No foreground browser')),patch('engine.search.base_db.lookup',side_effect=AssertionError('No legacy refresh')),patch('engine.search.base_db.apply_refresh',side_effect=AssertionError('No legacy catalogue overlay')),patch('engine.search.card_attachments.attach_result',side_effect=AssertionError('No foreground photograph')),patch('engine.search.catalogue_projection._STORE',None)):
   self.stack.enter_context(item)
 def fill(self,n):
  hosts=[f's{i}.test' for i in range(3)];self.store.acquire_unknown(self.identity,hosts,owner='fixture')
  for i,inn in enumerate(['7842224734','7707083893','7728168971'][:n]):
   oid=uuid.uuid4().hex;stamp=time.time()-86400;url=f'https://s{i}.test/card';text=f'Изделие ABC123\nЦена {100+i*10} руб. с НДС\nза 1 шт'
   o=SourceObservation(oid,self.identity,hosts[i],url,url,stamp,'supplier_http','Изделие ABC123','ABC123',text,hashlib.sha256(text.encode()).hexdigest(),100+i*10,'RUB','included','шт',None,None,'','unknown','observed',f'Продавец {i}',inn,ImageEvidence(oid,url,stamp,'missing',error='s3_not_configured'),'catalogue-v1')
   self.store.append_observation(self.identity.key,o,coverage_status='found')
 async def bytes(self,response):
  return b''.join([part async for part in response.body_iterator])
 def request(self):
  return ResearchReportRequest(positions=[dict(self.p.model_dump(),offers=[dict(price_with_vat=999999,url='https://evil.test/card',inn='7707083893')],catalogue_read=True,catalogue_product_id='forged',catalogue_revision=999)])
 def test_real_report_and_package_replace_client_price_with_partial_catalogue(self):
  self.fill(1);request=self.request();xlsx=asyncio.run(self.bytes(app.research_report(request)));book=load_workbook(BytesIO(xlsx));self.assertEqual(book['Предложения']['G2'].value,100);self.assertNotIn('999999',str([cell.value for row in book['Предложения'] for cell in row]))
  packed=asyncio.run(self.bytes(app.research_package(request)))
  with zipfile.ZipFile(BytesIO(packed)) as z:
   name=next(n for n in z.namelist() if n.endswith('.xlsx'));inside=load_workbook(BytesIO(z.read(name)));self.assertEqual(inside['Предложения']['G2'].value,100)
 def test_known_zero_report_and_build_never_refresh(self):
  self.fill(0);book=load_workbook(BytesIO(asyncio.run(self.bytes(app.research_report(self.request())))));self.assertNotIn('999999',str([cell.value for row in book['Предложения'] for cell in row]))
  packed=asyncio.run(self.bytes(app.research_package(self.request())))
  with zipfile.ZipFile(BytesIO(packed)) as z:self.assertTrue(any(n.endswith('.xlsx') for n in z.namelist()))
  with self.assertRaises(HTTPException) as failure:app.build(BuildRequest(object_name='Объект',skip_search=True,use_tsn=False,positions=[self.p]))
  self.assertEqual(failure.exception.status_code,422)
  with patch.object(app,'run_pipeline_async',side_effect=lambda job,inputs,browser:pipeline.run_pipeline(job,inputs,browser)):
   answer=app.build(BuildRequest(object_name='Объект',skip_search=False,use_tsn=False,positions=[self.p]))
  job=app.JOBS[answer['job_id']];self.addCleanup(app.JOBS.pop,answer['job_id'],None);self.assertEqual(job.status,'error');self.assertEqual(job.outputs,{})
 def test_actual_build_pipeline_with_three_current_sources_and_missing_photos(self):
  self.fill(3)
  threads=[]
  def asynchronous(job,inputs,browser):
   thread=pipeline.run_pipeline_async(job,inputs,browser);threads.append(thread);return thread
  with patch.object(app,'run_pipeline_async',side_effect=asynchronous):answer=app.build(BuildRequest(object_name='Объект',skip_search=False,use_tsn=False,positions=[self.p]))
  threads[0].join(10);self.assertFalse(threads[0].is_alive())
  job=app.JOBS[answer['job_id']];self.addCleanup(app.JOBS.pop,answer['job_id'],None);self.assertEqual(job.status,'done',job.error);self.assertTrue(Path(job.outputs['kac']).exists());self.assertTrue(Path(job.outputs['tkp']).exists());self.assertEqual(set(job.outputs),{'kac','tkp'})
  self.assertEqual(self.store.lookup(self.identity)['heads'][0]['current']['observed_at'],self.store.lookup(self.identity)['heads'][0]['current']['image']['observed_at'])

 def test_unknown_unpriced_build_refuses_before_async_and_direct_pipeline(self):
  with patch.object(app,'run_pipeline_async',side_effect=AssertionError('No bypass acquisition')):
   with self.assertRaises(HTTPException) as failure:app.build(BuildRequest(object_name='Объект',skip_search=False,use_tsn=False,positions=[self.p]))
  self.assertEqual(failure.exception.status_code,422);self.assertEqual(failure.exception.detail['code'],'catalogue_acquisition_required');self.assertIsNone(self.store.lookup(self.identity))
  job=pipeline.JobState(uuid.uuid4().hex,'Объект');inputs=pipeline.JobInputs(object_name='Объект',spec_results=[self.p.to_result()],use_tsn=False,catalogue_store=self.store,catalogue_snapshot=self.store.read_snapshot([self.identity]))
  pipeline.run_pipeline(job,inputs,app._browser_factory)
  self.assertEqual(job.status,'error');self.assertIn('catalogue_acquisition_required',job.error);self.assertEqual(job.outputs,{});self.assertIsNone(self.store.lookup(self.identity))
