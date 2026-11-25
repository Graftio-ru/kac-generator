"""Explicit delivery has a local money witness, never parent-wide immunity."""
from engine.test_selling_package355 import SellingPackage355Tests as _PackageFixture
from engine.search import http_card as dom,owned_purchase_row as rows

class DeliveryMoney356Tests(_PackageFixture):
 def source(self,extra,quotes='<p>100 руб. / уп</p>'):
  return self.proof(self.html(quotes,extra=extra))
 def test_explicit_delivery_local_money_role_preserves_price_and_fulltext(self):
  for label in ['Стоимость доставки','Доставка','Delivery','Shipping']:
   proof=self.source('<p>'+label+' 40 руб.</p>');self.assertIsNotNone(proof);self.assertEqual(proof['raw_amount'],100)
   role=next(v for v in proof['monetary_roles']if v['role']=='delivery');self.assertEqual(role['amount'],'40');self.assertNotEqual(role['path'],proof['owner_path']);self.assertNotEqual(role['role_path'],proof['owner_path'])
   retained=dom.read_source(proof['source'],allow_rendered=True);self.assertIn(label,retained[1]['text']);self.assertIn('40',retained[1]['text'])
 def test_courier_table_requires_separate_delivery_context(self):
  row='<tr><td>Курьер</td><td>29 октября<sup>1</sup></td><td><span>40 руб.</span><sup>2</sup></td></tr>'
  proof=self.source('<section class="shipping"><table>'+row+'</table></section>');self.assertIsNotNone(proof);self.assertTrue(any(v['role']=='delivery'for v in proof['monetary_roles']))
  self.assertIsNone(self.source('<table>'+row+'</table>'))
 def test_unknown_money_in_delivery_ancestor_is_not_hidden(self):
  self.assertIsNone(self.source('<section class="delivery"><p>Доставка 40 руб.</p><p>Сбор 10 руб.</p></section>'))
  self.assertIsNone(self.source('<section class="delivery"><p>Другой товар 40 руб. / уп</p></section>'))
 def test_product_and_delivery_in_same_money_row_refuse(self):
  for row in ['Доставка 40 руб. и товар 100 руб.','Стоимость доставки 40 руб. / уп','Доставка товара артикул ABC123 40 руб.','Доставка 40 руб.<button>Купить</button>','Доставка <span itemprop="price">40</span> руб.']:
   with self.subTest(row=row):self.assertIsNone(self.source('<p>'+row+'</p>'))
 def test_required_conditional_installment_from_price_tax_currency_refuse(self):
  for text in ['Доставка обязательная 40 руб.','Доставка от 40 руб.','Доставка при заказе 40 руб.','Доставка Рассрочка 40 руб.','Доставка 40 руб. без НДС','Доставка 40 USD','Доставка -40 руб.','Доставка 40 руб. в месяц','Доставка 40 руб. при сумме заказа до 1000','Доставка 40 руб. при условии самовывоза','Доставка 40 руб. если выбран самовывоз','Доставка 40 руб. в случае самовывоза','Delivery 40 RUB if pickup selected']:
   with self.subTest(text=text):self.assertIsNone(self.source('<p>'+text+'</p>'))
  self.assertIsNone(self.source('<section class="delivery"><p>Обязательная доставка</p><p>Курьер 40 руб.</p></section>'))
  self.assertIsNone(self.source('<p data-required="true">Доставка 40 руб.</p>'))
 def test_delivery_is_not_product_quote_or_unit_authority(self):
  self.assertIsNone(self.source('<p>Доставка 40 руб.</p>',quotes=''))
  self.assertIsNone(self.source('<p>Доставка 40 руб.</p>',quotes='<p>100 руб.</p>'))
  proof=self.source('<p>Доставка 40 руб.</p>');self.assertIsNone(rows.decision(proof,dict(unit='компл'))[0])
 def test_changed_amount_role_path_and_body_rejected_per_use(self):
  proof=self.source('<p>Доставка 40 руб.</p>');self.assertIsNotNone(proof)
  import copy
  for field,value in [('amount','0'),('role','historical'),('role_path',proof['owner_path'])]:
   changed=copy.deepcopy(proof);next(v for v in changed['monetary_roles']if v['role']=='delivery')[field]=value
   self.assertIsNone(rows.decision(changed,dict(unit='уп'))[0])
  path=self.root/proof['source']['ref'];path.write_bytes(path.read_bytes()+b'changed');self.assertIsNone(dom.read_source(proof['source'],allow_rendered=True))

del _PackageFixture
