"""Offline explicit coordinate wire and private envelope diagnostics."""
import copy,json,unittest
from unittest.mock import patch
import httpx
from .test_cloudru_failover import cloudru,llm
from . import pixel_evidence as pixel
from .cloudru_failover import payload_for
from .telemetry import capture_metrics

def wire():
 return {'coordinate_system':'normalized_1000_xyxy','status':'readable','main_product':{'title':'Item','title_box':[0,0,1000,1000],'identifiers':[{'value':'X','role':'manufacturer','box':[100,200,300,400]}]},'quotes':[{'amount':'12','currency':'₽','sale_unit':'шт','basis':'/ шт','role':'current','conditional_text':'','price_box':[400,500,600,700],'unit_box':[600,500,700,700],'basis_box':[600,500,800,700]}],'exclusions':[{'role':'old','text':'10','box':[1,2,3,4]}],'warnings':[]}
class CoordinateContract(unittest.TestCase):
 def test_non_square_and_all_fields_conversion_preserves_raw(self):
  raw=wire();original=copy.deepcopy(raw);v=pixel.decode_wire_reading(raw,1800,900)
  self.assertEqual(raw,original);self.assertNotIn('coordinate_system',v)
  self.assertEqual(v['main_product']['title_box'],[0,0,1800,900]);self.assertEqual(v['main_product']['identifiers'][0]['box'],[180,180,360,180])
  self.assertEqual(v['quotes'][0]['price_box'],[720,450,360,180]);self.assertAlmostEqual(v['exclusions'][0]['box'][0],1.8);self.assertEqual(pixel.decode_reading(v,1800,900),v)
 def test_unknown_absent_or_old_convention_never_guessed(self):
  for coordinate in (None,'image_pixels_xywh','normalized_1000_xywh',True,{}):
   v=wire()
   if coordinate is None:del v['coordinate_system']
   else:v['coordinate_system']=coordinate
   with self.assertRaises(ValueError):pixel.decode_wire_reading(v,1800,900)
 def test_adversarial_box_bounds_finite_order_types(self):
  for box in ([0,0,0,1],[5,1,2,3],[-1,0,2,3],[0,0,1001,3],[0,0,2,float('nan')],[0,0,float('inf'),3],[True,0,2,3],['0',0,2,3],[0,0,2]):
   v=wire();v['quotes'][0]['basis_box']=box
   with self.assertRaises(ValueError):pixel.decode_wire_reading(v,1800,900)
 def test_inert_ambiguous_output_and_changed_receipt_hash(self):
  v=wire();v['status']='ambiguous';v['main_product']['title_box']=[];self.assertEqual(pixel.decode_wire_reading(v,1800,900)['quotes'],[])
  self.assertEqual(pixel.PROMPT_VERSION,'current-card-image-only-v2');self.assertEqual(pixel.PARSER_VERSION,'current-card-reading-v2')
  old=__import__('hashlib').sha256(pixel.IMAGE_PROMPT.encode()+pixel.canonical(pixel.READER_SCHEMA)).hexdigest();self.assertNotEqual(pixel.prompt_sha256(),old)
class DiagnosticContract(unittest.TestCase):
 def test_safe_null_length_envelope_before_completion_error(self):
  body={'choices':[{'finish_reason':'length','message':{'content':None,'reasoning_content':'PRIVATE REASONING'}}],'usage':{'prompt_tokens':2,'completion_tokens':4096}}
  with patch.object(cloudru.settings,'cloudru_api_key','offline'),patch.object(cloudru,'provider_post',return_value=httpx.Response(200,json=body)),capture_metrics() as m:
   with self.assertRaises(llm.LLMOperationFailed) as caught:cloudru._request({'model':'Qwen/Qwen3.6-35B-A3B','messages':[]},10)
  self.assertEqual(caught.exception.cloudru_response_diagnostic['content_type'],'null');self.assertNotIn('PRIVATE',json.dumps(caught.exception.cloudru_response_diagnostic))
  diagnostic=[e for e in m.events if e['stage']=='cloudru_response_envelope'];self.assertEqual(len(diagnostic),1);label=json.loads(diagnostic[0]['label']);self.assertEqual(label['finish_reason'],'length');self.assertEqual(label['content_type'],'null');self.assertEqual(label['reasoning_content_length'],17);self.assertNotIn('PRIVATE',json.dumps(m.events))
 def test_unknown_finish_is_not_logged_and_types_are_bounded(self):
  v=cloudru.response_diagnostic({'choices':[{'finish_reason':'secret finish','message':{'content':'secret','reasoning_content':'secret'}}]})
  self.assertEqual(v['finish_reason'],'string');self.assertNotIn('secret',json.dumps(v));self.assertEqual(v['content_length'],6);self.assertEqual(cloudru.response_diagnostic({})['content_type'],'missing');self.assertEqual(cloudru.response_diagnostic([])['envelope_type'],'list')
 def test_candidate_specific_generation_and_no_flag_leak(self):
  q='Qwen/Qwen3.6-35B-A3B';payload={'model':q,'temperature':0,'messages':[{'role':'user','content':'same'}]};configured=payload_for(payload,q,'vision')
  self.assertEqual(configured['chat_template_kwargs'],{'enable_thinking':False});self.assertEqual(configured['temperature'],.7);self.assertEqual(configured['top_p'],.8);self.assertNotIn('chat_template_kwargs',payload)
  other=payload_for(configured,'google/gemini-3-flash-preview','vision');self.assertNotIn('chat_template_kwargs',other);self.assertNotIn('top_p',other);self.assertEqual(other['temperature'],0);self.assertEqual(other['messages'],payload['messages'])

 def test_actual_pixel_request_qwen_knobs_single_candidate(self):
  from pathlib import Path
  image=Path(__file__).resolve().parents[2]/'sources/kac-generator/current-pixel-evidence-2026-10-11/local-measured-controls/qualified.png'
  seen=[]
  def post(*args,**kwargs):
   seen.append(copy.deepcopy(kwargs['json']))
   return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(wire())}}]})
  with patch.object(cloudru.settings,'cloudru_api_key','offline'),patch.object(cloudru,'provider_post',post):
   result=pixel.read_png(image,{'model':'Qwen/Qwen3.6-35B-A3B'},10)
  self.assertEqual(len(seen),1);self.assertEqual(seen[0]['chat_template_kwargs'],{'enable_thinking':False});self.assertEqual(seen[0]['temperature'],.7);self.assertEqual(seen[0]['top_p'],.8)
  self.assertEqual(result['raw_response'],wire());self.assertNotIn('coordinate_system',result['reading'])

 def test_success_diagnostic_survives_without_telemetry(self):
  with patch.object(cloudru.settings,'cloudru_api_key','offline'),patch.object(cloudru,'provider_post',return_value=httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]})):
   result=cloudru._request({'model':'offline','messages':[]},10)
  self.assertEqual(result.cloudru_response_diagnostic['content_length'],2);self.assertEqual(result.cloudru_response_diagnostic['finish_reason'],'stop')
