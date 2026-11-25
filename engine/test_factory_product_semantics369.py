"""Bounded semantic whole-product authority; unknown meaning remains unknown."""
import copy,unittest
from unittest.mock import patch
from engine.test_factory_package_inference366 import FactoryPackageInference366Tests as _Fixture
from engine.search import factory_reference as factory

class FactoryProductSemantics369Tests(unittest.TestCase):
 setUp=_Fixture.setUp
 proof=_Fixture.proof
 nested=_Fixture.nested
 read=_Fixture.read
 html=_Fixture.html
 legacy_html=_Fixture.legacy_html
 authority=_Fixture.authority
 technical=_Fixture.technical
 body=_Fixture.body
 fixture=_Fixture.fixture
 def semantic_fixture(self):
  body,technical,position=self.fixture();title=technical['facts']['title']+' DKC ABC123';short='ABC123, Угол CD 90 вертикальный внешний 90 градусов 300х50 в комплекте крепежные элементы'
  body=body.replace(title,short).replace(technical['facts']['description'],'В комплекте крепежные элементы')
  return body,technical,position
 def test_native_short_partial_title_quote_append_and_reopen(self):
  from engine.search.literal_card_quote import current_literal_quote
  from engine.search.product_identity import rendered_identity
  from engine.search.catalogue import CatalogueStore,ProductIdentity,SourceObservation,ImageEvidence
  body,technical,position=self.semantic_fixture()
  for maker,code in [('DKC','ABC123'),('ACME','ZZ999')]:
   changed=copy.deepcopy(technical);changed['manufacturer']=maker;changed['article']=code;pos=copy.deepcopy(position);pos['type_mark']=code;pos['source_evidence']['cells']['article']=code;pos['source_evidence']['cells']['maker']=maker
   with self.authority(changed),factory.request_scope(pos):
    source,retained=self.read(body.replace('DKC',maker).replace('ДКС',maker).replace('ABC123',code));self.assertIsNotNone(retained);owned=retained[1];proof=factory.derive_singleton(source['source'],pos);self.assertEqual(proof['version'],3);self.assertEqual(proof['whole_product_witness']['official_only_components'],['connection_plates']);self.assertEqual(factory.verify_proof(proof,pos),proof)
    url=source['url'];identity=rendered_identity(url,url,source['title'],code,code,pos['name'],'merchant.example',observed_at=source['observed_at']);identity['rendered_source']=source['source'];facts={'rendered_source':source['source'],'identity_evidence':identity,'expected_type_mark':code,'expected_name':pos['name'],'expected_unit':'компл','url':url,'captured_at':source['observed_at'],'main_title':source['title'],'text':owned['text']};self.assertIsNotNone(current_literal_quote(facts))
    ident=ProductIdentity('article',code,maker);oid=('a'if maker=='DKC'else'b')*32;row=SourceObservation(oid,ident,'merchant.example',url,url,source['observed_at'],'supplier_rendered',source['title'],code,owned['text'],proof['source_sha256'],100,'RUB','unknown','уп',None,1,'','unknown','observed','','',ImageEvidence(oid,url,source['observed_at'],'missing',error='fixture_no_photo'),'fixture369',unit_equivalence=proof);path=self.root/(maker+'.sqlite3');store=CatalogueStore(path);store.acquire_unknown(ident,['merchant.example'],owner='test369');self.assertTrue(store.append_observation(ident.key,row));saved=CatalogueStore(path).lookup(ident)['heads'][0]['current']['unit_equivalence'];self.assertEqual(factory.verify_proof(saved,pos),proof)
 def test_plate_count_omission_is_official_only_not_merchant_claim(self):
  body,technical,position=self.semantic_fixture();old='ABC123, Угол CD 90 вертикальный внешний 90 градусов 300х50 в комплекте крепежные элементы';short='ABC123, Пластина крепежная GSV H50 в комплекте с метизами';body=body.replace(old,short);technical['facts']['title']='Пластина крепежная GSV H50 (4 шт.) в комплекте с метизами';technical['facts']['description']='В комплект входят 4 пластины и крепежные элементы.';technical['facts']['text']=technical['facts']['description'];position['name']=technical['facts']['title'];position['source_evidence']['cells']['name']=position['name']
  with self.authority(technical),factory.request_scope(position):
   source,_=self.read(body);proof=factory.derive_singleton(source['source'],position);self.assertEqual(proof['whole_product_witness']['field_provenance']['component_count'],'official_only');self.assertIsNone(proof['whole_product_witness']['supplier_fields']['component_count']);self.assertEqual(proof['whole_product_witness']['official_fields']['component_count'],4)
   self.read(body.replace('GSV H50','GSV H50 (2 шт.)'))
   with self.assertRaises(factory.FactoryEvidenceError):factory.derive_singleton(self.last_source,position)
 def test_wrong_engineering_near_identity_and_unknown_suffix_deny(self):
  body,technical,position=self.semantic_fixture()
  with self.authority(technical):
   for old,new in [('300х50','200х50'),('90 градусов','45 градусов'),('вертикальный','горизонтальный'),('внешний','внутренний'),('CD 90','CPO 90'),('Угол CD','Пластина CD'),('ABC123,','ABC124,'),('в комплекте крепежные элементы','в комплекте крепежные элементы неизвестного назначения')]:
    self.read(body.replace(old,new))
    with self.assertRaises(factory.FactoryEvidenceError):factory.derive_singleton(self.last_source,position)
 def test_explicit_source_counts_coating_repack_and_unknown_claims_deny(self):
  body,technical,position=self.semantic_fixture()
  variants=[body.replace('300х50','300х50 (2 шт.)'),body.replace('300х50','300х50, горячий цинк'),body.replace('В комплекте крепежные элементы','Основное изделие продаётся отдельно.'),body.replace('В комплекте крепежные элементы','Комплект разделён на отдельные части.'),body.replace('В комплекте крепежные элементы','Неизвестный способ применения.'),body.replace('В комплекте крепежные элементы','В комплекте только крепежные элементы')]
  with self.authority(technical):
   for changed in variants:
    self.read(changed)
    with self.assertRaises(factory.FactoryEvidenceError):factory.derive_singleton(self.last_source,position)
 def test_typed_dimension_and_semantic_witness_mutation_deny(self):
  body,technical,position=self.semantic_fixture()
  with self.authority(technical):
   source,_=self.read(body);proof=factory.derive_singleton(source['source'],position);changed=copy.deepcopy(proof);changed['whole_product_witness']['official_only_components']=[]
   with self.assertRaises(factory.FactoryEvidenceError):factory.verify_proof(changed,position)
   self.read(body.replace('</article>','<div><span>Ширина, мм</span><span>200</span></div></article>'))
   with self.assertRaises(factory.FactoryEvidenceError):factory.derive_singleton(self.last_source,position)

 def test_every_engineering_and_package_feature_is_compared(self):
  body,technical,position=self.semantic_fixture();technical['facts']['features'] += [{'label':'Толщина материала, мм','value':'0.8','label_path':[1],'value_path':[2],'direct_value':False},{'label':'Ширина, мм','value':'300','label_path':[3],'value_path':[4]}]
  with self.authority(technical):
   for label,value in [('Толщина материала, мм','1.5'),('В упаковке','2 уп.'),('Ширина, мм','200'),('Неизвестное свойство','1'),('Толщина материала, мм','0.8 без крепежа'),('В упаковке','1 шт.')]:
    self.read(body.replace('</article>','<div><span>'+label+'</span><span>'+value+'</span></div></article>'))
    with self.assertRaises(factory.FactoryEvidenceError):factory.derive_singleton(self.last_source,position)
   source,_=self.read(body.replace('</article>','<div><span>Толщина материала, см</span><span>0.08</span></div><div><span>В упаковке</span><span>1 уп.</span></div></article>'))
   proof=factory.derive_singleton(source['source'],position);self.assertTrue(proof['whole_product_witness']['feature_accounting']);self.assertEqual(factory.verify_proof(proof,position),proof)

del _Fixture
