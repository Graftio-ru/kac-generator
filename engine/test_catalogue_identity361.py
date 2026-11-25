"""Catalogue correlation is distinct authority, never an invented MPN/SKU."""
import copy,unittest
from unittest.mock import patch
from contextlib import ExitStack
from engine.test_factory_parity358 import FactoryParity358Tests as _Fixture
from engine.search import http_card as dom,factory_reference as factory

class CatalogueIdentity361Tests(unittest.TestCase):
 setUp=_Fixture.setUp
 proof=_Fixture.proof
 nested=_Fixture.nested
 legacy_html=_Fixture.html
 read=_Fixture.read
 def html(self,extra='',quantity=1,code='ABC123',maker='ACME'):
  s=self.legacy_html(extra,quantity).replace('<meta itemprop="mpn" content="ABC123">','').replace('Артикул производителя</span> <span>ABC123','Артикул</span> <span itemprop="model">ABC123')
  return s.replace('ABC123',code).replace('ACME',maker).replace('<p>100 руб. / уп</p>','<span itemprop="sku">8000939363</span><p>100 руб. / уп</p>')
 def technical(self,code='ABC123',maker='ACME'):
  return {'article':code,'manufacturer':maker,'review':{'receipt_sha256':'a'*64,'revision':1,'provenance':{'fixture':'synthetic-reviewed'}}}
 def authority(self,technical=None):
  scope=ExitStack();scope.enter_context(patch.object(factory,'lookup_reference',return_value={'ref':'approved-official'}));scope.enter_context(patch.object(factory,'read_reference',return_value=technical or self.technical()));return scope
 def test_owned_model_and_separate_sku_need_exact_official_correlation(self):
  for code,maker in [('30013K','DKC'),('XYZ456','ACME')]:
   with self.subTest(code=code),self.authority(self.technical(code,maker)):
    proof,retained=self.read(self.html(code=code,maker=maker));self.assertIsNotNone(retained);owned=retained[1]
    self.assertEqual(owned['identity_basis'],'catalogue_correlated_manufacturer_code');self.assertEqual(owned['catalogue_correlated_identity']['article'],code);self.assertEqual(owned['purchase_contract']['raw_amount'],100)
    self.assertTrue(any(v['role']=='seller_sku'and v['value']=='8000939363'for v in owned['code_observations']));self.assertFalse(any(v['role']=='manufacturer_code'for v in owned['code_observations']))
    structural=dom._neutral_owner(self.nested(self.html(code=code,maker=maker)),'https://merchant.example/product/item');self.assertNotEqual(structural['identity_basis'],'catalogue_correlated_manufacturer_code')
 def test_missing_expired_unapproved_or_wrong_catalogue_never_binds(self):
  for technical in [self.technical('OTHER'),self.technical(maker='OTHER')]:
   with self.authority(technical):self.assertNotIn('factory_reference',self.read(self.html())[1][1])
  with patch.object(factory,'lookup_reference',return_value=None):self.assertNotIn('factory_reference',self.read(self.html())[1][1])
  for code in ['factory_reference_expired','factory_reference_not_reviewed','factory_origin_inactive']:
   with self.authority(),patch.object(factory,'read_reference',side_effect=factory.FactoryEvidenceError(code)):
    self.assertNotIn('factory_reference',self.read(self.html())[1][1])
 def test_explicit_mpn_model_brand_and_owner_contradictions_never_fallback(self):
  body=self.html()
  variants=[body.replace('<h1>','<meta itemprop="mpn" content="WRONG999"><h1>'),body.replace('itemprop="model">ABC123','itemprop="model">OTHER999'),body.replace('content="ACME"','content="OTHER"'),body.replace('<span>ACME</span>','<span>OTHER</span>'),body.replace('</h1>','</h1><h1>Other owner</h1>'),body.replace('</main>','<div itemscope itemtype="https://schema.org/Product"></div></main>')]
  with self.authority():
   for changed in variants:
    with self.subTest(changed=changed):
     _,retained=self.read(changed);self.assertTrue(retained is None or 'factory_reference'not in retained[1])
 def test_sku_only_or_unprinted_model_has_no_manufacturer_role(self):
  with self.authority():
   for changed in [self.html().replace('<div><span>Артикул</span> <span itemprop="model">ABC123</span></div>',''),self.html().replace('Артикул</span>','SKU</span>'),self.html().replace('itemprop="model"','itemprop="sku"').replace('Артикул</span>','Внутренний код</span>')]:
    _,retained=self.read(changed);self.assertTrue(retained is None or 'factory_reference'not in retained[1])
 def test_price_control_quantity_and_source_guards_remain(self):
  with self.authority():
   for changed in [self.html().replace('data-pack-size="1"','data-pack-size="2"'),self.html().replace('data-product-id="p">В','data-product-id="other">В'),self.html('<p>Сбор 40 руб.</p>'),self.html(quantity=0)]:
    _,retained=self.read(changed);self.assertTrue(retained is None or 'factory_reference'not in retained[1])
   proof,retained=self.read(self.html(quantity=2));self.assertEqual(retained[1]['purchase_contract']['purchase_quantity'],2);source=copy.deepcopy(self.last_source);source['record_sha256']='0'*64;self.assertIsNone(dom.read_source(source,allow_rendered=True))
 def test_legacy_explicit_mpn_path_unchanged(self):
  with self.authority():
   body=self.legacy_html().replace('<p>100 руб. / уп</p>','<div>100 руб.</div><div>Единица измерения упак</div>');owned=dom.main_product(body,'https://merchant.example/product/item');self.assertEqual(owned['identity_basis'],'manufacturer_code');self.assertNotIn('catalogue_correlated_identity',owned)

 def test_native_factory_consumer_rederives_correlated_identity(self):
  description='Сборочная единица предназначена для изменения направления кабельной трассы. В комплект входят крепежные элементы и соединительные пластины.'
  title='Угол CD 90 вертикальный внешний 90° 300/50 в комплекте с крепежными элементами и соединительными пластинами, цинк-ламельный'
  body=self.html().replace('Part ACME ABC123',title+' ACME ABC123').replace('В комплект входят пластины и крепежные элементы.',description)
  technical=self.technical();technical['facts']={'title':title,'description':description,'text':description,'scope_sha256':'b'*64}
  position={'name':title,'type_mark':'ABC123','unit':'компл','source_evidence':{'reader':'native_pdf','cells':{'name':title,'article':'ABC123','maker':'ACME'}}}
  with self.authority(technical):
   proof,retained=self.read(body);self.assertIsNotNone(retained)
   derived=factory.derive_singleton(proof['source'],position);self.assertEqual(derived['listed_price'],100);self.assertEqual(derived['quantity'],1);self.assertEqual(factory.verify_proof(derived,position),derived)
   for extra in ['<p>Неизвестное техническое ограничение</p>','<p>Основное изделие поставляется отдельно.</p>']:
    self.read(body.replace('</article>',extra+'</article>'))
    with self.assertRaises(factory.FactoryEvidenceError):factory.derive_singleton(self.last_source,position)
   for extra in [' без крепежа',' неизвестное ограничение совместимости']:
    self.read(body.replace('100 руб. / уп','100 руб. / уп'+extra))
    with self.assertRaises(factory.FactoryEvidenceError):factory.derive_singleton(self.last_source,position)
   changed=copy.deepcopy(technical);changed['review']['revision']=2
   with patch.object(factory,'read_reference',return_value=changed),self.assertRaises(factory.FactoryEvidenceError):factory.verify_proof(derived,position)
   path=self.root/proof['source']['ref'];path.write_bytes(path.read_bytes()+b'changed')
   with self.assertRaises(factory.FactoryEvidenceError):factory.verify_proof(derived,position)

 def test_both_changed_modules_change_both_native_policies(self):
  from engine.test_factory_policy_closure353 import FactoryPolicyClosure353Tests
  from engine.search import acquisition_source_intent as intent
  from engine.search.document_context import current_document_policy
  fixture=FactoryPolicyClosure353Tests();fixture.setUp()
  try:
   for name in ['search/http_card.py','search/factory_reference.py']:
    old=intent.attest(fixture.runtime,intent.components(fixture.root));policy=current_document_policy(engine_root=fixture.root,supplier_file=fixture.suppliers);path=fixture.root/name;self.assertIn(str(path),policy);path.write_bytes(path.read_bytes()+b'\n# inert361mutation\n')
    self.assertNotEqual(old['sha256'],intent.attest(fixture.runtime,intent.components(fixture.root))['sha256']);self.assertNotEqual(policy,current_document_policy(engine_root=fixture.root,supplier_file=fixture.suppliers))
  finally:fixture.doCleanups()

# The fixture's inherited methods are aliases, not inherited test cases.
del _Fixture
