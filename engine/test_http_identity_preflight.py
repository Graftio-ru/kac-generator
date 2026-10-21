"""Bounded rejection-only HTTP preflight on mocked normal supplier delivery."""
import tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch,Mock
import httpx
from engine.models import SpecPosition
from engine.search import card_attachments,http_card,agent
from engine.search.http_identity_preflight import primary_contradiction
from engine.search.failure_policy import FailureContext,SearchServiceUnavailable
from engine.search.session import research_session,SearchBudgetExceeded

class PreflightTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.pos=SpecPosition(number=1,name='Transformer 5 VA accuracy class 0.5',type_mark='CT30 / 35266',unit='шт',qty=1,source_evidence=dict(reader='native_pdf',cells=dict(name='Transformer 5 VA accuracy class 0.5',model='CT30',article='35266',maker='DKC',unit='шт')))
  self.url='https://supplier.test/card';self.patch=patch.object(http_card,'CACHE_DIR',self.root);self.patch.start();self.addCleanup(self.patch.stop)
 def html(self,title='Transformer CT30 35266 5 VA accuracy class 0.5',article='35266',extra=''):
  return '<html><div itemscope itemtype="https://schema.org/Product"><h1>'+title+'</h1><div>Manufacturer code: '+article+'</div><div>Manufacturer: DKC</div>'+extra+'</div></html>'
 def response(self,body,status=200):return httpx.Response(status,headers={'content-type':'text/html'},text=body,request=httpx.Request('GET',self.url))
 def capture(self,response=None,error=None,advice=None):
  render=Mock(return_value=None);get=Mock(side_effect=error) if error else Mock(return_value=response)
  with patch('engine.browser.supplier_http_get',get),patch('engine.search.session.http_capability_advice',return_value=advice),patch('engine.search.session.finish_http_capability'),patch.object(agent,'_capture_rendered_card',render),agent.capture_card_diagnostics() as diagnostics:
   result=card_attachments.capture_current(Mock(),self.pos,self.url,http_preflight=True)
  return result,get,render,diagnostics
 def sibling_html(self,mpn='OTHER999',sku='MERCHANT123',title='Transformer other variant',printed=None):
  printed=mpn if printed is None else printed
  return ('<html><body><main><div itemscope itemtype="https://schema.org/Product">'
   '<meta itemprop="mpn" content="'+mpn+'"><meta itemprop="sku" content="'+sku+'">'
   '<meta itemprop="name" content="'+title+'"><div itemscope itemprop="brand" itemtype="https://schema.org/Brand"><meta itemprop="name" content="DKC"></div></div>'
   '<section><h1>'+title+'</h1><div><span>Артикул производителя</span><span>'+printed+'</span></div>'
   '<div><span>Производитель</span><span>DKC</span></div></section></main></body></html>')
 def test_metadata_sibling_owned_code_is_rejection_only_and_matching_request_continues(self):
  body=self.sibling_html();owned=http_card.main_product(body,self.url,admission_only=True)
  self.assertEqual(owned['article'],'OTHER999');self.assertEqual(owned['identity_basis'],'manufacturer_code')
  self.assertNotIn('MERCHANT123',str(owned['code_observations']));self.assertIsNone(http_card.main_product(body,self.url))
  self.assertEqual(primary_contradiction(self.pos,owned)['code'],'http_primary_identity_conflict')
  matching=self.sibling_html(mpn='35266',title='Transformer CT30 35266 5 VA accuracy class 0.5')
  self.assertIsNone(primary_contradiction(self.pos,http_card.main_product(matching,self.url,admission_only=True)))
 def test_metadata_sibling_ambiguous_hidden_unrelated_and_sku_only_fail_closed(self):
  body=self.sibling_html()
  variants=[body.replace('<main>',''),body.replace('<section>','</main><section>'),
   body.replace('<section>','<aside>').replace('</section>','</aside>'),
   body.replace('<section>','<section hidden>'),body.replace('<section>','<section data-related-products>'),body.replace('<section>','<section style="opacity:0.0">'),body.replace('<main>','<main style="opacity:0">'),
   body.replace('<h1>','<h1 hidden>'),body.replace('<span>OTHER999</span>','<span style="opacity:0">OTHER999</span>'),body.replace('<span>DKC</span>','<span style="opacity:0">DKC</span>'),body.replace('<div itemscope itemtype=', '<div hidden itemscope itemtype=',1),
   body.replace('itemprop="mpn"','itemprop="sku"'),
   body.replace('<meta itemprop="mpn" content="OTHER999">','<meta itemprop="mpn" content="OTHER999"><meta itemprop="mpn" content="SECOND999">'),
   body.replace('<h1>Transformer other variant','<h1>Unrelated variant'),
   body.replace('<span>OTHER999</span>','<span>DIFFERENT999</span>'),
   body.replace('<section>','<section><h2>Related products</h2>'),
   body.replace('<section>','<section><div itemscope itemtype="https://schema.org/Product"></div>'),
   body.replace('<section>','<section><h1>Competing title</h1>'),
   body.replace('<span>DKC</span>','<span>Other</span>'),
   body.replace('<meta itemprop="name" content="Transformer other variant">','<meta itemprop="name" content="Transformer other variant"><meta itemprop="url" content="https://other.test/card">')]
  for variant in variants:
   with self.subTest(html=variant):self.assertIsNone(http_card.main_product(variant,self.url,admission_only=True))
 def test_explicit_native_article_and_maker_conflict_zero_browser_or_model(self):
  for body in (self.html(article='OTHER999'),self.html().replace('Manufacturer: DKC','Manufacturer: Other')):
   result,get,render,diagnostics=self.capture(self.response(body))
   self.assertEqual(result,(None,None));self.assertEqual(get.call_count,1);self.assertEqual(render.call_count,0)
   self.assertTrue(any(d['code'] in ('http_primary_identity_conflict','http_primary_manufacturer_conflict') for d in diagnostics))
   self.assertTrue(list((self.root/'http_cards').glob('*.json')))
 def test_matching_ambiguous_multiple_missing_and_related_keep_rendered_path(self):
  for body in (self.html(),'<html><h1>OTHER999</h1></html>',self.html()+self.html(title='Other OTHER999'),self.html(extra='<aside>Manufacturer code: OTHER999</aside>'),self.html().replace('<div>Manufacturer code: 35266</div>','')):
   result,get,render,diagnostics=self.capture(self.response(body));self.assertEqual(render.call_count,1)
   self.assertEqual(result,(None,None));self.assertTrue(any(d['code']=='http_primary_continue' for d in diagnostics))
 def test_va_class_and_interface_only_explicit_contradictions(self):
  for title in ('Transformer CT30 35266 3 VA accuracy class 0.5','Transformer CT30 35266 5 VA accuracy class 0.5S'):
   _,_,render,_=self.capture(self.response(self.html(title=title)));self.assertEqual(render.call_count,0)
  missing=http_card.main_product(self.html(title='Transformer CT30 35266'),self.url,admission_only=True)
  self.assertIsNone(primary_contradiction(self.pos,missing))
 def test_capability_browser_first_zero_http_and_typed_failure_preserves_control(self):
  _,get,render,_=self.capture(advice=(Mock(),Mock(),{'allowed':False}));self.assertEqual(get.call_count,0);self.assertEqual(render.call_count,1)
  error=httpx.ReadTimeout('raw private text',request=httpx.Request('GET',self.url));error._kac_supplier_failure=FailureContext('supplier_http','local','network_timeout',time.time(),host='supplier.test')
  _,get,render,diagnostics=self.capture(error=error);self.assertEqual(get.call_count,1);self.assertEqual(render.call_count,1);self.assertNotIn('raw private text',str(diagnostics))
  for error in (httpx.ProxyError('private',request=httpx.Request('GET',self.url)),SearchServiceUnavailable('account_access'),RuntimeError('internal')):
   with self.assertRaises(type(error)):self.capture(error=error)
 def test_expired_parent_and_cancel_zero_new_http_browser(self):
  with research_session(deadline=time.monotonic()-1),self.assertRaises(SearchBudgetExceeded):self.capture(self.response(self.html()))
  with research_session(cancelled=lambda:True),self.assertRaises(Exception):self.capture(self.response(self.html()))
 def test_model_interface_negative_positive_and_unknown_share_native_guard(self):
  name='Multimeter 600 V 5 A RS485'
  self.pos=SpecPosition(number=1,name=name,type_mark='MT96D',unit='шт',qty=1,source_evidence=dict(reader='native_pdf',cells=dict(name=name,model='MT96D',article='',maker='DKC',unit='шт')))
  for declaration in ('Interface: RS485 not supported','Interface: RS485 optional','Interface: RS485'):
   _,_,render,diagnostics=self.capture(self.response(self.html(title='Multimeter MT96D 600 V 5 A',extra='<div>'+declaration+'</div>')))
   self.assertEqual(render.call_count,0 if 'not supported' in declaration else 1)
  _,_,render,_=self.capture(self.response(self.html(title='Multimeter MT97D 600 V 5 A')));self.assertEqual(render.call_count,0)
 def test_cancellation_after_actual_http_delivery_prevents_browser_and_publication(self):
  cancelled=[False];render=Mock(return_value=None)
  def delivered(*args,**kwargs):cancelled[0]=True;return self.response(self.html())
  with patch('engine.browser.supplier_http_get',side_effect=delivered),patch.object(agent,'_capture_rendered_card',render),research_session(cancelled=lambda:cancelled[0]),self.assertRaises(Exception):
   card_attachments.capture_current(Mock(),self.pos,self.url,http_preflight=True)
  self.assertEqual(render.call_count,0)
 def test_default_background_and_final_photo_capture_never_add_http(self):
  with patch('engine.browser.supplier_http_get',side_effect=AssertionError('No background preflight')),patch.object(agent,'_capture_rendered_card',return_value=None) as render:
   self.assertEqual(card_attachments.capture_current(Mock(),self.pos,self.url),(None,None))
  self.assertEqual(render.call_count,1)
 def test_normal_acquisition_supplies_same_explicit_callback_and_safe_refusal(self):
  from engine import app,test_catalogue_unknown as fixtures
  from engine.schemas import PositionIn
  fixture=fixtures.CatalogueUnknownTests();fixture.setUp();self.addCleanup(fixture.doCleanups);seen=[]
  def capture(*args,**kwargs):
   callback=kwargs['capture'];seen.append(callback.keywords['http_preflight'])
   agent._reject_card(args[3],'http_primary_identity_conflict','Артикул производителя основного товара противоречит спецификации',failure_context=FailureContext('supplier_http','local','identity_conflict',time.time(),200,'s0.test'))
   return None
  job,public=fixture.job([PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)],capture=capture)
  self.assertEqual(seen,[True]);check=public['rows'][0]['source_checks'][0]
  self.assertEqual(check['status'],'review');self.assertIsNotNone(check['observed_at']);self.assertEqual(check['product_presence'],'unknown')
  self.assertIn('противоречит',check['reason']);self.assertEqual(public['rows'][0]['offers'],[])

if __name__=='__main__':unittest.main()
