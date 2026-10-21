"""Document selection and intercepted native producer integration contracts."""
import copy
import json
from contextlib import nullcontext
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from .native_geometry import snapshot_envelopes, STYLE_NAMES
from .test_native_geometry import fixture


def multi_fixture():
    s=fixture();d=s['documents'][0];n=d['nodes'];s['strings'].append('IFRAME')
    n['parentIndex'].append(2);n['backendNodeId'].append(5)
    n['nodeName'].append(len(s['strings'])-1);n['nodeType'].append(1)
    n['contentDocumentIndex']={'index':[4],'value':[1]}
    layout=d['layout'];layout['nodeIndex'].append(4);layout['bounds'].append([400,400,100,50])
    layout['styles'].append(list(layout['styles'][1]));layout['paintOrders'].append(3)
    child=copy.deepcopy(fixture()['documents'][0]);child['nodes']['backendNodeId']=[11,12,13,14]
    s['documents'].append(child);return s


class NativeDocumentParserTests(unittest.TestCase):
    def test_live_backend_not_document_order_selects_main(self):
        s=multi_fixture();a=snapshot_envelopes(s,document_backend_id=1)
        self.assertIn('_binding',a);self.assertEqual(a['state']['document_backend_id'],1)
        self.assertEqual([e['owner_backend_id'] for e in a['envelopes']],[3])
        reordered=copy.deepcopy(s);reordered['documents'].reverse()
        reordered['documents'][1]['nodes']['contentDocumentIndex']['value']=[0]
        self.assertEqual(a,snapshot_envelopes(reordered,document_backend_id=1))
        self.assertEqual(len(a['_frames']),1)

    def test_missing_duplicate_detached_unmapped_documents_refuse(self):
        for mutation in ('missing','duplicate','unmapped','disconnected','cycle'):
            s=multi_fixture();live=999 if mutation=='missing' else 1
            if mutation=='duplicate':s['documents'][1]['nodes']['backendNodeId'][0]=1
            if mutation=='unmapped':s['documents'][0]['nodes']['contentDocumentIndex']={}
            if mutation=='disconnected':s['documents'].append(copy.deepcopy(fixture()['documents'][0]))
            if mutation=='cycle':s['documents'][1]['nodes']['contentDocumentIndex']={'index':[2],'value':[0]}
            result=snapshot_envelopes(s,document_backend_id=live)
            self.assertFalse(result['supported'],mutation);self.assertNotIn('_binding',result,mutation)

    def test_consumed_native_containers_are_typed_arrays(self):
        mutations = (
            lambda s:s['documents'][0]['nodes'].update(contentDocumentIndex={'index':{4:99},'value':{1:99}}),
            lambda s:s.update(strings={i:v for i,v in enumerate(s['strings'])}),
            lambda s:s.update(strings=['valid', True]),
            lambda s:s['documents'][0]['nodes'].update(parentIndex={i:v for i,v in enumerate(s['documents'][0]['nodes']['parentIndex'])}),
            lambda s:s['documents'][0]['layout'].update(styles={i:v for i,v in enumerate(s['documents'][0]['layout']['styles'])}),
            lambda s:s['documents'][0]['layout']['styles'].__setitem__(0,{i:v for i,v in enumerate(s['documents'][0]['layout']['styles'][0])}),
            lambda s:s['documents'][0]['layout']['bounds'].__setitem__(0,{i:v for i,v in enumerate(s['documents'][0]['layout']['bounds'][0])}),
            lambda s:s['documents'][0]['layout'].update(paintOrders=[True]*len(s['documents'][0]['layout']['paintOrders'])),
            lambda s:s['documents'][0]['nodes'].update(pseudoType={'index':[True],'value':[4]}),
        )
        for mutate in mutations:
            s=multi_fixture();mutate(s);result=snapshot_envelopes(s,document_backend_id=1)
            self.assertFalse(result['supported']);self.assertNotIn('_binding',result)

    def test_consumed_node_values_are_valid_in_every_document(self):
        for index in (0,1):
            for key,value in (('nodeName',-1),('nodeName',99999),('nodeType',2),('parentIndex',99999),('parentIndex',True)):
                s=multi_fixture();s['documents'][index]['nodes'][key][1]=value
                result=snapshot_envelopes(s,document_backend_id=1)
                self.assertFalse(result['supported'],(index,key,value));self.assertNotIn('_binding',result)
        s=fixture();s['documents'][0]['nodes']['nodeName'][1]=-1
        self.assertFalse(snapshot_envelopes(s)['supported'])

    def test_shadow_and_frame_effect_are_unknown(self):
        s=multi_fixture();s['documents'][0]['nodes']['shadowRootType']={'index':[2],'value':[0]}
        self.assertFalse(snapshot_envelopes(s,document_backend_id=1)['supported'])
        s=multi_fixture();s['strings'].append('100px')
        s['documents'][0]['layout']['styles'][-1][STYLE_NAMES.index('perspective')]=len(s['strings'])-1
        self.assertEqual(snapshot_envelopes(s,document_backend_id=1)['reason'],'unsupported_frame_enclosure')


    def test_owned_control_at_new_document_or_resolution_call_is_exact(self):
        from .native_geometry import native_visibility
        from .capture_visibility import VISIBILITY_SCRIPT
        from .search.failure_policy import ResearchControlError
        import time
        for target in ('DOM.getDocument','DOM.resolveNode'):
            control=ResearchControlError('owned cancellation');calls=[]
            class Session:
                def send(self,method,params):
                    calls.append(method)
                    if method==target:raise control
                    if method=='DOM.getDocument':return {'root':{'nodeType':9,'backendNodeId':1}}
                    if method=='DOMSnapshot.captureSnapshot':return fixture()
                    return {}
                def detach(self):calls.append('detach')
            class Page:
                context=None
                def __init__(self):self.context=self
                def new_cdp_session(self,page):return Session()
                def evaluate(self,script,*args):
                    if script==VISIBILITY_SCRIPT:return {'verified':False,'reason':'unverifiable_pseudo_extent'}
                    return {'viewport':{'width':600,'height':500},'url':'about:blank','scroll':[10,30],'scale':1,'left':0,'top':0}
            with self.assertRaises(ResearchControlError) as caught:native_visibility(Page(),[],time.monotonic()+2,VISIBILITY_SCRIPT)
            self.assertIs(caught.exception,control);self.assertIn('detach',calls)


class NativeDocumentRenderedTests(unittest.TestCase):
    evidence_dir = None
    selected_cases = None
    expected_overrides = {}
    def test_real_producer_sidecar_and_current_consumers(self):
        from pydantic_settings import BaseSettings
        init=BaseSettings.__init__
        def safe(obj,*a,**kw):kw['_env_file']=None;return init(obj,*a,**kw)
        def deny(*a,**kw):raise AssertionError('External network forbidden')
        cases=[('same_disjoint','left:1100px;top:600px',True,False),
               ('mapped_cross_origin','left:1100px;top:600px',True,True),
               ('cross_disjoint','left:1100px;top:600px',False,True),
               ('same_overlap','left:20px;top:200px;width:1200px;height:500px',False,False),
               ('cross_pointer_none','left:20px;top:200px;width:1200px;height:500px;pointer-events:none',False,True),
               ('fixed_disjoint','position:fixed;left:1100px;top:600px',False,False),
               ('transform_disjoint','left:1100px;top:600px;transform:translateX(1px)',False,False),
               ('postshot_frame_move','left:1100px;top:600px',False,False),
               ('child_price_no_donor','left:1100px;top:600px',False,False)]
        if self.selected_cases:cases=[(n,c,self.expected_overrides.get(n,w),x) for n,c,w,x in cases if n in self.selected_cases]
        location=nullcontext(str(self.evidence_dir)) if self.evidence_dir else tempfile.TemporaryDirectory(prefix='native-document-')
        with location as tmp:
            root=Path(tmp);root.mkdir(parents=True,exist_ok=True);rows=[]
            with patch.object(BaseSettings,'__init__',safe),patch.object(socket.socket,'connect',deny),patch('socket.create_connection',deny):
                from playwright.sync_api import sync_playwright
                from . import price_capture as cap,config
                from .capture_visibility import attestation_path,verify_attestation
                from .search.product_identity import capture_current_identity,valid_identity
                with patch.object(config,'CACHE_DIR',root),sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='chrome',headless=True,args=['--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run'])
                    try:
                        for name,css,want,cross in cases:
                            ctx=browser.new_context(viewport={'width':1366,'height':900});page=ctx.new_page()
                            url='https://native-local.invalid/card';child='https://native-local.invalid:444/frame' if name=='mapped_cross_origin' else 'https://child.invalid/frame' if cross else 'https://native-local.invalid/frame'
                            html='<meta charset=utf-8><style>body{margin:0;font:20px Arial;background:white;color:black;height:1400px}#owner{position:absolute;left:60px;top:20px;width:180px;height:90px}#owner::after{content:"";position:absolute;left:20px;right:0;top:20px;bottom:0;width:40px;height:0;border-top:8px solid red;pointer-events:none}main{position:relative;top:220px;left:40px;width:1000px}h1{font:26px Arial;position:relative}iframe{position:absolute;width:150px;height:80px;border:0;'+css+'}</style><nav id=owner></nav><main><h1>Item ABC123</h1><section><p>Артикул: ABC123</p><p id=price>100 ₽ / м</p><button>Купить</button></section></main><iframe src="'+child+'"></iframe>'
                            if name=='child_price_no_donor':html=html.replace('<p id=price>100 ₽ / м</p>','')
                            def route(r):
                                if r.request.url==url:r.fulfill(status=200,content_type='text/html',body=html)
                                elif r.request.url==child:r.fulfill(status=200,content_type='text/html',body='<body style="background:black">Child XYZ999 900 ₽ / шт</body>')
                                else:r.abort()
                            ctx.route('**/*',route);page.goto(url,wait_until='load')
                            if name=='postshot_frame_move':
                                shot=page.screenshot
                                def mutate(**kw):
                                    value=shot(**kw);page.evaluate("() => document.querySelector('iframe').style.cssText='position:absolute;left:20px;top:200px;width:1200px;height:500px;pointer-events:none'");return value
                                page.screenshot=mutate
                            out=root/(name+'.png');failures=[]
                            evidence,reason=cap.capture_price(page,out,url=url,title='Item ABC123',expected_mark='ABC123',expected_name='Item',listed=100,normalized=100,sale_unit='м',requested_unit='м',on_refusal=failures.append)
                            row={'case':name,'proof':bool(evidence),'reason':reason,'refusals':failures,'native':getattr(page,'_kac_native_visibility_diagnostics',{})}
                            rows.append(row);(root/'results.json').write_text(json.dumps(rows,indent=2,ensure_ascii=False))
                            self.assertEqual(bool(evidence),want,(name,reason,failures,row['native']))
                            if evidence:
                                self.assertTrue(evidence['visibility_before'].get('native_geometry'),name)
                                self.assertTrue(verify_attestation(out,evidence))
                                identity=capture_current_identity(page,root/(name+'-identity.png'),url,'ABC123','Item','native-local.invalid','Item ABC123')
                                self.assertTrue(valid_identity(identity),identity)
                                evidence,reason=cap.reconcile_capture_identity(evidence,identity,cap._snapshot(page))
                                self.assertTrue(evidence,(name,reason))
                                offer={'price_capture_evidence':evidence,'identity_evidence':identity,'url':url,'listed_price_with_vat':100,'price_with_vat':100,'price_unit':'м','normalized_price_unit':'м','screenshot_product':str(out)}
                                self.assertEqual(cap.capture_review_reason(offer),'')
                                self.assertEqual(cap.unit_capture_review_reason(offer,{'unit':'м','type_mark':'ABC123','name':'Item'}),'')
                            else:self.assertFalse(attestation_path(out).exists(),name)
                            row['consumers_passed']=bool(evidence);(root/'results.json').write_text(json.dumps(rows,indent=2,ensure_ascii=False))
                            ctx.close()
                    finally:browser.close()


if __name__=='__main__':unittest.main()
