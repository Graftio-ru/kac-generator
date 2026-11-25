"""Native retained-card finish: maintained authority, no remote requisites calls."""
from pathlib import Path
import tempfile,time,unittest
from contextlib import ExitStack
from unittest.mock import patch
import httpx
from engine import config,llm
from engine.search import agent,http_card,whitelist,product_identity,card_batch,session
from engine.models import SpecPosition
from engine.offer_selection import offer_is_comparable
from engine.test_http_card import html,URL
class DirectoryRequisitesTests(unittest.TestCase):
 def finish(self,*,directory=True,merchant=False,conflict=False,enabled=True,mutate='',remove_after_capture=False,directory_name='ООО Supplier',directory_inn='7842224734'):
  with tempfile.TemporaryDirectory()as d,ExitStack()as stack:
   root=Path(d);table=root/'Поставщики.md';table.write_text(f'| {directory_name} | | {directory_inn} | https://supplier.example |\n'if directory else '',encoding='utf-8')
   for module,key,value in [(config,'CACHE_DIR',root),(agent,'CACHE_DIR',root),(http_card,'CACHE_DIR',root),(whitelist,'SUPPLIERS_FILE',table)]:stack.enter_context(patch.object(module,key,value))
   if directory:self.assertIn('supplier.example',whitelist.whitelist_hosts())
   body=html()
   if not merchant:
    start=body.index('<div itemprop="seller"');end=body.index('</div>',start)+6;body=body[:start]+body[end:]
   if conflict:body=body.replace('7842224734','7701677700')
   record,source=http_card.save_response(httpx.Response(200,text=body,headers={'content-type':'text/html'},request=httpx.Request('GET',URL)),URL)
   owned=http_card.read_source(source)[1]
   ident=product_identity.http_identity(record,owned,source,'ABC123','Cable','supplier.example')
   facts=dict(url=URL,requested_url=URL,supplier_host='supplier.example',expected_type_mark='ABC123',expected_name='Cable',expected_unit='м',expected_qty=1,text=owned['text'],title=owned['title'],main_title=owned['title'],page_article=owned['article'],identity_evidence=ident,adapter={},roll_length=None,captured_at=record['observed_at'],screenshot_context='',context_capture_evidence={},context_capture_review_reason='Not selected for capture',http_source=source,merchant_requisites=owned.get('merchant_requisites',{}))
   snap=card_batch.CardSnapshot.create(facts,time.monotonic()+10)
   data=agent._deterministic_card_data(snap);self.assertIsNotNone(data)
   data['seller_inn']='7701677700'  # Model hint never substitutes missing trusted authority.
   if mutate=='unit':data['price_unit']='упак'
   if mutate=='model':data['product_title']='Cable OTHER999'
   if remove_after_capture:table.write_text('')
   remote=[]
   def deny(*a,**kw):remote.append(True);raise AssertionError('Per-card remote requisites/browser/model forbidden')
   for module,name in [(agent,'fetch_requisites'),(agent,'fetch_requisites_from_site'),(agent,'supplier_http_get'),(llm,'complete_json'),(httpx.Client,'send')]:stack.enter_context(patch.object(module,name,side_effect=deny))
   class Browser:
    page=staticmethod(deny)
   with session.research_session(deadline=time.monotonic()+10),agent.capture_card_diagnostics()as diagnostics:
    result=agent._finish_captured_card(Browser(),snap,data,enrich_seller=enabled)
   self.assertFalse(remote)
   comparable=bool(result and offer_is_comparable(result,SpecPosition(number=1,name='Cable',type_mark='ABC123',unit='м')))
   return result,list(diagnostics),comparable
 def test_complete_frozen_directory_no_remote_both_compatible_flags(self):
  for enabled in (True,False):
   result,_,comparable=self.finish(enabled=enabled)
   self.assertEqual(result.seller_evidence['decision']['status'],'reference_accepted')
   self.assertEqual(result.requisites.inn,'7842224734');self.assertTrue(comparable)
 def test_missing_directory_preserves_facts_and_actionable_final_reason(self):
  result,diagnostics,comparable=self.finish(directory=False)
  self.assertEqual(result.price_with_vat,120);self.assertEqual(result.page_article,'ABC123')
  self.assertEqual(result.seller_evidence['decision']['status'],'unknown')
  self.assertIn('Поставщики.md',result.price_capture_review_reason);self.assertIn('ИНН',result.price_capture_review_reason)
  self.assertFalse(comparable);self.assertTrue(any('Поставщики.md'in v.get('message',v.get('reason',''))for v in diagnostics))
 def test_actual_owned_merchant_conflict_not_relabelled_missing_directory(self):
  result,_,comparable=self.finish(merchant=True,conflict=True)
  self.assertEqual(result.seller_evidence['decision']['status'],'conflict');self.assertFalse(comparable)
  self.assertIn('противореч',result.price_capture_review_reason);self.assertNotIn('Поставщики.md',result.price_capture_review_reason)
 def test_genuine_source_bound_merchant_remains_authoritative(self):
  result,_,comparable=self.finish(directory=False,merchant=True)
  self.assertEqual(result.seller_evidence['decision']['status'],'role_confirmed');self.assertTrue(comparable)
 def test_captured_directory_authority_not_replaced_by_live_list(self):
  result,_,comparable=self.finish(remove_after_capture=True)
  self.assertEqual(result.seller_evidence['decision']['status'],'reference_accepted');self.assertTrue(comparable)
 def test_model_and_unit_guards_remain_strict(self):
  result,_,comparable=self.finish(mutate='model');self.assertIsNone(result)
  result,_,comparable=self.finish(mutate='unit');self.assertFalse(comparable)

 def test_partial_directory_rows_keep_host_and_actionable_native_candidate(self):
  for name,inn in [('', '7842224734'),('   ','7842224734'),('ООО Supplier',''),('ООО Supplier','123')]:
   with self.subTest(name=name,inn=inn):
    result,_,comparable=self.finish(directory_name=name,directory_inn=inn)
    self.assertEqual(result.price_with_vat,120);self.assertEqual(result.page_article,'ABC123')
    self.assertFalse(comparable);self.assertEqual(result.seller_evidence['decision']['status'],'unknown')
    self.assertIn('Поставщики.md',result.price_capture_review_reason)
