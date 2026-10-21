"""Execute the actual bounded font readiness script with deterministic font cycles."""
import json
import shutil
import subprocess
import unittest

from .price_capture import FONT_READY_SCRIPT, FONT_RUN_OBSERVATION


class FontReadinessScriptTests(unittest.TestCase):
    def run_case(self, mode):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node is required for actual readiness-script execution')
        program = r'''
const mode=process.argv[1];let status='loading',frames=0,readyCalls=0;
const fonts={get status(){return status},get ready(){readyCalls++;
 if(mode==='hung')return new Promise(()=>{});
 return Promise.resolve().then(()=>{status='loaded'});
}};
global.document=mode==='unavailable'?{}:{fonts};
global.requestAnimationFrame=callback=>{
 if(mode==='late_frames'){frames++;queueMicrotask(()=>{if(frames===2){const end=performance.now()+65;while(performance.now()<end){}}callback(performance.now());});return;}
 return setTimeout(()=>{
 frames++;
 if(mode==='second' && frames===2)status='loading';
 if(mode==='continuous' && frames%2===0)status='loading';
 callback(performance.now());
},1);};
const fn=eval('('+SCRIPT+')');
fn(40).then(result=>console.log(JSON.stringify({result,frames,readyCalls})));
'''.replace('SCRIPT', json.dumps(FONT_READY_SCRIPT))
        result = subprocess.run([node, '-e', program, mode], check=True,
                                capture_output=True, text=True, timeout=3)
        return json.loads(result.stdout)

    def test_first_ready_cycle_succeeds(self):
        data = self.run_case('first'); result = data['result']
        self.assertTrue(result['loaded']); self.assertEqual(result['outcome'], 'loaded')
        self.assertEqual(result['cycles'], 1); self.assertEqual(data['frames'], 2)
        self.assertGreaterEqual(result['elapsed_ms'], 0)
        self.assertEqual(result['diagnostics']['phase'],'loaded_status_check')
        self.assertEqual(result['diagnostics']['render_frames_observed'],2)

    def test_loading_after_render_awaits_new_cycle_with_same_deadline(self):
        data = self.run_case('second'); result = data['result']
        self.assertTrue(result['loaded']); self.assertEqual(result['cycles'], 2)
        self.assertEqual(data['readyCalls'], 2); self.assertEqual(data['frames'], 4)
        self.assertLess(result['elapsed_ms'], 40)

    def test_hung_ready_and_continuous_cycles_stop_at_original_deadline(self):
        for mode in ('hung', 'continuous'):
            with self.subTest(mode=mode):
                result = self.run_case(mode)['result']
                self.assertFalse(result['loaded']); self.assertEqual(result['outcome'], 'deadline_exceeded')
                self.assertGreaterEqual(result['elapsed_ms'], 35)
                self.assertLess(result['elapsed_ms'], 200)
                self.assertEqual(result['cycles'] == 0, mode == 'hung')
                if mode=='hung':
                    self.assertEqual(result['diagnostics']['phase'],'await_ready')
                    self.assertEqual(result['diagnostics']['render_frames_observed'],0)

    def test_missing_font_api_is_distinct_from_timeout(self):
        result = self.run_case('unavailable')['result']
        self.assertFalse(result['loaded']); self.assertEqual(result['outcome'], 'unavailable')
        self.assertEqual(result['cycles'], 0)

    def test_delayed_frame_delivery_cannot_succeed_after_deadline(self):
        data = self.run_case('late_frames'); result = data['result']
        self.assertFalse(result['loaded']); self.assertEqual(result['outcome'], 'deadline_exceeded')
        self.assertEqual(result['cycles'], 1); self.assertGreaterEqual(result['elapsed_ms'], 65)
        self.assertGreaterEqual(result['diagnostics']['render_wait_ms'],65)


    def test_required_matcher_caps_array_and_field_text_before_includes(self):
        program = r"""
let reads=0;
const fields=new Proxy(Array.from({length:100},()=>({role:'price',text:'x'.repeat(513)})),{
 get(target,key){if(/^\d+$/.test(String(key))){if(Number(key)>=64)throw Error('outside cap');reads++;}return target[key];}
});
global.document={body:{},createTreeWalker:()=>({nextNode:()=>null})};
global.NodeFilter={SHOW_TEXT:4};
const result=eval('('+SCRIPT+')')(fields);
console.log(JSON.stringify({result,reads}));
""".replace('SCRIPT',json.dumps(FONT_RUN_OBSERVATION))
        output=subprocess.run([shutil.which('node'),'-e',program],check=True,capture_output=True,text=True,timeout=3)
        data=json.loads(output.stdout)
        self.assertEqual(data['reads'],64)
        self.assertEqual(data['result']['required_field_count'],64)
        self.assertTrue(data['result']['truncated'])
        self.assertEqual(data['result']['runs'],[])
