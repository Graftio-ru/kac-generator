"""Same HTTP-first producer, zero photographs; selected capture stays native."""
import tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch,Mock,MagicMock
import httpx
from engine.models import SpecPosition,Requisites
from engine.search import agent,card_attachments,http_card
from engine.search.catalogue import CatalogueStore,identity_for_position
from engine.search.catalogue_images import CatalogueImageStore
from engine.search.catalogue_producer import capture_and_publish
from engine.search.catalogue_projection import catalogue_scope,project_product
from engine.search.selected_proofs import proof_ready,NOT_SELECTED
from engine.search.failure_policy import FailureContext
from engine.test_http_card import html,URL
class HttpPriceCollectionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.p=SpecPosition(number=1,name='Cable',type_mark='ABC123',unit='м',qty=24);self.url=URL;self.store=CatalogueStore(self.root/'store.sqlite3');self.identity=identity_for_position(self.p);self.store.acquire_unknown(self.identity,['supplier.example'],owner='fixture')
 def response(self,body,status=200):return httpx.Response(status,headers={'content-type':'text/html'},text=body,request=httpx.Request('GET',self.url))
 def collect(self,response=None,*,error=None,advice=None,preflight=True,render=None,extract=False):
  get=Mock(side_effect=error) if error else Mock(return_value=response or self.response(html()));render=render or Mock(return_value=None)
  with patch.object(agent,'CACHE_DIR',self.root),patch.object(http_card,'CACHE_DIR',self.root),patch('engine.config.CACHE_DIR',self.root),patch.object(agent,'supplier_http_get',get),patch('engine.search.session.http_capability_advice',return_value=advice),patch('engine.search.session.finish_http_capability'),patch.object(agent,'_capture_rendered_card',render),patch.object(agent,'wl_requisites_for',return_value=Requisites(name='Supplier',inn='7842224734')),patch('engine.review_context.capture_context',side_effect=AssertionError('collection photo')),agent.capture_card_diagnostics() as diagnostics:
   source=capture_and_publish(self.store,self.identity,self.p,self.url,browser=MagicMock(),image_store=CatalogueImageStore(self.root),capture=lambda b,p,u,**kw:card_attachments.collect_current(b,p,u,extract=extract,http_preflight=preflight,**kw),supplier_host=self.url.split('/')[2].removeprefix('www.'))
  return source,get,render,diagnostics
 def test_owned_static_price_normal_publication_zero_browser_photo_or_put(self):
  source,get,render,diagnostics=self.collect()
  self.assertEqual(get.call_count,1);render.assert_not_called();self.assertEqual((source.raw_amount,source.sale_unit,source.image.status),(120,'м','missing'));self.assertEqual(source.image.error,NOT_SELECTED);self.assertEqual(source.issuer,'supplier_http')
  with catalogue_scope(self.store):self.assertFalse(proof_ready(project_product(self.store,self.identity,self.p).all_offers[0],self.p))
  self.assertEqual(source.observed_at,self.store.lookup(self.identity)['heads'][0]['current']['observed_at']);self.assertTrue(list((self.root/'http_cards').glob('*.json')))
 def test_explicit_native_contradiction_same_delivery_no_browser(self):
  self.p=SpecPosition(number=1,name='Transformer 5 VA accuracy class 0.5',type_mark='CT30 / 35266',unit='шт',qty=1,source_evidence=dict(reader='native_pdf',cells=dict(name='Transformer 5 VA accuracy class 0.5',model='CT30',article='35266',maker='DKC')))
  self.identity=identity_for_position(self.p);self.store.acquire_unknown(self.identity,['supplier.example'],owner='fixture')
  # No publication is needed or permitted for the clearly different owner.
  body='<main itemscope itemtype="https://schema.org/Product"><h1>Transformer CT30 OTHER999 3 VA</h1><div>Manufacturer code: OTHER999</div><div>Manufacturer: DKC</div></main>'
  source,get,render,diagnostics=self.collect(self.response(body));self.assertIsNone(source);self.assertEqual(get.call_count,1);render.assert_not_called();self.assertTrue(any(d['code'].startswith('http_primary_') for d in diagnostics));self.assertEqual(self.store.lookup(self.identity)['heads'],[])
 def test_ambiguous_html_uses_existing_single_render_fallback(self):
  source,get,render,diagnostics=self.collect(self.response('<html><h1>Cable ABC123</h1><p>Loading</p></html>'));self.assertIsNone(source);self.assertEqual(get.call_count,1);self.assertEqual(render.call_count,1);self.assertIn('neutral_sink',render.call_args.kwargs)
 def test_browser_first_capability_skips_http(self):
  advice=(Mock(),Mock(),dict(allowed=False,reason='read_timeout',checked_at=time.time(),retry_at=time.time()+60,failure_code='network_timeout'))
  source,get,render,diagnostics=self.collect(advice=advice);get.assert_not_called();self.assertEqual(render.call_count,1);self.assertTrue(any(d['code']=='http_capability_browser_first' for d in diagnostics))
 def test_annotated_timeout_falls_back_but_proxy_and_unbound_errors_propagate(self):
  request=httpx.Request('GET',self.url);timeout=httpx.ReadTimeout('fixture',request=request);timeout._kac_supplier_failure=FailureContext('supplier_http','local','network_timeout',time.time(),None,'supplier.example')
  source,get,render,diagnostics=self.collect(error=timeout);self.assertEqual(get.call_count,1);self.assertEqual(render.call_count,1)
  for error in (httpx.ProxyError('fixture',request=request),httpx.ReadTimeout('fixture',request=request)):
   with self.subTest(error=type(error).__name__),self.assertRaises(type(error)):self.collect(error=error)
 def test_default_selected_photo_path_never_uses_http_first(self):
  with patch.object(agent,'_capture_card_live',side_effect=AssertionError('selected HTTP-first')),patch.object(agent,'_capture_rendered_card',return_value=None) as render:
   card_attachments.capture_current(Mock(),self.p,self.url,extract=False)
  self.assertEqual(render.call_count,1)

 def test_normal_extract_true_literal_http_uses_no_model_or_page(self):
  with patch('engine.search.card_batch.extract_batch',side_effect=AssertionError('literal HTTP needs no model')):
   source,get,render,diagnostics=self.collect(extract=True)
  self.assertEqual(source.raw_amount,120);self.assertEqual(get.call_count,1);render.assert_not_called()
 def test_missing_sale_unit_is_raw_review_not_buyer_unit_guess(self):
  source,get,render,diagnostics=self.collect(self.response(html().replace('<p>Цена за 1 м</p>','')))
  self.assertEqual(source.raw_amount,120);self.assertEqual(source.sale_unit,'unknown');self.assertEqual(source.state,'review');render.assert_not_called()
  with catalogue_scope(self.store):self.assertFalse(proof_ready(project_product(self.store,self.identity,self.p).all_offers[0],self.p))
 def test_multiple_products_still_fallback_without_static_quote(self):
  source,get,render,diagnostics=self.collect(self.response(html()+html(sku='OTHER999')));self.assertIsNone(source);self.assertEqual(get.call_count,1);self.assertEqual(render.call_count,1)
 def test_parent_deadline_and_cancel_stop_before_any_http_or_render(self):
  from engine.search.session import research_session,SearchBudgetExceeded
  for parameters in ({'deadline':time.monotonic()-1},{'cancelled':lambda:True}):
   with self.subTest(parameters=list(parameters)),research_session(**parameters),patch.object(agent,'supplier_http_get',side_effect=AssertionError('HTTP forbidden')),patch.object(agent,'_capture_rendered_card',side_effect=AssertionError('render forbidden')):
    with self.assertRaises(SearchBudgetExceeded):card_attachments.collect_current(Mock(),self.p,self.url)

 def test_retained_dinasty_positive_http_static_batch_source_and_unit_preserved(self):
  import json,base64
  from engine.search.card_batch import CardSnapshot
  from engine.offer_selection import offer_is_comparable
  root=Path(__file__).resolve().parents[2];live=root/'sources/kac-generator/http-first-live-2026-10-14'
  record=json.loads((live/'cache/http_cards/70f3d4e09292410ba933e3f3e797bd30.json').read_text());body=base64.b64decode(record['body_base64'],validate=True)
  rows=json.loads((root/'sources/kac-generator/full-service-2026-10-14-active-route-slice-1/parsed.json').read_text())['positions'];self.p=SpecPosition.model_validate(next(p for p in rows if p['number']==29));self.url=record['url'];self.identity=identity_for_position(self.p);self.store.acquire_unknown(self.identity,['dinastyasnab.ru'],owner='fixture')
  response=httpx.Response(200,headers={'content-type':record['content_type']},content=body,request=httpx.Request('GET',self.url));batch_calls=[]
  def batch(snapshots):
   snapshot=snapshots[0];batch_calls.append(snapshot);facts=snapshot.facts();self.assertEqual(facts['identity_evidence']['provenance'],'http_main_product');self.assertEqual(facts['captured_at'],record['observed_at'])
   return {snapshot.card_id:(dict(product_title=facts['main_title'],matches_requested_product=True,listed_price_with_vat=4618.81,price_with_vat=4618.81,price_unit='шт'),None)}
  # Save exactly the retained delivery as a new owned offline source reference,
  # keeping its original physical observation date (not a fresh supplier claim).
  save=http_card.save_response
  def retained(response,url):
   delivered,source=save(response,url);delivered['observed_at']=record['observed_at'];path=self.root/source['ref'];payload=__import__('json').dumps(delivered,ensure_ascii=False,sort_keys=True);path.write_text(payload);source['record_sha256']=__import__('hashlib').sha256(payload.encode()).hexdigest();return delivered,source
  with patch.object(http_card,'save_response',side_effect=retained),patch('engine.search.card_batch.extract_batch',side_effect=batch):source,get,render,diagnostics=self.collect(response,extract=True)
  self.assertEqual(len(batch_calls),1);render.assert_not_called();self.assertEqual(get.call_count,1);self.assertEqual((source.raw_amount,source.sale_unit,source.observed_at),(4618.81,'шт',record['observed_at']));self.assertEqual(source.image.status,'missing')
  with catalogue_scope(self.store):
   offer=project_product(self.store,self.identity,self.p).all_offers[0];self.assertAlmostEqual(offer.price_with_vat,1539.6033333333335);self.assertEqual(offer.price_unit,'шт');self.assertTrue(offer_is_comparable(offer,self.p));self.assertFalse(proof_ready(offer,self.p))
 def test_static_model_guessed_unit_or_cash_cannot_become_comparable(self):
  from engine.offer_selection import offer_is_comparable
  from engine.search.catalogue_producer import CataloguePublicationError
  body=html().replace('<p>Цена за 1 м</p>','<p>Единица измерения: шт</p>')
  for amount,unit in ((120,'м'),(999,'шт')):
   def batch(snapshots):
    s=snapshots[0];return {s.card_id:(dict(product_title=s.facts()['main_title'],matches_requested_product=True,listed_price_with_vat=amount,price_with_vat=amount,price_unit=unit),None)}
   with self.subTest(amount=amount,unit=unit),patch('engine.search.card_batch.extract_batch',side_effect=batch):
    try:source,get,render,diagnostics=self.collect(self.response(body),extract=True)
    except CataloguePublicationError as failure:
     self.assertIn(failure.code,{'catalogue_source_amount_unobserved','catalogue_generation_mismatch'});continue
   render.assert_not_called()
   with catalogue_scope(self.store):self.assertFalse(offer_is_comparable(project_product(self.store,self.identity,self.p).all_offers[0],self.p))
 def test_unusable_source_identity_uses_normal_render_instead_of_static_batch(self):
  with patch('engine.search.product_identity.http_identity',return_value={}),patch('engine.search.card_batch.extract_batch',side_effect=AssertionError('unbound source batch')):
   source,get,render,diagnostics=self.collect(extract=True)
  self.assertIsNone(source);self.assertEqual(render.call_count,1)
 def test_existing_forced_render_policy_is_retained(self):
  self.url='https://e-kc.ru/cena/ABC123';self.store.acquire_unknown(self.identity,['e-kc.ru'],owner='fixture')
  source,get,render,diagnostics=self.collect(self.response(html()),extract=True);self.assertIsNone(source);self.assertEqual(get.call_count,1);self.assertEqual(render.call_count,1)
