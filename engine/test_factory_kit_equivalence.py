"""Real immutable factory+supplier requalification, without actual acquisition."""
import base64,copy,functools,hashlib,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch,Mock
import httpx
from engine import config,llm,browser
from engine.models import SpecPosition
from engine.search import factory_reference as factory,http_card,agent
from engine.search.catalogue import CatalogueStore,identity_for_position
from engine.search.catalogue_images import CatalogueImageStore
from engine.search.catalogue_producer import capture_and_publish
from engine.search.card_attachments import collect_current
from engine.search.catalogue_projection import _facts,project_product,catalogue_scope
from engine.source_quote import quote_review_reason

BASE=None # Explicit immutable artifact-root binding supplied by offline runner.
class FactoryKitEquivalenceTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.assertIsNotNone(BASE,'offline artifact base required')
  self.review=json.loads((BASE/'EPIC-0001-TASK-0186/reference-review/receipt-v3.json').read_text())
  self.evidence=(BASE/'EPIC-0001-TASK-0185/quality/source-verdict.json').read_bytes()
  self.clock=patch.object(time,'time',return_value=max(1792419536.0,self.review['provenance']['reviewed_at']+1));self.clock.start();self.addCleanup(self.clock.stop)
  for module in [config,factory,http_card,agent]:
   p=patch.object(module,'CACHE_DIR',self.root);p.start();self.addCleanup(p.stop)
  http_card._PARSED.clear()
  self.assertIsNotNone(BASE,'offline artifact base required')
  self.inputs=json.loads((BASE/'EPIC-0001-TASK-0186/reference-intake/approved-import-inputs.json').read_text());self.seller=json.loads((BASE/'EPIC-0001-TASK-0183/actual-run/CACHE_DIR/http_cards/6b24583fa284407b89eacb7adefbc716.json').read_text())
  # Native approved supplier DATA is loaded in the synthetic test clock at
  # the immutable seller capture epoch; never stamp an old card with a newer
  # legal-reference observation. Original capture timestamps remain unchanged.
  from engine.search import whitelist
  reference_file=self.root/'suppliers.md';reference_file.write_bytes(Path(config.SUPPLIERS_FILE).read_bytes())
  scope=patch.object(whitelist,'SUPPLIERS_FILE',reference_file);scope.start();self.addCleanup(scope.stop)
  with patch.object(time,'time',return_value=self.seller['observed_at']):whitelist._refresh_seller_reference()
  self.body=base64.b64decode(self.seller['body_base64']);self.url=self.seller['url']
  self.factory_body=(BASE/'EPIC-0001-TASK-0185/manufacturer-large/body.html').read_bytes()
  self.assertEqual(hashlib.sha256(self.factory_body).hexdigest(),'a4497275a3fc422ddfbce5051a7c9609131579d4de60ac0e0907d206a114483e')
  self.assertEqual(hashlib.sha256(self.body).hexdigest(),self.seller['body_sha256'])
  parsed=json.loads((BASE/'EPIC-0001-TASK-0173/next-actual-run/parsed.json').read_text());self.position=SpecPosition.model_validate(parsed['positions'][0]);self.assertEqual(self.position.number,55)
  self.identity=identity_for_position(self.position)
  factory.approve_origin('DKC','https://www.dkc.ru','task186-root-dkc-origin-20261019-r1',1)
  self.reference=factory.import_reference(self.factory_body,url='https://www.dkc.ru/ru/catalog/239/36785kzl/',manufacturer='DKC',article='36785KZL',observed_at=1792409817.653383,valid_until=1792496217.653383)
  factory.activate_reference(self.reference,self.review,self.evidence)
 def acquire(self,*,body=None):
  body=self.body if body is None else body
  response=httpx.Response(200,content=body,headers={'content-type':self.seller['content_type']},request=httpx.Request('GET',self.url));response.encoding=self.seller['encoding']
  calls=[];save=http_card.save_response
  def retained(resp,url):
   with patch.object(time,'time',return_value=self.seller['observed_at']):return save(resp,url)
  store=CatalogueStore(self.root/'catalogue.sqlite3');store.acquire_unknown(self.identity,['elektro.ru'],owner='fixture')
  def wire(url,**kw):calls.append(url);return response
  def native_capture(*args,**kwargs):
   result=collect_current(*args,**kwargs,http_preflight=True);self.last_snapshot=result[0];return result
  with patch.object(agent,'supplier_http_get',side_effect=wire),patch.object(http_card,'save_response',side_effect=retained),patch.object(agent,'_capture_rendered_card',side_effect=AssertionError('No browser')),patch.object(llm,'complete_json',side_effect=AssertionError('No model')):
   observation=capture_and_publish(store,self.identity,self.position,self.url,browser=Mock(),image_store=CatalogueImageStore(self.root),capture=native_capture)
  self.assertEqual(calls,[self.url])
  return store,observation
 def test_real_sources_native_acquisition_quote_publication_and_reopen(self):
  started=time.monotonic();store,observation=self.acquire();self.assertIsNotNone(observation)
  self.assertEqual(observation.raw_amount,6595);self.assertEqual(observation.sale_unit,'упак');self.assertEqual(observation.observed_at,self.seller['observed_at'])
  self.assertIsNotNone(observation.unit_equivalence);self.assertEqual(observation.unit_equivalence['basis'],'derived_factory_singleton_kit')
  reopened=CatalogueStore(self.root/'catalogue.sqlite3');saved=reopened.resolve_observation(self.identity.key,observation.observation_id)['observation']
  facts,reason=_facts(saved,self.position);self.assertEqual(reason,'');self.assertEqual(facts['price'],6595)
  result=project_product(reopened,self.identity,self.position);self.assertEqual(result.all_offers[0].price_with_vat,6595)
  with catalogue_scope(reopened):self.assertEqual(quote_review_reason(result.all_offers[0],self.position),'')
  self.assertEqual(result.sources_found,1);self.assertLess(result.sources_found,3);self.assertEqual(observation.image.status,'missing')

  self.elapsed=time.monotonic()-started
  print('NATIVE_SAVED_ROUTE_SECONDS',round(self.elapsed,6),'PARSED_FACTS_CACHE',factory._technical_facts.cache_info())
 def source(self,body=None):
  response=httpx.Response(200,content=self.body if body is None else body,headers={'content-type':self.seller['content_type']},request=httpx.Request('GET',self.url));response.encoding=self.seller['encoding']
  with patch.object(time,'time',return_value=self.seller['observed_at']):return http_card.save_response(response,self.url)[1]
 def proof(self,body=None):
  return factory.derive_singleton(self.source(body),self.position)
 def test_current_authority_reference_expiry_and_no_time_refresh(self):
  proof=self.proof()
  with patch.object(time,'time',return_value=1792496218):
   with self.assertRaises(factory.FactoryEvidenceError):factory.verify_proof(proof,self.position)
  with self.assertRaisesRegex(factory.FactoryEvidenceError,'refresh'):
   factory.import_reference(self.factory_body,url='https://www.dkc.ru/ru/catalog/239/36785kzl/',manufacturer='DKC',article='36785KZL',observed_at=1792409818.653383,valid_until=1792496218.653383)
  factory.approve_origin('DKC','https://www.dkc.ru','task186-root-dkc-origin-20261019-r1',2,False)
  with self.assertRaises(factory.FactoryEvidenceError):factory.verify_proof(proof,self.position)
 def test_detached_request_source_and_persisted_proof_tampering(self):
  store,observation=self.acquire();saved=CatalogueStore(self.root/'catalogue.sqlite3').resolve_observation(self.identity.key,observation.observation_id)['observation']
  proof=observation.unit_equivalence
  for change in [dict(quantity=2),dict(quantity=True),dict(factory_reference=proof['supplier_source']),dict(supplier_source=proof['factory_reference']),dict(source_sha256='0'*64)]:
   with self.subTest(change=change):
    value=copy.deepcopy(saved);value['unit_equivalence'].update(change)
    self.assertIsNone(_facts(value,self.position)[0])
  for field,value in [('name','Different assembly'),('type_mark','36785KZL/S'),('unit','шт')]:
   with self.subTest(field=field),self.assertRaises(factory.FactoryEvidenceError):factory.verify_proof(proof,self.position.model_copy(update={field:value}))
  path=factory._root()/(self.reference['body_sha256']+'.html');path.write_bytes(self.factory_body+b'changed')
  with self.assertRaises(factory.FactoryEvidenceError):factory.verify_proof(proof,self.position)
 def test_supplier_partial_count_presence_and_unknown_claims_refuse(self):
  text=self.body.decode(self.seller['encoding'])
  for phrase in ['Данный товар не является полным комплектом','Без самого изделия','Только крепления','Упаковка содержит 2 комплекта','С крепежом в комплекте</div><div>Нет','С отсутствием крепежа в комплекте','Соединительные пластины отсутствуют']:
   for after in [False,True]:
    with self.subTest(phrase=phrase,after=after):
     extra='<p>'+phrase+'</p>';changed=text.replace('</h1>','</h1>'+extra,1) if after else text.replace('<h1',extra+'<h1',1)
     with self.assertRaises((factory.FactoryEvidenceError,TypeError)):self.proof(changed.encode(self.seller['encoding']))
  for old,new in [('data-pack-size="1"','data-pack-size="2"'),('data-count-per-pack="1"','data-unused-count="1"')]:
   self.assertIn(old,text)
   with self.subTest(old=old),self.assertRaises((factory.FactoryEvidenceError,TypeError)):self.proof(text.replace(old,new).encode(self.seller['encoding']))
  for label in ['С крепежом в комплекте','С соединительными пластинами в комплекте']:
   with self.assertRaises(factory.FactoryEvidenceError):factory._assembly_claims(label+'\nНет','T','',[dict(label=label,value='Нет')])
 def test_factory_owned_omission_repack_and_exact_code_refuse(self):
  text=self.factory_body.decode()
  for phrase in ['Данный товар не является полным комплектом','Без самого изделия','Только крепления','Перепаковано в 2 упаковки','Соединительные пластины отсутствуют']:
   for after in [False,True]:
    with self.subTest(phrase=phrase,after=after):
     changed=text.replace('</h1>','</h1><p>'+phrase+'</p>',1) if after else text.replace('<h1','<p>'+phrase+'</p><h1',1)
     ref=factory.import_reference(changed.encode(),url='https://www.dkc.ru/ru/catalog/239/36785kzl/',manufacturer='DKC',article='36785KZL',observed_at=1792409817.653383,valid_until=1792496217.653383)
     with self.assertRaises(factory.FactoryEvidenceError):self.proof()
  with self.assertRaises(factory.FactoryEvidenceError):factory.import_reference(text.replace('36785KZL','36785KZL/S').encode(),url='https://www.dkc.ru/ru/catalog/239/36785kzl/',manufacturer='DKC',article='36785KZL',observed_at=1792409817.653383,valid_until=1792496217.653383)
 def test_internal_cli_technical_reader_refuses_oversize_before_read(self):
  from engine import import_factory_reference as cli
  import sys
  path=self.root/'oversize.html'
  with path.open('wb') as out:out.truncate(factory.MAX_BYTES+1)
  args=['import-factory','import','--body',str(path),'--url','https://www.dkc.ru/ru/catalog/239/36785kzl/','--manufacturer','DKC','--article','36785KZL','--observed-at','1792409817.653383','--valid-until','1792496217.653383']
  with patch.object(sys,'argv',args),self.assertRaises(factory.FactoryEvidenceError):cli.main()
 def test_generic_second_authority_and_article_native_reopen(self):
  data=BASE/'EPIC-0001-TASK-0186/generic-reference-v3'
  source=json.loads((data/'source-input.json').read_text());review=json.loads((BASE/'EPIC-0001-TASK-0186/reference-review/generic-receipt-v3.json').read_text())
  with patch.object(time,'time',return_value=max(self.review['provenance']['reviewed_at'],review['provenance']['reviewed_at'])+1):
   factory.approve_origin(**source['authority'])
   self.reference=factory.import_reference((data/'factory.html').read_bytes(),**source['source'])
   factory.activate_reference(self.reference,review,(BASE/'EPIC-0001-TASK-0186/reference-review/generic-technical-review-evidence-v3.json').read_bytes())
   self.assertNotEqual(factory.read_reference(self.reference)['facts']['description'],factory.read_reference(self.review['reference'])['facts']['description'])
   position=json.loads(json.dumps(self.position.model_dump(),ensure_ascii=False).replace('36785KZL','56785KZL').replace('DKC','ACME'))
   position['source_evidence']['source_hash']=hashlib.sha256(b'synthetic-second-request-v3').hexdigest()
   self.position=SpecPosition.model_validate(position);self.identity=identity_for_position(self.position)
   self.body=(data/'supplier.html').read_bytes();self.url=source['supplier_url'];self.seller={**self.seller,'observed_at':source['supplier_observed_at'],'encoding':'utf-8'}
   store,observation=self.acquire()
   self.assertEqual(observation.unit_equivalence['request']['manufacturer'],'ACME');self.assertEqual(observation.unit_equivalence['request']['article'],'56785KZL')
   saved=CatalogueStore(self.root/'catalogue.sqlite3').resolve_observation(self.identity.key,observation.observation_id)['observation']
   self.assertEqual(_facts(saved,self.position)[0]['price'],120)
   self.assertEqual(observation.unit_equivalence['factory_review']['receipt_sha256'],factory._hash(factory._json(review).encode()))
   with self.assertRaises(factory.FactoryEvidenceError):factory.verify_proof(observation.unit_equivalence,self.position.model_copy(update={'type_mark':'56785KZL/S'}))
 def test_untyped_owned_assertions_all_tags_fields_and_links_refuse(self):
  text=self.body.decode(self.seller['encoding'])
  for block in ['<p>Изделие отсутствует</p>','<div>Основное изделие поставляется отдельно</div>','<span>Unknown whole-product condition</span>',"<a href='#details'>Изделие отсутствует</a>",'<div><div>Материал</div><div>Изделие отсутствует</div></div>','<div><div>Цвет</div><div>Основное изделие отсутствует</div></div>']:
   for before in [False,True]:
    with self.subTest(block=block,before=before):
     changed=text.replace('<h1',block+'<h1',1) if before else text.replace('</h1>','</h1>'+block,1)
     with self.assertRaises(factory.FactoryEvidenceError):self.proof(changed.encode(self.seller['encoding']))
  for block in ['<p>Изделие отсутствует</p>',"<a href='#details'>Основное изделие поставляется отдельно</a>"]:
   technical=self.factory_body.decode().replace('</h1>','</h1>'+block,1)
   factory.import_reference(technical.encode(),url='https://www.dkc.ru/ru/catalog/239/36785kzl/',manufacturer='DKC',article='36785KZL',observed_at=1792409817.653383,valid_until=1792496217.653383)
   with self.assertRaises(factory.FactoryEvidenceError):self.proof()
 def test_qa_omission_native_quote_refuses_and_raw_publication_reopens_review(self):
  # First obtain a real native snapshot, not a hand-built financial receipt.
  store,baseline=self.acquire();snapshot=self.last_snapshot
  changed=self.body.decode(self.seller['encoding']).replace('</h1>','</h1><p>Изделие отсутствует</p>',1).encode(self.seller['encoding'])
  with self.assertRaisesRegex(AssertionError,'No browser|No model'):self.acquire(body=changed)
  source=self.source(changed);record,owned=http_card.read_source(source)
  from engine.search.product_identity import http_identity,_original_native_model_role
  from engine.search.literal_card_quote import current_literal_quote
  from engine.search.catalogue_producer import publish_facts
  facts=dict(snapshot.facts(),text=owned['text'],http_source=source,title=owned['title'],main_title=owned['title'],page_article=owned['article'],sale_unit=owned['sale_unit'])
  with _original_native_model_role(store,self.identity,self.position),factory.request_scope(self.position):
   facts['identity_evidence']=http_identity(record,owned,source,self.position.type_mark,self.position.name,'elektro.ru')
   self.assertTrue(facts['identity_evidence']);self.assertIsNone(current_literal_quote(facts))
   # Retain the source-selected raw cash as review data; no unit receipt or
   # derived proof accompanies it. Financial eligibility is rechecked below.
   review=project_product(store,self.identity,self.position).all_offers[0].model_copy(update={'source_quote_evidence':{},'unit_review_required':True,'seller_evidence':None,'price_unit':owned['sale_unit'],'listed_price_with_vat':owned['listed_price']})
   raw=publish_facts(store,self.identity,self.position,facts,image_store=CatalogueImageStore(self.root),offer=review,verified_owner=True)
  saved=CatalogueStore(self.root/'catalogue.sqlite3').resolve_observation(self.identity.key,raw.observation_id)['observation']
  self.assertIn('Изделие отсутствует',saved['source_text']);self.assertIsNone(saved.get('unit_equivalence'))
  self.assertEqual(saved['raw_amount'],6595);self.assertIsNone(_facts(saved,self.position)[0]);self.assertTrue(_facts(saved,self.position)[1])

 def test_factory_description_unknown_joined_assertions_and_extra_clauses_refuse(self):
  technical=self.factory_body.decode()
  # Mutate BOTH visible description and schema description, so equality of
  # these two source representations cannot authorize an unknown assertion.
  original=factory.read_reference(self.reference)['facts']['description']
  for phrase in ['Изделие отсутствует','Основная деталь изъята']:
   with self.subTest(phrase=phrase):
    marker='Внутренний радиус аксессуара'
    self.assertGreaterEqual(technical.count(marker),2,'visible+schema marker required')
    changed=technical.replace(marker,phrase+'. '+marker)
    factory.import_reference(changed.encode(),url='https://www.dkc.ru/ru/catalog/239/36785kzl/',manufacturer='DKC',article='36785KZL',observed_at=1792409817.653383,valid_until=1792496217.653383)
    with self.assertRaises(factory.FactoryEvidenceError):self.proof()
  # New body/description cannot inherit the old independent curation receipt.
  with self.assertRaises(factory.FactoryEvidenceError):factory.activate_reference(factory.lookup_reference('DKC','36785KZL'),self.review,self.evidence)

 def test_stock_and_brand_roles_do_not_hide_free_product_assertions(self):
  text=self.body.decode(self.seller['encoding'])
  for key in ['nalichie','balance_list']:
   changed=text.replace('</h1>',"</h1><div data-name='"+key+"'><p>Изделие отсутствует</p></div>",1)
   with self.subTest(key=key),self.assertRaises(factory.FactoryEvidenceError):self.proof(changed.encode(self.seller['encoding']))
  changed=text.replace('О бренде','О бренде</div><p>Основная деталь изъята</p><div>',1)
  with self.assertRaises(factory.FactoryEvidenceError):self.proof(changed.encode(self.seller['encoding']))

 def test_pending_import_wrong_review_and_revocation_cannot_grant_authority(self):
  # Same source, new import record due to a shortened expiry is pending.
  pending=factory.import_reference(self.factory_body,url='https://www.dkc.ru/ru/catalog/239/36785kzl/',manufacturer='DKC',article='36785KZL',observed_at=1792409817.653383,valid_until=1792496000.0)
  self.assertEqual(factory.read_pending(pending)['body_sha256'],self.reference['body_sha256'])
  with self.assertRaises(factory.FactoryEvidenceError):factory.read_reference(pending)
  with self.assertRaises(factory.FactoryEvidenceError):factory.activate_reference(pending,self.review,self.evidence)
  for change in [dict(role='supplier_description'),dict(review_revision=True),dict(reference=pending),dict(provenance={**self.review['provenance'],'evidence_sha256':'0'*64})]:
   with self.subTest(change=change),self.assertRaises(factory.FactoryEvidenceError):factory.activate_reference(self.reference,{**self.review,**change},self.evidence)
 def test_active_review_hash_revision_and_evidence_rechecked_after_reopen(self):
  store,observation=self.acquire();saved=CatalogueStore(self.root/'catalogue.sqlite3').resolve_observation(self.identity.key,observation.observation_id)['observation']
  self.assertEqual(_facts(saved,self.position)[0]['price'],6595)
  active_path=factory._root()/'active.json';active_before=active_path.read_bytes();state=json.loads(active_before)
  next(iter(state.values()))['review_sha256']='../outside'
  active_path.write_text(json.dumps(state))
  self.assertIsNone(_facts(saved,self.position)[0]);active_path.write_bytes(active_before)
  factory.revoke_reference(self.reference,revision=2)
  self.assertIsNone(_facts(saved,self.position)[0])
  with self.assertRaises(factory.FactoryEvidenceError):factory.activate_reference(self.reference,self.review,self.evidence)
  replacement={**self.review,'review_revision':3};factory.activate_reference(self.reference,replacement,self.evidence)
  # Exact body is unchanged but old persistent proof still binds old review.
  self.assertIsNone(_facts(saved,self.position)[0])
  fresh=self.proof();self.assertEqual(fresh['factory_review']['revision'],3)
  path=factory._root()/(replacement['provenance']['evidence_sha256']+'.review-evidence');path.write_bytes(b'changed')
  with self.assertRaises(factory.FactoryEvidenceError):factory.verify_proof(fresh,self.position)

 def test_native_singleton_quote_then_sqlite_lot_count_corruption_refuses(self):
  store,observation=self.acquire();path=self.root/'catalogue.sqlite3'
  saved=CatalogueStore(path).resolve_observation(self.identity.key,observation.observation_id)['observation']
  self.assertEqual(saved['raw_amount'],6595);self.assertEqual(saved['sale_unit'],'упак')
  self.assertIsNone(saved['lot_quantity']);self.assertEqual(saved['package_quantity'],1)
  self.assertEqual(_facts(saved,self.position)[0]['price'],6595)
  with store._connection() as db:
   row=db.execute('SELECT payload FROM observations WHERE id=?',(observation.observation_id,)).fetchone()
   before=json.loads(row[0]);corrupted=copy.deepcopy(before);corrupted['lot_quantity']=2
   db.execute('UPDATE observations SET payload=? WHERE id=?',(json.dumps(corrupted),observation.observation_id))
  resolved=CatalogueStore(path).resolve_observation(self.identity.key,observation.observation_id)['observation']
  self.assertEqual(resolved['lot_quantity'],2);self.assertEqual({**resolved,'lot_quantity':saved['lot_quantity']},saved)
  facts,reason=_facts(resolved,self.position);self.assertIsNone(facts);self.assertEqual(reason,'Единица заводской упаковки противоречива')
  for field in ['lot_quantity','package_quantity']:
   for value in [2,True,float('inf')]:
    with self.subTest(field=field,value=value):self.assertIsNone(_facts({**saved,field:value},self.position)[0])
   self.assertEqual(_facts({**saved,field:1},self.position)[0]['price'],6595)
