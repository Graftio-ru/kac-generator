"""Generic clipping/stacking contracts; never infer supplier-specific visibility."""
import subprocess
import unittest
from pathlib import Path
from .capture_visibility import VISIBILITY_SCRIPT

class PaintTests(unittest.TestCase):
 def test_complete_production_script_preserves_cover_guards_and_clips_axes(self):
  base=(Path(__file__).parent/'testdata/price-frame/visibility-dom-probe.js').read_text()
  extra=r'''
heading.pseudo=null;nodes=[heading,span];top=span;
const gallery=element('',{x:0,y:180,width:15,height:100},null,{position:'relative',overflowX:'hidden',overflowY:'visible'});
gallery.clientLeft=2;gallery.clientTop=0;gallery.clientWidth=10;gallery.clientHeight=100;
const slide=element('',{x:20,y:200,width:1000,height:60},gallery,{position:'absolute',pointerEvents:'none'});
nodes=[heading,span,gallery,slide];
assert.equal(check(required).verified,true,'slide paint clipped in X by gallery client box');
gallery.style.overflowX='visible';gallery.style.overflowY='hidden';gallery.getBoundingClientRect=()=>({x:0,y:100,width:1000,height:30,left:0,right:1000,top:100,bottom:130});gallery.clientWidth=1000;gallery.clientHeight=30;
assert.equal(check(required).verified,true,'slide paint clipped in Y');
gallery.style.overflowY='visible';
assert.equal(check(required).verified,false,'actual pointer-none cover outside no clipping remains refused');
gallery.style.overflowX='hidden';gallery.style.transform='matrix(1,0,0,1,0,0)';
assert.equal(check(required).verified,false,'transformed clipping geometry unverifiable');
gallery.style.transform='none';gallery.style.clipPath='polygon(0 0,100% 0,100% 100%)';
assert.equal(check(required).verified,false,'unknown clip path never licenses paint');
gallery.style.clipPath='none';gallery.style.overflowX='visible';
heading.style.position='relative';heading.style.zIndex='10';slide.style.zIndex='1';
assert.equal(check(required).verified,true,'strict lower sibling stacking context cannot cover glyph');
slide.style.zIndex='11';
assert.equal(check(required).verified,false,'higher opaque pointer-none context refuses');
slide.style.zIndex='10';
assert.equal(check(required).verified,false,'equal stacking order remains conservative');
heading.style.zIndex='auto';slide.style.zIndex='auto';gallery.style.overflowX='hidden';gallery.style.position='static';slide.style.position='fixed';
assert.equal(check(required).verified,false,'fixed child paint escapes ordinary overflow ancestor');
slide.style.position='absolute';
assert.equal(check(required).verified,false,'absolute paint cannot borrow clipping from non-containing ancestor');
gallery.style.position='relative';gallery.getBoundingClientRect=()=>({x:0,y:180,width:15,height:100,left:0,right:15,top:180,bottom:280});gallery.clientWidth=10;gallery.clientHeight=100;
assert.equal(check(required).verified,true,'positioned gallery restores genuine contained clipping');
console.log('11 generic paint clipping/stacking assertions passed');
'''
  # Existing fixture function reads the exact production script from stdin.
  import tempfile
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'paint.js';path.write_text(base+extra)
   result=subprocess.run(['node',str(path)],input=VISIBILITY_SCRIPT,text=True,capture_output=True,timeout=10)
  self.assertEqual(result.returncode,0,result.stderr)
  self.assertIn('11 generic',result.stdout)

if __name__=='__main__':unittest.main()
