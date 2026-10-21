"""Known output failures are local review; account/transport/control remain strict."""
import json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock,patch
from fastapi.testclient import TestClient
from engine import llm,cloudru
from engine.telemetry import capture_metrics
from engine.search import agent
from engine.search.session import research_session,supplier_transport,PersistentSupplierHealth,SearchBudgetExceeded
from engine.search.supplier_health_store import SupplierHealthStore
from engine.search.source_checks import source_checks
from engine.parsers import spec_pdf,letters


def response(content='{}',finish='stop',status=200):
 r=MagicMock(status_code=status);r.json.return_value={'choices':[{'finish_reason':finish,'message':{'content':content}}],'usage':{'prompt_tokens':3,'completion_tokens':2}}
 return r

class ModelOutputLocalityTests(unittest.TestCase):
 def setUp(self):
  for name,value in [('llm_provider','cloudru'),('cloudru_api_key','offline'),('cloudru_proxy_server',''),('deepseek_api_key','offline')]:
   p=patch.object(llm.settings,name,value);p.start();self.addCleanup(p.stop)
 def test_invalid_then_valid_cloud_does_not_latch_and_safe_metadata_is_immutable(self):
  guard=llm.LLMRequestGuard()
  with research_session(llm_guard=guard),capture_metrics()as metrics,patch.object(cloudru,'provider_post',side_effect=[response('secretbody'),response('{"ok":true}')])as post:
   with self.assertRaises(llm.ModelOutputInvalid)as caught:llm.complete_json('secretprompt')
   self.assertEqual({'ok':True},llm.complete_json('next'))
  self.assertEqual(2,post.call_count);self.assertIsNone(guard.diagnostic());error=caught.exception
  self.assertNotIsInstance(error,llm.LLMUnavailable);self.assertEqual('stop',error.metadata.finish_reason)
  self.assertEqual('operation',error.diagnostic()['failure_scope']);self.assertNotIn('secret',json.dumps(error.diagnostic()))
  with self.assertRaises(FrozenInstanceError):error.metadata.reason='new'
  self.assertNotIn('secret',json.dumps(metrics.summary()))
 def test_account_rejection_latches_first_same_error_one_wire(self):
  guard=llm.LLMRequestGuard()
  with research_session(llm_guard=guard),patch.object(cloudru,'provider_post',return_value=response(status=402))as post:
   with self.assertRaises(llm.LLMAccountError)as a:llm.complete_json('one')
   with self.assertRaises(llm.LLMAccountError)as b:llm.complete_json('two')
  self.assertIs(a.exception,b.exception);self.assertEqual(1,post.call_count)
 def test_cloud_empty_length_envelope_and_ocr_are_known_local_outputs(self):
  for data,reason in [(response(''),'empty_output'),(response('{}','length'),'truncated_output')]:
   guard=llm.LLMRequestGuard()
   with self.subTest(reason=reason),research_session(llm_guard=guard),patch.object(cloudru,'provider_post',return_value=data):
    with self.assertRaises(llm.ModelOutputInvalid)as e:llm.complete_json('one')
    self.assertEqual(reason,e.exception.metadata.reason);self.assertIsNone(guard.diagnostic())
  with patch.object(cloudru,'provider_post',return_value=response('')),patch.object(cloudru,'_deepseek_image_block',return_value={'image_url':{}}):
   with self.assertRaises(llm.ModelOutputInvalid)as e:cloudru.recognize_page('unused')
   self.assertEqual('ocr',e.exception.metadata.output_kind)
 def test_deepseek_invalid_then_valid_and_length_metadata(self):
  guard=llm.LLMRequestGuard()
  with research_session(llm_guard=guard),patch.object(llm,'provider_post',side_effect=[response('bad','length'),response('{"ok":1}')])as post:
   with self.assertRaises(llm.ModelOutputInvalid)as e:llm._complete_deepseek('x','',False,10,None,120)
   self.assertTrue(e.exception.metadata.truncated);self.assertEqual({'ok':1},llm._complete_deepseek('x','',False,10,None,120))
  self.assertEqual(2,post.call_count);self.assertIsNone(guard.diagnostic())
 def test_anthropic_invalid_then_valid_keeps_stop_metadata(self):
  guard=llm.LLMRequestGuard();client=MagicMock();client.with_options.return_value=client
  def msg(text):return SimpleNamespace(model='offline',usage=SimpleNamespace(model_dump=lambda:{}),stop_reason='end_turn',content=[SimpleNamespace(type='text',text=text)])
  client.messages.create.side_effect=[msg('bad'),msg('{}')]
  with research_session(llm_guard=guard),patch.object(llm,'_anthropic_client',return_value=client):
   with self.assertRaises(llm.ModelOutputInvalid)as e:llm._complete_anthropic('x','',False,10,None)
   self.assertEqual('end_turn',e.exception.metadata.finish_reason);self.assertEqual({},llm._complete_anthropic('x','',False,10,None))
  self.assertEqual(2,client.messages.create.call_count);self.assertIsNone(guard.diagnostic())
 def test_programming_errors_transport_and_controls_not_benign_output(self):
  guard=llm.LLMRequestGuard()
  with research_session(llm_guard=guard),patch.object(cloudru,'provider_post',return_value=response()),patch.object(llm,'_extract_json',side_effect=RuntimeError('programmer')):
   with self.assertRaises(llm.LLMUnavailable):llm.complete_json('x')
  self.assertIsNotNone(guard.diagnostic())
  control=SearchBudgetExceeded('cancel')
  with research_session(cancelled=lambda:True),patch.object(cloudru,'provider_post')as post:
   with self.assertRaises(SearchBudgetExceeded):llm.complete_json('x')
   post.assert_not_called()
  with research_session(llm_guard=llm.LLMRequestGuard()),patch.object(cloudru,'provider_post',side_effect=control):
   with self.assertRaises(SearchBudgetExceeded)as e:llm.complete_json('x')
   self.assertIs(control,e.exception)
 def test_supplier_lease_unknown_without_outage_and_review_publication(self):
  with tempfile.TemporaryDirectory()as d:
   store=SupplierHealthStore(Path(d)/'health.sqlite');health=PersistentSupplierHealth(store,hosts=['a.ru']);guard=llm.LLMRequestGuard()
   with research_session(health,llm_guard=guard),self.assertRaises(llm.ModelOutputInvalid),supplier_transport('https://a.ru/card'):
    raise llm.ModelOutputInvalid('invalid_json')
   state=store.snapshot('a.ru');self.assertEqual('unknown',state['state']);self.assertIsNone(state['checked_at']);self.assertIsNone(state['token']);self.assertIsNone(guard.diagnostic())
  issue={**llm.ModelOutputInvalid('invalid_json').diagnostic(),'url':'https://a.ru/card'}
  checks=source_checks(['a.ru'],{'a.ru':{'cards':1}},{'diagnostics':[issue]},terminal=True)
  self.assertEqual('review',checks[0]['status']);self.assertEqual('unknown',checks[0]['product_presence'])
 def test_card_invalid_output_continues_next_card_without_quote_or_site_penalty(self):
  browser=MagicMock();page=browser.page.return_value.__enter__.return_value
  page._kac_response_status=200;page.title.return_value='Cable ABC';page.locator.return_value.first.inner_text.return_value='Cable ABC'
  error=llm.ModelOutputInvalid('invalid_json');guard=llm.LLMRequestGuard()
  with research_session(llm_guard=guard),agent.capture_card_diagnostics()as diagnostics,patch.object(agent,'_page_text_after_browser_check',return_value='Cable ABC'),patch.object(agent,'_extract_from_page',side_effect=[error,None])as extract,patch.object(agent,'accepted')as accept:
   offers,_=agent._collect_offers(browser,['https://a.ru/card','https://b.ru/card'],3,'ABC','Cable','м')
  self.assertEqual(2,extract.call_count);self.assertFalse(offers);accept.assert_not_called();self.assertIsNone(guard.diagnostic());self.assertEqual('model_output_invalid',diagnostics[0]['code']);self.assertNotIn('site_error',[x['code']for x in diagnostics])
 def test_semantic_and_page_helpers_propagate_output_invalid(self):
  from engine.models import SpecPosition
  error=llm.ModelOutputInvalid('invalid_json')
  with patch.object(agent,'complete_json',side_effect=error):
   for action in [lambda:agent.build_queries(SpecPosition(number=1,name='Cable')),lambda:agent._extract_from_page('body')]:
    with self.assertRaises(llm.ModelOutputInvalid)as e:action()
    self.assertIs(error,e.exception)
 def test_ocr_batch_and_cell_invalid_fail_before_partial_ingestion(self):
  from engine.cell_transcription import transcribe_sheet
  error=llm.ModelOutputInvalid('invalid_json',output_kind='ocr')
  with patch.object(cloudru,'recognize_page',side_effect=['page1',error])as ocr,patch.object(spec_pdf,'_structure_positions')as structure:
   with self.assertRaises(llm.ModelOutputInvalid):spec_pdf._cloudru_ocr_positions(['one','two'])
   self.assertEqual(2,ocr.call_count);structure.assert_not_called()
  with patch.object(cloudru,'complete_json',return_value={'cells':[{'id':'a','state':'read','text':'x'}]}):
   with self.assertRaises(llm.ModelOutputInvalid):transcribe_sheet('unused',['a','b'])
 def test_spec_and_letter_known_schema_invalid_fail_closed(self):
  with patch.object(spec_pdf,'complete_json',return_value={'positions':[{'number':1},None]}):
   with self.assertRaises(llm.ModelOutputInvalid):spec_pdf._structure_positions(text='x')
  doc=MagicMock();doc.page_count=1;doc.__getitem__.return_value.get_text.return_value='letter'
  with patch.object(letters.fitz,'open',return_value=doc),patch.object(letters,'complete_json',return_value={'items':[{'name':'Good','price_with_vat':1},None]}):
   with self.assertRaises(llm.ModelOutputInvalid):letters.parse_letter('x')
 def test_public_parse_invalid_source_zero_positions_next_source_continues(self):
  from engine import app as module
  from engine.models import SpecPosition
  error=llm.ModelOutputInvalid('invalid_json')
  with tempfile.TemporaryDirectory()as d,patch.object(module,'UPLOAD_DIR',Path(d)),patch.object(module,'parse_spec',side_effect=[error,[SpecPosition(number=2,name='Good')]]),patch.object(module,'_start_research_job',return_value='job')as start:
   result=TestClient(module.app).post('/parse',files=[('specs',('bad.xlsx',b'bad')),('specs',('good.xlsx',b'good'))])
  self.assertEqual(200,result.status_code);data=result.json();self.assertEqual([2],[p['number']for p in data['positions']]);self.assertEqual(['review','done'],[o['status']for o in data['source_outcomes']]);self.assertEqual(0,data['source_outcomes'][0]['position_count']);self.assertEqual('model_output_invalid',data['source_outcomes'][0]['code']);self.assertEqual(1,len(start.call_args.args[0]))

 def test_optional_tsn_base_and_requisites_never_fallback_from_invalid_output(self):
  from engine.tsn import local_db,mos_ru
  from engine.search import base_db
  from engine.requisites import supplier
  from engine.models import SpecPosition
  error=llm.ModelOutputInvalid('invalid_json');browser=MagicMock();browser.page.return_value.__enter__.return_value.inner_text.return_value='Company details'
  with patch.object(local_db,'_relevant_fragment',return_value='text'),patch.object(local_db,'complete_json',side_effect=error),patch.object(mos_ru,'complete_json',side_effect=error),patch.object(base_db,'complete_json',side_effect=error):
   for action in [lambda:local_db.lookup_tsn_price('Cable'),lambda:mos_ru.lookup_tsn_price(browser,'Cable'),lambda:mos_ru.fetch_current_coefficient(browser),lambda:base_db._llm_match_key(SpecPosition(number=1,name='Cable electrical'),{'key':{'name':'Cable electrical'}})]:
    with self.assertRaises(llm.ModelOutputInvalid)as e:action()
    self.assertIs(error,e.exception)
  with patch.object(supplier,'complete_json',side_effect=error),patch.object(supplier,'_cache',{}):
   with self.assertRaises(llm.ModelOutputInvalid):supplier.fetch_requisites_from_site(browser,'https://a.ru/card')
 def test_unknown_finish_is_sanitized_without_raw_content_and_length_remains_local(self):
  for finish in ['secret_finish']:
   with patch.object(cloudru,'provider_post',return_value=response('bad',finish)):
    with self.assertRaises(llm.ModelOutputInvalid)as e:llm.complete_json('x')
    self.assertEqual('unknown',e.exception.metadata.finish_reason);self.assertNotIn('secret',json.dumps(e.exception.diagnostic()))
 def test_independent_vision_invalid_output_discards_the_affected_batch(self):
  primary={'number':1,'name':'Item','unit':'шт','qty':1}
  error=llm.ModelOutputInvalid('invalid_json')
  from contextlib import nullcontext
  with patch.object(spec_pdf.settings,'cloudru_vision_verify',True),patch.object(spec_pdf,'_structure_positions',side_effect=[[primary],error]),patch('engine.vision_views.page_bands',return_value=nullcontext(['fragment'])):
   with self.assertRaises(llm.ModelOutputInvalid)as e:spec_pdf._cloudru_page_positions('x')
   self.assertIs(error,e.exception)

 def test_http200_invalid_api_envelope_is_local_without_automatic_replay(self):
  for payload in (None,{}, {'choices':[]}, {'choices':[{'message':{}}]}, {'choices':[{'message':{'content':None}}]}):
   guard=llm.LLMRequestGuard();r=response();r.json.return_value=payload
   with self.subTest(payload=payload),research_session(llm_guard=guard),patch.object(cloudru,'provider_post',side_effect=[r,response('{}')])as post:
    with self.assertRaises(llm.LLMOperationFailed)as first:llm.complete_json('x')
    self.assertEqual(1,post.call_count);self.assertIsNone(guard.diagnostic())
    self.assertEqual({},llm.complete_json('next'));self.assertEqual(2,post.call_count)
  for provider in ['cloudru','deepseek']:
   guard=llm.LLMRequestGuard();r=response();r.json.side_effect=ValueError('secretbody')
   with research_session(llm_guard=guard),patch.object(llm.settings,'llm_provider',provider),patch.object(cloudru if provider=='cloudru' else llm,'provider_post',return_value=r):
    with self.assertRaises(llm.LLMUnavailable)as error:llm.complete_json('x')
    self.assertNotIn('secret',str(error.exception));self.assertIsNone(guard.diagnostic())

 def test_explicit_refusal_and_corrupt_content_are_operation_local_for_shared_adapters(self):
  for provider in ['cloudru','deepseek']:
   refused=response();refused.json.return_value['choices'][0]['message']={'content':None,'refusal':'secret reason'}
   guard=llm.LLMRequestGuard()
   with research_session(llm_guard=guard),patch.object(llm.settings,'llm_provider',provider),patch.object(cloudru if provider=='cloudru' else llm,'provider_post',side_effect=[refused,response('{}')])as post:
    with self.assertRaises(llm.ModelOutputInvalid)as e:llm.complete_json('x')
    self.assertEqual('refused_output',e.exception.metadata.reason);self.assertNotIn('secret',json.dumps(e.exception.diagnostic()));self.assertEqual({},llm.complete_json('next'));self.assertEqual(2,post.call_count)
   guard=llm.LLMRequestGuard()
   with research_session(llm_guard=guard),patch.object(llm.settings,'llm_provider',provider),patch.object(cloudru if provider=='cloudru' else llm,'provider_post',side_effect=[response(3,'length'),response('{}')])as post:
    with self.assertRaises(llm.LLMOperationFailed)as first:llm.complete_json('x')
    self.assertEqual(1,post.call_count);self.assertIsNone(guard.diagnostic())
    self.assertEqual({},llm.complete_json('next'));self.assertEqual(2,post.call_count)

 def test_mixed_model_rows_never_silently_ingest_valid_prefix(self):
  payload={'positions':[{'number':1,'name':'Cable','unit':'м','qty':1},{'name':'Heading'}]}
  with patch.object(spec_pdf,'complete_json',return_value=payload):
   raw=spec_pdf._structure_positions(text='spec')
  with patch.object(spec_pdf,'_raw_positions',return_value=raw):
   with self.assertRaises(llm.ModelOutputInvalid):spec_pdf.parse_spec('unused.xlsx')

if __name__=='__main__':unittest.main()
