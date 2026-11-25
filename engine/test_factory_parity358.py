"""Shared strict structural enrichment; quote roles remain native authority."""
import copy,unittest
from unittest.mock import patch
from engine.test_selling_package355 import SellingPackage355Tests as _Fixture
from engine.search import http_card as dom,factory_reference as factory

class FactoryParity358Tests(unittest.TestCase):
 setUp=_Fixture.setUp
 proof=_Fixture.proof
 def html(self,extra='',quantity=1):
  return ('<main><div itemscope itemtype="https://schema.org/Product"><meta itemprop="mpn" content="ABC123"><meta itemprop="name" content="Part ACME ABC123"><span itemprop="brand" itemscope itemtype="https://schema.org/Brand"><meta itemprop="name" content="ACME"></span></div><article data-product-id="p"><h1>Part ACME ABC123</h1><div><span>Артикул производителя</span> <span>ABC123</span></div><div><span>Производитель</span> <span>ACME</span></div><p>100 руб. / уп</p><div><input type="number" name="quantity" data-price="100" data-count-per-pack="1" data-pack-size="1" value="'+str(quantity)+'"><button data-id="p" data-product-id="p">В корзину</button></div><div>В комплект входят пластины и крепежные элементы.</div>'+extra+'</article></main>')
 def nested(self,html):
  return html.replace('<main><div itemscope','<main><div data-product-id="p" itemscope').replace('</div><article data-product-id="p">','').replace('</article></main>','</div></main>')
 def read(self,html):
  html=self.nested(html)
  proof=self.proof(html)
  return (proof,dom.read_source(self.last_source,allow_rendered=True)) if proof else (None,None)
 def test_legacy_and_typed_delivery_quote_share_strict_reference_join(self):
  with patch.object(factory,'lookup_reference',return_value={'ref':'reviewed-fixture'}) as lookup:
   legacy=dom.main_product(self.html().replace('<p>100 руб. / уп</p>','<div>100 руб.</div><div>Единица измерения упак</div>'),'https://merchant.example/product/item');self.assertIsNotNone(legacy);self.assertEqual(legacy['factory_reference'],{'ref':'reviewed-fixture'})
   proof,retained=self.read(self.html('<p>Доставка 40 руб.</p>'));self.assertIsNotNone(proof);self.assertIsNotNone(retained)
   owned=retained[1];self.assertEqual(owned['factory_reference'],legacy['factory_reference']);self.assertEqual(owned['listed_price'],100);self.assertEqual(owned['sale_unit'],'уп');self.assertEqual(owned['purchase_contract']['raw_amount'],proof['raw_amount']);self.assertEqual(owned['purchase_contract']['monetary_roles'],proof['monetary_roles']);self.assertIn('Доставка 40',owned['text']);self.assertEqual(owned['pack_counts'],{'count_per_pack':'1','pack_size':'1'});lookup.assert_called_with('ACME','ABC123')
 def test_missing_reference_or_control_never_mints_equivalence(self):
  with patch.object(factory,'lookup_reference',return_value=None):
   proof,retained=self.read(self.html('<p>Доставка 40 руб.</p>'));self.assertIsNotNone(proof);self.assertNotIn('factory_reference',retained[1])
  with patch.object(factory,'lookup_reference',return_value={'ref':'reviewed-fixture'}):
   for changed in [self.html().replace('data-pack-size="1"','data-pack-size="2"'),self.html().replace('data-product-id="p">В','data-product-id="foreign">В'),self.html().replace('<span>ACME</span>','<span>OTHER</span>'),self.html().replace('<span>ABC123</span>','<span>WRONG</span>')]:
    with self.subTest(changed=changed):
     proof,retained=self.read(changed);self.assertTrue(retained is None or 'factory_reference' not in retained[1])
 def test_money_source_and_quantity_guards_not_enrichment_authority(self):
  with patch.object(factory,'lookup_reference',return_value={'ref':'reviewed-fixture'}):
   for html in [self.html('<p>Сбор 40 руб.</p>'),self.html('<p>Доставка при условии 40 руб.</p>'),self.html(quantity=0),self.html('<p>200 руб. / уп</p>'),self.html().replace('100 руб.','0 руб.')]:self.assertIsNone(self.read(html)[1])
   proof,retained=self.read(self.html('<p>Доставка 40 руб.</p>'));source=copy.deepcopy(self.last_source);source['body_sha256']='0'*64;self.assertIsNone(dom.read_source(source,allow_rendered=True))

 def test_metadata_identity_owner_and_title_conflicts_refuse_factory_role(self):
  with patch.object(factory,'lookup_reference',return_value={'ref':'reviewed-fixture'}):
   body=self.html('<p>Доставка 40 руб.</p>')
   for changed in [body.replace('content="ABC123"','content="OTHER999"'),body.replace('content="ACME"','content="OTHER"'),body.replace('content="Part ACME ABC123"','content="Different product"'),body.replace('</h1>','</h1><h1>Different owner</h1>'),body.replace('</main>','<div itemscope itemtype="https://schema.org/Product"></div></main>')]:
    with self.subTest(changed=changed):
     proof,retained=self.read(changed);self.assertTrue(retained is None or 'factory_reference'not in retained[1])
 def test_order_quantity_is_preserved_separate_from_single_pack_controls(self):
  with patch.object(factory,'lookup_reference',return_value={'ref':'reviewed-fixture'}):
   proof,retained=self.read(self.html('<p>Доставка 40 руб.</p>',quantity=2));self.assertIsNotNone(retained);self.assertEqual(retained[1]['purchase_contract']['purchase_quantity'],2);self.assertEqual(retained[1]['purchase_contract']['price_denominator'],1);self.assertEqual(retained[1]['pack_counts'],{'count_per_pack':'1','pack_size':'1'})

del _Fixture
