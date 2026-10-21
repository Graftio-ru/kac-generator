"""Financial ranking survives optional attachment failures; forged evidence refuses."""
import hashlib,time,uuid,tempfile,unittest
from pathlib import Path
from io import BytesIO
from unittest.mock import patch,MagicMock
from dataclasses import replace
from PIL import Image
from engine.models import SpecPosition
from engine.schemas import PositionIn,ResearchReportRequest
from engine.search.catalogue import CatalogueStore,SourceObservation,ImageEvidence,identity_for_position,CatalogueConflict
from engine.search.catalogue_projection import catalogue_scope,project_product
from engine.search.selected_proofs import proof_ready,final_offers,capture_selected_proofs,PROOF_FAILURE
class SelectedProofTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.store=CatalogueStore(self.root/'catalogue.sqlite3');self.p=SpecPosition(number=1,name='Изделие',type_mark='ABC123',unit='шт',qty=12);self.identity=identity_for_position(self.p);self.hosts=[f's{i}.test' for i in range(5)];self.admission=self.store.acquire_unknown(self.identity,self.hosts,owner='fixture');self.sources={}
  self.cache=patch('engine.config.CACHE_DIR',self.root);self.cache.start();self.addCleanup(self.cache.stop)
 def source(self,index,price,*,photo=False):
  url=f'https://s{index}.test/product/ABC123';stamp=time.time();oid=uuid.uuid4().hex;title='Изделие ABC123';text=f'{title}\nАртикул: ABC123\nЦена {price} руб. с НДС\nза 1 шт'
  image=ImageEvidence(oid,url,stamp,'missing',error='not_selected_price_observation')
  if photo:
   path=self.root/(oid+'.png');Image.new('RGB',(20,10),'white').save(path);raw=path.read_bytes();sha=hashlib.sha256(raw).hexdigest();context=dict(version=1,role='review_context',filename=path.name,sha256=sha,url=url,captured_at=stamp,width=20,height=10,document_width=20,document_height=10,truncated=False)
   image=ImageEvidence(oid,url,stamp,'captured',local_path=path.name,sha256=sha,size=len(raw),captured_at=stamp,context_evidence=context)
  source=SourceObservation(oid,self.identity,self.hosts[index],url,url,stamp,'supplier_rendered',title,'ABC123',text,hashlib.sha256(text.encode()).hexdigest(),price,'RUB','included','шт',1,None,'','in_stock','observed',f'Seller {index}',['7707083893','7842224734','7708503727','7812014560','7702070139'][index],image,'catalogue-v1')
  self.store.append_observation(self.identity.key,source,acquisition=self.admission,coverage_status='found');self.sources[url]=source;return source
 def result(self):return project_product(self.store,self.identity,self.p)
 def test_price_only_matrix_preserved_and_client_photo_flag_cannot_qualify(self):
  self.source(0,100)
  with catalogue_scope(self.store):
   result=self.result();self.assertEqual(len(result.offers),1);self.assertEqual([o.price_with_vat for o in final_offers(result)],[100])
   forged=result.offers[0].model_copy(update={'screenshot_context':'fake.png','catalogue_image':{'status':'captured'}});self.assertFalse(proof_ready(forged,self.p))
  from engine import app
  with patch.object(app,'_catalogue_store',return_value=self.store):row=app._catalogue_known_row(PositionIn.model_validate(self.p.model_dump()))
  self.assertEqual(row['found'],1);self.assertEqual(len(row['candidates']),1);self.assertEqual(len(row['offers']),1)
 def test_three_highest_independent_current_images_only_and_old_image_not_reused(self):
  for i in range(5):self.source(i,(i+1)*100,photo=True)
  with catalogue_scope(self.store):self.assertEqual([o.price_with_vat for o in final_offers(self.result())],[300,400,500])
  self.source(4,600,photo=False)
  with catalogue_scope(self.store):self.assertEqual([o.price_with_vat for o in final_offers(self.result())],[300,400,600])
 def test_changed_price_reselects_new_pool_each_url_once(self):
  for i in range(5):self.source(i,(i+1)*100)
  calls=[]
  def capture(store,identity,p,url,**kwargs):
   calls.append(url);i=self.hosts.index(url.split('/')[2]);return self.source(i,50 if i==4 else (i+1)*100,photo=True)
  with patch('engine.search.catalogue_producer.capture_and_publish',side_effect=capture):report=capture_selected_proofs(self.store,[self.p],browser=MagicMock(),image_store=MagicMock(),acquisition=self.admission)
  self.assertEqual(len(calls),4);self.assertEqual(len(set(calls)),4);self.assertEqual(report['positions'][0]['selected'],3)
  with catalogue_scope(self.store):self.assertEqual([o.price_with_vat for o in final_offers(self.result())],[200,300,400])
 def test_failed_attachments_keep_highest_prices_without_replacement(self):
  for i in range(5):self.source(i,(i+1)*100)
  calls=[]
  def fail(*args,**kwargs):calls.append(args[3]);return None
  with patch('engine.search.catalogue_producer.capture_and_publish',side_effect=fail):report=capture_selected_proofs(self.store,[self.p],browser=MagicMock(),image_store=MagicMock(),acquisition=self.admission)
  self.assertEqual(len(calls),3);self.assertEqual(len(set(calls)),3);self.assertEqual(report['positions'][0]['shortfall'],0);self.assertEqual(report['positions'][0]['attachment_shortfall'],3)
  with catalogue_scope(self.store):self.assertEqual([o.price_with_vat for o in final_offers(self.result())],[300,400,500])
  self.assertTrue(all(a['reason'].startswith(PROOF_FAILURE) for a in self.store.lookup(self.identity)['refresh_attempts']))
 def test_photo_hash_corruption_and_foreign_position_rejected(self):
  source=self.source(0,100,photo=True)
  with catalogue_scope(self.store):
   offer=self.result().offers[0];self.assertTrue(proof_ready(offer,self.p));self.assertFalse(proof_ready(offer,self.p.model_copy(update={'type_mark':'DIFFERENT'})))
   (self.root/source.image.local_path).write_bytes(b'bad');self.assertFalse(proof_ready(offer,self.p))
 def test_lease_revoke_before_attempt_publication_zero_write(self):
  source=self.source(0,100);self.store.cancel_acquisition(self.admission,reason='fixture cancellation')
  before=self.store.lookup(self.identity)['refresh_attempts']
  with self.assertRaises(CatalogueConflict):self.store.record_refresh_attempt(self.identity.key,self.hosts[0],source.url,attempted_at=time.time(),outcome='unknown',reason=PROOF_FAILURE+'failed',acquisition=self.admission)
  self.assertEqual(self.store.lookup(self.identity)['refresh_attempts'],before)
 def test_pause_before_proof_has_zero_capture_and_no_financial_mutation(self):
  self.source(0,100);before=self.store.lookup(self.identity)
  with patch('engine.search.catalogue_producer.capture_and_publish',side_effect=AssertionError('capture prohibited')):report=capture_selected_proofs(self.store,[self.p],browser=MagicMock(),image_store=MagicMock(),acquisition=self.admission,paused=lambda:True)
  self.assertTrue(report['paused']);self.assertEqual(self.store.lookup(self.identity),before)
 def test_normal_unknown_collection_then_finite_selected_pass_and_known_read_no_calls(self):
  from engine.test_catalogue_unknown import CatalogueUnknownTests
  from engine.search.card_attachments import collect_current
  fixture=CatalogueUnknownTests();fixture.setUp();self.addCleanup(fixture.doCleanups);modes=[]
  def capture(*args,**kwargs):
   modes.append(getattr(kwargs.get('capture'),'func',None));return fixture.capture(*args,**kwargs)
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=24)
  job,public=fixture.job([p],capture=capture)
  self.assertEqual(job['status'],'done',job.get('error'));self.assertEqual(modes,[collect_current,None]);self.assertEqual(public['rows'][0]['found'],1);self.assertEqual(len(public['rows'][0]['candidates']),1)
  self.assertTrue(job['catalogue_selected_proofs']);old=fixture.store.lookup(identity_for_position(p))
  fixture.job([p],search=lambda *a,**k:(_ for _ in ()).throw(AssertionError('known search')),capture=lambda *a,**k:(_ for _ in ()).throw(AssertionError('known photo')))
  self.assertEqual(fixture.store.lookup(identity_for_position(p)),old)
 def test_normal_photo_free_capture_preserves_raw_generation_without_screenshot_call(self):
  from engine.test_semantic_purchase_capture import SemanticCaptureTests
  from engine.search import card_attachments,agent
  fixture=SemanticCaptureTests();fixture.setUp();self.addCleanup(fixture.doCleanups);native=card_attachments.capture_current;calls=[];render=agent._capture_rendered_card
  def mode(*args,**kwargs):return native(*args,photos=False,**kwargs)
  def observed(*args,**kwargs):calls.append(card_attachments.capturing_attachment());return render(*args,**kwargs)
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=24).to_result().position
  with patch.object(card_attachments,'capture_current',side_effect=mode),patch.object(agent,'_capture_rendered_card',side_effect=observed):store,identity,source=fixture.run_capture(fixture.frame(),p)
  self.assertEqual(calls,[False]);self.assertEqual(source.raw_amount,100);self.assertEqual(source.sale_unit,'м');self.assertEqual(source.image.status,'missing')
 def test_default_capture_remains_photo_enabled(self):
  from engine.test_semantic_purchase_capture import SemanticCaptureTests
  from engine.search import card_attachments,agent
  fixture=SemanticCaptureTests();fixture.setUp();self.addCleanup(fixture.doCleanups);render=agent._capture_rendered_card;calls=[]
  def observed(*args,**kwargs):calls.append(card_attachments.capturing_attachment());return render(*args,**kwargs)
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=24).to_result().position
  with patch.object(agent,'_capture_rendered_card',side_effect=observed):fixture.run_capture(fixture.frame(),p)
  self.assertEqual(calls,[True])
 def test_explicit_daily_refresh_collects_all_prices_then_proves_selected(self):
  from engine.search.selected_proofs import refresh_prices_and_selected_proofs
  from engine.search.card_attachments import collect_current
  with patch('engine.search.catalogue_producer.refresh_recorded',return_value=['price']) as refresh,patch('engine.search.selected_proofs.capture_selected_proofs',return_value={'paused':False}) as proofs:
   result=refresh_prices_and_selected_proofs(self.store,[self.p],browser=MagicMock(),image_store=MagicMock())
  self.assertIs(refresh.call_args.kwargs['capture'],collect_current);self.assertEqual(proofs.call_args.args[1],[self.p]);self.assertEqual(result['prices'],['price'])
 def test_source_intent_closure_and_financial_lineage_preserved(self):
  from engine.search.acquisition_source_intent import attest,components,validate,IntentInvalid
  from engine.search.acquisition_recovery import validate_lineage
  sources=components(Path(__file__).parent);intent=attest({'fixture':'route'},sources);self.assertEqual(intent['version'],'acquisition-source-intent-v6')
  for path in ['search/selected_proofs.py','pipeline.py','builders/research_report_xlsx.py','run_catalogue_refresh.py']:
   self.assertIn(path,sources)
   changed={**sources,path:'f'*64};self.assertIn(path,validate_lineage(intent,{'fixture':'route'},changed,intent['sha256']))
  with self.assertRaises(IntentInvalid):validate_lineage(intent,{'fixture':'route'},{**sources,'source_quote.py':'f'*64},intent['sha256'])
 def test_actual_report_keeps_every_raw_price_but_selects_only_proof_ready(self):
  from engine import app
  from engine.builders.research_report_xlsx import build_research_report
  from openpyxl import load_workbook
  for i in range(5):self.source(i,(i+1)*100,photo=i in (2,3))
  row=app._catalogue_known_row(PositionIn.model_validate(self.p.model_dump()),store=self.store)
  self.assertEqual(len(row['candidates']),5);self.assertEqual(len(row['offers']),3)
  request=ResearchReportRequest(positions=[dict(self.p.model_dump(),offers=row['offers'],candidates=row['candidates'],source_checks=row['source_checks'])])
  with catalogue_scope(self.store):book=load_workbook(BytesIO(build_research_report(request)))
  sheet=book['Предложения'];self.assertEqual(sheet.max_row,6);self.assertEqual(sorted(sheet.cell(i,7).value for i in range(2,7)),[100,200,300,400,500])
  selected_header=next(c.column for c in sheet[1] if c.value=='Для сметы')
  self.assertEqual(sum(sheet.cell(i,selected_header).value=='Выбрано предварительно' for i in range(2,7)),3)
 def test_final_adapter_cannot_silently_reselect_unphotographed_fourth_source(self):
  from engine.search.selected_proofs import final_result
  from engine.final_eligibility import prepare_final_results
  for i in range(4):self.source(i,(i+1)*100,photo=i in (1,2))
  with catalogue_scope(self.store):
   raw=self.result();final=final_result(raw);self.assertEqual(len(raw.all_offers),4);self.assertEqual(len(final.all_offers),3)
   prepare_final_results([final]);self.assertEqual([o.price_with_vat for o in final.offers],[200,300,400])
 def test_cancel_priority_before_photo_does_not_publish_proof_attempt(self):
  self.source(0,100);before=self.store.lookup(self.identity)
  from engine.search.failure_policy import ResearchControlError
  with patch('engine.search.catalogue_producer.capture_and_publish',side_effect=AssertionError('photo forbidden')):
   with self.assertRaises(ResearchControlError):capture_selected_proofs(self.store,[self.p],browser=MagicMock(),image_store=MagicMock(),control=lambda:(_ for _ in ()).throw(ResearchControlError()))
  self.assertEqual(self.store.lookup(self.identity),before)

 def test_failed_highest_successful_replacements_report_and_final_agree(self):
  from engine.search.selected_proofs import selected_for_report
  for i in range(5):self.source(i,(i+1)*100)
  def capture(store,identity,p,url,**kwargs):
   i=self.hosts.index(url.split('/')[2])
   return None if i==4 else self.source(i,(i+1)*100,photo=True)
  with patch('engine.search.catalogue_producer.capture_and_publish',side_effect=capture):report=capture_selected_proofs(self.store,[self.p],browser=MagicMock(),image_store=MagicMock(),acquisition=self.admission)
  self.assertEqual(report['positions'][0]['selected'],3)
  frozen=self.store.lookup(self.identity)
  with catalogue_scope(self.store,snapshot=frozen):
   result=self.result();self.assertEqual(len(result.all_offers),5)
   expected=[300,400,500]
   self.assertEqual([o.price_with_vat for o in final_offers(result)],expected)
   self.assertEqual([o.price_with_vat for o in selected_for_report(result.all_offers,self.p)],expected)
   # DTO attempts cannot suppress an actual source or create a replacement.
   result.catalogue_refresh_attempts=[]
   self.assertEqual([o.price_with_vat for o in final_offers(result)],expected)
  self.source(4,600,photo=True)
  with catalogue_scope(self.store,snapshot=frozen):self.assertEqual([o.price_with_vat for o in selected_for_report(result.all_offers,self.p)],expected)
  with catalogue_scope(self.store):self.assertEqual([o.price_with_vat for o in final_offers(self.result())],[300,400,600])

 def test_unique_letter_no_seller_survives_original_validator_without_catalogue_lookup(self):
  from engine.models import PriceOffer,PositionResult
  from engine.search.selected_proofs import final_result,selected_for_report
  from engine.final_eligibility import prepare_final_results
  position=self.p.model_copy(update={'is_unique':True});letter=PriceOffer(price_with_vat=10,from_letter=True)
  result=PositionResult(position=position,offers=[letter],all_offers=[letter])
  with patch('engine.search.selected_proofs._server_attempts',side_effect=AssertionError('legacy catalogue lookup')),patch('engine.search.base_db.apply_refresh',side_effect=lambda *args:None):
   self.assertIs(final_result(result),result);prepare_final_results([final_result(result)])
   self.assertEqual(result.offers,[letter]);selected_for_report([letter],position)
 def test_missing_graftio_own_price_policy_unchanged_and_mixed_legacy_pool_not_trimmed(self):
  from engine.models import PriceOffer,PositionResult
  from engine.search.selected_proofs import final_result
  from engine.equipment_output_policy import missing_own_price
  position=self.p.model_copy(update={'name':'Оборудование Графтио','is_unique':True,'source_evidence':{'reader':'native_pdf','cells':{'number':'1','name':'Оборудование Графтио','model':'ABC123','maker':'Графтио'}}})
  result=PositionResult(position=position,offers=[],all_offers=[])
  with patch('engine.search.selected_proofs._server_attempts',side_effect=AssertionError('no catalogue lookup')):
   self.assertIs(final_result(result),result);self.assertTrue(missing_own_price(position,result.offers))
  self.source(0,100)
  with catalogue_scope(self.store):
   raw=self.result();legacy=[PriceOffer(price_with_vat=amount,url='https://legacy'+str(amount)+'.test/card') for amount in (10,20,30,40)]
   mixed=raw.model_copy(update={'all_offers':legacy+raw.all_offers,'offers':legacy})
   filtered=final_result(mixed);self.assertEqual(filtered.all_offers[:4],legacy);self.assertEqual(filtered.offers[:4],legacy);self.assertEqual(filtered.offers[-1].price_with_vat,100)

class OptionalAttachmentRegressions(unittest.TestCase):
 setUp=SelectedProofTests.setUp
 source=SelectedProofTests.source
 result=SelectedProofTests.result
 def test_projected_then_deleted_bytes_preserves_price_and_strips_output_only(self):
  from engine.offer_selection import offer_is_comparable
  source=self.source(0,100,photo=True)
  with catalogue_scope(self.store):
   original=self.result();offer=original.offers[0];path=Path(offer.screenshot_context);path.unlink()
   self.assertTrue(offer_is_comparable(offer,self.p));self.assertFalse(proof_ready(offer,self.p))
   final=final_offers(original)[0];self.assertEqual(final.price_with_vat,100);self.assertFalse(final.screenshot_context);self.assertTrue(final.context_capture_review_reason);self.assertEqual(offer.screenshot_context,str(path))
   for update in [{'screenshot_context':'forged.png'},{'price_with_vat':999},{'price_unit':'м'},{'catalogue_source_sha256':'0'*64}]:
    self.assertFalse(offer_is_comparable(offer.model_copy(update=update),self.p))
 def test_corrupt_bytes_are_not_attached_and_seller_forgery_still_refuses(self):
  from engine.offer_selection import offer_is_comparable
  source=self.source(0,100,photo=True)
  with catalogue_scope(self.store):
   offer=self.result().offers[0];Path(offer.screenshot_context).write_bytes(b'corrupt')
   self.assertTrue(offer_is_comparable(offer,self.p));self.assertFalse(final_offers(self.result())[0].screenshot_context)
   altered=offer.model_copy(update={'requisites':offer.requisites.model_copy(update={'inn':'7842224734'})})
   self.assertFalse(offer_is_comparable(altered,self.p))
 def test_optional_supplier_deferral_does_not_pause_or_replace_prices(self):
  from engine.search.session import SupplierDeferred
  for i in range(3):self.source(i,(i+1)*100)
  failure=SupplierDeferred({'retry_at':time.time()+60,'reason':'synthetic supplier defer'})
  with patch('engine.search.catalogue_producer.capture_and_publish',side_effect=failure) as capture:
   report=capture_selected_proofs(self.store,[self.p],browser=MagicMock(),image_store=MagicMock(),acquisition=self.admission)
  self.assertFalse(report['paused']);self.assertEqual(capture.call_count,3);self.assertEqual(report['positions'][0]['selected'],3);self.assertEqual(report['positions'][0]['attachment_shortfall'],3)
