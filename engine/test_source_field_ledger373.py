"""Source leaf ownership, never shared-value or ancestor neutrality."""
import unittest,copy
from unittest.mock import patch
from engine.search import factory_reference as factory
from engine.test_factory_product_semantics369 import FactoryProductSemantics369Tests as _Fixture

class SourceFieldLedger373Tests(unittest.TestCase):
 setUp=_Fixture.setUp
 read=_Fixture.read
 authority=_Fixture.authority
 semantic_fixture=_Fixture.semantic_fixture
 fixture=_Fixture.fixture
 body=_Fixture.body
 html=_Fixture.html
 legacy_html=_Fixture.legacy_html
 nested=_Fixture.nested
 technical=_Fixture.technical
 proof=_Fixture.proof
 def rich(self):
  body,technical,position=self.semantic_fixture()
  body=body.replace('<span itemprop="sku">8000939363</span>','<div><span>Номенклатурный номер</span><span itemprop="sku">8000939363</span></div>')
  body=body.replace('<p>100 руб. / уп</p>','<div><span>100</span><span> руб. / уп</span></div>')
  body=body.replace('</article>','<div><a href="#description">Описание</a><a href="#reviews">Отзывы</a></div></article>')
  return body,technical,position
 def test_rich_source_quote_append_cold_reopen_two_makers(self):
  from engine.test_factory_product_semantics369 import FactoryProductSemantics369Tests as Base
  original=Base.semantic_fixture
  def rich(obj):
   body,technical,position=original(obj)
   body=body.replace('<span itemprop="sku">8000939363</span>','<div><span>Номенклатурный номер</span><span itemprop="sku">8000939363</span></div>').replace('<p>100 руб. / уп</p>','<div><span>100</span><span> руб. / уп</span></div>').replace('</article>','<div><a href="#description">Описание</a><a href="#reviews">Отзывы</a></div></article>')
   return body,technical,position
  case=Base('test_native_short_partial_title_quote_append_and_reopen');case.setUp()
  try:
   with patch.object(Base,'semantic_fixture',rich):case.test_native_short_partial_title_quote_append_and_reopen()
  finally:case.doCleanups()
 def test_exact_source_roles_and_reopen_witness_tamper(self):
  body,technical,position=self.rich()
  with self.authority(technical):
   source,retained=self.read(body);self.assertIsNotNone(retained);proof=factory.derive_singleton(source['source'],position);ledger=proof['whole_product_witness']['field_roles'];roles={e['role'] for e in ledger['entries']};self.assertIn('seller_sku',roles);self.assertIn('navigation',roles);self.assertIn('current_price',roles)
   changed=copy.deepcopy(proof);changed['whole_product_witness']['field_roles']['entries'][0]['role']='navigation'
   with self.assertRaises(factory.FactoryEvidenceError):factory.verify_proof(changed,position)
 def test_unknown_engineering_shared_sku_and_ui_negative_are_not_neutral(self):
  body,technical,position=self.rich()
  cases=[body.replace('itemprop="sku"','data-unknown="sku"'),body.replace('Номенклатурный номер','Толщина материала'),body.replace('>Отзывы<','>Отзывы без крепежа<'),body.replace('</article>','<div><span>Толщина материала</span><span>8000939363</span></div></article>'),body.replace('</article>','<div><a href="#description">Толщина материала</a><a href="#reviews">1.5 мм</a></div></article>'),body.replace('</article>','<div><span>В упаковке</span><span>2 уп.</span></div></article>')]
  with self.authority(technical):
   for changed in cases:
    source,retained=self.read(changed)
    with self.assertRaises(factory.FactoryEvidenceError):factory.derive_singleton(source['source'],position)
 def test_unknown_mixed_ancestor_and_incomplete_compatibility_fail(self):
  body,technical,position=self.rich()
  cases=[body.replace('</article>','<div><span>Описание</span><span>Основное изделие поставляется отдельно.</span></div></article>'),body.replace('В комплекте крепежные элементы','В комплекте крепежные элементы совместим с'),body.replace('Номенклатурный номер','Номенклатурный номер неизвестное ограничение'),body.replace('<div><a href="#description">Описание', '<div>без крепежа<a href="#description">Описание')]
  with self.authority(technical):
   for changed in cases:
    source,retained=self.read(changed)
    with self.assertRaises(factory.FactoryEvidenceError):factory.derive_singleton(source['source'],position)

 def test_stock_is_source_role_not_package_cardinality_and_legacy_shape_readable(self):
  body,technical,position=self.rich();body=body.replace('</article>','<div><span>Остаток</span><span itemprop="inventoryLevel">876 уп.</span></div></article>')
  with self.authority(technical):
   source,retained=self.read(body);proof=factory.derive_singleton(source['source'],position);self.assertEqual(proof['quantity'],1);self.assertTrue(any(e['role']=='stock'for e in proof['whole_product_witness']['field_roles']['entries']))
   old=copy.deepcopy(proof);old['whole_product_witness']['version']=1;old['whole_product_witness'].pop('field_roles')
   observation={'url':proof['supplier_url'],'observed_at':proof['observed_at'],'source_sha256':proof['source_sha256'],'raw_amount':proof['listed_price'],'identity':{'kind':'article','manufacturer':proof['request']['manufacturer'],'code':proof['request']['article']}}
   self.assertEqual(factory.validate_observation_proof(old,observation),old)
   with self.assertRaises(factory.FactoryEvidenceError):factory.verify_proof(old,position)


 def test_official_selector_or_conflicting_multiple_values_never_donate_scalar(self):
  body,technical,position=self.rich();body=body.replace('</article>','<div><span>Ширина, мм</span><span>300</span></div></article>')
  for values in [('200','300'),('200 мм 300 мм','300')]:
   changed=copy.deepcopy(technical);changed['facts']['features'] += [{'label':'Ширина, мм','value':v,'label_path':[11,i],'value_path':[12,i],'direct_value':False}for i,v in enumerate(values)]
   with self.authority(changed):
    source,_=self.read(body)
    with self.assertRaises(factory.FactoryEvidenceError):factory.derive_singleton(source['source'],position)


del _Fixture
