"""No-network parser parity and actual wrapper publication controls."""
import copy,importlib.util,time,unittest
from unittest.mock import patch
from pathlib import Path
from . import native_geometry as n
from .test_native_document_binding import multi_fixture
from .search.failure_policy import ResearchControlError
p=Path(__file__).resolve().parents[2]/'sources/kac-generator/native-frame-subcause-2026-10-11/predecessor/native_geometry.py'
spec=importlib.util.spec_from_file_location('native_old',p);old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
def style(s,node,prop,value):
 s['strings'].append(value);d=s['documents'][0]['layout'];i=d['nodeIndex'].index(node);d['styles'][i][n.STYLE_NAMES.index(prop)]=len(s['strings'])-1
class FrameDiagnostics(unittest.TestCase):
 def test_exact_failure_and_success_parity(self):
  cases=[]
  s=multi_fixture();d=s['documents'][0]['layout'];i=d['nodeIndex'].index(4)
  for k in ('nodeIndex','bounds','styles','paintOrders'):d[k].pop(i)
  cases.append((s,'missing_layout','host',None))
  s=multi_fixture();d=s['documents'][0]['layout'];i=d['nodeIndex'].index(4)
  for k in ('nodeIndex','bounds','styles','paintOrders'):d[k].append(copy.deepcopy(d[k][i]))
  s['strings'].append('fixed');d['styles'][-1][n.STYLE_NAMES.index('position')]=len(s['strings'])-1
  cases.append((s,'inconsistent_fragment_style','host',None))
  s=multi_fixture();d=s['documents'][0]['layout'];d['bounds'][d['nodeIndex'].index(4)]=[400,400,0,0];cases.append((s,'no_positive_layout','host',None))
  for prop,value,reason in [('position','unknown','unsupported_position'),('filter','blur(10px)','unsupported_paint_effect'),('will-change','transform','unsupported_coordinate_effect'),('writing-mode','vertical-rl','unsupported_writing_mode'),('opacity','0','unsupported_visibility_state'),('display','contents','unsupported_display_state')]:
   s=multi_fixture();style(s,2,prop,value);cases.append((s,reason,'ancestor',prop))
  for s,reason,role,prop in cases:
   sink={};value=n.snapshot_envelopes(s,document_backend_id=1,diagnostic_sink=sink)
   self.assertEqual(value,old.snapshot_envelopes(s,document_backend_id=1));self.assertEqual(sink['frame_host_count'],1)
   detail=n._safe_frame_detail(sink['frame_enclosure']);self.assertEqual(detail['subtype'],reason);self.assertEqual(detail['role'],role);self.assertEqual(detail.get('property'),prop);self.assertNotIn('secret',str(detail))
  s=multi_fixture();sink={};self.assertEqual(n.snapshot_envelopes(s,document_backend_id=1,diagnostic_sink=sink),old.snapshot_envelopes(s,document_backend_id=1));self.assertEqual(sink,{'frame_host_count':1})
 def test_malformed_sink_and_public_allowlist(self):
  class Sink(dict):
   def __setitem__(self,*args):raise RuntimeError('secret')
  s=multi_fixture();style(s,4,'position','unknown');self.assertEqual(n.snapshot_envelopes(s,document_backend_id=1,diagnostic_sink=Sink()),old.snapshot_envelopes(s,document_backend_id=1))
  with patch.object(n,'_frame_guard_property',side_effect=RuntimeError('secret')):
   self.assertEqual(n.snapshot_envelopes(s,document_backend_id=1,diagnostic_sink={}),old.snapshot_envelopes(s,document_backend_id=1))
  base={'subtype':'unsupported_position','role':'host','host_backend_id':5,'ancestor_depth':0,'property':'position','box':[0,1,2,3],'raw':'secret'}
  self.assertNotIn('secret',str(n._safe_frame_detail(base)))
  for k,v in [('subtype',[]),('role',True),('host_backend_id',True),('ancestor_depth',-1)]:self.assertEqual(n._safe_frame_detail({**base,k:v}),{})
  for box in ({0:1},[0,0,float('nan'),2],[2,0,1,2]):self.assertNotIn('box',n._safe_frame_detail({**base,'box':box}))
  row={'phase':'native_snapshot_before','route':'native_fallback','frame_enclosure':base};self.assertEqual(n.safe_native_visibility_diagnostics({'observations':1,'recent':[row]})['recent'][0]['frame_enclosure'],n._safe_frame_detail(base))
 def test_actual_wrapper_no_added_calls_and_control(self):
  s=multi_fixture();style(s,4,'position','unknown');calls=[]
  class Session:
   def send(self,method,params):
    calls.append(method)
    if method=='DOM.getDocument':return {'root':{'nodeType':9,'backendNodeId':1}}
    if method=='DOMSnapshot.captureSnapshot':return s
    return {}
   def detach(self):calls.append('detach')
  class Page:
   def __init__(self):self.context=self
   def new_cdp_session(self,page):return Session()
   def evaluate(self,script,*args):
    if script=='manual':return {'verified':False,'reason':'unverifiable_pseudo_extent'}
    return {'viewport':{'width':600,'height':500},'url':'about:blank','scroll':[10,30],'scale':1,'left':0,'top':0}
  page=Page();result=n.native_visibility(page,[],time.monotonic()+2,'manual');self.assertEqual(result['native_geometry']['reason'],'unsupported_frame_enclosure');self.assertEqual(page._kac_native_visibility_diagnostics['recent'][0]['frame_enclosure']['property'],'position');self.assertEqual(calls,['DOM.getDocument','DOMSnapshot.captureSnapshot','Runtime.releaseObjectGroup','detach'])
  class BadPage(Page):
   @property
   def _kac_native_visibility_diagnostics(self):raise RuntimeError('secret')
  self.assertEqual(n.native_visibility(BadPage(),[],time.monotonic()+2,'manual'),result)
  control=ResearchControlError('owned')
  class Controlled(Page):
   def new_cdp_session(self,page):raise control
  with self.assertRaises(ResearchControlError) as caught:n.native_visibility(Controlled(),[],time.monotonic()+2,'manual')
  self.assertIs(caught.exception,control)
