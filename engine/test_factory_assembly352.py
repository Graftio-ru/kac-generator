"""Portable complete-product membership claims; no domain/article exceptions."""
import hashlib,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from engine.search import factory_reference as factory

CLAIM='В состав комплектного изделия входят соединительные пластины и крепежные элементы'
class CompleteAssembly352Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.scope=patch.object(factory,'CACHE_DIR',Path(self.temp.name));self.scope.start();self.addCleanup(self.scope.stop)
  factory.approve_origin('EXAMPLE','https://factory.example','test352-origin')
 def body(self,claim=CLAIM,article='ZX42',url='https://factory.example/item/zx42',extra=''):
  title='Угол внешний 100/50 в комплекте';description=claim+'.'
  product={'@type':'Product','sku':article,'name':title,'url':url,'description':description}
  return (f'<main><section><h1>{title}</h1><div>Код<br>{article}</div><div>В упаковке<br>1 уп</div></section><p>{description}</p>{extra}<script type="application/ld+json">'+json.dumps(product,ensure_ascii=False)+'</script></main>').encode()
 def reference(self,body=None):
  now=time.time();return factory.import_reference(body or self.body(),url='https://factory.example/item/zx42',manufacturer='EXAMPLE',article='ZX42',observed_at=now,valid_until=now+3600)
 def test_native_import_review_and_reopen(self):
  ref=self.reference();binding=factory.review_bindings(ref)
  self.assertEqual(binding['excerpts']['assembly']['text'],CLAIM)
  self.assertEqual(factory.read_pending(ref)['article'],'ZX42')
  with self.assertRaisesRegex(factory.FactoryEvidenceError,'not_reviewed'):factory.read_reference(ref)
 def test_closed_claim_class_variations(self):
  for noun in ['изделия','аксессуара','набора']:
   for tail in ['соединительные пластины и крепежные элементы','детали, необходимые для качественного и надежного монтажа: соединительные пластины и крепежные элементы','все необходимое для надежного монтажа: необходимые крепежные элементы и соединительные пластины']:
    self.assertTrue(factory._complete_assembly_assertion('В состав комплектного '+noun+' входят '+tail))
 def test_negative_optional_partial_foreign_and_trailing_claims(self):
  for text in [CLAIM+' при дополнительном заказе',CLAIM+' без основного изделия',CLAIM.replace('входят','не входят'),CLAIM.replace('комплектного','неполного'),CLAIM.replace('комплектного','другого'),CLAIM.replace('входят','могут входить'),CLAIM.replace('соединительные пластины и ','только '),'В состав системы входят соединительные пластины и крепежные элементы']:
   with self.subTest(text=text):
    ref=self.reference(self.body(text))
    with self.assertRaises(factory.FactoryEvidenceError):factory.review_bindings(ref)
 def test_full_context_rejects_negative_prefix_and_suffix(self):
  for extra in ['Без самого изделия','Только крепления','Изделие отсутствует','Соединительные пластины отсутствуют','Упаковка содержит 2 комплекта','Основное изделие поставляется отдельно']:
   for text in [extra+'\n'+CLAIM,CLAIM+'\n'+extra]:
    with self.subTest(text=text),self.assertRaises(factory.FactoryEvidenceError):factory._assembly_claims(text,'T',CLAIM,prose_blocks=[extra,CLAIM])
 def test_binding_tamper_wrong_identity_and_expiry(self):
  ref=self.reference();root=factory._root();body=root/(ref['body_sha256']+'.html');raw=body.read_bytes();body.write_bytes(raw+b'changed')
  with self.assertRaisesRegex(factory.FactoryEvidenceError,'body_changed'):factory.review_bindings(ref)
  body.write_bytes(raw)
  with patch.object(time,'time',return_value=time.time()+4000),self.assertRaisesRegex(factory.FactoryEvidenceError,'expired'):factory.review_bindings(ref)
  with self.assertRaises(factory.FactoryEvidenceError):self.reference(self.body(article='OTHER'))

 def test_coherent_full_description_primary_acquisition_before_after(self):
  claims=['Основное изделие поставляется отдельно','Основное изделие продаётся отдельно','Основной товар приобретается отдельно','Для заказа основного изделия требуется отдельная покупка','Отдельно поставляется основное изделие','Само изделие отсутствует']
  for extra in claims:
   for description in [extra+'. '+CLAIM,CLAIM+'. '+extra]:
    with self.subTest(description=description):
     ref=self.reference(self.body(description))
     self.assertTrue(factory.read_pending(ref))
     with self.assertRaises(factory.FactoryEvidenceError):factory.review_bindings(ref)
     with self.assertRaises(factory.FactoryEvidenceError):factory._assembly_claims(description,'T',description,prose_blocks=[description])

 def test_taxonomy_relative_clause_is_not_primary_disposition(self):
  self.assertFalse(factory._primary_acquisition_claim('Конструкции и детали конструкций из черных металлов прочие, не включенные в другие группировки'))
  self.assertTrue(factory._primary_acquisition_claim('Основное изделие не включено'))

 def test_compound_disposition_cannot_inherit_membership_authority(self):
  legacy='В комплект входят соединительные пластины и крепежные элементы'
  for positive in [legacy,CLAIM]:
   for disposition in ['Основное изделие поставляется отдельно','Основное изделие продаётся отдельно','Основной товар приобретается отдельно']:
    for text in [positive+', а '+disposition,disposition+', а '+positive]:
     with self.subTest(text=text):
      ref=self.reference(self.body(text));self.assertTrue(factory.read_pending(ref))
      with self.assertRaises(factory.FactoryEvidenceError):factory.review_bindings(ref)
      with self.assertRaises(factory.FactoryEvidenceError):factory._assembly_claims(text,'T',text,prose_blocks=[text])
