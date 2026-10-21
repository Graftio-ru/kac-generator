"""Real DOM fixtures only; run through TASK144 independently approved offline-DOM pinned harness."""
import base64,hashlib,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from engine.search import agent
from engine.search.purchase_preview import derive_unit_basis,valid_unit_basis
from engine.search.session import research_session,SearchBudgetExceeded
from engine.source_quote import primary_text

class NativeReadinessBindingTests(unittest.TestCase):
 fixture_root=Path(__file__).resolve().parents[2]/'sources/kac-generator/atomic-simplification-2026-10-14/EPIC-0001-TASK-0138/next-actual-run/CACHE_DIR'
 def test_native_frame_with_synthetic_selected_panes_preserves_strict_binding(self):
  f=json.loads((self.fixture_root/'main_facts/99d6b143d35d41958ffbfc64df77d521.json').read_text())
  controlled=dict(f,preview_cash='18 450 ₽',preview_text='1 шт. на сумму 18 450 ₽')
  b=agent._owned_readiness_preview(controlled,f['requested_url']);self.assertIsNotNone(b)
  for fields in [dict(preview_cash='100 ₽'),dict(preview_text=''),dict(preview_text='15 шт. со склада'),dict(preview_text='1 шт. на сумму 100 ₽'),dict(preview_text='1 шт. на сумму 18 450 ₽ еще 1 шт.'),dict(owner_bound=False),dict(owner_truncated=True),dict(document_url='https://www.chipdip.ru/product/other')]:
   self.assertIsNone(agent._owned_readiness_preview(dict(controlled,**fields),f['requested_url']))

class OwnedPreviewReadinessTests(unittest.TestCase):
 browser=None
 fixture_root=Path(__file__).resolve().parents[2]/'sources/kac-generator/atomic-simplification-2026-10-14/EPIC-0001-TASK-0138/next-actual-run/CACHE_DIR'
 def setUp(self):
  if self.browser is None:self.skipTest('Qualified offline DOM harness required')
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.cache=Path(self.temp.name);p=patch.object(agent,'CACHE_DIR',self.cache);p.start();self.addCleanup(p.stop)
  self.context=self.browser.new_context(offline=True);self.addCleanup(self.context.close)
  self.context.route('**/*',lambda route:route.abort())
  self.page=self.context.new_page();self.addCleanup(self.page.close)
  self.url='https://www.chipdip.ru/product/offline-card'
 def document(self,html,url=None):
  self.page.set_content(html,wait_until='domcontentloaded')
  target=url or self.url
  self.page.evaluate('(url)=>Object.defineProperty(document,"URL",{configurable:true,get:()=>window.__ownedURL||url})',target)
  real=self.page
  class Context:
   def new_cdp_session(self,ignored):return real.context.new_cdp_session(real)
  class Page:
   context=Context()
   @property
   def url(self):return real.evaluate('document.URL')
   def inner_text(self,*a,**kw):return real.inner_text(*a,**kw)
   def locator(self,*a,**kw):return real.locator(*a,**kw)
  return Page()
 def fixture(self):
  p=self.fixture_root/'http_cards/6e16ee7258aa40629115db10fef5390e.json';d=json.loads(p.read_text());raw=base64.b64decode(d['body_base64'],validate=True)
  self.assertEqual(hashlib.sha256(raw).hexdigest(),d['body_sha256']);self.assertEqual(len(raw),109540)
  return raw.decode(d['encoding'],errors='strict')
 def small(self,*,cash='100 ₽',quantity='1 шт.',total='на сумму 100 ₽',stock='',title='Item ABC123',article='ABC123'):
  return f'''<main><div itemscope itemtype="https://schema.org/Product"><h1>{title}</h1><p>Артикул: {article}</p>{stock}
  <div class="product__ordering" itemprop="offers" itemscope itemtype="https://schema.org/AggregateOffer">
  <div class="ordering-mainoffer-price-w">{cash}</div><div class="ordering-tocart-info"><div>{quantity}</div><div>{total}</div></div><button>Добавить в корзину</button></div></div></main>'''
 def ready(self,html,*,wait=.25,mutate=None,url=None):
  page=self.document(html,url)
  if mutate:self.page.evaluate(mutate)
  started=time.monotonic()
  with research_session(deadline=time.monotonic()+3):result,ref=agent._wait_main_card_facts(page,self.url,max_wait=wait)
  elapsed=time.monotonic()-started
  raw=(self.cache/ref['ref']).read_bytes();self.assertEqual(hashlib.sha256(raw).hexdigest(),ref['sha256'])
  self.assertEqual(json.loads(raw)['kind'],result['kind'])
  return result,elapsed
 def verify_basis(self,result):
  b=result['readiness_unit_basis'];text=primary_text(result['owner_text'])
  observation=dict(source_text=text,source_sha256=hashlib.sha256(text.encode()).hexdigest(),raw_amount=b['raw_amount'],currency='RUB',observed_at=result['observed_at'],url=result['document_url'],title=result['main_title'],purchase_terms='',state='review')
  self.assertTrue(valid_unit_basis(observation,b));self.assertEqual(derive_unit_basis(observation,b['selected_cash']),b)
  self.assertEqual(b['price_quantity'],1)
  return observation,b
 def test_exact_frozen_product_html_native_proof_and_no_money_promotion(self):
  result,elapsed=self.ready(self.fixture(),wait=1)
  self.assertEqual(result['kind'],'complete');self.assertEqual(result['missing_fields'],[]);self.assertLess(elapsed,.9)
  observation,b=self.verify_basis(result);self.assertEqual(b['raw_amount'],18450);self.assertEqual(b['sale_unit'],'шт');self.assertEqual(b['selected_quantity'],1);self.assertEqual(b['subtotal'],18450)
  self.assertEqual(observation['state'],'review')
  self.assertEqual(result['purchase_text'],'');self.assertNotIn('sale_unit',result);self.assertNotIn('price',result)
  self.assertIsNone(derive_unit_basis(observation,None))
 def test_only_bound_cash_and_quantity_total_complete(self):
  r,_=self.ready(self.small(stock='<p>15 шт. со склада</p>'));self.assertEqual(r['kind'],'complete');self.verify_basis(r)
  for html in [self.small(cash='100 ₽ 200 ₽'),self.small(quantity='15 шт. со склада'),self.small(quantity='1'),self.small(total='на сумму 101 ₽'),self.small(title='Item ABC123 упаковка 10 шт.'),self.small(stock='<p>В наличии</p>',quantity='24 м',total='на сумму 2500 ₽')]:
   with self.subTest(html=html):
    r,_=self.ready(html);self.assertNotEqual(r['kind'],'complete');self.assertNotIn('readiness_unit_basis',r)
 def test_unrelated_hidden_decoy_and_ambiguous_owners_do_not_supply_unit(self):
  original=self.small()
  cases=[original.replace('class="ordering-tocart-info"','hidden class="ordering-tocart-info"'),original.replace('<h1>','<h1>Other</h1><h1>'),original.replace('itemtype="https://schema.org/AggregateOffer"','itemtype="https://schema.org/Offer"'),original.replace('itemprop="offers"','itemprop="unrelated"'),original.replace('class="product__ordering"','class="product__ordering related-products"'),original.replace('<button>','<button hidden>'),original.replace('</main>','<div itemscope itemtype="https://schema.org/Product"><h1>Decoy OTHER999</h1></div></main>')]
  for html in cases:
   with self.subTest(html=html):
    r,_=self.ready(html);self.assertNotEqual(r['kind'],'complete');self.assertNotIn('readiness_unit_basis',r)
 def test_same_offer_panes_cannot_borrow_another_preview(self):
  cases=[
   self.small(quantity='',total='').replace('</main>','<section>1 шт.\\nна сумму 100 ₽</section></main>'),
   self.small(quantity='',total='').replace('<button>','<p>1 шт.\\nна сумму 100 ₽</p><button>'),
   self.small().replace('class="ordering-mainoffer-price-w"','hidden class="ordering-mainoffer-price-w"'),
   self.small().replace('<button>','<div class="ordering-mainoffer-price-w">100 ₽</div><button>'),
   self.small().replace('<button>','<div class="ordering-tocart-info">1 шт.\\nна сумму 100 ₽</div><button>'),
  ]
  for html in cases:
   with self.subTest(html=html):
    r,_=self.ready(html);self.assertNotEqual(r['kind'],'complete');self.assertNotIn('readiness_unit_basis',r)
 def test_invisible_preview_elements_and_all_ancestors_do_not_complete(self):
  original=self.small()
  cases=[
   original.replace('class="ordering-mainoffer-price-w"','style="opacity:0" class="ordering-mainoffer-price-w"'),
   original.replace('class="ordering-tocart-info"','style="opacity:0" class="ordering-tocart-info"'),
   original.replace('<button>','<button style="opacity:0">'),
   original.replace('class="product__ordering"','style="opacity:0" class="product__ordering"'),
   original.replace('<div itemscope','<div style="opacity:0" itemscope',1),
   original.replace('<main>','<main style="opacity:0">'),
   self.small(cash='<span style="opacity:0">100 ₽</span>'),
   self.small(quantity='<span style="opacity:0">1 шт.</span>'),
   self.small(total='<span style="opacity:0">на сумму 100 ₽</span>'),
   original.replace('Добавить в корзину','<span style="opacity:0">Добавить в корзину</span>'),
  ]
  for html in cases:
   with self.subTest(html=html):
    r,_=self.ready(html);self.assertNotEqual(r['kind'],'complete');self.assertNotIn('readiness_unit_basis',r)
 def test_preview_opacity_becoming_visible_remains_eligible(self):
  for selector,html in [
   ('.ordering-mainoffer-price-w',self.small().replace('class="ordering-mainoffer-price-w"','style="opacity:0" class="ordering-mainoffer-price-w"')),
   ('.ordering-tocart-info',self.small().replace('class="ordering-tocart-info"','style="opacity:0" class="ordering-tocart-info"')),
   ('button',self.small().replace('<button>','<button style="opacity:0">')),
   ('.product__ordering',self.small().replace('class="product__ordering"','style="opacity:0" class="product__ordering"')),
   ('main',self.small().replace('<main>','<main style="opacity:0">'))]:
   with self.subTest(selector=selector):
    mutation="() => setTimeout(()=>document.querySelector("+json.dumps(selector)+").style.opacity='1',60)"
    r,elapsed=self.ready(html,wait=.25,mutate=mutation);self.assertEqual(r['kind'],'complete');self.assertGreater(elapsed,.04);self.verify_basis(r)
 def test_late_js_price_quantity_and_unit_still_arrive(self):
  html=self.small(cash='',quantity='',total='')
  mutations="""() => {setTimeout(()=>document.querySelector('.ordering-mainoffer-price-w').textContent='100 ₽',40);setTimeout(()=>document.querySelector('.ordering-tocart-info').innerHTML='<div>1 шт.</div><div>на сумму 100 ₽</div>',90)}"""
  r,elapsed=self.ready(html,wait=.3,mutate=mutations);self.assertEqual(r['kind'],'complete');self.assertGreater(elapsed,.07);self.verify_basis(r)
  r,_=self.ready(html,wait=.25,mutate="() => setTimeout(()=>document.querySelector('.ordering-tocart-info').innerHTML='<div>1 шт.</div><div>на сумму 100 ₽</div>',40)")
  self.assertNotEqual(r['kind'],'complete')
 def test_navigation_change_and_parent_control_preserve(self):
  r,_=self.ready(self.small(quantity=''),mutate="() => setTimeout(()=>window.__ownedURL='https://www.chipdip.ru/product/other',40)");self.assertEqual(r['kind'],'navigation_changed');self.assertNotIn('readiness_unit_basis',r)
  page=self.document(self.small())
  with research_session(deadline=time.monotonic()-1),self.assertRaises(SearchBudgetExceeded):agent._wait_main_card_facts(page,self.url)
  with research_session(cancelled=lambda:True),self.assertRaises(SearchBudgetExceeded):agent._wait_main_card_facts(page,self.url)
 def test_ten_real_missing_units_and_dkc_stay_unqualified(self):
  root=self.fixture_root
  names=['e94faa4d18224a6498e3d119049d3a4a','658b212c05944cd4970020ee29bef6df','6b4fbe2f370a4772b9ba3e9c2269ed83','c1e0f7f9f37941719dd1b7c609a06b67','830112d4c0ec4af0b7d94d5888b556a0','8efd680c4a06437883da1d409e94e728','97b1283ebb2446999e199c7cb8b0afc8','64447e00b36d4b84a3cdd3493dfd38c4','047d80b7c8e8487aaa94326cfa98a9c0','d30403d93c624d28b88329aaaa935e18','5032331d46dc41a6ab89c04bb323ad4e']
  for name in names:
   d=json.loads((root/'main_facts'/f'{name}.json').read_text());self.assertIsNone(agent._owned_readiness_preview(d,d['requested_url']))
