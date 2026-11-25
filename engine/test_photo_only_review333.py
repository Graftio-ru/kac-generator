"""Native typed photo-only outcome; no browser, provider or paid requests."""
import hashlib,json,time,unittest,uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch
from .test_document_context323 import DocumentContextTests as Fixture
from .models import PriceOffer
from .search import agent,card_attachments as ca,catalogue_producer as cp
from .search.card_batch import CardSnapshot
from .search.financial_outcome import RawFinancialRejection
from .search.failure_policy import ResearchControlError
from .search.product_identity import rendered_identity
from .search.catalogue_images import CatalogueImageStore

class PhotoOnlyReviewTests(unittest.TestCase):
 setUp=Fixture.setUp
 record=Fixture.record
 observation=Fixture.observation
 def snapshot(self):
  url=self.urls[0];stamp=time.time();title='Изделие ABC123';text=title+'\nЦена 100 руб.\nза 1 шт'
  facts=dict(url=url,requested_url=url,supplier_host='one.test',captured_at=stamp,text=text,title=title,main_title=title,page_article='ABC123',expected_type_mark='ABC123',expected_name='Изделие',expected_unit='шт',expected_qty=14,adapter={},screenshot_context='',context_capture_evidence={})
  facts['identity_evidence']=rendered_identity(url,url,title,'ABC123','ABC123','Изделие','one.test',observed_at=stamp)
  return CardSnapshot.create(facts,time.monotonic()+30)
 def offer(self,snapshot,*,review=True,quote=False):
  f=snapshot.facts();return PriceOffer(price_with_vat=100,listed_price_with_vat=100,price_unit='шт',url=f['url'],product_title=f['main_title'],unit_review_required=review,source_quote_evidence=dict(observed_at=f['captured_at'],url=f['url'],title=f['main_title'])if quote else {})
 def native(self,snapshot,offer,raw=None,control=None):
  stack=ExitStack();stack.enter_context(patch.object(agent,'CACHE_DIR',self.cache));stack.enter_context(patch.object(agent,'_capture_rendered_card',return_value=snapshot))
  def data(value,**kwargs):
   if raw is not None:kwargs['financial_outcome'](raw)
   return {'native_test_fields':True}
  stack.enter_context(patch.object(agent,'_deterministic_card_data',side_effect=data))
  if control:stack.enter_context(patch.object(agent,'_finish_captured_card',side_effect=control))
  else:stack.enter_context(patch.object(agent,'_finish_captured_card',return_value=offer))
  return stack
 def rows(self):
  with self.source._connection()as db:return {table:list(db.execute('SELECT * FROM '+table+' ORDER BY rowid'))for table in ('observations','heads','attempts')}
 def test_unit_review_retains_input_without_replacing_head_or_publishing(self):
  snapshot=self.snapshot();offer=self.offer(snapshot);before=self.rows()
  with self.native(snapshot,offer):
   result=cp.capture_and_publish(self.source,self.identity,self.pos,self.urls[0],browser=object(),image_store=CatalogueImageStore(self.cache),photo_only=True)
  self.assertIsNone(result);self.assertEqual(before,self.rows())
  record=json.loads((self.cache/'captured_inputs'/(snapshot.card_id+'.json')).read_text())
  self.assertEqual(record['payload'],snapshot.payload);self.assertEqual(record['sha256'],snapshot.sha256)
 def test_same_review_offer_is_returned_to_ordinary_native_caller(self):
  snapshot=self.snapshot();offer=self.offer(snapshot);outcomes=[]
  with self.native(snapshot,offer):retained,returned=ca.capture_current(object(),self.pos,self.urls[0],extract=False,financial_outcome=outcomes.append)
  self.assertIs(returned,offer);self.assertEqual(outcomes,[ca.CaptureFinancialResult(ca.CaptureFinancialOutcome.INSUFFICIENT)])
 def test_normal_bound_quote_still_publishes(self):
  snapshot=self.snapshot();offer=self.offer(snapshot,review=False,quote=True)
  with self.native(snapshot,offer):observed=cp.capture_and_publish(self.source,self.identity,self.pos,self.urls[0],browser=object(),image_store=CatalogueImageStore(self.cache),photo_only=True)
  self.assertIsNotNone(observed);self.assertEqual(observed.raw_amount,100);self.assertEqual(observed.sale_unit,'шт')
 def test_empty_quote_without_native_unit_review_remains_generation_error(self):
  snapshot=self.snapshot();offer=self.offer(snapshot,review=False);before=self.rows()
  with self.native(snapshot,offer),self.assertRaises(cp.CataloguePublicationError)as caught:
   cp.capture_and_publish(self.source,self.identity,self.pos,self.urls[0],browser=object(),image_store=CatalogueImageStore(self.cache),photo_only=True)
  self.assertEqual(caught.exception.code,'catalogue_generation_mismatch');self.assertEqual(before,self.rows())
 def test_raw_rejection_has_priority_and_explicit_amount_conflict_refuses(self):
  snapshot=self.snapshot();offer=self.offer(snapshot);raw=RawFinancialRejection({'listed_price_with_vat':200,'price_unit':'шт'},'native_raw_rejection');outcomes=[];before=self.rows()
  with self.native(snapshot,offer,raw):
   ca.capture_current(object(),self.pos,self.urls[0],extract=False,financial_outcome=outcomes.append)
   with self.assertRaises(cp.CataloguePublicationError)as caught:cp.capture_and_publish(self.source,self.identity,self.pos,self.urls[0],browser=object(),image_store=CatalogueImageStore(self.cache),photo_only=True)
  self.assertEqual(outcomes[0].outcome,ca.CaptureFinancialOutcome.REJECTED);self.assertIs(outcomes[0].raw_rejection,raw)
  self.assertEqual(caught.exception.code,'catalogue_source_amount_unobserved');self.assertEqual(before,self.rows())
 def test_control_failure_still_propagates_after_input_retained(self):
  snapshot=self.snapshot();offer=self.offer(snapshot);before=self.rows();error=ResearchControlError('synthetic_native_control')
  with self.native(snapshot,offer,control=error),self.assertRaises(ResearchControlError)as caught:
   cp.capture_and_publish(self.source,self.identity,self.pos,self.urls[0],browser=object(),image_store=CatalogueImageStore(self.cache),photo_only=True)
  self.assertIs(caught.exception,error);self.assertTrue((self.cache/'captured_inputs'/(snapshot.card_id+'.json')).is_file());self.assertEqual(before,self.rows())
