"""Safe capture causes from the native document boundary, no external requests."""
import json,time,unittest
from unittest.mock import patch
from .test_document_context323 import DocumentContextTests as Fixture
from .search import document_context as dc,catalogue_producer as producer
from .search.card_batch import CardSnapshot
from .search.failure_policy import ResearchControlError

class DocumentCaptureDiagnosticTests(unittest.TestCase):
 setUp=Fixture.setUp
 record=Fixture.record
 observation=Fixture.observation
 operation=Fixture.operation
 def capture_error(self,error,callback=None):
  op=self.operation();before=self.source.path.read_bytes()
  def stopped(*args,**kwargs):raise error
  with op,patch.object(producer,'capture_and_publish',callback or stopped):
   with self.assertRaises(type(error))as caught:op.capture(self.urls[0],browser=object(),image_store=object())
   first=json.loads((op.root/'operation.json').read_text())
   self.assertEqual(first['state'],'error');self.assertIsNone(first['eligibility_diagnostic'])
  last=json.loads((op.root/'operation.json').read_text())
  self.assertEqual(first,last);self.assertEqual(before,self.source.path.read_bytes())
  return op,first,caught.exception
 def test_every_registered_code_is_durable_before_exit(self):
  for code in producer._PUBLICATION_REASONS:
   with self.subTest(code=code):
    error=producer.CataloguePublicationError(code)
    op,receipt,raised=self.capture_error(error)
    self.assertIs(raised,error);self.assertEqual(receipt['error_type'],'CataloguePublicationError')
    self.assertEqual(receipt['capture_failure_diagnostic'],producer.CataloguePublicationError(code).diagnostic())
    self.assertEqual(receipt['error'],'document_capture_stopped')
 def test_real_producer_validation_failure_at_capture_boundary(self):
  snapshot=CardSnapshot('a'*32,'{}','0'*64,time.monotonic()+30)
  def invalid(store,identity,position,url,**kwargs):return producer.publish_snapshot(store,identity,position,snapshot,image_store=kwargs['image_store'])
  _,receipt,_=self.capture_error(producer.CataloguePublicationError('catalogue_snapshot_changed'),invalid)
  self.assertEqual(receipt['capture_failure_diagnostic']['code'],'catalogue_snapshot_changed')
 def test_instance_message_chain_and_diagnostic_override_never_exported(self):
  error=producer.CataloguePublicationError('catalogue_source_binding_invalid')
  error.args=('PRIVATE https://secret.invalid?key=DO_NOT_EXPORT',)
  error.__cause__=RuntimeError('PRIVATE_CHAIN')
  error.diagnostic=lambda:dict(code='PRIVATE_OVERRIDE',reason='PRIVATE')
  _,receipt,_=self.capture_error(error)
  self.assertNotIn('PRIVATE',json.dumps(receipt));self.assertNotIn('secret.invalid',json.dumps(receipt))
  self.assertEqual(receipt['capture_failure_diagnostic'],producer.CataloguePublicationError('catalogue_source_binding_invalid').diagnostic())
 def test_unknown_tainted_or_custom_errors_stay_generic(self):
  class Foreign(Exception):pass
  class Subclass(producer.CataloguePublicationError):pass
  class EqualString(str):pass
  cases=[Foreign('PRIVATE_CUSTOM'),Subclass('catalogue_snapshot_changed')]
  for value in ('PRIVATE_CODE',None,True,EqualString('catalogue_snapshot_changed'),{'private':'PRIVATE'}):
   error=producer.CataloguePublicationError('catalogue_snapshot_changed');error.code=value;cases.append(error)
  for error in cases:
   with self.subTest(kind=type(error).__name__):
    _,receipt,_=self.capture_error(error)
    self.assertIsNone(receipt['capture_failure_diagnostic']);self.assertEqual(receipt['error_type'],'Exception');self.assertNotIn('PRIVATE',json.dumps(receipt))
 def test_control_is_not_catalogue_cause_and_clears_late_diagnostic(self):
  class Control(ResearchControlError):pass
  op,receipt,raised=self.capture_error(Control('PRIVATE_CONTROL'))
  self.assertEqual(receipt['error_type'],'ResearchControlError');self.assertIsNone(receipt['capture_failure_diagnostic'])
  op.capture_failure_diagnostic=producer.CataloguePublicationError('catalogue_snapshot_changed').diagnostic()
  op._retain_failure(Control('PRIVATE_LATE'));op._write_receipt('error')
  final=json.loads((op.root/'operation.json').read_text());self.assertIsNone(final['capture_failure_diagnostic']);self.assertEqual(final['error_type'],'ResearchControlError')
  op._retain_failure(producer.CataloguePublicationError('catalogue_snapshot_changed'));op._write_receipt('error')
  final=json.loads((op.root/'operation.json').read_text());self.assertIsNone(final['capture_failure_diagnostic']);self.assertEqual(final['error_type'],'ResearchControlError')
 def test_cancel_and_deadline_refuse_before_capture_boundary(self):
  for stop in ('cancel','deadline','ownership'):
   op=self.operation();calls=[]
   with op,patch.object(producer,'capture_and_publish',lambda *a,**kw:calls.append(True)):
    if stop=='cancel':op.cancelled=lambda:True
    elif stop=='deadline':op.deadline=time.monotonic()-1
    else:op.approval['policy']={}
    with self.assertRaises(ResearchControlError):op.capture(self.urls[0],browser=object(),image_store=object())
   self.assertFalse(calls)
   receipt=json.loads((op.root/'operation.json').read_text());self.assertIsNone(receipt['capture_failure_diagnostic'])
