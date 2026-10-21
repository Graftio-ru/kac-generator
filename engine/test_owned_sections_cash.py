import base64,copy,hashlib,json,re,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from engine.search import http_card as h,agent,factory_reference as factory
from engine import config
BASE=None
class OwnedSectionsCashTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir=BASE);self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  for module in [h,agent,factory,config]:
   patcher=patch.object(module,'CACHE_DIR',self.root);patcher.start();self.addCleanup(patcher.stop)
  h._PARSED.clear();self.body=(BASE/'frozen208/body.html').read_text();self.generic=(BASE/'generic.html').read_text();self.url=json.loads((BASE/'frozen208/CACHE_DIR/rendered_cards/946d46cf8d474cb49b8293c47ffa5015.json').read_text())['url'];self.generic_url='https://different.invalid/product/X123'
 def test_exact208_owned_technical_identity_and_visible_cash(self):
  owned=h.main_product(self.body,self.url,check_controls=False);self.assertIsNotNone(owned);self.assertEqual(owned['article'],'36785KZL');self.assertEqual(owned['identity_basis'],'manufacturer_code');self.assertEqual(owned['listed_price'],5431.06);self.assertIn('Артикул производителя',owned['text']);self.assertIn('пластин',owned['text']);self.assertNotIn('factory_reference',owned)
 def test_different_generic_tab_caption_primary_sections_and_related_exclusion(self):
  owned=h.main_product(self.generic,self.generic_url,check_controls=False);self.assertIsNotNone(owned);self.assertEqual(owned['article'],'X123');self.assertEqual(owned['listed_price'],120);self.assertIn('Full assembly',owned['text']);self.assertIn('Steel assembly',owned['text']);self.assertNotIn('OTHER999',owned['text']);self.assertNotIn('FOREIGN111',owned['text'])
 def test_identity_visible_row_conflicts_hidden_related_metadata_only(self):
  for body in [self.generic.replace('<p itemprop="value">X123</p>','<p itemprop="value">WRONG222</p>'),self.generic.replace('<p itemprop="value">X123</p>','<p itemprop="value">X123</p><p itemprop="value">WRONG222</p>'),self.generic.replace('itemprop="mpn" content="X123"','itemprop="mpn" content="WRONG222"')]:self.assertIsNone(h.main_product(body,self.generic_url,check_controls=False))
  for attr in ['style="display:none"','data-related-products','style="opacity:0"']:
   body=self.generic.replace('itemprop="additionalProperty"','itemprop="additionalProperty" '+attr)
   observed=h.main_product(body,self.generic_url,check_controls=False);self.assertTrue(observed is None or observed['identity_basis']!='manufacturer_code')
  body=self.generic.replace('<p>Manufacturer code</p>','<p hidden>Manufacturer code</p>');observed=h.main_product(body,self.generic_url,check_controls=False);self.assertTrue(observed is None or (observed['identity_basis']=='title_only' and observed['article']=='' and observed['code_observations']==[]))
 def test_metadata_cash_never_supplies_hidden_foreign_ambiguous_or_noncurrent_price(self):
  cash='<p>Current price 120 RUB за шт.</p>'
  changes=[self.generic.replace(cash,''),self.generic.replace(cash,'<p hidden>120 RUB</p>'),self.generic.replace('itemprop="price" content="120"','itemprop="price" content="121"'),self.generic.replace(cash,cash+'<p>80 RUB</p>'),self.generic.replace('Current price 120 RUB','Current price 120 EUR'),self.generic.replace('itemprop="priceCurrency" content="RUB"','itemprop="priceCurrency" content="USD"'),self.generic.replace('Current price 120 RUB','Платёж 120 RUB в рассрочку'),self.generic.replace('schema.org/Offer','schema.org/AggregateOffer'),self.generic.replace(cash,'') .replace('</main>',cash+'</main>')]
  for body in changes:self.assertIsNone(h.main_product(body,self.generic_url,check_controls=False))
 def test_actual_JS_derivative_native_writer_source_and_original208_refusal(self):
  for p in (BASE/'frozen208/CACHE_DIR').rglob('*.json'):
   target=self.root/p.relative_to(BASE/'frozen208/CACHE_DIR');target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(p.read_bytes())
  ref='rendered_cards/946d46cf8d474cb49b8293c47ffa5015.json'
  old=None
  for p in (self.root/'main_facts').glob('*.json'):
   frame=json.loads(p.read_text())
   if frame['rendered_source']['ref']==ref:old={**frame['rendered_source'],'main_facts':{'ref':str(p.relative_to(self.root)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'observed_at':frame['observed_at']}}
  self.assertIsNotNone(old);self.assertIsNone(h.read_source(old,allow_rendered=True))
  results=json.loads((BASE/'offline-dom-run-v2/results.json').read_text());self.assertTrue(results['passed']);self.assertFalse(results['external_responses'])
  for name in ['actual208_derivative','generic']:
   fields=results['frames'][name]
   class Channel:
    def send(this,*a,**kw):return {'result':{'value':copy.deepcopy(fields)}}
    def detach(this):pass
   page=Mock();page.url=fields['document_url'];page.context.new_cdp_session.return_value=Channel();current,reference=agent._wait_main_card_facts(page,page.url,max_wait=.1,immediate=True)
   self.assertEqual(current['rendered_writer_status'],'stored');source=h.rendered_source(current,reference);read=h.read_source(source,allow_rendered=True);self.assertIsNotNone(read,name);self.assertEqual(read[1]['article'],'36785KZL'if name=='actual208_derivative'else 'X123');self.assertEqual(read[1]['listed_price'],5431.06 if name=='actual208_derivative' else 120)
   if name=='actual208_derivative':self.derivative_source=source
  # Curated factory activation uses original data/time. This parser repair does
  # not enrich the H1-contained Product branch with a factory_reference.
  # Later package/assembly gates have not been reached or waived.
  import inspect,time
  inputs=json.loads((BASE.parent/'EPIC-0001-TASK-0186/reference-intake/approved-import-inputs.json').read_text());review=json.loads((BASE.parent/'EPIC-0001-TASK-0186/reference-review/receipt-v3.json').read_text());source=inputs['reference_call'];factory.approve_origin(**inputs['authority_call']['kwargs']);reference=factory.import_reference(Path(source['body_path']).read_bytes(),**source['kwargs']);factory.activate_reference(reference,review,(BASE.parent/'EPIC-0001-TASK-0185/quality/source-verdict.json').read_bytes());self.assertIsNotNone(factory.lookup_reference('DKC','36785KZL'))
  from engine.models import SpecPosition
  position=SpecPosition.model_validate(json.loads((BASE.parent/'EPIC-0001-TASK-0205/selected55-fixture.json').read_text())['positions'][0])
  with self.assertRaisesRegex(factory.FactoryEvidenceError,'factory_supplier_role_missing'):factory.derive_singleton(self.derivative_source,position)
  record,owned=h.read_source(self.derivative_source,allow_rendered=True)
  (BASE/'v2-native-gate-witness.json').write_text(json.dumps({'derivative_only':True,'original208_unchanged_refused':True,'source':self.derivative_source,'owned':{k:owned.get(k) for k in ['article','identity_basis','listed_price','sale_unit','manufacturer','factory_reference','pack_counts']},'active_factory_lookup':factory.lookup_reference('DKC','36785KZL') is not None,'first_refusal':'factory_supplier_role_missing','exact_predicate':"not owned.get('factory_reference') or owned.get('identity_basis')!='manufacturer_code'",'native_branch':'H1-contained Product ordinary Offer; factory enrichment only metadata-sibling branch','later_unit_assembly_package_gates':'not reached'},indent=2)+'\n')
 def test_hidden_and_conflicting_actual_JS_frames_do_not_invent_code(self):
  frames=json.loads((BASE/'offline-dom-run-v2/results.json').read_text())['frames'];self.assertEqual(frames['hidden_code']['article'],'');self.assertEqual(frames['conflicting_code']['article'],'');self.assertIn('article',frames['conflicting_code']['missing_fields']);self.assertEqual(frames['actual208_derivative']['kind'],'partial');self.assertEqual(frames['actual208_derivative']['missing_fields'],['sale_unit'])

 def test_verified_owned_native_factory_text_and_persisted_tamper_controls(self):
  from engine import test_factory_kit_equivalence as retained
  from engine.source_quote import verified_owned_text,primary_text
  from engine.search.catalogue import CatalogueStore
  from engine.search.catalogue_projection import _facts
  retained.BASE=BASE.parent
  case=retained.FactoryKitEquivalenceTests('test_real_sources_native_acquisition_quote_publication_and_reopen');case.setUp()
  try:
   store,observation=case.acquire();saved=CatalogueStore(case.root/'catalogue.sqlite3').resolve_observation(case.identity.key,observation.observation_id)['observation']
   source=observation.unit_equivalence['supplier_source'];record,owned=h.read_source(source,allow_rendered=True);text=owned['text']
   self.assertIn('Сопутствующие товары',text);self.assertLess(len(primary_text(text)),len(text))
   self.assertEqual(verified_owned_text(text,source,url=record['url'],observed_at=record['observed_at']),text)
   self.assertEqual(observation.source_text,text);self.assertEqual(observation.source_sha256,hashlib.sha256(text.encode()).hexdigest());self.assertEqual(observation.unit_equivalence['source_sha256'],observation.source_sha256)
   for changed,ref,url,stamp in [(text+'Unknown assertion',source,record['url'],record['observed_at']),(text,{'owned_text':True},record['url'],record['observed_at']),(text,source,record['url']+'/changed',record['observed_at']),(text,source,record['url'],record['observed_at']+1),(text,{**source,'record_sha256':'0'*64},record['url'],record['observed_at'])]:
    with self.subTest(url=url,stamp=stamp,source=ref),self.assertRaises((ValueError,TypeError,KeyError)):verified_owned_text(changed,ref,url=url,observed_at=stamp)
   bad=copy.deepcopy(saved);bad['source_text']=text+'Unknown assertion';bad['source_sha256']=hashlib.sha256(bad['source_text'].encode()).hexdigest();self.assertIsNone(_facts(bad,case.position)[0])
   self.assertIsNone(_facts(saved,case.position.model_copy(update={'type_mark':'WRONG222'}))[0])
   import time
   with patch.object(time,'time',return_value=1792496218):self.assertIsNone(_facts(saved,case.position)[0])
   factory.approve_origin('DKC','https://www.dkc.ru','task186-root-dkc-origin-20261019-r1',2,False);self.assertIsNone(_facts(saved,case.position)[0])
   self.assertEqual(primary_text('Cash\nСопутствующие товары\nUnowned 999 RUB'),'Cash\n')
  finally:case.doCleanups()
