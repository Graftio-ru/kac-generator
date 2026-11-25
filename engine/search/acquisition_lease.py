"""One job owns both queued and running catalogue leases; no recovery dispatcher."""
import time
from dataclasses import dataclass,field
from threading import Event,Lock,Thread,current_thread
from .catalogue import Acquisition,CatalogueStoreError
from .failure_policy import ResearchControlError

class AcquisitionOwnershipLost(ResearchControlError):
 def __init__(self):super().__init__('Владение приобретением каталога утрачено; новые операции не начаты')

@dataclass
class _Owned:
 admission:Acquisition
 identity:object=None
 lock:object=field(default_factory=Lock)
 active:bool=True
 lost:bool=False
 next_renew_at:float=0

class AcquisitionLeaseKeeper:
 def __init__(self,store,control,*,interval=30,cancelled=lambda:False,owner=None):
  if type(interval) not in (int,float) or not 0<interval<=30:raise ValueError('Invalid keeper interval')
  self.store=store;self.control=control;self.interval=interval;self.cancelled=cancelled;self.owner=owner
  self._lock=Lock();self._entries={};self._revoked=set();self._stop=Event();self._thread=None;self._failure=None
 @staticmethod
 def _key(admission):return admission.product_id,admission.token,admission.generation
 def add(self,admission,*,identity=None):
  self.check_control()
  if type(admission) is not Acquisition or not admission.allowed:raise AcquisitionOwnershipLost()
  with self._lock:
   if self._stop.is_set():raise AcquisitionOwnershipLost()
   self._entries.setdefault(self._key(admission),_Owned(admission,identity,next_renew_at=time.monotonic()+self.interval))
 def start(self):
  with self._lock:
   if self._thread is not None:return
   self._thread=Thread(target=self._run,name='catalogue-lease-keeper',daemon=True);self._thread.start()
 def check_control(self):
  if self._failure is not None:raise self._failure
  self.control()
  if self._stop.is_set():raise AcquisitionOwnershipLost()
 def _entry(self,admission):
  with self._lock:entry=self._entries.get(self._key(admission))
  if entry is None:raise AcquisitionOwnershipLost()
  return entry
 def _validate(self,entry):
  """Called under token lock: fresh authority every time, TTL writes only due."""
  current=self.store.validate_acquisition(entry.admission,owner=self.owner)
  if not current.allowed:entry.lost=True;return current
  if time.monotonic()>=entry.next_renew_at or current.lease_until-time.time()<=120-self.interval:
   current=self.store.renew_acquisition(current,owner=self.owner)
   if not current.allowed:entry.lost=True;return current
   entry.next_renew_at=time.monotonic()+self.interval
  entry.admission=current
  return current
 def check(self,admission):
  self.check_control();entry=self._entry(admission)
  with entry.lock:
   if not entry.active or entry.lost:raise AcquisitionOwnershipLost()
   renewed=self._validate(entry)
   if not renewed.allowed:
    entry.lost=True;raise AcquisitionOwnershipLost()
   entry.admission=renewed
  self.check_control()
 def lost(self,admission):
  if self._stop.is_set() or self._failure is not None:return True
  self.check_control()
  with self._lock:entry=self._entries.get(self._key(admission))
  if entry is None:return True
  with entry.lock:
   if not entry.active or entry.lost:return True
   renewed=self._validate(entry)
   if not renewed.allowed:entry.lost=True;return True
   entry.admission=renewed;return False
 def may_record_stop(self,admission):
  with self._lock:
   key=self._key(admission);entry=self._entries.get(key)
   if key in self._revoked:return True
  if entry is None:return False
  return not self.lost(admission)
 def begin(self,admission):
  """SQL token/generation/expiry validation before the first worker wire."""
  self.check(admission)
 def pulse(self):
  self.check_control()
  with self._lock:entries=list(self._entries.values())
  for entry in entries:
   self.check_control()
   with entry.lock:
    if not entry.active or entry.lost:continue
    renewed=self._validate(entry)
    if not renewed.allowed:entry.lost=True
    else:entry.admission=renewed
 def finish(self,admission,*,error='',completion=None):
  with self._lock:entry=self._entries.get(self._key(admission))
  if entry is None:return False
  with entry.lock:
   if not entry.active:return False
   entry.active=False
   with self._lock:self._entries.pop(self._key(admission),None)
   if entry.lost:return False
   return self.store.finish_acquisition(entry.admission,error=error,owner=self.owner,completion=completion)
 def release(self,admission):
  """Park original unfinished work; stop renewing without completing SQL."""
  entry=self._entry(admission)
  with entry.lock:
   if not entry.active or entry.lost:raise AcquisitionOwnershipLost()
   self.check_control()
   renewed=self._validate(entry)
   if not renewed.allowed:entry.lost=True;raise AcquisitionOwnershipLost()
   entry.active=False
   with self._lock:self._entries.pop(self._key(admission),None)
  return True
 def revoke_all(self,*,reason='Получение каталога остановлено',cancelled=None):
  self._stop.set()
  cancelled=bool(self.cancelled()) if cancelled is None else cancelled
  with self._lock:entries=list(self._entries.values())
  error=None
  for entry in entries:
   with entry.lock:
    if not entry.active:continue
    entry.active=False
    with self._lock:self._entries.pop(self._key(entry.admission),None)
    try:
     if not self.store.validate_acquisition(entry.admission,owner=self.owner).allowed:entry.lost=True;continue
     # Stop ownership is authoritative. Preserve unknown source coverage and
     # immutable card intents; eager per-host diagnostic rewrites/lookup can
     # consume the entire reserved publication time across a large input.
     revoked=(self.store.cancel_acquisition(entry.admission,reason=reason,owner=self.owner) if cancelled else self.store.finish_acquisition(entry.admission,error=reason,owner=self.owner))
     if revoked:
      with self._lock:self._revoked.add(self._key(entry.admission))
    except CatalogueStoreError as failure:error=failure
  if error is not None:
   self._failure=CatalogueStoreError('Не удалось завершить права приобретения каталога')
   raise self._failure
 def stop(self,*,revoke=False,reason='Получение каталога остановлено',cancelled=None):
  self._stop.set()
  if self._thread is not None and self._thread is not current_thread():
   # Store operations use the existing finite SQLite timeout, not wire work.
   self._thread.join(20)
   if self._thread.is_alive():raise CatalogueStoreError('Продление прав каталога не завершено')
  if revoke:self.revoke_all(reason=reason,cancelled=cancelled)
 @property
 def stopped(self):return self._stop.is_set()
 def _run(self):
  while not self._stop.wait(self.interval):
   try:self.pulse()
   except BaseException as failure:
    self._failure=failure if isinstance(failure,(ResearchControlError,CatalogueStoreError)) else CatalogueStoreError('Не удалось продлить права каталога')
    reason=str(self._failure) if isinstance(self._failure,ResearchControlError) else 'Не удалось продлить права каталога'
    try:self.revoke_all(reason=reason)
    except CatalogueStoreError:pass
    return
