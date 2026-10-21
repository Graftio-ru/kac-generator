"""Finite UI phase, real delayed LOCAL current proof and unchanged admission."""
import ast,json,socket,time,unittest
from pathlib import Path
from unittest.mock import patch
from . import price_capture as cap
from .search.failure_policy import ResearchControlError
from .search.session import research_session,SearchBudgetExceeded
D=Path(__file__).resolve().parents[2]/'sources/kac-generator/current-card-readiness-2026-10-11'
class ReadinessTests(unittest.TestCase):
 def test_fast_parent_never_ready_and_late_sync_return(self):
  from pydantic_settings import BaseSettings
  init=BaseSettings.__init__
  def safe(obj,*a,**kw):kw['_env_file']=None;return init(obj,*a,**kw)
  with patch.object(BaseSettings,'__init__',safe):
   for mode,deadline,want,max_elapsed in [('fast',None,True,.3),('never',None,False,20.01),('parent',100.6,False,.61),('late',None,False,20.01)]:
    clock=[100.];calls=[]
    class Page:
     def wait_for_timeout(self,ms):clock[0]+=ms/1000
    def snapshot(page):
     calls.append(clock[0])
     if mode=='late':clock[0]+=10.1
     return {'url':'https://local.invalid/card','title':'Item ABC123','text':'100 ₽ / м','prices':[{'text':'100 ₽ / м'}]} if mode in ('fast','late') else {}
    with patch.object(cap.time,'monotonic',side_effect=lambda:clock[0]),patch.object(cap,'_snapshot',side_effect=snapshot),research_session(deadline=deadline):
     result=cap.ready_price_snapshot(Page())
    self.assertEqual(bool(result),want,mode);self.assertLessEqual(clock[0]-100,max_elapsed if mode!='late' else 20.51);self.assertEqual(len(calls),2) if mode=='fast' else None
 def test_cancel_control_and_source_error(self):
  class Page:pass
  with research_session(cancelled=lambda:True):
   with self.assertRaises(SearchBudgetExceeded):cap.ready_price_snapshot(Page())
  error=ResearchControlError('owned')
  with patch.object(cap,'_snapshot',side_effect=error):
   with self.assertRaises(ResearchControlError) as caught:cap.ready_price_snapshot(Page())
  self.assertIs(caught.exception,error)
  with patch.object(cap,'_snapshot',side_effect=RuntimeError('source unavailable')):self.assertEqual(cap.ready_price_snapshot(Page()),{})
 def test_real_delayed_current_chain_and_wrong_sku(self):
  from pydantic_settings import BaseSettings
  init=BaseSettings.__init__
  def safe(obj,*a,**kw):kw['_env_file']=None;return init(obj,*a,**kw)
  def deny(*a,**kw):raise AssertionError('External forbidden')
  root=D/'local-controls';root.mkdir(exist_ok=True)
  with patch.object(BaseSettings,'__init__',safe),patch.object(socket.socket,'connect',deny),patch('socket.create_connection',deny):
   from playwright.sync_api import sync_playwright
   from . import config
   from .search.product_identity import capture_current_identity,valid_identity
   from .capture_visibility import verify_attestation
   tree=ast.parse((D/'predecessor/price_capture.py').read_text());node=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='ready_price_snapshot')
   env={'__name__':'engine.price_capture','__package__':'engine','time':time,'_snapshot':cap._snapshot,'ResearchControlError':ResearchControlError};exec(compile(ast.Module(body=[node],type_ignores=[]),'accepted-ready-predecessor','exec'),env)
   rows=[]
   with patch.object(config,'CACHE_DIR',root),sync_playwright() as pw:
    browser=pw.chromium.launch(channel='chrome',headless=True,args=['--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run'])
    try:
     ctx=browser.new_context(viewport={'width':1366,'height':900});page=ctx.new_page();url='https://ready-local.invalid/card'
     html='<meta charset=utf-8><style>body{font:20px Arial;background:white;color:black}h1{font:26px Arial}</style><main id=card></main><script>setTimeout(()=>document.querySelector("main").innerHTML="<h1>Item ABC123</h1><section><p>Артикул: ABC123</p><p>100 ₽ / м</p><button>Купить</button></section>",9500)</script>'
     ctx.route('**/*',lambda r:r.fulfill(status=200,content_type='text/html',body=html));page.goto(url,wait_until='load')
     start=time.monotonic();old=env['ready_price_snapshot'](page);old_elapsed=time.monotonic()-start;self.assertFalse(old);self.assertGreaterEqual(old_elapsed,8)
     page.close();page=ctx.new_page();page.goto(url,wait_until='load')
     begin=time.monotonic();snapshot=cap.ready_price_snapshot(page);ready_elapsed=time.monotonic()-begin;self.assertTrue(snapshot);self.assertGreater(ready_elapsed,9)
     fields=cap.visible_quote_fields(snapshot,'м',url=url,title=snapshot['title'],expected_mark='ABC123',expected_name='Item');self.assertTrue(fields)
     out=root/'current-price.png';proof,reason=cap.capture_price(page,out,url=url,title=snapshot['title'],expected_mark='ABC123',expected_name='Item',listed=100,normalized=100,sale_unit='м',requested_unit='м');self.assertTrue(proof,reason);self.assertTrue(verify_attestation(out,proof))
     identity=capture_current_identity(page,root/'identity.png',url,'ABC123','Item','ready-local.invalid',snapshot['title']);self.assertTrue(valid_identity(identity));proof,reason=cap.reconcile_capture_identity(proof,identity,cap._snapshot(page));self.assertTrue(proof,reason)
     offer={'url':url,'listed_price_with_vat':100,'price_with_vat':100,'price_unit':'м','normalized_price_unit':'м','screenshot_product':str(out),'price_capture_evidence':proof,'identity_evidence':identity};self.assertEqual(cap.capture_review_reason(offer),'');self.assertEqual(cap.unit_capture_review_reason(offer,{'unit':'м','type_mark':'ABC123','name':'Item'}),'')
     rows.append({'old_ready_empty':True,'old_seconds':old_elapsed,'new_ready_seconds':ready_elapsed,'hydration_ms':9500,'full_current_consumers':True})
     page.evaluate("() => {document.querySelector('h1').textContent='Item OTHER456';document.querySelector('p').textContent='Артикул: OTHER456'}")
     wrong=cap.ready_price_snapshot(page);self.assertTrue(wrong);self.assertEqual(cap.visible_quote_fields(wrong,'м',url=url,title=wrong['title'],expected_mark='ABC123',expected_name='Item'),{})
     rows.append({'changed_sku_current_gate_refused':True});(root/'results.json').write_text(json.dumps(rows,indent=2));ctx.close()
    finally:browser.close()
