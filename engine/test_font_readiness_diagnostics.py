"""Safety and actual consumer delivery for observational font diagnostics."""
import json,unittest,tempfile
from pathlib import Path
from unittest.mock import patch
from .price_capture import _safe_font_diagnostics,_safe_font_readiness,FONT_READY_SCRIPT,capture_price
from .test_capture_visibility import Page,VisibilityTests
from .search import agent

class FontDiagnosticTests(unittest.TestCase):
 def test_untrusted_values_are_projected_and_bounded(self):
  raw={'phase':'await_ready','ready_wait_ms':float('inf'),'preview_before':{'scope':'preview_candidates','runs':[{'role':'secret','check':'secret','text_code_units':10**20,'secret':'private'}]*90,'faces':{'loading':99999},'secret':'private'},'secret':'private'}
  value=_safe_font_diagnostics(raw)
  self.assertNotIn('private',json.dumps(value));self.assertNotIn('secret',json.dumps(value));self.assertEqual(len(value['preview_before']['runs']),24);self.assertEqual(value['preview_before']['faces']['loading'],256)
  self.assertEqual(_safe_font_readiness({'loaded':True,'status':{'secret':'private'},'cycles':'private','elapsed_ms':float('nan')}),{'loaded':True})
 def test_actual_font_timeout_callback_reaches_actual_reject_consumer(self):
  data=VisibilityTests().snapshot();errors=[];stages=[]
  class Loading(Page):
   def evaluate(self,script,arg=None):
    if script==FONT_READY_SCRIPT:return {'loaded':False,'diagnostics':{'phase':'await_ready','ready_wait_ms':25,'secret':'private'}}
    return super().evaluate(script,arg)
  with tempfile.TemporaryDirectory() as folder:
   proof,reason=capture_price(Loading(data['snapshot']),Path(folder)/'none.png',on_error=errors.append,on_refusal=stages.append,**data['fields'])
  self.assertFalse(proof);self.assertEqual(stages,['price_font_readiness_timeout']);self.assertEqual(errors[0]['font_readiness_diagnostics']['phase'],'await_ready')
  with agent.capture_card_diagnostics() as events,patch.object(agent,'_log'),patch.object(agent,'rejected'),patch.object(agent,'current_queue',return_value=None):
   agent._reject_card('https://local.invalid/card','price_capture_review',reason,**errors[0])
  self.assertEqual(events[0]['capture_stage'],'price_font_readiness_timeout');self.assertNotIn('private',json.dumps(events))
 def test_existing_error_fields_are_safe_consumer_arguments(self):
  with agent.capture_card_diagnostics() as events,patch.object(agent,'_log'),patch.object(agent,'rejected'),patch.object(agent,'current_queue',return_value=None):
   agent._reject_card('https://local.invalid/card','price_capture_review','fixture',capture_stage='price_screenshot_failed',capture_error_type='TimeoutError',capture_error_cause='font_readiness_timeout')
  self.assertEqual(events[0]['capture_error_type'],'TimeoutError');self.assertEqual(events[0]['capture_error_cause'],'font_readiness_timeout')
