"""Ambiguous maker aliases remain known local reports, never a new acquisition."""
import asyncio,copy,tempfile,unittest,uuid
from pathlib import Path
from io import BytesIO
from unittest.mock import patch
from openpyxl import load_workbook
from fastapi import HTTPException
from engine import app
from engine.schemas import PositionIn,ResearchReportRequest,BuildRequest
from engine.search.catalogue import identity_for_position

class AmbiguousStore:
 def __init__(self,identity,hosts):
  reason='В каталоге несколько записей эквивалентного производителя; требуется разбор идентичности'
  self.identity=identity
  self.product=dict(product_id='',identity=identity.payload(),known=True,revision=7,created_at=None,heads=[],history=[],refresh_attempts=[],acquisition=None,identity_ambiguous=True,ambiguity_reason=reason,conflicting_product_ids=['a'*64,'b'*64],coverage={h:dict(status='unknown',observed_at=None,product_presence='unknown',reason=reason) for h in hosts})
 def lookup(self,identity):return copy.deepcopy(self.product) if identity==self.identity else None
 def read_snapshot(self,identities):return dict(revision=7,products={i.key:self.lookup(i) for i in identities})
 def acquire_unknown(self,*a,**kw):raise AssertionError('No new acquisition')
 def resolve_observation(self,*args):raise AssertionError('No financial observation')

class CatalogueMakerAliasAppTests(unittest.TestCase):
 def test_ambiguity_empty_id_terminal_public_fill_report_and_build_no_network(self):
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1);hosts=['shop.test','other.test'];store=AmbiguousStore(identity_for_position(p),hosts)
  with tempfile.TemporaryDirectory() as directory,patch.object(app,'_catalogue_store',return_value=store),patch.object(app,'RESEARCH_JOB_DIR',Path(directory)/'jobs'),patch.object(app,'_browser_factory',side_effect=AssertionError('No browser')),patch('engine.search.base_db.lookup',side_effect=AssertionError('No legacy')),patch('engine.llm.LLMRequestGuard',side_effect=AssertionError('No model')),patch.object(app,'run_pipeline_async',side_effect=AssertionError('No financial build')):
   row=app._research_row(p);self.assertEqual(row['catalogue_product_id'],'');self.assertEqual(row['offers'],[]);self.assertEqual(row['candidates'],[]);self.assertTrue(row['catalogue_identity_ambiguous']);self.assertEqual(row['found'],0)
   for c in row['source_checks']:
    self.assertEqual(c['status'],'incomplete');self.assertEqual(c['code'],'catalogue_identity_ambiguous');self.assertIsNone(c['observed_at']);self.assertIsNone(c.get('checked_at'));self.assertEqual(c['product_presence'],'unknown')
   key=uuid.uuid4().hex[:12];app.RESEARCH_JOBS[key]=dict(job_id=key,rows=[None],status='pending',total=1,completed=0,configured_hosts=hosts);app._RESEARCH_INPUTS[key]=[p]
   try:
    app._save_research_job(key);app._run_research_job(key);public=app._research_store().public(key);self.assertEqual(public['status'],'done');self.assertEqual(public['completed'],1);self.assertTrue(app._research_row_finished(public['rows'][0],app.RESEARCH_JOBS[key]));self.assertEqual(app._research_store().unfinished(key),[])
    self.assertEqual(public['rows'][0]['catalogue_conflicting_product_ids'],['a'*64,'b'*64]);self.assertEqual(public['rows'][0]['catalogue_product_id'],'')
    filled=app.base_fill(app.FillRequest(positions=[p]));self.assertTrue(filled['results'][0]['in_base']);self.assertIn('несколько записей',filled['results'][0]['reason'])
    request=ResearchReportRequest(positions=public['rows'])
    async def data(response):return b''.join([part async for part in response.body_iterator])
    book=load_workbook(BytesIO(asyncio.run(data(app.research_report(request)))));matrix=book['Позиции × сайты'];self.assertIn('несколько записей',str([c.value for row in matrix for c in row]));self.assertGreater(len(asyncio.run(data(app.research_package(request)))),100)
    with self.assertRaises(HTTPException) as refused:app.build(BuildRequest(object_name='Test',skip_search=True,use_tsn=False,positions=[p]))
    self.assertEqual(refused.exception.status_code,422)
   finally:app.RESEARCH_JOBS.pop(key,None);app._RESEARCH_INPUTS.pop(key,None)
 def test_client_ambiguity_claim_cannot_create_server_known_record(self):
  from engine.search.catalogue import CatalogueStore
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)
  with tempfile.TemporaryDirectory() as directory,patch.object(app,'_catalogue_store',return_value=CatalogueStore(Path(directory)/'empty.sqlite3')),patch.object(app,'run_pipeline_async',side_effect=AssertionError('No bypass')):
   request=BuildRequest.model_validate(dict(object_name='Test',skip_search=False,use_tsn=False,positions=[{**p.model_dump(),'catalogue_read':True,'catalogue_identity_ambiguous':True,'catalogue_product_id':'','catalogue_conflicting_product_ids':['a'*64,'b'*64]}]))
   with self.assertRaises(HTTPException) as refused:app.build(request)
   self.assertEqual(refused.exception.detail['code'],'catalogue_acquisition_required')
 def test_actual_store_alias_original_ref_then_duplicate_ambiguity_finishes_locally(self):
  from engine import test_catalogue_maker_aliases as fixtures,config
  from engine.search.catalogue import ProductIdentity
  fixture=fixtures.CatalogueMakerAliasTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
  fixture.store.acquire_unknown(fixture.identity,fixture.resources,owner='original');observed=fixture.observe()
  p=PositionIn.model_validate(fixture.position('ДКС',150).model_dump())
  with patch.object(app,'_catalogue_store',return_value=fixture.store),patch.object(config,'CACHE_DIR',fixture.root),patch.object(app,'_browser_factory',side_effect=AssertionError('No alias refresh')),patch('engine.llm.LLMRequestGuard',side_effect=AssertionError('No model')):
   row=app._research_row(p);self.assertEqual(row['found'],1);self.assertEqual(row['catalogue_product_id'],fixture.identity.key);self.assertEqual(row['offers'][0]['catalogue_ref']['observation_id'],observed.observation_id);self.assertEqual(row['offers'][0]['catalogue_observed_at'],observed.observed_at)
   alternate=ProductIdentity('article','ABC123','ДКС')
   key=uuid.uuid4().hex[:12];app.RESEARCH_JOBS[key]=dict(job_id=key,rows=[None],status='pending',total=1,completed=0,configured_hosts=fixture.resources);app._RESEARCH_INPUTS[key]=[p]
   try:
    with patch.object(app,'RESEARCH_JOB_DIR',fixture.root/'jobs'):
     app._save_research_job(key);initial=app._catalogue_known_row(p,store=fixture.store);app.RESEARCH_JOBS[key]['rows'][0]=initial;app._publish_catalogue_row(key,0,initial,terminal=False);self.assertEqual(len(app._research_store().public(key)['rows'][0]['candidates']),1)
     with patch.object(fixture.store,'_identity_candidates',return_value=[]):fixture.store.acquire_unknown(alternate,fixture.resources,owner='historical-duplicate')
     app._run_research_job(key);public=app._research_store().public(key);self.assertEqual(public['status'],'done');self.assertEqual(public['completed'],1);row=public['rows'][0]
     self.assertEqual(row['catalogue_product_id'],'');self.assertEqual(row['offers'],[]);self.assertEqual(row['candidates'],[]);self.assertTrue(row['catalogue_identity_ambiguous']);self.assertEqual(row['catalogue_conflicting_product_ids'],sorted([fixture.identity.key,alternate.key]));self.assertEqual(app._research_store().unfinished(key),[])
     self.assertEqual(fixture.store.all_snapshot()['products'][fixture.identity.key]['heads'][0]['current']['observation_id'],observed.observation_id)
     self.assertTrue(all(c['status']=='incomplete' and c.get('checked_at') is None and c['observed_at'] is None for c in row['source_checks']))
     async def data(response):return b''.join([part async for part in response.body_iterator])
     book=load_workbook(BytesIO(asyncio.run(data(app.research_report(ResearchReportRequest(positions=[row]))))))
     self.assertIn('несколько записей',str([c.value for r in book['Позиции × сайты'] for c in r]))
   finally:app.RESEARCH_JOBS.pop(key,None);app._RESEARCH_INPUTS.pop(key,None)
