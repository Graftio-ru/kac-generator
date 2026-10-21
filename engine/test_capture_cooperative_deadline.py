"""A synchronous browser callback cannot admit proof after the capture deadline."""
import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from . import price_capture as capture
from .capture_visibility import VISIBILITY_SCRIPT,attestation_path
from . import test_capture_transient_visibility as transient_fixtures
from . import test_capture_visibility as fixtures
class CooperativeDeadlineTests(unittest.TestCase):
 def check_overrun(self,phase):
  page,clock=transient_fixtures.CaptureTransientVisibilityTests().scenario()
  # No synthetic interceptor: this fixture is otherwise a valid fresh quote.
  from .test_capture_visibility import Page
  evaluate=Page.evaluate.__get__(page, type(page));shot=page.screenshot
  def observed(script,arg=None):
   result=evaluate(script,arg)
   if script==phase:clock[0]+=8.01
   return result
  def screenshot(**kw):
   result=shot(**kw)
   if phase=='shot':clock[0]+=8.01
   return result
  page.evaluate=observed;page.screenshot=screenshot;stages=[]
  with tempfile.TemporaryDirectory() as folder,patch.object(capture.time,'monotonic',lambda:clock[0]):
   path=Path(folder)/'price.png'
   proof,reason=capture.capture_price(page,path,on_refusal=stages.append,**fixtures.VisibilityTests().snapshot()['fields'])
   self.assertFalse(proof);self.assertEqual(stages,['price_capture_deadline'])
   self.assertIn('время проверки снимка истекло',reason)
   self.assertEqual(page.shots,1 if phase=='shot' else 0)
   self.assertFalse(attestation_path(path).exists())
   self.assertEqual(page.scroll,{'x':0,'y':0});self.assertEqual(page.viewport_size,{'width':1366,'height':900})
 def test_ready_result_delivered_after_deadline_never_attests(self):self.check_overrun(capture.FONT_READY_SCRIPT)
 def test_snapshot_result_delivered_after_deadline_never_attests(self):self.check_overrun(capture.DOM_SNAPSHOT)
 def test_true_visibility_delivered_after_deadline_never_attests(self):self.check_overrun(VISIBILITY_SCRIPT)
 def test_screenshot_returned_after_deadline_is_unadmitted(self):self.check_overrun('shot')
