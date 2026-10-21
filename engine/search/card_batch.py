"""Immutable captured facts and bounded job-owned text extraction batches."""
from __future__ import annotations
from contextvars import ContextVar
from contextlib import contextmanager
from concurrent.futures import Future
from dataclasses import dataclass
from collections import deque
import hashlib,json,threading,time,uuid
from .failure_policy import ResearchControlError
from ..llm import ModelOutputInvalid

class CardBatchInputLimit(ModelOutputInvalid):
 def __init__(self):super().__init__('invalid_schema')
 def diagnostic(self):
  return {**super().diagnostic(),'code':'extract_failed','reason':'Данные карточки превышают ограничение пакета; извлечение не запускалось','batch_input_rejected':True}


_KNOWN_FIELDS={'product_title','matches_requested_product','match_reason','price_unit','listed_price_with_vat','price_with_vat','package_quantity','listed_price_covers_multiple_requested_units','seller_inn','region_status','region_evidence','city','moscow_pickup'}
def json_type(value):
 return {type(None):'null',bool:'boolean',int:'number',float:'number',str:'string',list:'array',dict:'object'}.get(type(value),'other')
class CardBatchSchemaInvalid(ModelOutputInvalid):
 def __init__(self,stage,*,field=None,received=None,expected=None,count=None):
  if stage not in {'wrapper','cards','item','id','duplicate_id','missing_id','extraction','field_type','readonly_mismatch','merged_fields'}:raise ValueError('Unknown batch stage')
  self.details={'batch_schema_stage':stage}
  if field in _KNOWN_FIELDS:self.details['batch_schema_field']=field
  if received in {'null','boolean','number','string','array','object','other'}:self.details['batch_received_type']=received
  if expected in {'null','boolean','number','string','array','object','known_id','valid_enum','finite_number'}:self.details['batch_expected_type']=expected
  if type(count) is int:self.details['batch_item_count']=max(0,min(10000,count))
  super().__init__('invalid_schema')
 def diagnostic(self):return {**super().diagnostic(),**self.details}

@dataclass(frozen=True)
class CardSnapshot:
 card_id: str
 payload: str
 sha256: str
 deadline: float | None
 @classmethod
 def create(cls,facts,deadline=None):
  if facts.get('url') and facts.get('captured_at'):
   from .whitelist import capture_seller_reference
   facts={**facts,'seller_reference_snapshot':capture_seller_reference(facts['url'],facts['captured_at'])}
  payload=json.dumps(facts,ensure_ascii=False,sort_keys=True,allow_nan=False)
  return cls(uuid.uuid4().hex,payload,hashlib.sha256(payload.encode()).hexdigest(),deadline)
 def facts(self):return json.loads(self.payload)

 def retain(self,root):
  """Durable captured input, not a quotation proof or qualification receipt."""
  from pathlib import Path
  import math,re
  from .session import bounded_timeout
  bounded_timeout(1)
  if type(self.card_id) is not str or not re.fullmatch('[a-f0-9]{32}',self.card_id):raise ValueError('Invalid captured input identifier')
  facts=self.facts()
  if len(self.payload.encode())>1024*1024:raise CardBatchInputLimit()
  record={'version':1,'id':self.card_id,'payload':self.payload,'sha256':self.sha256,'deadline':self.deadline if self.deadline is None or math.isfinite(self.deadline) else None,'deadline_unbounded':self.deadline is not None and not math.isfinite(self.deadline)}
  raw=json.dumps(record,ensure_ascii=False,allow_nan=False).encode()
  root=Path(root).resolve();folder=root/'captured_inputs';folder.mkdir(parents=True,exist_ok=True);path=folder/(self.card_id+'.json')
  if folder.resolve().parent!=root or path.resolve().parent!=folder.resolve():raise ValueError('Captured input path is not owned')
  if path.exists():
   if path.stat().st_size>3*1024*1024:raise ValueError('Captured input size exceeded')
   if path.read_bytes()!=raw:raise ValueError('Captured input changed')
  else:
   with path.open('xb') as file:file.write(raw)
  return {'ref':str(path.relative_to(root)),'record_sha256':hashlib.sha256(raw).hexdigest(),'snapshot_sha256':self.sha256,'card_id':self.card_id,'url':facts.get('url'),'captured_at':facts.get('captured_at')}

class CardCapturePending(ResearchControlError):
 def __init__(self,snapshot):self.snapshot=snapshot;super().__init__('Captured card awaits bounded text extraction')

@dataclass
class PendingCards:
 future: Future
 resume: object

_COORDINATOR=ContextVar('card_batch_coordinator',default=None)
_REPLAY=ContextVar('card_batch_replay',default=None)
@contextmanager
def card_batch_scope(coordinator=None,replay=None):
 a=_COORDINATOR.set(coordinator);b=_REPLAY.set(replay)
 try:yield
 finally:_REPLAY.reset(b);_COORDINATOR.reset(a)
def replay_for(url):return (_REPLAY.get() or {}).get(url)
def pending_enabled():return _COORDINATOR.get() is not None
def replay_started_at():
 values=_REPLAY.get() or {}
 return min((value[0].facts()['captured_at'] for value in values.values()),default=None)

def extraction_request(snapshot):
 f=snapshot.facts()
 from .agent import _literal_card_fields
 from ..source_quote import primary_text
 fields=_literal_card_fields(snapshot)
 all_fields=('product_title','matches_requested_product','match_reason','price_unit','listed_price_with_vat','price_with_vat','package_quantity','listed_price_covers_multiple_requested_units','seller_inn','region_status','region_evidence','city','moscow_pickup')
 text=primary_text(f['text'])
 # Main-card excerpt plus currency/denomination/conditions; omit unrelated footer.
 import re
 lines=text.splitlines();indexes=set(range(min(len(lines),30)))
 for i,line in enumerate(lines):
  if re.search(r'₽|руб|RUB|за\s+|ИНН|только\s+для|при\s+(?:заказ|покупк)|по\s+карте',line,re.I):indexes.update(range(max(0,i-2),min(len(lines),i+3)))
 excerpt='\n'.join(lines[i] for i in sorted(indexes))[:6000]
 return {'id':snapshot.card_id,'snapshot_sha256':snapshot.sha256,'url':f['url'],
         'captured_at':f['captured_at'],'requested_name':f['expected_name'],
         'requested_type_mark':f['expected_type_mark'],'requested_unit':f['expected_unit'],
         'readonly_observed_fields':fields,'unresolved_fields':[k for k in all_fields if k not in fields],'main_card_excerpt':excerpt,'excerpt_truncated':len(excerpt)<len(text)}

def batch_payload(snapshots):
 requests=[extraction_request(s) for s in snapshots]
 return requests,json.dumps({'cards':requests},ensure_ascii=False)

def extract_batch(snapshots):
 """One ordinary text call; response IDs never become source facts."""
 from ..llm import complete_json,ModelOutputInvalid,MODEL_OPERATION_TIMEOUT_SECONDS
 from .agent import EXTRACT_SYSTEM,_validate_extraction_fields
 requests,payload=batch_payload(snapshots)
 if len(payload)>60000:raise CardBatchInputLimit()
 value=complete_json(payload,
   system=EXTRACT_SYSTEM+'\nReturn {"cards":[{"id":"exact request id","extraction":{...}}]}. One independent extraction per request. Never mix cards. Preserve exact IDs. Return ONLY unresolved_fields; readonly_observed_fields cannot be changed. Use null prices when uncertain.',
   timeout_seconds=MODEL_OPERATION_TIMEOUT_SECONDS,max_tokens=4096)
 from ..cloudru_failover import ModelDict
 # Trusted provider metadata is an attribute, never a JSON/source field.
 # Normalize only this exact outer wrapper; all nested checks stay strict.
 if type(value) is ModelDict:value=dict(value)
 if type(value) is not dict:raise CardBatchSchemaInvalid('wrapper',received=json_type(value),expected='object')
 if type(value.get('cards')) is not list:raise CardBatchSchemaInvalid('cards',received=json_type(value.get('cards')),expected='array')
 expected={s.card_id for s in snapshots};mapped={};duplicates=set()
 for item in value['cards']:
  if type(item) is not dict:raise CardBatchSchemaInvalid('item',received=json_type(item),expected='object',count=len(value['cards']))
  if type(item.get('id')) is not str:raise CardBatchSchemaInvalid('id',received=json_type(item.get('id')),expected='string')
  if item['id'] not in expected:raise CardBatchSchemaInvalid('id',received='string',expected='known_id')
  if item['id'] in mapped:duplicates.add(item['id'])
  mapped[item['id']]=item.get('extraction')
 result={}
 for snapshot in snapshots:
  try:
   if snapshot.card_id in duplicates:raise CardBatchSchemaInvalid('duplicate_id',count=len(value['cards']))
   if snapshot.card_id not in mapped:raise CardBatchSchemaInvalid('missing_id',count=len(value['cards']))
   request=next(r for r in requests if r['id']==snapshot.card_id)
   if type(mapped[snapshot.card_id]) is not dict:raise CardBatchSchemaInvalid('extraction',received=json_type(mapped[snapshot.card_id]),expected='object')
   supplied=_validate_extraction_fields(mapped[snapshot.card_id])
   readonly=request['readonly_observed_fields']
   mismatch=next((k for k,v in supplied.items() if k not in request['unresolved_fields'] and (k not in readonly or v!=readonly[k])),None)
   if mismatch is not None:raise CardBatchSchemaInvalid('readonly_mismatch',field=mismatch,received=json_type(supplied[mismatch]))
   try:data=_validate_extraction_fields(dict(readonly,**supplied))
   except CardBatchSchemaInvalid as error:
    error.details['batch_schema_stage']='merged_fields';raise
   result[snapshot.card_id]=(data,None)
  except ModelOutputInvalid as error:result[snapshot.card_id]=(None,error)
 return result

class CardBatchCoordinator:
 """One model worker; capture workers enqueue without waiting for the model."""
 def __init__(self,guard,cancelled=lambda:False,extractor=extract_batch,policy=None):
  self.policy=policy
  self.guard=guard;self.cancelled=cancelled;self.extractor=extractor
  self.condition=threading.Condition();self.queue=deque();self.closed=False;self.producers=1
  self.thread=threading.Thread(target=self._run,name='card-text-batches',daemon=True);self.thread.start()
 def submit(self,snapshot,on_metric=None):
  future=Future();future.metric_sink=on_metric
  try:
   if len(batch_payload([snapshot])[1])>60000:raise CardBatchInputLimit()
  except Exception as error:
   future.set_result((snapshot,None,error));return future
  with self.condition:
   if self.closed or len(self.queue)>=64:
    from ..llm import ModelOutputInvalid
    future.set_result((snapshot,None,ModelOutputInvalid('invalid_schema')))
   else:self.queue.append((snapshot,future,time.monotonic()));self.condition.notify_all()
  return future
 def producer_activity(self,active,waiting):
  with self.condition:self.producers=active+waiting;self.condition.notify_all()
 def flush(self):
  with self.condition:
   for i,(s,f,t) in enumerate(self.queue):self.queue[i]=(s,f,0)
   self.condition.notify_all()
 def close(self):
  with self.condition:self.closed=True;self.condition.notify_all()
  self.thread.join()  # Calls retain their original finite session/model deadlines.
 def _run(self):
  from .session import research_session,bounded_timeout,SearchBudgetExceeded
  while True:
   with self.condition:
    while not self.queue and not self.closed:self.condition.wait(.05)
    if not self.queue:return
    oldest=self.queue[0][0]
    remaining=oldest.deadline-time.monotonic() if oldest.deadline is not None else 5
    if len(self.queue)<5 and not self.closed and self.producers and time.monotonic()-self.queue[0][2]<5 and remaining>0 and not self.cancelled() and not self.guard.diagnostic():
     self.condition.wait(min(.05,remaining));continue
    batch=[];size=0
    while self.queue and len(batch)<10:
     snapshot,future,stamp=self.queue[0]
     try:length=len(batch_payload([*(s for s,f in batch),snapshot])[1])
     except Exception as error:
      self.queue.popleft();future.set_result((snapshot,None,error));continue
     if length>60000:
      if batch:break
      self.queue.popleft();future.set_result((snapshot,None,CardBatchInputLimit()));continue
     self.queue.popleft();batch.append((snapshot,future));size=length
   valid=[]
   for s,f in batch:
    try:
     with research_session(llm_guard=self.guard,cancelled=self.cancelled,deadline=s.deadline):bounded_timeout(1)
     valid.append((s,f))
    except ResearchControlError as error:f.set_result((s,None,error))
   if not valid:continue
   deadlines=[s.deadline for s,f in valid if s.deadline is not None]
   deadline=min(deadlines) if deadlines else None
   try:
    with research_session(llm_guard=self.guard,cancelled=self.cancelled,deadline=deadline):
     bounded_timeout(1)
     from ..telemetry import stream_metrics,capture_metrics,timed_stage,record_stage
     sink=valid[0][1].metric_sink
     if sink is not None and self.policy is not None:
      with stream_metrics(sink,self.policy),capture_metrics(),timed_stage('card_batch',f'cards={len(valid)}'):
       values=self.extractor([s for s,f in valid])
     else:values=self.extractor([s for s,f in valid])
     bounded_timeout(1)
    for s,f in valid:
     try:
      with research_session(llm_guard=self.guard,cancelled=self.cancelled,deadline=s.deadline):bounded_timeout(1)
      data,error=values[s.card_id];f.set_result((s,data,error))
     except ResearchControlError as error:f.set_result((s,None,error))
   except Exception as error:
    for s,f in valid:f.set_result((s,None,error))
