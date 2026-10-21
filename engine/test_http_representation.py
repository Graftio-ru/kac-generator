"""Strict bounded stream decoding and actual HTTP producer consumption."""
import gzip,zlib,time,unittest
from unittest.mock import patch
from contextlib import contextmanager
import httpx
from engine.http_representation import decode_chunks,RepresentationError,RepresentationLimit,BLOCK,VERSION
from engine import browser,llm
from engine.search import session,agent
from engine import test_http_card as fixture
class Stream(httpx.SyncByteStream):
 def __init__(self,chunks,before=None):self.chunks=chunks;self.closed=False;self.yields=0;self.before=before
 def __iter__(self):
  for chunk in self.chunks:
   if self.before:self.before()
   self.yields+=1;yield chunk
 def close(self):self.closed=True
class RepresentationTests(unittest.TestCase):
 def decode(self,kind,payload,bound=200000,step=3):return decode_chunks(kind,(payload[i:i+step] for i in range(0,len(payload),step)),bound,lambda:None)
 def test_identity_gzip_zlib_split_and_empty_members(self):
  body=('Cable ABC123 120 ₽ /м '*500).encode()
  for kind,payload in [('identity',body),('gzip',gzip.compress(body)),('deflate',zlib.compress(body)),('gzip',gzip.compress(b'')+gzip.compress(body)+gzip.compress(b''))]:
   result=self.decode(kind,payload);self.assertEqual(result.content,body);self.assertEqual(result.metadata['raw_bytes'],len(payload));self.assertEqual(result.metadata['decoded_bytes'],len(body));self.assertEqual(result.metadata['version'],VERSION)
  self.assertEqual(self.decode('gzip',gzip.compress(b'')*5).metadata['members'],5)
 def test_raw_and_decoded_limits_with_bounded_expansion(self):
  with self.assertRaises(RepresentationLimit) as raw:self.decode('identity',b'X'*101,bound=100)
  self.assertEqual(raw.exception.reason,'raw_bytes_limit')
  with self.assertRaises(RepresentationLimit) as decoded:self.decode('gzip',gzip.compress(b'A'*500000),bound=100000)
  self.assertEqual(decoded.exception.reason,'decoded_bytes_limit');self.assertLessEqual(decoded.exception.metadata['decoded_bytes'],100001)
 def test_crc_truncation_trailing_padding_raw_deflate_and_chains_refused(self):
  data=gzip.compress(b'abc');bad=bytearray(data);bad[-8]^=1
  raw=zlib.compressobj(wbits=-15);raw_data=raw.compress(b'abc')+raw.flush()
  for kind,payload in [('gzip',bytes(bad)),('gzip',data[:-1]),('gzip',data+b'\x00'),('gzip',data+b'junk'),('deflate',zlib.compress(b'abc')[:-1]),('deflate',zlib.compress(b'abc')+b'junk'),('deflate',raw_data),('gzip',b''),('gzip',b'notgzip')]:
   with self.subTest(kind=kind,payload_length=len(payload)),self.assertRaises(RepresentationError):self.decode(kind,payload)
  for kind in ['br','gzip, deflate','unknown']:
   with self.assertRaises(RepresentationError) as error:self.decode(kind,b'anything')
   self.assertEqual(error.exception.metadata['encoding_kind'],'unsupported');self.assertEqual(error.exception.metadata['reason'],'unsupported_encoding')
 def test_budget_check_every_input_and_output_drain_not_swallowed(self):
  class Cancelled(RuntimeError):pass
  calls=[]
  def check():
   calls.append(1)
   if len(calls)==9:raise Cancelled()
  with self.assertRaises(Cancelled):decode_chunks('gzip',[gzip.compress(b'X'*150000)],200000,check)
  self.assertEqual(len(calls),9)
 def test_decompress_never_unbounded_or_flush(self):
  original=zlib.decompressobj;limits=[]
  class Bounded:
   def __init__(self,*a):self.actual=original(*a)
   def decompress(self,data,max_length):limits.append(max_length);return self.actual.decompress(data,max_length)
   def __getattr__(self,name):
    if name=='flush':raise AssertionError('No flush')
    return getattr(self.actual,name)
  with patch('engine.http_representation.zlib.decompressobj',Bounded):result=self.decode('gzip',gzip.compress(b'X'*180000))
  self.assertEqual(len(result.content),180000);self.assertTrue(all(0<n<=BLOCK for n in limits))
 def test_format_exception_cannot_hide_late_owned_control(self):
  flag=[False]
  class OwnedControl(RuntimeError):pass
  class Broken:
   def decompress(self,*args):flag[0]=True;raise zlib.error('invalid')
  def check():
   if flag[0]:raise OwnedControl()
  with patch('engine.http_representation.zlib.decompressobj',return_value=Broken()),self.assertRaises(OwnedControl):decode_chunks('gzip',[b'bad'],100,check)
class HttpRepresentationTests(unittest.TestCase):
 setUp=fixture.HttpCardTests.setUp
 @contextmanager
 def delivered(self,kind,body,declared=None,before=None,consumed=False):
  stream=Stream([body[i:i+3] for i in range(0,len(body),3)],before);calls=[];original=httpx.Client
  def handler(request):
   calls.append(request);headers={'content-type':'text/html; charset=utf-8','content-encoding':kind}
   if declared is not None:headers['content-length']=declared
   return httpx.Response(200,headers=headers,content=body if consumed else None,stream=None if consumed else stream,request=request)
  with patch.object(httpx,'Client',side_effect=lambda **kw:original(transport=httpx.MockTransport(handler),**kw)):yield stream,calls
 def test_actual_streamed_normal_producer_identity_and_quote_for_supported_codings(self):
  text=fixture.html().encode()
  for kind,body in [('identity',text),('gzip',gzip.compress(text)),('deflate',zlib.compress(text))]:
   with self.delivered(kind,body) as (stream,calls),patch.object(browser.Browser,'_start',side_effect=AssertionError('No browser')),patch.object(llm,'complete_json',side_effect=AssertionError('No model')),session.research_session(deadline=time.monotonic()+10):
    with browser.Browser() as owned:offers,_=agent._collect_offers(owned,[fixture.URL],1,'ABC123','Cable','м',supplier_hosts={'supplier.example'})
   self.assertEqual(len(offers),1);self.assertEqual(offers[0].price_with_vat,120);self.assertEqual(offers[0].identity_evidence['provenance'],'http_main_product');self.assertEqual(len(calls),1);self.assertTrue(stream.closed)
 def test_success_metadata_and_consumed_compressed_fallback_type(self):
  with self.delivered('gzip',gzip.compress(b'abc')) as (stream,calls),session.research_session(deadline=time.monotonic()+10):result=browser.supplier_http_get(fixture.URL,max_bytes=100)
  self.assertEqual(result.content,b'abc');self.assertNotIn('content-encoding',result.headers);self.assertEqual(result.extensions['supplier_http_representation']['decoded_bytes'],3);self.assertTrue(stream.closed)
  with self.delivered('gzip',gzip.compress(b'abc'),consumed=True),session.research_session(deadline=time.monotonic()+10):
   with self.assertRaises(browser.SupplierHttpRepresentation) as error:browser.supplier_http_get(fixture.URL,max_bytes=100)
  self.assertEqual(error.exception._kac_http_representation['reason'],'consumed_compressed')
 def test_bad_stream_and_declared_size_lie_close_response(self):
  for kind,body,expected in [('gzip',gzip.compress(b'x'*1000),browser.SupplierHttpLimit),('gzip',gzip.compress(b'abc')[:-1],browser.SupplierHttpRepresentation),('br',b'fake',browser.SupplierHttpRepresentation)]:
   with self.delivered(kind,body,declared='1') as (stream,calls),session.research_session(deadline=time.monotonic()+10):
    with self.assertRaises(expected):browser.supplier_http_get(fixture.URL,max_bytes=100)
   self.assertTrue(stream.closed);self.assertEqual(len(calls),1)
 def test_compressed_raw_drip_obeys_original_child_and_parent_control(self):
  clock=[100.0]
  def tick():clock[0]+=1
  # zlib may emit no decoded bytes through a partial gzip header; raw boundary
  # checks still expire the HTTP child at15, not after all compressed chunks.
  raw=gzip.compress(bytes(range(100)))
  with self.delivered('gzip',raw,before=tick) as (stream,calls),patch('engine.browser.time.monotonic',side_effect=lambda:clock[0]),session.research_session(deadline=160):
   with self.assertRaises(httpx.ReadTimeout):browser.supplier_http_get(fixture.URL,max_bytes=1000)
  self.assertEqual(clock[0],115);self.assertLess(stream.yields,len(stream.chunks));self.assertTrue(stream.closed)
 def test_cancel_during_raw_delivery_authoritative_and_no_retry(self):
  flag=[False]
  def cancel():flag[0]=True
  from engine.search.failure_policy import ResearchControlError
  with self.delivered('gzip',gzip.compress(b'abc'),before=cancel) as (stream,calls),session.research_session(deadline=time.monotonic()+10,cancelled=lambda:flag[0]):
   with self.assertRaises(ResearchControlError):browser.supplier_http_get(fixture.URL,max_bytes=100)
  self.assertEqual(len(calls),1);self.assertTrue(stream.closed)

 def test_true_parent_expiry_stays_control_not_http_local_timeout(self):
  clock=[100.0]
  def tick():clock[0]+=1
  from engine.search.failure_policy import ResearchControlError
  with self.delivered('gzip',gzip.compress(b'abc'),before=tick) as (stream,calls),patch('engine.browser.time.monotonic',side_effect=lambda:clock[0]),session.research_session(deadline=102):
   with self.assertRaises(ResearchControlError):browser.supplier_http_get(fixture.URL,max_bytes=100)
  self.assertTrue(stream.closed);self.assertEqual(len(calls),1)
 def test_unsupported_format_does_not_penalize_normal_supplier_health(self):
  from engine.search.supplier_health_store import SupplierHealthStore,Outcome
  store=SupplierHealthStore(self.root/'health.sqlite3');store.finish(store.acquire('supplier.example'),Outcome.SUCCESS);health=session.PersistentSupplierHealth(store,hosts=['supplier.example'])
  for kind,body in [('br',b'unsupported'),('gzip',gzip.compress(b'abc')[:-2])]:
   with self.delivered(kind,body),session.research_session(health,deadline=time.monotonic()+10):
    with self.assertRaises(browser.SupplierHttpRepresentation):
     with session.supplier_transport(fixture.URL):browser.supplier_http_get(fixture.URL,max_bytes=100)
   snapshot=store.snapshot('supplier.example');self.assertEqual(snapshot['state'],'healthy');self.assertEqual(snapshot.get('retry_at') or 0,0)
 def test_brotli_real_stream_routes_existing_render_once_without_consuming_body(self):
  from engine.models import Requisites
  page=fixture.HttpCardTests.rendered_page(self);opens=[]
  class Owned:
   @contextmanager
   def page(self,url):opens.append(url);yield page
  with self.delivered('br',b'never read') as (stream,calls),patch.object(agent,'wl_requisites_for',return_value=Requisites(name='Supplier',inn='7842224734')),patch.object(llm,'complete_json',side_effect=AssertionError('No model')),session.research_session(deadline=time.monotonic()+10):
   offers,_=agent._collect_offers(Owned(),[fixture.URL],1,'ABC123','Cable','м',supplier_hosts={'supplier.example'})
  self.assertEqual(len(offers),1);self.assertEqual(opens,[fixture.URL]);self.assertEqual(stream.yields,0);self.assertTrue(stream.closed);self.assertEqual(len(calls),1)
 def test_huge_declared_length_is_typed_limit_without_integer_overflow(self):
  with self.delivered('identity',b'abc',declared='9'*5000) as (stream,calls),session.research_session(deadline=time.monotonic()+10):
   with self.assertRaises(browser.SupplierHttpLimit):browser.supplier_http_get(fixture.URL,max_bytes=100)
  self.assertTrue(stream.closed);self.assertEqual(stream.yields,0)
