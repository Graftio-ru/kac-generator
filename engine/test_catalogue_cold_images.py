"""Configured object retrieval plus actual server-owned XLSX/ZIP composition."""
import asyncio,hashlib,json,tempfile,time,unittest,uuid,zipfile
from pathlib import Path
from io import BytesIO
from dataclasses import asdict
from contextlib import ExitStack
from unittest.mock import patch
from PIL import Image
from openpyxl import load_workbook
from engine.search.catalogue_images import CatalogueImageStore
from engine.search.catalogue import ImageEvidence,SourceObservation

class Objects:
 def __init__(self):self.object=None;self.calls=[];self.body=None;self.failure=None;self.change=lambda response:response
 def put_object(self,**kw):self.object=kw;return {'ResponseMetadata':{'HTTPStatusCode':200}}
 def get_object(self,**kw):
  self.calls.append(kw)
  if self.failure:raise self.failure
  self.body=BytesIO(self.object['Body'])
  return self.change(dict(ResponseMetadata={'HTTPStatusCode':200},ContentType='image/png',ContentLength=len(self.object['Body']),Metadata=self.object['Metadata'],Body=self.body))

class ColdImageTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.client=Objects();self.oid=uuid.uuid4().hex;self.url='https://s0.test/card?id=17';self.stamp=time.time()-86400
  output=BytesIO();Image.new('RGB',(80,60),'white').save(output,format='PNG');self.data=output.getvalue()
  warm=CatalogueImageStore(self.root/'original',client=self.client,bucket='shots')
  retained=warm.retain(self.data,observation_id=self.oid,url=self.url,observed_at=self.stamp,captured_at=self.stamp+1)
  filename=Path(retained['local_path']).name;self.context=dict(version=1,role='review_context',filename=filename,sha256=retained['sha256'],url=self.url,captured_at=self.stamp+1,width=80,height=60,document_width=80,document_height=60,truncated=False)
  self.image=asdict(ImageEvidence(self.oid,self.url,self.stamp,'captured',sha256=retained['sha256'],local_path=filename,size=len(self.data),bucket=retained['bucket'],key=retained['key'],captured_at=self.stamp+1,context_evidence=self.context))
  self.store=CatalogueImageStore(self.root/'cold',client=self.client,bucket='shots');self.args=dict(observation_id=self.oid,url=self.url,observed_at=self.stamp)
 def test_cold_get_exact_binding_bytes_date_and_warm_reuse_no_put(self):
  before=json.loads(json.dumps(self.image));result=self.store.materialize(self.image,**self.args)
  self.assertEqual(result['status'],'materialized');self.assertEqual(Path(result['local_path']).read_bytes(),self.data);self.assertEqual(self.image,before);self.assertEqual(len(self.client.calls),1);self.assertTrue(self.client.body.closed)
  self.assertEqual(self.store.materialize(self.image,**self.args)['status'],'local');self.assertEqual(len(self.client.calls),1)
 def test_sdk_aws_chunked_label_keeps_exact_png_hash_and_other_binding_checks(self):
  self.client.change=lambda response:{**response,'ContentEncoding':'aws-chunked'}
  result=self.store.materialize(self.image,**self.args);self.assertEqual(result['status'],'materialized');self.assertEqual(Path(result['local_path']).read_bytes(),self.data)
  Path(result['local_path']).unlink()
  self.client.change=lambda response:{**response,'ContentEncoding':'aws-chunked','Body':BytesIO(b'x'*len(self.data))}
  self.assertEqual(self.store.materialize(self.image,**self.args)['error'],'s3_image_hash_mismatch');self.assertFalse((self.store.root/self.image['local_path']).exists())
 def test_foreign_reference_observation_date_key_path_and_size_refuse_without_get(self):
  for override in ({'observation_id':uuid.uuid4().hex},{'observed_at':self.stamp+1},{'bucket':'foreign'},{'key':'other/object.png'},{'local_path':'../secret.png'},{'size':True},{'size':21*1024*1024},{'sha256':None}):
   with self.subTest(override=override):self.assertTrue(self.store.materialize({**self.image,**override},**self.args)['error'])
  self.assertEqual(self.client.calls,[])
 def test_remote_hash_metadata_and_length_mismatch_never_publish_and_close(self):
  for kind in ('hash','metadata','size'):
   with self.subTest(kind=kind):
    def changed(response):
     if kind=='hash':response['Body']=self.client.body=BytesIO(b'x'*len(self.data))
     elif kind=='metadata':response['Metadata']={**response['Metadata'],'context':'wrong-date-binding'}
     else:response['ContentLength']=len(self.data)+1
     return response
    self.client.change=changed;result=self.store.materialize(self.image,**self.args)
    self.assertTrue(result['error']);self.assertFalse((self.store.root/self.image['local_path']).exists());self.assertTrue(self.client.body.closed)
 def test_safe_missing_remote_keeps_original_and_control_propagates(self):
  self.client.failure=RuntimeError('SECRET SDK credential response');result=self.store.materialize(self.image,**self.args);self.assertEqual(result['error'],'s3_image_get_failed');self.assertNotIn('SECRET',str(result));self.assertTrue((self.root/'original'/self.image['local_path']).is_file())
  from engine.search.failure_policy import ResearchControlError
  self.client.failure=ResearchControlError('Stopped')
  with self.assertRaises(ResearchControlError):self.store.materialize(self.image,**self.args)
 def test_oversized_body_and_symlink_fail_without_late_file(self):
  def changed(response):response['Body']=self.client.body=BytesIO(self.data+b'extra');return response
  self.client.change=changed;self.assertEqual(self.store.materialize(self.image,**self.args)['error'],'s3_image_size_mismatch');self.assertFalse((self.store.root/self.image['local_path']).exists())
  self.store.root.mkdir(exist_ok=True);(self.store.root/self.image['local_path']).symlink_to(self.root/'original'/self.image['local_path']);count=len(self.client.calls);self.assertTrue(self.store.materialize(self.image,**self.args)['error']);self.assertEqual(len(self.client.calls),count)
 def test_read_budget_and_cancellation_close_body_without_file(self):
  from engine.search import catalogue_images
  from engine.search.session import research_session
  from engine.search.failure_policy import ResearchControlError
  from types import SimpleNamespace
  clock=[0.0];cancel=[False]
  class Body(BytesIO):
   def read(inner,*args):
    data=super().read(*args)
    if cancel[0]:cancel[1]=True
    else:clock[0]+=31
    return data
  def changed(response):response['Body']=self.client.body=Body(self.data);return response
  self.client.change=changed
  with patch.object(catalogue_images,'time',SimpleNamespace(monotonic=lambda:clock[0])):
   self.assertEqual(self.store.materialize(self.image,**self.args)['error'],'s3_image_read_budget')
  self.assertTrue(self.client.body.closed);self.assertFalse((self.store.root/self.image['local_path']).exists())
  cancel[:]=[True,False]
  with research_session(cancelled=lambda:cancel[1]):
   with self.assertRaises(ResearchControlError):self.store.materialize(self.image,**self.args)
  self.assertTrue(self.client.body.closed);self.assertFalse((self.store.root/self.image['local_path']).exists())
 def endpoint(self):
  from engine import test_catalogue_endpoints as fixtures,app,config
  from engine.builders import research_package
  f=fixtures.CatalogueEndpointTests();f.setUp();self.addCleanup(f.doCleanups)
  f.store.acquire_unknown(f.identity,['s0.test'],owner='fixture');text='Изделие ABC123\n100 руб. с НДС\nза 1 шт'
  observation=SourceObservation(self.oid,f.identity,'s0.test',self.url,self.url,self.stamp,'supplier_rendered','Изделие ABC123','ABC123',text,hashlib.sha256(text.encode()).hexdigest(),100,'RUB','included','шт',1,None,'','in_stock','observed','Продавец','7707083893',ImageEvidence(**self.image),'fixture-v1')
  f.store.append_observation(f.identity.key,observation,coverage_status='found');stack=ExitStack();self.addCleanup(stack.close)
  for item in (patch.object(app,'CACHE_DIR',self.store.root),patch.object(config,'CACHE_DIR',self.store.root),patch.object(research_package,'CACHE_DIR',self.store.root),patch.object(app,'_catalogue_image_store',return_value=self.store),patch('engine.llm.LLMRequestGuard',side_effect=AssertionError('No model'))):stack.enter_context(item)
  return f,app
 def test_real_package_cold_s3_server_authority_unchanged_price_date_png_and_links(self):
  f,app=self.endpoint();self.client.change=lambda response:{**response,'ContentEncoding':'aws-chunked'};request=f.request();request.positions[0].offers[0].catalogue_image={'bucket':'caller-forged','key':'caller/path'}
  revision=f.store.all_snapshot()['revision'];packed=asyncio.run(f.bytes(app.research_package(request)))
  self.assertEqual(self.client.calls,[dict(Bucket='shots',Key=self.image['key'])]);self.assertEqual(f.store.all_snapshot()['revision'],revision)
  with zipfile.ZipFile(BytesIO(packed)) as archive:
   names=[n for n in archive.namelist() if n.startswith('screenshots/')];self.assertTrue(names);self.assertTrue(all(archive.read(n)==self.data for n in names))
   book=load_workbook(BytesIO(archive.read('supplier-research-summary.xlsx')));self.assertEqual(book['Предложения']['G2'].value,100)
   from engine.builders.research_report_xlsx import _checked_date
   self.assertEqual(book['Предложения']['Y2'].value,_checked_date(self.stamp));targets=[c.hyperlink.target for row in book['Предложения'] for c in row if c.hyperlink];self.assertTrue(any(t.startswith('screenshots/') for t in targets))
  asyncio.run(f.bytes(app.research_report(request)));self.assertEqual(len(self.client.calls),1)
 def test_real_export_get_failure_retains_price_date_and_typed_missing_photo(self):
  f,app=self.endpoint();self.client.failure=RuntimeError('SECRET')
  packed=asyncio.run(f.bytes(app.research_package(f.request())))
  with zipfile.ZipFile(BytesIO(packed)) as archive:
   self.assertFalse(any(n.startswith('screenshots/') for n in archive.namelist()));book=load_workbook(BytesIO(archive.read('supplier-research-summary.xlsx')))
   self.assertEqual(book['Предложения']['G2'].value,100);self.assertIn('s3_image_get_failed',str(list(book['Предложения'].values)));self.assertNotIn('SECRET',str(list(book['Предложения'].values)))
   from engine.builders.research_report_xlsx import _checked_date
   self.assertEqual(book['Предложения']['Y2'].value,_checked_date(self.stamp))
 def test_export_unknown_transport_failure_latches_without_retry_storm(self):
  from engine import app
  image2={**self.image,'observation_id':uuid.uuid4().hex};observations=[dict(observation_id=self.oid,url=self.url,observed_at=self.stamp,image=self.image),dict(observation_id=image2['observation_id'],url=self.url,observed_at=self.stamp,image=image2)]
  snapshot={'products':{'fixture':{'heads':[{'current':value} for value in observations]}}};self.client.failure=RuntimeError('SECRET endpoint failure')
  with patch.object(app,'CACHE_DIR',self.store.root),patch.object(app,'_catalogue_image_store',return_value=self.store):errors=app._catalogue_export_images(snapshot)
  self.assertEqual(len(self.client.calls),1);self.assertEqual(errors[observations[0]['observation_id']],'s3_image_get_failed');self.assertEqual(errors[observations[1]['observation_id']],'s3_image_export_unavailable')
