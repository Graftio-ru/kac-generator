"""Native catalogue plus canonical research-store diagnostic/full-projection split."""
import runpy,ast,copy,types,unittest,socket,subprocess,json,time
from pathlib import Path
from unittest.mock import patch
ns=runpy.run_path(str(Path(__file__).with_name('test_derived_progress439.py')),run_name='fixture442')
from engine.research_publication import IntegrationStore,DeltaPublisher,DerivedProgressGate,BufferedPublicationError
class DiagnosticTests(ns['Tests']):
 def environment(self):
  tree=ast.parse(Path(__file__).with_name('app.py').read_text());names={'_catalogue_source_checks','_catalogue_card_review_diagnostic','_publish_catalogue_diagnostics','_catalogue_legacy_check_patch'};functions=[copy.deepcopy(n)for n in tree.body if isinstance(n,ast.FunctionDef)and n.name in names];env={'__package__':'engine','json':json};research=IntegrationStore(self.root/'research');self.addCleanup(research.db.close);job='c'*12;research.create_job(job,[self.position.model_dump(mode='json')],['one.test','two.test','three.test']);publisher=DeltaPublisher(research,job);env.update(_research_publisher=lambda j:publisher);exec(compile(ast.fix_missing_locations(ast.Module(body=functions,type_ignores=[])),'app.metadata','exec'),env)
  outer=next(n for n in tree.body if isinstance(n,ast.FunctionDef)and n.name=='_catalogue_product_steps');fn=copy.deepcopy(next(n for n in outer.body if isinstance(n,ast.FunctionDef)and n.name=='publish'));fn.body=[n for n in fn.body if not isinstance(n,ast.Nonlocal)];fn.body.insert(0,ast.Global(names=['urgent_progress']));clock=[0.];builds=[]
  def build():
   product=self.store.lookup(self.identity)
   with ns['catalogue_scope'](self.store,snapshot=product):result=ns['project_product'](self.store,self.identity,self.position,snapshot=product).model_dump(mode='json')
   builds.append(result);publisher.publish(0,row_patch={'native_result':result,'status':'partial'},terminal=False,force=True)
  env.update(check=lambda:None,derived_progress=DerivedProgressGate(clock=lambda:clock[0]),diagnostic_hosts=set(),urgent_progress=False,store=self.store,product_id=self.identity.key,admission=self.ad,job_id=job,indices=[0],build_progress=build)
  # Readonly coverage ownership uses the native acquisition owner, not synthetic research job.
  class CoverageReader:
   def read_coverage(_,product,host,**kwargs):return self.store.read_coverage(product,host,acquisition=self.ad,owner='offline')
  env['store']=CoverageReader();exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'app.publish','exec'),env);return env,research,publisher,job,clock,builds
 def test_local_error_burst_visible_without_full_build_then_fresh_tail(self):
  env,research,publisher,job,clock,builds=self.environment();env['publish']();initial=copy.deepcopy(builds[-1])
  for i in range(6):
   self.store.publish_coverage(self.identity.key,'one.test',acquisition=self.ad,status='review',observed_at=time.time(),reason='local error '+str(i),diagnostics=[{'code':'card_review','reason':'local error '+str(i)}]);env['diagnostic_hosts'].add('one.test');env['urgent_progress']=True;env['publish']();row=research.public(job)['rows'][0];self.assertEqual(row['source_checks'][0]['reason'],'local error '+str(i));self.assertEqual(len(builds),1);self.assertEqual(row['native_result'],initial)
  self.append(1,999);env['publish'](force=True);self.assertEqual(len(builds),2);self.assertNotEqual(builds[-1],initial);self.assertEqual(research.public(job)['rows'][0]['native_result'],builds[-1])
 def test_existing_legacy_overlay_updates_without_minting_native_overlay(self):
  env,research,publisher,job,clock,builds=self.environment();env['publish']();payload=json.loads(research.db.execute('SELECT payload FROM rows WHERE job=?',(job,)).fetchone()[0]);self.assertNotIn('legacy_source_checks',payload)
  legacy=research.public(job)['rows'][0]['source_checks'];publisher.publish(0,row_patch={'legacy_source_checks':legacy},force=True)
  self.store.publish_coverage(self.identity.key,'one.test',acquisition=self.ad,status='review',observed_at=time.time(),reason='fresh legacy diagnostic');env['diagnostic_hosts'].add('one.test');env['urgent_progress']=True;env['publish']();self.assertEqual(research.public(job)['rows'][0]['source_checks'][0]['reason'],'fresh legacy diagnostic');self.assertEqual(len(builds),1)
  fresh=[{'host':'one.test','status':'review','reason':'full fresh diagnostic'}];patch=env['_catalogue_legacy_check_patch'](publisher,0,fresh,replace=True);self.assertEqual(patch,{'legacy_source_checks':fresh})
 def test_malformed_or_duplicate_legacy_checks_fail_closed(self):
  from engine.search.catalogue import CatalogueStoreError
  env,research,publisher,job,clock,builds=self.environment();env['publish']()
  for legacy in [None,[{'host':'one.test'},{'host':'one.test'}],[{'host':123}]]:
   publisher.publish(0,row_patch={'legacy_source_checks':legacy},force=True)
   with self.assertRaises(CatalogueStoreError):env['_catalogue_legacy_check_patch'](publisher,0,[])
 def test_unknown_ack_keeps_hosts_and_blocks_new_delta(self):
  env,research,publisher,job,clock,builds=self.environment();env['publish']();self.store.publish_coverage(self.identity.key,'one.test',acquisition=self.ad,status='review',observed_at=time.time(),reason='error');env['diagnostic_hosts'].add('one.test');env['urgent_progress']=True;trigger=[True]
  def fault(boundary):
   if boundary=='after_commit'and trigger[0]:trigger[0]=False;raise OSError('lost acknowledgement')
  research.fault=fault
  with self.assertRaises(BufferedPublicationError):env['publish']()
  self.assertEqual(env['diagnostic_hosts'],{'one.test'});self.assertTrue(env['urgent_progress']);before=research.db.execute('SELECT revision FROM rows WHERE job=?',(job,)).fetchone()[0]
  with self.assertRaises(Exception):env['publish']()
  publisher.flush(0);self.assertEqual(research.db.execute('SELECT revision FROM rows WHERE job=?',(job,)).fetchone()[0],before)
if __name__=='__main__':
 with patch.object(socket.socket,'connect',side_effect=AssertionError('no network')),patch.object(subprocess,'Popen',side_effect=AssertionError('no process')):unittest.main()
