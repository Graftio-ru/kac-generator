"""Zero-based neutral supplier ranking is not financial authority."""
from dataclasses import asdict
import copy,json,sqlite3,time,unittest
from pathlib import Path
from unittest.mock import patch
from engine import test_acquisition_frontier as frontier_fixture
from engine.search import supplier_discovery as own
from engine.search.acquisition_frontier import FrontierInvalid,FrontierController

class DiscoveryRankTests(unittest.TestCase):
 setUp=frontier_fixture.FrontierTests.setUp
 def test_actual_saved_producer_rank_zero_survives_response_and_card_cursor(self):
  root=Path(__file__).resolve().parents[2]
  body=(root/'sources/kac-generator/mpo-own-search-20261013/response.html').read_text()
  hints=own.parse_candidate_hints(body,own.PROVIDERS['electro-mpo.ru'].endpoint,'3526210')
  self.assertTrue(any(h.rank==0 for h in hints.values()))
  # Keep actual href/title/rank; only the admitted supplier boundary changes.
  f=copy.deepcopy(self.f);f['configured_hosts']=['electro-mpo.ru'];records=type(self.records)(self.records.root/'mpo',f);controller=FrontierController(f,records)
  _,task=controller.add_query('group','"ABC123" цена',['electro-mpo.ru']);controller.start_query(task);operation=controller.value['queries'][0]['operation_id']
  items=[]
  for url,hint in hints.items():
   neutral=asdict(hint);neutral={k:v for k,v in neutral.items() if k in {'title','manufacturer_article','brand','supplier_codes','owner_role','source_sha256','observed_at','rank'}};neutral['supplier_codes']=list(neutral['supplier_codes'])
   items.append(dict(url=url,host='electro-mpo.ru',hint=neutral))
  response=records.write_response(operation,items,[]);controller.accept_response(task,response)
  for expected in items:
   self.assertEqual(records.candidate(response,operation,controller.value['queries'][0]['cursor']),expected)
   card=controller.start_card(task)['queries'][0]['current_card'];outcome=records.write_outcome(card['operation_id'],card['url'],card['host'],state='no_facts');controller.settle_card(task,outcome)
  self.assertEqual(controller.value['queries'][0]['state'],'done');self.assertFalse(controller.value['frontier_complete'])
 def test_rank_writer_reader_reject_negative_boolean_and_noninteger(self):
  for rank in (-1,True,False,0.0,'0',None):
   item=dict(url='https://shop.test/card',host='shop.test',hint={'rank':rank})
   with self.subTest(rank=rank),self.assertRaises(FrontierInvalid):self.records.write_response('f'*32,[item],[])
   page=self.records._write(dict(binding=self.records.binding,kind='candidates',items=[item]))
   reference=self.records._write(dict(binding=self.records.binding,kind='response',operation_id='f'*32,pages=[page],count=1,host_observations=[]));reference['count']=1
   with self.subTest(read_rank=rank),self.assertRaises(FrontierInvalid):self.records.candidate(reference,'f'*32,0)
 def test_native96_normal_app_capacity_checkpoint_keeps_candidates_and_original_cause(self):
  from engine.test_catalogue_unknown import CatalogueUnknownTests
  from engine.schemas import PositionIn
  from engine.search.search_engine import DiscoveryURLs
  from engine.search.failure_policy import SearchCapacityDeferred
  from engine.llm import _REQUEST_GUARD
  root=Path(__file__).resolve().parents[2];database=root/'sources/kac-generator/full-service-2026-10-13/jobs/research.sqlite3'
  with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:source=json.loads(db.execute("SELECT payload FROM inputs WHERE job='178d3a0d6140'").fetchone()[0])
  fixture=CatalogueUnknownTests();fixture.setUp();fixture.hosts=source['configured_hosts'];captures=[]
  def search(query,hosts,**kwargs):
   stamp=time.time();failure=SearchCapacityDeferred(observed_at=stamp);_REQUEST_GUARD.get().defer_search(failure)
   url='https://'+hosts[0]+'/product/observed'
   metadata={h:dict(origin='unknown',window='unknown',observed_at=stamp,candidate_count=int(h==hosts[0]),diagnostics=[dict(failure.diagnostic(),url='https://'+h+'/')]) for h in hosts}
   metadata[hosts[0]]['candidate_hints']={url:{'rank':0,'title':'Observed neutral product'}}
   failure.partial_discovery=DiscoveryURLs([url],host_observations=metadata);raise failure
  try:
   job,public=fixture.job([PositionIn.model_validate(p) for p in source['positions']],search=search,capture=lambda *a,**kw:captures.append(a[3]))
   self.assertEqual(job['status'],'running',job.get('error'));self.assertTrue(public['awaiting_provider']);self.assertNotIn('first_stop',public)
   self.assertEqual(len(public['rows']),96);self.assertEqual(sum(len(r.get('source_checks',[])) for r in public['rows']),3096)
   self.assertTrue(captures);self.assertTrue(any(d.get('code')=='search_capacity_deferred' for row in public['rows'] for check in row.get('source_checks',[]) for d in check.get('diagnostics',[])))
  finally:fixture.doCleanups()
