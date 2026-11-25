"""Explicit human-approved cross-source premise, not a package-unit alias."""
import copy,json,unittest
from unittest.mock import patch
from engine.test_brand_normalization364 import BrandNormalization364Tests as _Fixture
from engine.search import http_card as dom,factory_reference as factory

class FactoryPackageInference366Tests(unittest.TestCase):
 setUp=_Fixture.setUp
 proof=_Fixture.proof
 nested=_Fixture.nested
 read=_Fixture.read
 html=_Fixture.html
 legacy_html=_Fixture.legacy_html
 authority=_Fixture.authority
 technical=_Fixture.technical
 body=_Fixture.body
 def fixture(self):
  description='Сборочная единица предназначена для изменения направления кабельной трассы. В комплект входят крепежные элементы и соединительные пластины.'
  title='Угол CD 90 вертикальный внешний 90° 300/50 в комплекте с крепежными элементами и соединительными пластинами, цинк-ламельный'
  body=self.body().replace('Part DKC ABC123',title+' DKC ABC123').replace('В комплект входят пластины и крепежные элементы.',description).replace(' data-count-per-pack="1" data-pack-size="1"','')
  technical=self.technical(maker='DKC');technical['facts']={'title':title,'description':description,'text':description,'scope_sha256':'b'*64,'features':[{'label':'В упаковке:','value':'1 уп.','label_path':[1],'value_path':[2]}]}
  position={'name':title,'type_mark':'ABC123','unit':'компл','source_evidence':{'reader':'native_pdf','cells':{'name':title,'article':'ABC123','maker':'DKC'}}}
  return body,technical,position
 def test_native_read_derive_quote_verify_and_durable_proof_reopen(self):
  from engine.search.literal_card_quote import current_literal_quote
  from engine.search.product_identity import rendered_identity
  body,technical,position=self.fixture()
  with self.authority(technical),factory.request_scope(position):
   proof,retained=self.read(body);self.assertIsNotNone(retained);owned=retained[1];self.assertNotIn('pack_counts',owned)
   derived=factory.derive_singleton(proof['source'],position);self.assertEqual(derived['version'],2);self.assertEqual(derived['package_witness']['role'],'cross_source_factory_package');self.assertEqual(factory.verify_proof(derived,position),derived)
   source=proof['source'];url=proof['url'];identity=rendered_identity(url,url,proof['title'],proof['article'],'ABC123',position['name'],'merchant.example',observed_at=proof['observed_at']);identity['rendered_source']=source
   facts={'rendered_source':source,'identity_evidence':identity,'expected_type_mark':'ABC123','expected_name':position['name'],'expected_unit':'компл','url':url,'captured_at':proof['observed_at'],'main_title':proof['title'],'text':owned['text']}
   quote=current_literal_quote(facts);self.assertIsNotNone(quote);self.assertEqual(quote['factory_unit_proof'],derived)
   observation={'url':url,'observed_at':proof['observed_at'],'source_sha256':derived['source_sha256'],'raw_amount':100,'identity':{'kind':'article','manufacturer':'DKC','code':'ABC123'}}
   factory.validate_observation_proof(derived,observation)
   from engine.search.catalogue import CatalogueStore,ProductIdentity,SourceObservation,ImageEvidence
   identity=ProductIdentity('article','ABC123','DKC');oid='3'*32
   row=SourceObservation(oid,identity,'merchant.example',url,url,proof['observed_at'],'supplier_rendered',proof['title'],'ABC123',owned['text'],derived['source_sha256'],100,'RUB','unknown','уп',None,1,'','unknown','observed','','',ImageEvidence(oid,url,proof['observed_at'],'missing',error='fixture_no_photo'),'fixture366',unit_equivalence=derived)
   path=self.root/'catalogue.sqlite3';store=CatalogueStore(path);store.acquire_unknown(identity,['merchant.example'],owner='test366');self.assertTrue(store.append_observation(identity.key,row))
   reopened=CatalogueStore(path).lookup(identity)['heads'][0]['current']['unit_equivalence'];self.assertEqual(factory.verify_proof(reopened,position),derived)
 def test_bad_explicit_counts_cannot_use_inference(self):
  body,technical,position=self.fixture()
  with self.authority(technical):
   for attrs in ['data-pack-size="2"','data-count-per-pack="1"','data-pack-size="0" data-count-per-pack="0"','data-pack-size="1" data-count-per-pack="true"']:
    _,retained=self.read(body.replace('type="number"','type="number" '+attrs));self.assertTrue(retained is None or 'factory_reference'not in retained[1])
 def test_official_single_package_and_full_assembly_are_mandatory(self):
  body,technical,position=self.fixture()
  for features in [[],[{'label':'В упаковке:','value':'2 уп.'}],[{'label':'Остаток','value':'1 уп.'}]]:
   changed=copy.deepcopy(technical);changed['facts']['features']=features
   with self.authority(changed):
    proof,_=self.read(body)
    with self.assertRaises(factory.FactoryEvidenceError):factory.derive_singleton(proof['source'],position)
  with self.authority(technical):
   for extra in ['Основное изделие поставляется отдельно.','В комплект не входят крепежные элементы.','Неизвестное ограничение совместимости.','Изделие перепаковано в две упаковки.','Продаётся часть комплекта.']:
    self.read(body.replace('</article>','<p>'+extra+'</p></article>'))
    with self.assertRaises(factory.FactoryEvidenceError):factory.derive_singleton(self.last_source,position)
 def test_order_value_is_never_factory_package_cardinality(self):
  body,technical,position=self.fixture()
  with self.authority(technical):
   source,_=self.read(body.replace('value="1"','value="3"').replace('type="number"','type="search" min="3" step="3"'))
   proof=factory.derive_singleton(source['source'],position);self.assertEqual(proof['quantity'],1);self.assertEqual(proof['package_witness']['purchase_contract']['purchase_quantity'],3);self.assertEqual(proof['package_witness']['official_package_feature']['value'],'1 уп.')
 def test_source_reference_and_package_witness_changes_refuse(self):
  body,technical,position=self.fixture()
  with self.authority(technical):
   source,_=self.read(body);proof=factory.derive_singleton(source['source'],position)
   for key,value in [('quantity',True),('supplier_scope_sha256','0'*64),('official_package_feature',{'label':'В упаковке','value':'2 уп.'})]:
    changed=copy.deepcopy(proof);changed['package_witness'][key]=value
    with self.assertRaises(factory.FactoryEvidenceError):factory.verify_proof(changed,position)
   path=self.root/source['source']['ref'];path.write_bytes(path.read_bytes()+b'changed')
   with self.assertRaises(factory.FactoryEvidenceError):factory.verify_proof(proof,position)
 def test_policy_closure_and_exact_previous_readonly_attestation(self):
  from engine.test_factory_policy_closure353 import FactoryPolicyClosure353Tests
  from engine.search import acquisition_source_intent as intent
  from engine.search.document_context import current_document_policy
  fixture=FactoryPolicyClosure353Tests();fixture.setUp()
  try:
   old=fixture.historical(intent.PRE_LITERAL_SOURCE_FILES);self.assertEqual(intent.validate(old),old)
   with self.assertRaises(intent.IntentInvalid):intent.attest(fixture.runtime,old['components'])
   with self.assertRaises(intent.IntentInvalid):intent.verify(old,fixture.runtime,intent.components(fixture.root),{'route_intent_sha256':old['legacy_sha256']})
   current=intent.attest(fixture.runtime,intent.components(fixture.root));policy=current_document_policy(engine_root=fixture.root,supplier_file=fixture.suppliers);path=fixture.root/'search/literal_card_quote.py';self.assertIn(str(path),policy);path.write_bytes(path.read_bytes()+b'\n# inert366\n');self.assertNotEqual(current['sha256'],intent.attest(fixture.runtime,intent.components(fixture.root))['sha256']);self.assertNotEqual(policy,current_document_policy(engine_root=fixture.root,supplier_file=fixture.suppliers))
   fixture.test_policy_upgrade_never_reopens_old_pending_supplier_url()
  finally:fixture.doCleanups()

del _Fixture
