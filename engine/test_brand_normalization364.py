"""Standard brand representations share one existing alias authority."""
import unittest
from unittest.mock import patch
from engine.test_catalogue_identity361 import CatalogueIdentity361Tests as _Fixture
from engine.search import http_card as dom,factory_reference as factory

class BrandNormalization364Tests(unittest.TestCase):
 setUp=_Fixture.setUp
 proof=_Fixture.proof
 nested=_Fixture.nested
 read=_Fixture.read
 html=_Fixture.html
 legacy_html=_Fixture.legacy_html
 authority=_Fixture.authority
 technical=_Fixture.technical
 def body(self,brand='ДКС',printed='DKC',label='Бренд'):
  return self.html(maker='DKC').replace('<span itemprop="brand" itemscope itemtype="https://schema.org/Brand"><meta itemprop="name" content="DKC"></span>','<span itemprop="brand">'+brand+'</span>').replace('<span>Производитель</span> <span>DKC</span>','<span>'+label+'</span> <span>'+printed+'</span>')
 def test_scalar_nested_and_printed_labels_share_canonical_maker(self):
  for label in ['Бренд','Производитель','Manufacturer','Brand']:
   for brand in ['DKC','ДКС','DKC (ДКС)']:
    with self.subTest(label=label,brand=brand),self.authority(self.technical(maker='DKC')):
     proof,retained=self.read(self.body(brand=brand,label=label));self.assertIsNotNone(retained);self.assertEqual(retained[1]['manufacturer'],'DKC');self.assertEqual(retained[1]['catalogue_correlated_identity']['manufacturer'],'DKC');self.assertEqual(retained[1]['purchase_contract']['raw_amount'],100)
  with self.authority(self.technical(maker='DKC')):
   self.assertEqual(self.read(self.html(maker='DKC'))[1][1]['manufacturer'],'DKC')
 def test_conflicting_scalar_nested_printed_and_foreign_maker_refuse(self):
  base=self.body()
  variants=[self.body(brand='OTHER'),self.body(printed='OTHER'),base.replace('<span itemprop="brand">ДКС</span>','<span itemprop="brand">ДКС</span><span itemprop="brand">OTHER</span>'),base.replace('<span itemprop="brand">ДКС</span>','<span itemprop="brand" itemscope itemtype="https://schema.org/Brand"><meta itemprop="name" content="DKC"><meta itemprop="name" content="OTHER"></span>'),base.replace('<span itemprop="brand">ДКС</span>','<span itemprop="brand" content="OTHER">ДКС</span>'),base.replace('<span itemprop="brand">ДКС</span>','<div itemscope itemtype="https://schema.org/Product"><span itemprop="brand">ДКС</span></div>')]
  with self.authority(self.technical(maker='DKC')):
   for body in variants:
    with self.subTest(body=body):
     _,retained=self.read(body);self.assertTrue(retained is None or 'factory_reference'not in retained[1])
 def test_unknown_names_never_map_to_unrelated_official_manufacturer(self):
  for brand in ['DKCS','ДКЦ','Unknown Maker','ДКС OTHER']:
   with self.authority(self.technical(maker='DKC')):
    _,retained=self.read(self.body(brand=brand,printed=brand));self.assertTrue(retained is None or 'factory_reference'not in retained[1])
 def test_missing_or_unowned_metadata_never_borrows_printed_brand(self):
  with self.authority(self.technical(maker='DKC')):
   for body in [self.body().replace('<span itemprop="brand">ДКС</span>',''),self.body().replace('<span itemprop="brand">ДКС</span>','<meta itemprop="brand" content="DKC">'),self.body().replace('<span itemprop="brand">ДКС</span>','<span itemprop="brand"></span>')]:
    _,retained=self.read(body);self.assertTrue(retained is None or 'factory_reference'not in retained[1])
 def test_native_consumer_existing_aliases_and_bound_brand_prose(self):
  description='Сборочная единица предназначена для изменения направления кабельной трассы. В комплект входят крепежные элементы и соединительные пластины.'
  title='Угол CD 90 вертикальный внешний 90° 300/50 в комплекте с крепежными элементами и соединительными пластинами, цинк-ламельный'
  body=self.body().replace('Part DKC ABC123',title+' DKC ABC123').replace('В комплект входят пластины и крепежные элементы.',description)
  technical=self.technical(maker='DKC');technical['facts']={'title':title,'description':description,'text':description,'scope_sha256':'b'*64}
  position={'name':title,'type_mark':'ABC123','unit':'компл','source_evidence':{'reader':'native_pdf','cells':{'name':title,'article':'ABC123','maker':'ДКС'}}}
  with self.authority(technical):
   proof,retained=self.read(body);self.assertIsNotNone(retained);derived=factory.derive_singleton(proof['source'],position);self.assertEqual(derived['listed_price'],100);self.assertEqual(factory.verify_proof(derived,position),derived)
   wrong={**position,'source_evidence':{'reader':'native_pdf','cells':{'name':title,'article':'ABC123','maker':'OTHER'}}}
   with self.assertRaises(factory.FactoryEvidenceError):factory.derive_singleton(proof['source'],wrong)

del _Fixture
