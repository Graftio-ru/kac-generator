"""Revalidated quote semantics versus mandatory screenshot geometry."""
import tempfile,unittest,copy
from pathlib import Path
from unittest.mock import patch
from . import price_capture as c
from .capture_visibility import VISIBILITY_SCRIPT,attestation_path
from . import test_capture_visibility as fixtures
class SemanticLayoutTests(unittest.TestCase):
 def scenario(self,mode):
  data=fixtures.VisibilityTests().snapshot();scope=data['snapshot']['prices'][0]['purchase_scope'];owner=scope['owner_id'];article='3526410'
  scope.update(identity_facts_version=1,identity_scope_id=owner,own_articles=[article],identity_facts=[dict(kind='labelled_article',value=article,text='Артикул: '+article,node=owner+'/article',owner_id=owner,rect=dict(x=20,y=1800,width=100,height=20))],ancillary_titles=[],quote_role='current')
  clock=[1000.0]
  class MovingPage(fixtures.Page):
   checks=0;moved=False
   def change(self):
    if self.moved:return
    self.moved=True;fact=self.snapshot['prices'][0]['purchase_scope']['identity_facts'][0]
    if mode in ('wait_fact','shot_fact'):fact['rect']['y']+=19.125
    elif mode=='wait_identity':fact['value']='OTHER456';fact['text']='Артикул: OTHER456'
    elif mode=='wait_price':self.snapshot['prices'][0]['text']='9999 ₽ / м'
    elif mode=='shot_price_rect':self.snapshot['prices'][0]['y']+=3
    elif mode=='shot_heading_rect':self.snapshot['heading']['y']+=3
   def evaluate(self,script,arg=None):
    if script==c.VISIBILITY_FRAME_SCRIPT:clock[0]+=.1;self.change();return True
    result=super().evaluate(script,arg)
    if script==VISIBILITY_SCRIPT:
     self.checks+=1
     if mode.startswith('wait_') and self.checks==1:
      return dict(verified=False,reason='occluded',fields=[dict(role='heading',uncovered=False,reason='occluded',witness=dict(branch='painted_overlay_intersection'))])
    return result
   def wait_for_timeout(self,ms):clock[0]+=ms/1000
   def screenshot(self,**kw):
    result=super().screenshot(**kw)
    if mode.startswith('shot_'):self.change()
    return result
  return MovingPage(data['snapshot']),clock,data
 def run_case(self,mode,expected):
  page,clock,data=self.scenario(mode)
  with tempfile.TemporaryDirectory() as folder,patch.object(c.time,'monotonic',lambda:clock[0]):
   path=Path(folder)/'price.png';proof,reason=c.capture_price(page,path,**data['fields'])
   self.assertEqual(bool(proof),expected,reason)
   self.assertEqual(attestation_path(path).exists(),expected)
   if expected:
    self.assertEqual(page.shots,1);self.assertTrue(proof['visibility_before']['verified']);self.assertEqual(proof['visibility_before'],proof['visibility_after'])
   else:self.assertEqual(page.shots,1 if mode.startswith('shot_') else 0)
   self.assertEqual(page.scroll,{'x':0,'y':0})
 def test_offscreen_fact_moves_during_initial_visibility_wait(self):self.run_case('wait_fact',True)
 def test_offscreen_fact_moves_during_screenshot(self):self.run_case('shot_fact',True)
 def test_observed_identity_change_during_wait_refuses(self):self.run_case('wait_identity',False)
 def test_current_price_change_during_wait_refuses(self):self.run_case('wait_price',False)
 def test_captured_price_geometry_changes_during_screenshot_refuses(self):self.run_case('shot_price_rect',False)
 def test_captured_heading_geometry_changes_during_screenshot_refuses(self):self.run_case('shot_heading_rect',False)
