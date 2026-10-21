"""No-network native geometry schema, fragment and cooperative binding controls."""
import copy
import time
import unittest
import subprocess
import tempfile
from pathlib import Path
from .native_geometry import STYLE_NAMES, snapshot_envelopes, capture_native_envelopes, native_visibility


def fixture():
    strings = ['#document', 'HTML', 'BODY', '::after', 'after', 'none', 'auto', '1',
               'horizontal-tb', 'visible', 'block', 'absolute', '""', 'normal',
               'secret raw text https://private.invalid']
    def style(pseudo):
        d = {k:'none' for k in STYLE_NAMES}
        d.update({'content':'""' if pseudo else 'normal','position':'absolute',
                  'display':'block','visibility':'visible','opacity':'1',
                  'will-change':'auto','zoom':'1','writing-mode':'horizontal-tb',
                  'overflow-x':'visible','overflow-y':'visible','overflow-clip-margin':'none'})
        return [strings.index(d[k]) for k in STYLE_NAMES]
    return {'strings':strings,'documents':[{'scrollOffsetX':10,'scrollOffsetY':30,
        'nodes':{'parentIndex':[-1,0,1,2],'backendNodeId':[1,2,3,4],
                 'nodeName':[0,1,2,3],'nodeType':[9,1,1,1],
                 'pseudoType':{'index':[3],'value':[4]}},
        'layout':{'nodeIndex':[1,2,3,3,3],
                  'bounds':[[0,0,600,500],[0,0,600,500],[20,40,10,5],[80,100,20,8],[0,0,0,0]],
                  'styles':[style(False),style(False),style(True),style(True),style(True)],
                  'paintOrders':[1,1,2,2,2]}}]}


class NativeGeometryTests(unittest.TestCase):
    def test_every_positive_fragment_and_scroll_retained(self):
        value=snapshot_envelopes(fixture(),[(3,'::after')])
        self.assertTrue(value['supported'])
        self.assertEqual(value['envelopes'][0]['envelope'],{'left':10,'top':10,'right':90,'bottom':78})
        self.assertEqual(len(value['envelopes'][0]['fragments']),2)

    def test_expected_missing_is_unknown(self):
        self.assertEqual(snapshot_envelopes(fixture(),[(99,'::before')])['reason'],'missing_expected_pseudo')

    def test_empty_pseudo_and_zero_geometry_not_absent(self):
        for mutation in ('pseudo','bounds'):
            value=fixture(); d=value['documents'][0]
            if mutation=='pseudo':d['nodes']['pseudoType']={}
            else:d['layout']['bounds'][2:]=[[0,0,0,0]]*3
            parsed=snapshot_envelopes(value)
            self.assertFalse(parsed['supported'])

    def test_invalid_native_types_refused(self):
        mutations=[('backendNodeId',True),('backendNodeId','ID'),('parentIndex',True)]
        for key,bad in mutations:
            s=fixture();s['documents'][0]['nodes'][key][2]=bad
            self.assertFalse(snapshot_envelopes(s)['supported'])
        for bad in ('secret',True,float('nan'),None,-1):
            s=fixture();s['documents'][0]['layout']['paintOrders'][2]=bad
            self.assertEqual(snapshot_envelopes(s)['reason'],'invalid_paint_order')

    def test_effects_and_ancestor_effects_refuse(self):
        for prop in ('filter','box-shadow','backdrop-filter','outline-style','zoom','mask-image','writing-mode'):
            s=fixture();s['strings'].append('unsupported');s['documents'][0]['layout']['styles'][0][STYLE_NAMES.index(prop)]=len(s['strings'])-1
            self.assertFalse(snapshot_envelopes(s)['supported'],prop)

    def test_sticky_pseudo_and_ancestor_remain_unsupported(self):
        for rows in ([0],[2,3,4]):
            s=fixture();s['strings'].append('sticky')
            for row in rows:s['documents'][0]['layout']['styles'][row][STYLE_NAMES.index('position')]=len(s['strings'])-1
            value=snapshot_envelopes(s)
            self.assertFalse(value['supported']);self.assertEqual(value['envelopes'][0]['reason'],'unsupported_position')

    def test_frame_embedding_without_child_document_refuses(self):
        s=fixture();s['strings'][1]='IFRAME'
        self.assertEqual(snapshot_envelopes(s)['reason'],'unsupported_tree_boundary')

    def test_capture_binds_styles_ids_and_viewport_then_strips_private_data(self):
        class Session:
            def __init__(self,snapshots):self.snapshots=iter(snapshots);self.detached=False
            def send(self,*args):return next(self.snapshots)
            def detach(self):self.detached=True
        class Context:
            def new_cdp_session(self,page):return page.session
        class Page:
            context=Context()
            def __init__(self,a,b):self.session=Session([a,b])
            def evaluate(self,script):return dict(width=600,height=500,x=10,y=30,scale=1,left=0,top=0)
        p=Page(fixture(),fixture());value=capture_native_envelopes(p,time.monotonic()+5)
        self.assertTrue(value['supported']);self.assertTrue(p.session.detached)
        self.assertNotIn('_binding',value);self.assertNotIn('secret',str(value))
        changed=fixture();changed['documents'][0]['nodes']['backendNodeId'][2]=99
        value=capture_native_envelopes(Page(fixture(),changed),time.monotonic()+5)
        self.assertEqual(value['reason'],'unstable_native_geometry')

    def test_no_capability_and_expired_deadline_unknown(self):
        self.assertEqual(capture_native_envelopes(object(),0)['reason'],'deadline_exceeded')
        self.assertEqual(capture_native_envelopes(object(),time.monotonic()+5)['reason'],'native_capability_unavailable')

    def test_native_not_requested_for_successful_manual_visibility(self):
        class Page:
            def evaluate(self,*args):return {'version':1,'verified':True}
        self.assertTrue(native_visibility(Page(),[],time.monotonic()+5,'script')['verified'])

    def test_expired_runtime_wrapper_performs_zero_browser_calls(self):
        class Page:
            def evaluate(self,*args):raise AssertionError('Expired deadline browser call')
        value=native_visibility(Page(),[],0,'script')
        self.assertFalse(value['verified']);self.assertEqual(value['native_geometry']['reason'],'deadline_exceeded')

    def test_native_midrequest_control_error_is_propagated(self):
        from .capture_visibility import VISIBILITY_SCRIPT
        from .search.failure_policy import ResearchControlError
        class Session:
            def send(self,*args):raise ResearchControlError('Cancelled')
            def detach(self):pass
        class Page:
            def __init__(self):self.context=self
            def new_cdp_session(self,p):return Session()
            def evaluate(self,script,*args):
                if script==VISIBILITY_SCRIPT:return {'verified':False,'reason':'unverifiable_pseudo_extent'}
                return {'viewport':{'width':600,'height':500},'url':'about:blank','scroll':[0,0],'scale':1,'left':0,'top':0}
        with self.assertRaises(ResearchControlError):native_visibility(Page(),[],time.monotonic()+5,VISIBILITY_SCRIPT)

    def test_runtime_bracket_stale_identity_and_missing_native_refuse(self):
        from .capture_visibility import VISIBILITY_SCRIPT
        class Session:
            def __init__(self, changed):self.count=0;self.changed=changed
            def send(self,method,params):
                if method=='DOM.getDocument':return {'root':{'nodeType':9,'backendNodeId':1}}
                if method=='DOMSnapshot.captureSnapshot':
                    self.count+=1;s=fixture()
                    if self.count==2 and self.changed:s['documents'][0]['nodes']['backendNodeId'][2]=99
                    return s
                if method=='DOM.resolveNode':return {'object':{'objectId':str(params['backendNodeId'])}}
                if method=='Runtime.callFunctionOn':return {'result':{'value':{'version':1,'verified':True,'viewport':{'width':600,'height':500},'_native_state_valid':True,'_native_viewport':{'width':600,'height':500},'_native_used':[{'owner_backend_id':3,'pseudo_backend_id':4,'part':'::after','envelope':{'left':10,'top':10,'right':90,'bottom':78}}]}}}
                return {}
            def detach(self):pass
        class Page:
            def __init__(self,changed):self.session=Session(changed);self.context=self
            def new_cdp_session(self,p):return self.session
            def evaluate(self,script,*args):
                if script==VISIBILITY_SCRIPT:return {'verified':False,'reason':'unverifiable_pseudo_extent'}
                return {'viewport':{'width':600,'height':500},'url':'about:blank','scroll':[10,30],'scale':1,'left':0,'top':0}
        good=native_visibility(Page(False),[],time.monotonic()+5,VISIBILITY_SCRIPT)
        self.assertTrue(good['verified']);self.assertTrue(good['native_geometry']['used'])
        bad=native_visibility(Page(True),[],time.monotonic()+5,VISIBILITY_SCRIPT)
        self.assertFalse(bad['verified']);self.assertEqual(bad['native_geometry']['reason'],'stale_native_snapshot')
        class Missing(Page):
            def new_cdp_session(self,p):raise RuntimeError('unavailable')
        self.assertFalse(native_visibility(Missing(False),[],time.monotonic()+5,VISIBILITY_SCRIPT)['verified'])

    def test_site_global_callback_cannot_license_unresolved_cover(self):
        from .capture_visibility import VISIBILITY_SCRIPT
        base=(Path(__file__).parent/'testdata/price-frame/visibility-dom-probe.js').read_text()
        extra="\nglobal.nativePseudoExtent=()=>({extents:[{left:0,right:1,top:0,bottom:1}]});\nassert.equal(check(required).verified,false,'site global helper cannot replace lexical native proof');\n"
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'global.js';p.write_text(base+extra)
            result=subprocess.run(['node',str(p)],input=VISIBILITY_SCRIPT,text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_native_receipt_schema_is_required_when_present(self):
        from .capture_visibility import native_receipt_valid
        row={'owner_backend_id':3,'pseudo_backend_id':4,'part':'::after','envelope':{'left':0,'top':0,'right':10,'bottom':10}}
        good={'version':1,'document_backend_id':1,'signature_sha256':'a'*64,'used':[row]}
        self.assertTrue(native_receipt_valid(good))
        for patch in ({'version':True},{'used':[]},{'used':[row,row]},{'signature_sha256':'fake'},{'document_backend_id':True},{'diagnostic':'unsupported_geometry','used':[]}):
            self.assertFalse(native_receipt_valid({**good,**patch}))


if __name__ == '__main__':unittest.main()
