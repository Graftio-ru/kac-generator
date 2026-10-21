"""Job-only neutral delivered document cache; never row judgments or prices.

No background work, retry layer, persisted cache, error memo or global state.
Original observation date survives every hit. An uncacheable owner result is
not shared with followers; they receive an explicit no-document outcome.
"""
from dataclasses import dataclass
from collections import OrderedDict
from concurrent.futures import Future,TimeoutError as FutureTimeout
import hashlib,json,math,threading,time,uuid
from .session import bounded_timeout
from .product_identity import canonical_card_url,card_host

_FIELDS={'http':{'record','source'},'rendered':{'coherent','source','status','identity_observation'}}
_FORBIDDEN={'expected_name','expected_type_mark','expected_unit','expected_qty','identity_evidence','price_with_vat','listed_price_with_vat','matches_requested_product','adapter','extraction','screenshot_context','context_capture_evidence'}

@dataclass(frozen=True)
class RawDocument:
 generation:str
 payload:str
 sha256:str
 captured_at:float
 cacheable:bool
 @classmethod
 def create(cls,kind,requested_url,actual_url,host,route_hash,presentation,data,captured_at,*,cacheable=True):
  requested=canonical_card_url(requested_url);actual=canonical_card_url(actual_url)
  if kind not in _FIELDS or type(data) is not dict or set(data)!=_FIELDS[kind]:raise ValueError('Neutral document shape invalid')
  def neutral(value):
   if type(value) is dict:
    if set(value)&_FORBIDDEN:return False
    return all(type(k) is str and neutral(v) for k,v in value.items())
   if type(value) is list:return all(neutral(v) for v in value)
   return type(value) in (str,int,float,bool,type(None))
  if not neutral(data):raise ValueError('Row-bound values cannot be cached')
  if not requested or not actual or card_host(requested)!=card_host(actual) or not (card_host(actual)==host or card_host(actual).endswith('.'+host)):raise ValueError('Neutral document URL binding invalid')
  if type(captured_at) not in (int,float) or not math.isfinite(captured_at) or not 0<captured_at<=time.time()+300:raise ValueError('Neutral observation date invalid')
  if type(route_hash) is not str or len(route_hash)!=64 or any(c not in '0123456789abcdef' for c in route_hash):raise ValueError('Configured route key invalid')
  if type(presentation) is not str or not presentation or len(presentation)>128:raise ValueError('Presentation key invalid')
  facts={'version':1,'kind':kind,'requested_url':requested,'actual_url':actual,'host':host,'configured_route_hash':route_hash,'presentation':presentation,'producer':'neutral_v1','captured_at':captured_at,'data':data}
  payload=json.dumps(facts,ensure_ascii=False,sort_keys=True,allow_nan=False)
  if len(payload.encode())>4*1024*1024:raise ValueError('Neutral document size invalid')
  return cls(uuid.uuid4().hex,payload,hashlib.sha256(payload.encode()).hexdigest(),captured_at,cacheable)
 def facts(self):
  if hashlib.sha256(self.payload.encode()).hexdigest()!=self.sha256:raise ValueError('Neutral document changed')
  return json.loads(self.payload)
 def provenance(self,reused):
  return {'raw_document_reused':reused,'generation':self.generation,'sha256':self.sha256,'captured_at':self.captured_at,'reused_at':time.time() if reused else None,'age_seconds':max(0,time.time()-self.captured_at),'physical_capture_attempted':not reused}

class DocumentCache:
 def __init__(self,*,ttl=120,max_keys=256,max_bytes=64*1024*1024,clock=time.time):
  if ttl<=0 or ttl>120 or max_keys<=0 or max_keys>256 or max_bytes<=0 or max_bytes>64*1024*1024:raise ValueError('Job document bounds invalid')
  self.ttl=ttl;self.max_keys=max_keys;self.max_bytes=max_bytes;self.clock=clock;self.epoch=uuid.uuid4().hex;self.lock=threading.Lock();self.values=OrderedDict();self.flights={};self.bytes=0
 def key(self,url,host,route,presentation):
  url=canonical_card_url(url)
  if not url or not (card_host(url)==host or card_host(url).endswith('.'+host)):raise ValueError('Raw capture key invalid')
  return (url,host,route,presentation,'neutral_v1',self.epoch)
 def fresh(self,document):return 0<=self.clock()-document.captured_at<self.ttl
 def capture(self,key,capture,*,alternate_keys=()):
  bounded_timeout(1)
  with self.lock:
   document=self.values.get(key)
   hit=bool(document and self.fresh(document))
   if hit:self.values.move_to_end(key)
   else:
    if document:self.bytes-=len(self.values.pop(key).payload.encode())
    future=self.flights.get(key);owner=future is None
    if owner:
     future=Future()
     if len(self.flights)<self.max_keys:self.flights[key]=future
  if hit:
   bounded_timeout(1);document.facts();return document,True
  if not owner:
   while True:
    timeout=bounded_timeout(.05)
    try:document=future.result(timeout=timeout);break
    except FutureTimeout:continue
   bounded_timeout(1)
   # A failed/uncacheable owner never triggers an automatic follower retry.
   if document is None or not self.fresh(document):return None,True
   document.facts();return document,True
  try:
   document=capture();bounded_timeout(1)
   if document is not None and not isinstance(document,RawDocument):raise ValueError('Expected neutral document')
   if document is not None:
    facts=document.facts()
    actual_key=self.key(facts['requested_url'],facts['host'],facts['configured_route_hash'],facts['presentation'])
    if actual_key!=key and actual_key not in alternate_keys:raise ValueError('Raw capture generation key mismatch')
   with self.lock:
    if document is not None and document.cacheable and self.fresh(document):
     size=len(document.payload.encode())
     old=self.values.pop(actual_key,None)
     if old is not None:self.bytes-=len(old.payload.encode())
     while self.values and (len(self.values)>=self.max_keys or self.bytes+size>self.max_bytes):
      _,old=self.values.popitem(last=False);self.bytes-=len(old.payload.encode())
     if size<=self.max_bytes:self.values[actual_key]=document;self.bytes+=size
    future.set_result(document if document is not None and document.cacheable and actual_key==key else None)
   return document,False
  except BaseException:
   # Pending followers receive unknown/deferred, not a memoized exception or
   # invented wire attempt. A later explicit capture is a new ordinary call.
   with self.lock:
    if not future.done():future.set_result(None)
   raise
  finally:
   with self.lock:
    if self.flights.get(key) is future:self.flights.pop(key,None)

 def invalidate(self,key,observed_at):
  """A genuinely newer independent observation retires old raw delivery only.

  It never seeds a cache from a normalized/expected-specific price or photo.
  """
  bounded_timeout(1)
  if type(observed_at) not in (int,float) or not math.isfinite(observed_at):raise ValueError('Observation date invalid')
  with self.lock:
   old=self.values.get(key)
   if old is not None and observed_at>old.captured_at:
    self.bytes-=len(self.values.pop(key).payload.encode());return True
  return False

 def peek(self,key):
  bounded_timeout(1)
  with self.lock:
   document=self.values.get(key)
   if document is not None and self.fresh(document):self.values.move_to_end(key)
   else:
    if document is not None:self.bytes-=len(self.values.pop(key).payload.encode())
    document=None
  bounded_timeout(1)
  if document is not None:document.facts()
  return document
 def publish(self,document):
  """Only a fresh neutral observation from the physical producer, never a quote."""
  bounded_timeout(1)
  if not isinstance(document,RawDocument) or not document.cacheable or not self.fresh(document):return False
  facts=document.facts();key=self.key(facts['requested_url'],facts['host'],facts['configured_route_hash'],facts['presentation']);size=len(document.payload.encode())
  with self.lock:
   previous=self.values.get(key)
   if previous is not None and previous.captured_at>=document.captured_at:return False
   if previous is not None:self.bytes-=len(self.values.pop(key).payload.encode())
   while self.values and (len(self.values)>=self.max_keys or self.bytes+size>self.max_bytes):
    _,old=self.values.popitem(last=False);self.bytes-=len(old.payload.encode())
   if size>self.max_bytes:return False
   self.values[key]=document;self.bytes+=size
  return True
 def invalidate_observation(self,url,observed_at):
  url=canonical_card_url(url)
  with self.lock:keys=[k for k in self.values if k[0]==url]
  for key in keys:self.invalidate(key,observed_at)
 def close(self):
  with self.lock:self.values.clear();self.bytes=0
