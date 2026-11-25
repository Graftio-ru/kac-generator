"""Selling packages remain units of sale, never inferred requested kits."""
import hashlib,json,tempfile,time,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from engine.search import http_card as dom,owned_purchase_row as rows
from engine.price_capture import _unit

class SellingPackage355Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.scope=patch.object(dom,'CACHE_DIR',self.root);self.scope.start();self.addCleanup(self.scope.stop)
 def html(self,quotes,quantity=1,price=100,extra=''):
  return ('<main itemscope itemtype="https://schema.org/Product"><h1>Part ABC123</h1>'
   '<div>Артикул:<br><span itemprop="mpn">ABC123</span></div><span itemprop="sku">SELLER789</span>'+quotes+extra+
   f'<input type="number" name="quantity" value="{quantity}" data-price="{price}"><button>Купить</button></main>')
 def proof(self,html):
  url='https://merchant.example/product/item';owned=dom._neutral_owner(html,url)
  if not owned:return None
  stamp=time.time()-1;frame=dict(version=1,role='main_card_facts',document_url=url,main_title=owned['title'],article=owned['article'],owner_text=owned['text'],purchase_text='',owner_bound=True,owner_truncated=False,observed_at=stamp)
  source=dom.save_rendered_dom(html,frame,url);frame['rendered_source']=source;folder=self.root/'main_facts';folder.mkdir(exist_ok=True);path=folder/(uuid.uuid4().hex+'.json');raw=json.dumps(frame,sort_keys=True).encode();path.write_bytes(raw)
  source={**source,'main_facts':dict(ref=str(path.relative_to(self.root)),sha256=hashlib.sha256(raw).hexdigest(),observed_at=stamp)};self.last_source=source
  return rows.derive(source,url=url,observed_at=stamp,title=owned['title'],article=owned['article'],text=owned['text'])
 def test_package_variants_keep_distinct_native_sale_unit(self):
  for unit in ['уп','уп.','упак','упак.','упаковка','упаковки']:
   with self.subTest(unit=unit):
    proof=self.proof(self.html('<p><span>100 руб.</span> / '+unit+'</p>'));self.assertIsNotNone(proof);self.assertEqual(proof['sale_unit'],'уп');self.assertNotIn(proof['sale_unit'],['шт','компл','м']);self.assertTrue(dom.read_source(proof['source'],allow_rendered=True))
 def test_owned_package_quantity_subtotal_arithmetic(self):
  proof=self.proof(self.html('<p><span>100 руб.</span></p><div>3 уп на сумму <span>300 руб.</span></div>',quantity=3));self.assertIsNotNone(proof);self.assertEqual(proof['sale_unit'],'уп');self.assertEqual(proof['purchase_quantity'],3);self.assertEqual(proof['price_denominator'],1)
  for subtotal in [299.99,301,0]:self.assertIsNone(self.proof(self.html('<p>100 руб.</p><div>3 уп на сумму '+str(subtotal)+' руб.</div>',quantity=3)))
 def test_package_proof_never_infers_kit_piece_or_metre(self):
  proof=self.proof(self.html('<p>100 руб. / уп</p>'));self.assertIsNotNone(proof)
  for requested in ['компл','шт','м']:
   decision,reason=rows.decision(proof,dict(unit=requested));self.assertIsNone(decision);self.assertTrue(reason)
 def test_missing_unknown_mixed_independent_zero_negative_refuse(self):
  for quote in ['100 руб.','100 руб. / ящик','100 руб. / уп и шт','100 руб. / уп 100 руб. / шт','0 руб. / уп','-100 руб. / уп','−100 руб. / уп','100 руб. / 0 уп']:
   with self.subTest(quote=quote):self.assertIsNone(self.proof(self.html('<p>'+quote+'</p>')))
  for quantity in [0,-1]:self.assertIsNone(self.proof(self.html('<p>100 руб. / уп</p>',quantity=quantity)))
  self.assertIsNone(self.proof(self.html('<p>100 руб. / уп</p><div itemtype="https://schema.org/Offer">110 руб. / уп<button>Купить</button></div>')))
 def test_subtotal_stock_minimum_or_optional_offer_not_cash_authority(self):
  for quote in ['1 уп на сумму 100 руб.','В наличии 1 уп 100 руб.','Минимальный заказ 1 уп 100 руб.','Рассрочка 100 руб. / уп']:
   self.assertIsNone(self.proof(self.html('<p>'+quote+'</p>')))
 def test_current_conditions_source_and_proof_tamper_refuse(self):
  for extra in ['<p>Клубная цена по карте</p>','<p>Цена при заказе от 10 уп</p>','<span itemprop="mpn">WRONG</span>']:
   self.assertIsNone(self.proof(self.html('<p>100 руб. / уп</p>',extra=extra)))
  proof=self.proof(self.html('<p>100 руб. / уп</p>'));changed=dict(proof,sale_unit='шт');self.assertIsNone(rows.decision(changed,dict(unit='шт'))[0])
  path=self.root/proof['source']['ref'];path.write_bytes(path.read_bytes()+b'changed');self.assertIsNone(dom.read_source(proof['source'],allow_rendered=True))

class PurchasePolicy355Tests(unittest.TestCase):
 def test_exact_previous_factory_set_read_only_not_current_attestation(self):
  from engine.search import acquisition_source_intent as intent
  runtime={'fixture':'355'};sources={n:'a'*64 for n in intent.PRE_PURCHASE_SOURCE_FILES};legacy={n:sources[n] for n in intent.LEGACY_FILES}
  old=dict(version='acquisition-source-intent-v7',runtime_sha256=intent.digest(runtime),components=sources,legacy_components=legacy,legacy_sha256=intent.legacy_digest(runtime,legacy),receipt=None);old['sha256']=intent.digest(old)
  self.assertEqual(intent.validate(json.loads(json.dumps(old))),old)
  with self.assertRaises(intent.IntentInvalid):intent.attest(runtime,sources)
  self.assertIn('search/owned_purchase_row.py',intent.SOURCE_FILES)

 def test_purchase_module_byte_change_invalidates_both_policies(self):
  from engine.test_factory_policy_closure353 import FactoryPolicyClosure353Tests
  from engine.search import acquisition_source_intent as intent
  from engine.search.document_context import current_document_policy
  fixture=FactoryPolicyClosure353Tests();fixture.setUp()
  try:
   old=intent.attest(fixture.runtime,intent.components(fixture.root));policy=current_document_policy(engine_root=fixture.root,supplier_file=fixture.suppliers);path=fixture.root/'search/owned_purchase_row.py';self.assertIn(str(path),policy);path.write_bytes(path.read_bytes()+b'\n# inert355change\n')
   self.assertNotEqual(old['sha256'],intent.attest(fixture.runtime,intent.components(fixture.root))['sha256']);self.assertNotEqual(policy,current_document_policy(engine_root=fixture.root,supplier_file=fixture.suppliers))
  finally:fixture.doCleanups()
