"""Native model roles never alias explicit manufacturer articles or variants."""
import hashlib,json,tempfile,time,unittest,uuid
from pathlib import Path
from engine.models import SpecPosition,PriceOffer,Requisites
from engine.search.catalogue import CatalogueStore,identity_for_position
from engine.search.catalogue_images import CatalogueImageStore
from engine.search.catalogue_producer import publish_snapshot,CataloguePublicationError
from engine.search.catalogue_projection import project_product,_identity_reason
from engine.search.product_identity import native_request_role,valid_identity
from engine.search.card_batch import CardSnapshot

class NativeRoleTests(unittest.TestCase):
 def position(self,*,model='ZX-96D',article='',maker='Maker',name='Multimeter',qty=1):
  mark=' / '.join(v for v in (model,article) if v)
  return SpecPosition(number=1,name=name,type_mark=mark,unit='шт',qty=qty,source_evidence=dict(reader='native_pdf',cells=dict(name=name,model=model,article=article,maker=maker)))
 def proof(self,p,*,title='Multimeter ZX-96D Maker A51417',article='A51417'):
  return dict(version=1,provenance='rendered_main_card',source_url='https://shop.test/product/ZX-96D',canonical_url='https://shop.test/product/ZX-96D',host='shop.test',expected_mark=p.type_mark,expected_name=p.name,main_title=title,main_article=article,observed_at=time.time())
 def test_verified_model_not_printed_article_and_unknown_legacy_conservative(self):
  p=self.position();proof=self.proof(p)
  self.assertEqual(native_request_role(p),'model');self.assertFalse(valid_identity(proof,p.type_mark,p.name))
  self.assertTrue(valid_identity(proof,p.type_mark,p.name,request_position=p))
  for changed in (p.model_copy(update={'source_evidence':{}}),p.model_copy(update={'review_fields':{'model':'uncertain'}}),p.model_copy(update={'type_mark':'OTHER999'})):
   self.assertEqual(native_request_role(changed),'unknown');self.assertFalse(valid_identity(proof,p.type_mark,p.name,request_position=changed))
 def test_explicit_article_mismatch_and_suffix_are_not_model_aliases(self):
  p=self.position(model='',article='A51416');self.assertEqual(native_request_role(p),'article')
  self.assertFalse(valid_identity(self.proof(p),p.type_mark,p.name,request_position=p))
  p=self.position();self.assertFalse(valid_identity(self.proof(p,title='Multimeter ZX-96DX Maker A51417'),p.type_mark,p.name,request_position=p))
  self.assertFalse(valid_identity(self.proof(p,title='Multimeter generic Maker',article='ZX-96D'),p.type_mark,p.name,request_position=p))
 def test_owned_model_snapshot_publishes_raw_code_unit_date_independently(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);store=CatalogueStore(root/'store.sqlite3');images=CatalogueImageStore(root);p=self.position();identity=identity_for_position(p);admission=store.acquire_unknown(identity,['shop.test'],owner='test');stamp=time.time();proof=self.proof(p);proof['observed_at']=stamp
   text='Multimeter ZX-96D Maker A51417\nАртикул: A51417\nПроизводитель: Maker\n100 руб. с НДС\nЦена за1шт'
   facts=dict(url=proof['canonical_url'],requested_url=proof['source_url'],main_title=proof['main_title'],page_article='A51417',captured_at=stamp,text=text,identity_evidence=proof,screenshot_context='',context_capture_evidence={})
   offer=PriceOffer(price_with_vat=100,listed_price_with_vat=100,price_unit='шт',requisites=Requisites(name='Сбербанк',inn='7707083893'))
   # Raw publication does not need an old buyer-normalized quote receipt.
   observed=publish_snapshot(store,identity,p,CardSnapshot.create(facts),image_store=images,acquisition=admission)
   self.assertEqual((observed.raw_amount,observed.manufacturer_code,observed.observed_at),(100,'A51417',stamp));self.assertEqual(observed.sale_unit,'1шт');self.assertEqual(observed.image.status,'missing')
   before=store.lookup(identity)['heads'];project_product(store,identity,p.model_copy(update={'qty':24}));self.assertEqual(store.lookup(identity)['heads'],before)
 def test_native_variants_require_interface_and_refuse_current_mismatch(self):
  p=self.position(name='Multimeter 600В 5А RS485');identity=identity_for_position(p)
  def reason(title,text=''):
   return _identity_reason(dict(identity=identity.payload(),title=title,manufacturer_code='A51417',url='https://shop.test/product/ZX-96D',source_text=title+'\nПроизводитель: Maker\n'+text),p)
  self.assertIn('Интерфейс',reason('Multimeter ZX-96D Maker 600В 5А'))
  self.assertIn('Интерфейс',reason('Multimeter ZX-96D Maker 600В 5А','Другие исполнения поддерживают RS485'))
  self.assertEqual(reason('Multimeter ZX-96D Maker 600В 5А','Интерфейс: RS485'),'')
  self.assertIn('параметры',reason('Multimeter ZX-96D Maker 600В 1А RS485'))
  self.assertEqual(reason('Multimeter ZX-96D Maker 600В 5А RS485'),'')
  self.assertIn('Производитель',reason('Multimeter ZX-96D Other 600В 5А RS485','Производитель: Other'))
 def test_negative_optional_or_contradictory_interface_publish_review_without_changing_raw_facts(self):
  cases=[('Интерфейс: без RS485',False),('Интерфейс: RS485 отсутствует',False),('Интерфейс: RS485 не поддерживается',False),('RS485: нет',False),('Интерфейс: RS485 (опция)',False),('Интерфейс: optional RS485',False),('Интерфейс: RS485',True)]
  combinations=[(declaration,token,positive) for declaration,positive in cases for token in ('',' RS485')]
  combinations.extend(('Интерфейс: RS485',token,False) for token in (' без RS485',' RS485 отсутствует',' RS485 не поддерживается',' RS485: нет',' optional RS485',' not supported RS485',' RS485 is not supported',' without support for RS485',' maybe RS485',' RS485 disabled',' RS485 (absent)',' RS485: unavailable',' disabled RS485',' RS485 unknown',' RS485 (optional)'))
  combinations.extend((label,token,False) for token in ('',' RS485') for label in ('RS485:\nнет','Интерфейс:\nбез RS485','Интерфейс: RS485 по запросу','Interface: RS485 not supported','Interface: RS485 unsupported','Интерфейс: нет (RS485)','Interface: without (RS485)','Interface: RS485 (optional)','Interface: maybe RS485','Интерфейс: RS485 зависит от исполнения','Interface: RS232'))
  combinations.extend((label,'',True) for label in ('Interface: RS485','Interface: RS485 supported','RS485: yes','Интерфейс: RS485 есть'))
  combinations.extend(('Интерфейс: RS485',token,True) for token in (' RS485',' RS485 supported',' RS485 (present)'))
  for declaration,title_token,positive in combinations:
    with self.subTest(declaration=declaration,title_token=title_token),tempfile.TemporaryDirectory() as directory:
     root=Path(directory);store=CatalogueStore(root/'store.sqlite3');images=CatalogueImageStore(root);p=self.position(name='Multimeter 600В 5А RS485');identity=identity_for_position(p);a=store.acquire_unknown(identity,['shop.test'],owner='test');title='Multimeter ZX-96D Maker A51417 600В 5А'+title_token;stamp=time.time();proof=self.proof(p,title=title);proof['observed_at']=stamp
     text=title+'\nАртикул: A51417\nПроизводитель: Maker\n'+declaration+'\n100 руб. с НДС\nЦена за1шт'
     facts=dict(url=proof['canonical_url'],requested_url=proof['source_url'],main_title=title,page_article='A51417',captured_at=stamp,text=text,identity_evidence=proof,screenshot_context='',context_capture_evidence={})
     observation=publish_snapshot(store,identity,p,CardSnapshot.create(facts),image_store=images,acquisition=a);store.finish_acquisition(a)
     current=CatalogueStore(root/'store.sqlite3');result=project_product(current,identity,p);offer=result.all_offers[0]
     self.assertEqual((observation.raw_amount,observation.observed_at,observation.sale_unit),(100,stamp,'1шт'));self.assertEqual(observation.image.observation_id,observation.observation_id);self.assertEqual(observation.image.status,'missing')
     if positive:self.assertEqual(observation.state,'observed');self.assertFalse(offer.unit_review_required,offer.unit_review_reason)
     else:self.assertEqual(observation.state,'identity_conflict');self.assertTrue(offer.unit_review_required);self.assertIn('Интерфейс',offer.unit_review_reason)
 def test_owned_typed_interface_property_boundaries(self):
  from engine.search.product_identity import native_model_spec_reason
  p=self.position(name='Multimeter 600В 5А RS485');title='Multimeter ZX-96D Maker 600В 5А'
  # The value RS485 belongs to the typed label, not to the next unrelated
  # caption. Blank DOM layout lines do not alter that label/value ownership.
  for label in ('Интерфейс','Interface','Возможность передачи данных'):
   for separator in (': ','\n','\n\n\n','\r\n\r\n'):
    with self.subTest(label=label,separator=repr(separator)):
     text=label+separator+'RS485\n\nВозможность фиксации показаний\nнет'
     self.assertEqual(native_model_spec_reason(p,title,text),'')
  for text in ('RS485','Описание\nRS485','Другие исполнения поддерживают RS485',
               'Возможность передачи данных неизвестна: RS485',
               'Похожие товары\nВозможность передачи данных\nRS485'):
   from engine.source_quote import primary_text
   with self.subTest(untyped=text):self.assertIn('Интерфейс',native_model_spec_reason(p,title,primary_text(text)))
 def test_typed_interface_property_refuses_optional_negative_conflict_and_wrong_electrical(self):
  from engine.search.product_identity import native_model_spec_reason
  p=self.position(name='Multimeter 600В 5А RS485');title='Multimeter ZX-96D Maker 600В 5А'
  for value in ('нет','без RS485','RS485 отсутствует','RS485 не поддерживается','RS485 (опция)',
                'optional RS485','RS485 по запросу','RS232','RS485 или RS232','RS485 зависит от исполнения'):
   for separator in (': ','\n\n'):
    with self.subTest(value=value,separator=repr(separator)):
     self.assertIn('Интерфейс',native_model_spec_reason(p,title,'Возможность передачи данных'+separator+value))
  for conflict in ('Интерфейс: RS232','Интерфейс: нет','RS485: нет','Возможность передачи данных: optional RS485'):
   with self.subTest(conflict=conflict):
    self.assertIn('Интерфейс',native_model_spec_reason(p,title,'Возможность передачи данных\nRS485\n'+conflict))
  self.assertIn('параметры',native_model_spec_reason(p,'Multimeter ZX-96D Maker 600В 1А','Возможность передачи данных\nRS485'))
 def test_overlong_typed_interface_values_fail_closed_instead_of_disappearing(self):
  from engine.search.product_identity import native_model_spec_reason
  p=self.position(name='Multimeter 600В 5А RS485');title='Multimeter ZX-96D Maker 600В 5А RS485'
  for label in ('Интерфейс','Возможность передачи данных','RS485'):
   for length in (255,256,257,303):
    for seed in ('нет','optional RS485','unknown','RS485','yes'):
     value=seed.ljust(length)
     with self.subTest(label=label,length=length,seed=seed):
      reason=native_model_spec_reason(p,title,label+': '+value)
      positive=length<=256 and (seed=='RS485' or label=='RS485' and seed=='yes')
      if positive:self.assertEqual(reason,'')
      else:self.assertIn('Интерфейс',reason)
 def test_actual_146_owned_rs485_property_does_not_manufacture_unit_proof(self):
  from engine.search.catalogue_projection import _facts
  # The existing workspace real-data fixture is immutable; this is a pure
  # saved-source replay, not a fresh supplier observation or acquisition.
  workspace=next(parent for parent in Path(__file__).resolve().parents if (parent/'kac-generator/engine').is_dir())
  source=workspace/'sources/kac-generator/atomic-simplification-2026-10-14/EPIC-0001-TASK-0145'
  p=SpecPosition.model_validate(json.loads((source/'pilot-input.json').read_text())['positions'][1])
  path=source/'next-actual-run/CACHE_DIR/main_facts/b029ec36606d459a9195830dbebd137b.json'
  self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),'89d0368d824a2becc00618bfa014187cfd09085a8c7544fd6de5188a7a6b2d45')
  facts=json.loads(path.read_text());text=facts['owner_text'];title=facts['main_title']
  self.assertTrue(facts['owner_bound']);self.assertEqual(facts['article'],'51416DEK')
  self.assertEqual(facts['document_url'],facts['requested_url']);self.assertIn('600В 5А',title)
  self.assertIn('LED',title);self.assertIn('Возможность передачи данных',text);self.assertIn('RS485',text)
  observation=dict(identity=identity_for_position(p).payload(),title=title,manufacturer_code=facts['article'],
   url=facts['document_url'],source_text=text,source_sha256=hashlib.sha256(text.encode()).hexdigest(),
   raw_amount=19380.0,state='observed',currency='RUB',vat='unknown',purchase_terms='',sale_unit='шт.',
   lot_quantity=None,package_quantity=None,unit_basis=None)
  self.assertEqual(_identity_reason(observation,p),'')
  self.assertEqual(facts['missing_fields'],['sale_unit']);self.assertEqual(facts['purchase_text'],'')
  amount,reason=_facts(observation,p)
  self.assertIsNone(amount);self.assertIn('единиц',reason.casefold())
 def test_background_refresh_uses_only_resolved_versioned_intent_and_new_dates(self):
  from engine.search.catalogue_producer import refresh_recorded
  from engine.search.product_identity import _resolved_model_role,_NATIVE_MODEL_INTENT
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);store=CatalogueStore(root/'store.sqlite3');images=CatalogueImageStore(root);p=self.position();identity=identity_for_position(p);a=store.acquire_unknown(identity,['shop.test'],owner='test')
   def snapshot(position,amount):
    proof=self.proof(position);stamp=time.time();proof['observed_at']=stamp
    text=proof['main_title']+'\nАртикул: A51417\nПроизводитель: Maker\n'+str(amount)+' руб. с НДС\nЦена за1шт'
    return CardSnapshot.create(dict(url=proof['canonical_url'],requested_url=proof['source_url'],main_title=proof['main_title'],page_article='A51417',captured_at=stamp,text=text,identity_evidence=proof,screenshot_context='',context_capture_evidence={})),None
   snap,_=snapshot(p,100);first=publish_snapshot(store,identity,p,snap,image_store=images,acquisition=a);store.finish_acquisition(a)
   self.assertEqual(first.validation_version,_NATIVE_MODEL_INTENT)
   for amount in (75,125):
    report=refresh_recorded(store,browser=None,image_store=images,capture=lambda browser,position,url,**kwargs:snapshot(position,amount))
    self.assertEqual(report[0]['outcome'],'observed',report);current=store.lookup(identity)['heads'][0]['current']
    self.assertEqual(current['validation_version'],_NATIVE_MODEL_INTENT);self.assertEqual(current['raw_amount'],amount);self.assertGreater(current['observed_at'],first.observed_at);self.assertEqual(current['image']['observation_id'],current['observation_id']);self.assertEqual(current['image']['status'],'missing')
   legacy=p.model_copy(update={'source_evidence':{}})
   self.assertEqual(native_request_role(legacy),'unknown')
   with _resolved_model_role(dict(current,validation_version='catalogue-native-v1'),identity):self.assertEqual(native_request_role(legacy),'unknown')
   with _resolved_model_role(dict(current,identity=dict(current['identity'],code='OTHER999')),identity):self.assertEqual(native_request_role(legacy),'unknown')
   with _resolved_model_role(current,identity):
    self.assertEqual(native_request_role(legacy),'model');self.assertEqual(native_request_role(legacy.model_copy(update={'full_name':'Other descriptor'})),'unknown')
    self.assertEqual(native_request_role(self.position(model='',article='ZX-96D')),'article')
    self.assertEqual(native_request_role(p.model_copy(update={'review_fields':{'model':'uncertain'}})),'unknown')
   restarted=CatalogueStore(root/'store.sqlite3');result=project_product(restarted,identity,legacy)
   self.assertEqual(result.all_offers[0].listed_price_with_vat,125);self.assertNotIn('Товар/артикул',result.all_offers[0].unit_review_reason)
 def test_normal_render_observer_uses_matching_owner_not_navigation_body_article(self):
  from unittest.mock import MagicMock,patch
  from engine.search import agent
  url='https://shop.test/product/ZX-96D';title='Multimeter ZX-96D Maker A51417';text=title+'\nАртикул: A51417\nЦена по запросу'
  page=MagicMock();page.url=page._kac_document_url=url;page._kac_response_status=200
  page.inner_text.return_value='Сопутствующие товары\nNavigation\n'+text
  page.locator.return_value.count.return_value=1;page.locator.return_value.first.inner_text.return_value=title
  browser=MagicMock();browser.page.return_value.__enter__.return_value=page
  coherent=dict(kind='explicit_price_on_request',owner_bound=True,body=page.inner_text.return_value,owner_text=text,main_title=title,article='A51417',document_title='',document_url=url,observed_at=time.time())
  for changed in (False,True):
   records=[];next_frame=dict(coherent,owner_text='Changed owner') if changed else coherent
   with patch.object(agent,'_render_observation',return_value=({'observation_status':'observed'},{})),patch.object(agent,'_wait_main_card_facts',side_effect=[(coherent,{}),(next_frame,{})]),patch.object(agent,'_admit_card_text',return_value=None):
    result=agent._capture_rendered_card(browser,url,url,'shop.test','ZX-96D','Multimeter','шт',1,'',set(),time.monotonic()+10,neutral_sink=records.append)
   self.assertIsNone(result);self.assertEqual(len(records),0 if changed else 1)
   if records:self.assertEqual(records[0]['identity_observation']['main_article'],'A51417');self.assertEqual(records[0]['identity_observation']['observed_at'],coherent['observed_at'])
 def test_initial_role_scope_does_not_erase_other_competing_sku_or_unknown_article(self):
  from engine.search.product_identity import _original_native_model_role,_active_model_role
  from engine.search import agent
  with tempfile.TemporaryDirectory() as directory:
   store=CatalogueStore(Path(directory)/'store.sqlite3');p=self.position(name='Multimeter RS485');identity=identity_for_position(p);store.acquire_unknown(identity,['shop.test'],owner='test')
   with _original_native_model_role(store,identity,p):
    self.assertTrue(_active_model_role(p.type_mark,p.name))
    explicit=self.position(model='',article='ZX-96D',name=p.name)
    self.assertFalse(valid_identity(self.proof(explicit),explicit.type_mark,explicit.name,request_position=explicit))
    self.assertEqual(agent._different_explicit_article(p.type_mark,'Multimeter ZX-96D RS485','https://shop.test/product/ZX-96D-RS485'),'')
    self.assertEqual(agent._different_explicit_article(p.type_mark,'Multimeter ZX-96D ABC777','https://shop.test/product/ZX-96D-ABC777'),'abc777')
    self.assertFalse(valid_identity(self.proof(p,title='Multimeter ZX-96DX Maker A51417'),p.type_mark,p.name))
   for altered in (p.model_copy(update={'source_evidence':{}}),p.model_copy(update={'review_fields':{'model':'uncertain'}}),self.position(model='',article='ZX-96D',name='Multimeter RS485')):
    with _original_native_model_role(store,identity,altered):self.assertFalse(_active_model_role(p.type_mark))
   self.assertTrue(agent._conflicting_page_article('ZX-96D','A51417'))
   from engine.search.catalogue_producer import capture_and_publish
   explicit=self.position(model='',article='ZX-96D',name=p.name)
   with self.assertRaises(CataloguePublicationError):capture_and_publish(store,identity,explicit,'https://shop.test/product/ZX-96D',browser=None,image_store=None,capture=lambda *a,**k:self.fail('No capture on native identity mismatch'))

 def test_first_normal_capture_retained_actual_owner_carries_native_role_through_finish(self):
  from unittest.mock import MagicMock,patch
  from engine.search import agent,card_attachments
  from engine.search.catalogue_producer import capture_and_publish
  source=Path(__file__).resolve().parents[2]/'sources/kac-generator'
  parsed=json.loads((source/'full-service-2026-10-14-defer-boundary-1/parsed.json').read_text())
  p=SpecPosition.model_validate(parsed['positions'][9])
  original=json.loads((source/'owned-card-live-2026-10-14/cache/main_facts/fa6acdb9296d42879ef905526479f0ec.json').read_text())
  snapshot=json.loads(json.loads((source/'owned-card-live-2026-10-14/cache/captured_inputs/84db91d193dd4f6999901ed7279c4e2a.json').read_text())['payload'])
  url=snapshot['url'];title=original['main_title']
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);store=CatalogueStore(root/'store.sqlite3');images=CatalogueImageStore(root);identity=identity_for_position(p);a=store.acquire_unknown(identity,['favorit-el.ru'],owner='test');folder=root/'main_facts';folder.mkdir();path=folder/'source.json'
   # Retain actual source facts unchanged, using an owned allowed reference name.
   path=folder/(uuid.uuid4().hex+'.json');data=json.dumps(original,ensure_ascii=False).encode();path.write_bytes(data);ref=dict(ref=str(path.relative_to(root)),sha256=hashlib.sha256(data).hexdigest(),observed_at=original['observed_at'])
   coherent={k:v for k,v in original.items() if k not in ('version','role','requested_url')}
   page=MagicMock();page.url=page._kac_document_url=url;page._kac_response_status=200;page.inner_text.return_value=snapshot['document_body'];page.title.return_value=original['document_title'];page.locator.return_value.count.return_value=1;page.locator.return_value.first.inner_text.return_value=title
   browser=MagicMock();browser.page.return_value.__enter__.return_value=page
   finish=agent._finish_captured_card
   def observed_extraction(snapshot):
    import re
    facts=snapshot.facts();values=re.findall(r'Цена:\s*(\d[\d\s.,]*)руб\.\s*/\s*(шт|м)',facts['text'])
    self.assertEqual(len(values),1)
    amount=float(re.sub(r'\s+','',values[0][0]).replace(',','.'))
    return dict(product_title=facts['main_title'],listed_price_with_vat=amount,price_with_vat=amount,price_unit=values[0][1],matches_requested_product=True)
   def capture(browser,position,url,**kwargs):return card_attachments.capture_current(browser,position,url,extract=False,**kwargs)
   with patch.object(agent,'CACHE_DIR',root),patch.object(agent,'_render_observation',return_value=({'observation_status':'observed'},{})),patch.object(agent,'_wait_main_card_facts',return_value=(coherent,ref)),patch('engine.review_context.capture_context',return_value=({},'fixture_missing')),patch.object(agent,'_finish_captured_card',wraps=finish) as finished,patch.object(agent,'_deterministic_card_data',side_effect=observed_extraction),patch('engine.search.card_batch.extract_batch',side_effect=AssertionError('No model')):
    observation=capture_and_publish(store,identity,p,url,browser=browser,image_store=images,capture=capture,acquisition=None,supplier_host='favorit-el.ru')
   # Historical actual-source replay preserves its original date; it is not a
   # fresh acquisition timing qualification or a paid capture.
   self.assertIsNotNone(observation);self.assertEqual(observation.manufacturer_code,'51500DEK');self.assertEqual(observation.observed_at,original['observed_at']);self.assertEqual(finished.call_count,1)
   retained=list((root/'captured_inputs').glob('*.json'));self.assertEqual(len(retained),1);facts=json.loads(json.loads(retained[0].read_text())['payload']);self.assertEqual(facts['identity_evidence']['main_article'],'51500DEK');self.assertEqual(facts['page_article'],'51500DEK')
   from engine.search.product_identity import _active_model_role
   self.assertEqual(observation.image.status,'missing');self.assertFalse(_active_model_role('МТ-96D'))
   # Outside the producer scope, legacy role handling is still conservative.
   self.assertEqual(agent._different_explicit_article(p.type_mark,title,url),'rs485')
 def test_actual_retained_wrong_variant_stays_review_and_unsupported_snapshot_not_repaired(self):
  root=Path(__file__).resolve().parents[2];dom=json.loads((root/'sources/kac-generator/owned-card-dom-2026-10-14/dom.json').read_text());title=dom['headings'][0]['text'];owner=dom['declarative'][0]['text'];p=self.position(model='МТ-96D',maker='Systeme Electric',name='Мультиметр вход600В 5А RS485')
  reason=_identity_reason(dict(identity=identity_for_position(p).payload(),title=title,manufacturer_code='51417DEK',url=dom['url'],source_text=title+'\n'+owner),p)
  self.assertTrue(reason);self.assertIn('Интерфейс',reason)
  saved=json.loads((root/'sources/kac-generator/full-service-2026-10-14-defer-boundary-1/cache/captured_inputs/3bd25dbf6c5a41bc94a9daa7d123d65f.json').read_text());facts=json.loads(saved['payload']);self.assertFalse(facts['identity_evidence']);self.assertEqual(facts['readiness_kind'],'unsupported_scope')
