"""Canonical PUBLIC output alone preserves native identity and export accounting."""
import asyncio,json,tempfile,unittest,uuid
from pathlib import Path
from io import BytesIO
from unittest.mock import patch
from openpyxl import load_workbook
from engine import app
from engine.schemas import PositionIn,ResearchReportRequest
from engine.search.catalogue import CatalogueStore,identity_for_position
from engine.equipment_output_policy import is_graftio_equipment

class CataloguePublicRoundtripTests(unittest.TestCase):
 def test_actual_native96_public_to_export_without_external_position_merge(self):
  source=Path(__file__).resolve().parents[2]/'sources/kac-generator/native-document-notes-2026-10-13/root-native-visual-rework/input.json'
  positions=[PositionIn.model_validate(p) for p in json.loads(source.read_text())['positions']]
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);store=CatalogueStore(root/'catalogue.sqlite3')
   from engine.search.whitelist import whitelist_hosts,fallback_hosts
   hosts=list(dict.fromkeys(whitelist_hosts()+fallback_hosts()));self.assertEqual(len(hosts),36)
   for p in positions:
    if not is_graftio_equipment(p):
     admission=store.acquire_unknown(identity_for_position(p),hosts,owner='fixture')
     if admission.allowed:store.finish_acquisition(admission,error='No prices in offline fixture')
   key=uuid.uuid4().hex[:12];app.RESEARCH_JOBS[key]=dict(job_id=key,rows=[None]*96,status='pending',total=96,completed=0,configured_hosts=hosts);app._RESEARCH_INPUTS[key]=positions
   try:
    with patch.object(app,'_catalogue_store',return_value=store),patch.object(app,'RESEARCH_JOB_DIR',root/'jobs'),patch.object(app,'_browser_factory',side_effect=AssertionError('No browser')),patch('engine.llm.LLMRequestGuard',side_effect=AssertionError('No model')),patch('engine.search.base_db.apply_refresh',side_effect=AssertionError('No legacy overlay')):
     app._save_research_job(key);app._run_research_job(key);public=app._research_store().public(key);rows=public['rows']
     self.assertEqual(len(rows),96);self.assertEqual(sum(is_graftio_equipment(PositionIn.model_validate(p)) for p in rows),10);self.assertEqual(sum(len(p['source_checks']) for p in rows),3096)
     for original,row in zip(positions,rows):
      self.assertEqual(row['source_evidence']['cells'],original.source_evidence['cells']);self.assertEqual(row['full_name'],original.full_name)
      self.assertEqual(identity_for_position(PositionIn.model_validate(row)).key,identity_for_position(original).key)
     restored=app._research_store().runtime_snapshot(key)['rows'];self.assertEqual(restored[22]['source_evidence']['cells'],rows[22]['source_evidence']['cells'])
     request=ResearchReportRequest(positions=rows)
     async def data(response):return b''.join([part async for part in response.body_iterator])
     book=load_workbook(BytesIO(asyncio.run(data(app.research_report(request)))))
     self.assertEqual(book['Позиции × сайты'].max_row-1,3106)
     packed=asyncio.run(data(app.research_package(request)));self.assertGreater(len(packed),100)
   finally:app.RESEARCH_JOBS.pop(key,None);app._RESEARCH_INPUTS.pop(key,None)
 def test_public_source_whitelist_does_not_expose_transport_payload(self):
  from engine.research_publication import public_source_evidence
  value=dict(reader='native_pdf',cells=dict(name='Cable',model='ABC',article='ABC',maker='Maker',qty='24'),source_hash='a'*64,headers={'Authorization':'SECRET'},raw_response='SECRET',cookie='SECRET')
  safe=public_source_evidence(value);self.assertEqual(safe['cells']['maker'],'Maker');self.assertNotIn('SECRET',str(safe));self.assertNotIn('headers',safe)

 def test_actual_base_fill_native_maker_current_price_and_unknown_knownzero(self):
  import hashlib,time
  from engine.search.catalogue import SourceObservation,ImageEvidence
  p=PositionIn(number=1,name='Изделие',full_name='Изделие полное',type_mark='ABC123',unit='шт',qty=10,source_evidence={'reader':'native_pdf','cells':{'number':'1','name':'Изделие','model':'','article':'ABC123','maker':'Maker','unit':'шт','qty':'10'}})
  unknown=p.model_copy(update={'number':2,'type_mark':'OTHER999','source_evidence':{}})
  zero=p.model_copy(update={'number':3,'type_mark':'ZERO123','source_evidence':{}})
  with tempfile.TemporaryDirectory() as directory:
   store=CatalogueStore(Path(directory)/'catalogue.sqlite3');identity=identity_for_position(p);store.acquire_unknown(identity,['shop.test'],owner='fixture');store.acquire_unknown(identity_for_position(zero),['shop.test'],owner='fixture')
   oid=uuid.uuid4().hex;stamp=time.time()-86400;url='https://shop.test/card';title='Изделие Maker ABC123';text=title+'\nАртикул: ABC123\n100 руб. с НДС\nза 1 шт'
   o=SourceObservation(oid,identity,'shop.test',url,url,stamp,'supplier_http',title,'ABC123',text,hashlib.sha256(text.encode()).hexdigest(),100,'RUB','included','шт',None,None,'','unknown','observed','Продавец','7707083893',ImageEvidence(oid,url,stamp,'missing',error='fixture'),'v1');store.append_observation(identity.key,o,coverage_status='found')
   with patch.object(app,'_catalogue_store',return_value=store),patch('engine.search.base_db.lookup',side_effect=AssertionError('No legacy999')),patch.object(app,'_browser_factory',side_effect=AssertionError('No supplier')),patch('engine.llm.LLMRequestGuard',side_effect=AssertionError('No model')):
    answer=app.base_fill(app.FillRequest(positions=[p,unknown,zero]));self.assertEqual(answer['found'],2);self.assertEqual(answer['total'],3);first,miss,empty=answer['results'];self.assertTrue(first['in_base']);self.assertEqual(first['catalogue_product_id'],identity.key);self.assertEqual(first['candidates'][0]['price_with_vat'],100);self.assertEqual(first['candidates'][0]['catalogue_ref']['observation_id'],oid);self.assertEqual(first['candidates'][0]['catalogue_observed_at'],stamp);self.assertFalse(miss['in_base']);self.assertEqual(miss['offers'],[]);self.assertTrue(empty['in_base']);self.assertEqual(empty['offers'],[])

   key=uuid.uuid4().hex[:12];app.RESEARCH_JOBS[key]=dict(job_id=key,rows=[None],status='pending',total=1,completed=0,configured_hosts=['shop.test']);app._RESEARCH_INPUTS[key]=[p]
   try:
    with patch.object(app,'_catalogue_store',return_value=store),patch.object(app,'RESEARCH_JOB_DIR',Path(directory)/'jobs'),patch.object(app,'_browser_factory',side_effect=AssertionError('No supplier')):
     app._save_research_job(key);app._run_research_job(key);row=app._research_store().public(key)['rows'][0];restored=app._research_store().runtime_snapshot(key)['rows'][0]
     self.assertEqual(identity_for_position(PositionIn.model_validate(row)),identity);self.assertEqual(row['candidates'][0]['catalogue_ref'],{'product_id':identity.key,'observation_id':oid});self.assertEqual(restored['candidates'][0]['catalogue_ref'],row['candidates'][0]['catalogue_ref']);self.assertEqual(restored['candidates'][0]['catalogue_observed_at'],stamp)
   finally:app.RESEARCH_JOBS.pop(key,None);app._RESEARCH_INPUTS.pop(key,None)
