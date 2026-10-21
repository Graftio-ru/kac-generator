"""Paid-free durable catalogue semantics and real SQLite transaction controls."""
import copy,hashlib,json,sqlite3,tempfile,time,unittest,uuid
from dataclasses import replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from engine.search.catalogue import CatalogueStore,CatalogueStoreError,CatalogueConflict,ProductIdentity,SourceObservation,ImageEvidence,identity_for_position

class CatalogueTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'catalogue.sqlite3';self.store=CatalogueStore(self.path);self.identity=ProductIdentity('article','ABC123','Maker');self.hosts=[f's{i}.test' for i in range(36)]
 def acquire(self,identity=None):return self.store.acquire_unknown(identity or self.identity,self.hosts,owner='worker')
 def observation(self,price=100,*,stamp=None,state='observed',identity=None,url='https://s0.test/card?variant=blue',image=True):
  identity=identity or self.identity;stamp=time.time()-30 if stamp is None else stamp;identifier=uuid.uuid4().hex
  text=f'Cable {identity.code}\nАртикул: {identity.code}\n'+(f'{price} RUB\nЦена за 1 м' if price is not None else 'Цена по запросу')
  photo=ImageEvidence(identifier,url,stamp,'captured',sha256='a'*64,local_path='images/'+identifier+'.png',size=1024,bucket='supplier-images',key=identifier+'.png',captured_at=stamp+.01,context_evidence={'url':url,'captured_at':stamp+.01}) if image else ImageEvidence(identifier,url,stamp,'missing',error='capture_timeout')
  return SourceObservation(identifier,identity,'s0.test',url,url,stamp,'supplier_http','Cable '+identity.code,identity.code,text,hashlib.sha256(text.encode()).hexdigest(),price,'RUB','included','м',1,None,'', 'in_stock',state,'Seller','7842224734',photo,'raw_source_v1')
 def test_concurrent_unknown_acquisition_one_owner_known_zero_never_restarts(self):
  with ThreadPoolExecutor(max_workers=12) as pool:admissions=list(pool.map(lambda i:CatalogueStore(self.path).acquire_unknown(self.identity,self.hosts,owner=str(i)),range(24)))
  allowed=[a for a in admissions if a.allowed];self.assertEqual(len(allowed),1)
  self.assertEqual(len(self.store.lookup(self.identity)['coverage']),36);self.assertEqual(self.store.lookup(self.identity)['heads'],[])
  self.assertTrue(self.store.finish_acquisition(allowed[0],error='provider_capacity'))
  self.assertFalse(CatalogueStore(self.path).acquire_unknown(self.identity,self.hosts,owner='new_spec').allowed)
  known=self.store.lookup(self.identity);self.assertTrue(known['known']);self.assertEqual(known['acquisition']['status'],'failed');self.assertTrue(all(v['status']=='unknown' and v['observed_at'] is None for v in known['coverage'].values()))
 def test_newest_prices_replace_both_directions_late_history_and_no_facts_failure(self):
  a=self.acquire();now=time.time();first=self.observation(100,stamp=now-30);higher=self.observation(200,stamp=now-20);lower=self.observation(80,stamp=now-10)
  for o in (first,higher,lower):self.assertTrue(self.store.append_observation(a.product_id,o,coverage_status='found'))
  self.store.record_refresh_attempt(a.product_id,'s0.test',lower.url,attempted_at=now,outcome='network_error',reason='ReadTimeout')
  snap=self.store.lookup(self.identity);self.assertEqual(snap['heads'][0]['current']['raw_amount'],80);self.assertEqual(snap['heads'][0]['current']['observed_at'],lower.observed_at);self.assertEqual(snap['refresh_attempts'][-1]['attempted_at'],now)
  older=self.observation(500,stamp=now-25);self.assertFalse(self.store.append_observation(a.product_id,older));self.assertEqual(self.store.lookup(self.identity)['heads'][0]['current']['observation_id'],lower.observation_id)
  tied=self.observation(700,stamp=lower.observed_at);self.assertFalse(self.store.append_observation(a.product_id,tied));self.assertEqual(len(self.store.lookup(self.identity,include_history=True)['history']),5)
  with self.assertRaises(ValueError):self.store.publish_coverage(a.product_id,'s0.test',status='found',observed_at=first.observed_at,product_presence='observed',observation_id=first.observation_id)
  self.assertFalse(self.store.append_observation(a.product_id,lower))
  with self.assertRaises(CatalogueConflict):self.store.append_observation(a.product_id,replace(lower,raw_amount=81,source_text=lower.source_text.replace('80','81'),source_sha256=hashlib.sha256(lower.source_text.replace('80','81').encode()).hexdigest()))
 def test_conditional_current_and_missing_image_do_not_restore_old_firm(self):
  a=self.acquire();old=self.observation(100,stamp=time.time()-30);self.store.append_observation(a.product_id,old)
  current=self.observation(150,stamp=time.time()-10,state='review',image=False);current=replace(current,purchase_terms='Стоимость продукции рассчитывается индивидуально')
  self.store.append_observation(a.product_id,current,coverage_status='review');snap=self.store.lookup(self.identity);head=snap['heads'][0]
  self.assertEqual(head['current']['state'],'review');self.assertEqual(head['current']['raw_amount'],150);self.assertEqual(head['last_good']['observation_id'],old.observation_id);self.assertEqual(head['current']['image']['status'],'missing');self.assertFalse(head['current']['image']['local_path']);self.assertEqual(head['current']['image']['error'],'capture_timeout')
  self.assertFalse(self.store.resolve_observation(a.product_id,old.observation_id)['current']);self.assertTrue(self.store.resolve_observation(a.product_id,current.observation_id)['current'])
  with self.assertRaises(ValueError):self.store.append_observation(a.product_id,replace(current,observation_id=uuid.uuid4().hex,image=old.image))
 def test_atomic_revision_snapshot_detached_image_coverage_and_url_queries(self):
  a=self.acquire();o=self.observation();self.store.append_observation(a.product_id,o,coverage_status='found');before=self.store.read_snapshot([self.identity]);p=before['products'][a.product_id]
  self.assertEqual(p['coverage']['s0.test']['observation_id'],p['heads'][0]['current']['observation_id']);self.assertEqual(p['coverage']['s0.test']['observed_at'],p['heads'][0]['current']['observed_at']);self.assertEqual(p['heads'][0]['current']['image']['observation_id'],o.observation_id)
  new=self.observation(90,stamp=time.time()-5,url='https://s0.test/card?variant=red');self.store.append_observation(a.product_id,new,coverage_status='found');after=self.store.lookup(self.identity)
  self.assertGreater(after['revision'],before['revision']);self.assertEqual(len(after['heads']),2);self.assertEqual(len(p['heads']),1)
  p['heads'][0]['current']['image']['context_evidence']['url']='tampered';self.assertEqual(self.store.lookup(self.identity)['heads'][0]['current']['image']['context_evidence']['url'],o.url)
  self.assertIsNone(self.store.resolve_observation('f'*64,o.observation_id));self.assertIsNone(self.store.resolve_observation(a.product_id,'a'*32))
  inventory=self.store.all_snapshot();self.assertEqual(inventory['revision'],after['revision']);self.assertEqual(set(inventory['products']),{a.product_id});self.assertEqual(inventory['products'][a.product_id]['heads'],after['heads'])
 def test_durable_worker_claim_cancel_and_late_owner_refusal(self):
  a=self.store.acquire_unknown(self.identity,self.hosts,owner='first',lease_seconds=1)
  with patch('engine.search.catalogue.time.time',return_value=a.lease_until+1):
   claimed=CatalogueStore(self.path).claim_acquisition(a.product_id,owner='recovered');self.assertTrue(claimed.allowed);self.assertEqual(claimed.generation,2)
   self.assertFalse(self.store.finish_acquisition(a));o=self.observation(stamp=a.lease_until-10)
   self.assertFalse(self.store.append_observation(a.product_id,o,acquisition=a));self.assertEqual(self.store.lookup(self.identity)['history'],[])
   self.assertTrue(self.store.cancel_acquisition(claimed));self.assertFalse(self.store.append_observation(a.product_id,o,acquisition=claimed));self.assertFalse(self.store.claim_acquisition(a.product_id,owner='later').allowed)
  self.assertFalse(self.store.acquire_unknown(self.identity,self.hosts,owner='spec').allowed)
 def test_neutral_identity_verified_aliases_no_qty_unit_row_seller_maker_guess(self):
  p={'number':1,'name':'Cable','full_name':'Cable full','type_mark':'ABC123','qty':1,'unit':'м','discipline':'ЭМ'};first=identity_for_position(p)
  self.assertEqual(first.key,identity_for_position({**p,'number':50,'qty':24,'unit':'шт','discipline':'ВК'}).key)
  self.assertNotEqual(first.key,identity_for_position({**p,'type_mark':'ABC123-ZL'}).key)
  native={**p,'source_evidence':{'reader':'native_pdf','cells':{'name':'Cable','model':'','article':'ABC123','maker':'Maker'}}};self.assertEqual(identity_for_position(native).key,self.identity.key)
  native['source_evidence']['cells']['article']='OTHER';self.assertEqual(identity_for_position(native).kind,'model')
  target=ProductIdentity('model','ABC123','Maker','Cable full');alias=ProductIdentity('model','ABC123','Maker','Cable alt');a=self.acquire(target)
  self.store.add_alias(alias,target,observed_identity=target,source_url='https://s0.test/card',observed_at=time.time()-1,source_title='Cable alt ABC123');self.assertEqual(self.store.lookup(alias)['product_id'],a.product_id);self.assertFalse(self.store.acquire_unknown(alias,self.hosts,owner='spec').allowed)
  for bad in (replace(alias,code='ABC123-ZL'),replace(alias,manufacturer='Other'),replace(alias,features=(('diameter','10'),))):
   with self.assertRaises(ValueError):self.store.add_alias(bad,target,observed_identity=target,source_url='https://s0.test/card',observed_at=time.time()-1,source_title='Cable alt')
 def test_corruption_unavailable_and_bad_record_never_unknown(self):
  bad=Path(self.tmp.name)/'bad.db';bad.write_bytes(b'not sqlite')
  with self.assertRaises(CatalogueStoreError):CatalogueStore(bad)
  self.assertIsNone(self.store.lookup(self.identity));a=self.acquire();o=self.observation();self.store.append_observation(a.product_id,o)
  with sqlite3.connect(self.path) as db:db.execute("UPDATE observations SET payload='{' WHERE id=?",(o.observation_id,))
  with self.assertRaises(CatalogueStoreError):self.store.lookup(self.identity)
  with self.assertRaises(CatalogueStoreError):self.store.resolve_observation(a.product_id,o.observation_id)
  with sqlite3.connect(self.path) as db:db.execute('DROP TABLE coverage')
  with self.assertRaises(CatalogueStoreError):self.store.read_snapshot([self.identity])
  with self.assertRaises(CatalogueStoreError):CatalogueStore(self.path)
 def test_heartbeat_and_found_coverage_original_date_resource_guards(self):
  a=self.acquire();renewed=self.store.renew_acquisition(a,lease_seconds=300);self.assertTrue(renewed.allowed);self.assertEqual(renewed.token,a.token);self.assertEqual(renewed.generation,a.generation)
  o=self.observation();self.store.append_observation(a.product_id,o)
  with self.assertRaises(ValueError):self.store.publish_coverage(a.product_id,'s0.test',status='found',observed_at=time.time(),product_presence='observed',observation_id=o.observation_id)
  with self.assertRaises(ValueError):self.store.publish_coverage(a.product_id,'s0.test',status='found',observed_at=o.observed_at,observation_id=o.observation_id)
  self.store.publish_coverage(a.product_id,'s0.test',status='found',observed_at=o.observed_at,product_presence='observed',observation_id=o.observation_id)
  with self.assertRaises(ValueError):self.store.record_refresh_attempt(a.product_id,'foreign.test','https://foreign.test/card',attempted_at=time.time(),outcome='network_error')
  with self.assertRaises(ValueError):self.store.record_refresh_attempt(a.product_id,'s0.test','https://foreign.test/card',attempted_at=time.time(),outcome='network_error')
  with self.assertRaises(ValueError):self.store.append_observation(a.product_id,self.observation(),acquisition=renewed)
  self.store.finish_acquisition(renewed);self.assertFalse(self.store.renew_acquisition(renewed).allowed)
 def test_bound_model_maker_and_corrupt_stored_identity_fail_closed(self):
  p={'name':'Cable','type_mark':'ABC123','source_evidence':{'reader':'native_pdf','cells':{'name':'Cable','model':'ABC123','article':'','maker':'MakerA'}}}
  first=identity_for_position(p);self.assertEqual(first.manufacturer,'MakerA');second=copy.deepcopy(p);second['source_evidence']['cells']['maker']='MakerB';self.assertNotEqual(first.key,identity_for_position(second).key)
  for bad in ({**p,'source_evidence':['bad']},{**p,'source_evidence':{'cells':['bad']}}):
   with self.assertRaises(ValueError):identity_for_position(bad)
  a=self.acquire()
  with sqlite3.connect(self.path) as db:db.execute('UPDATE products SET identity=? WHERE id=?',(json.dumps({'kind':'article','code':'OTHER','manufacturer':'Maker','descriptor':'','features':[]}),a.product_id))
  with self.assertRaises(CatalogueStoreError):self.store.lookup(self.identity)
 def test_conditional_source_role_cannot_be_promoted_by_observed_enum(self):
  a=self.acquire();o=self.observation();text=o.source_text.replace('100 RUB','от 100 RUB');o=replace(o,source_text=text,source_sha256=hashlib.sha256(text.encode()).hexdigest())
  with self.assertRaises(ValueError):self.store.append_observation(a.product_id,o,coverage_status='found')
  self.store.append_observation(a.product_id,o,coverage_status='review');head=self.store.lookup(self.identity)['heads'][0];self.assertEqual(head['current']['state'],'review');self.assertIsNone(head['last_good'])
 def test_observed_redirect_retires_only_requested_head_and_late_cannot_retire(self):
  a=self.acquire();now=time.time();old=self.observation(100,stamp=now-30,url='https://s0.test/card?id=7');other=self.observation(125,stamp=now-25,url='https://s0.test/card?id=8')
  self.store.append_observation(a.product_id,old);self.store.append_observation(a.product_id,other)
  redirected=self.observation(75,stamp=now-10,url='https://s0.test/card?id=redirected');redirected=replace(redirected,requested_url=old.url)
  self.store.append_observation(a.product_id,redirected);heads=self.store.lookup(self.identity)['heads'];self.assertEqual({h['url'] for h in heads},{other.url,redirected.url});self.assertFalse(self.store.resolve_observation(a.product_id,old.observation_id)['current'])
  self.store.record_refresh_attempt(a.product_id,'s0.test',old.url,attempted_at=now,outcome='observed',observation_id=redirected.observation_id);attempt=self.store.lookup(self.identity)['refresh_attempts'][-1];self.assertEqual(attempt['url'],old.url);self.assertEqual(attempt['source_url'],redirected.url)
  late=self.observation(999,stamp=now-20,url='https://s0.test/card?id=late');late=replace(late,requested_url=redirected.url);self.assertFalse(self.store.append_observation(a.product_id,late));self.assertTrue(self.store.resolve_observation(a.product_id,redirected.observation_id)['current']);self.assertFalse(self.store.resolve_observation(a.product_id,late.observation_id)['current'])
  with self.assertRaises(ValueError):self.store.record_refresh_attempt(a.product_id,'s0.test','https://s0.test/card?id=unrelated',attempted_at=now,outcome='observed',observation_id=redirected.observation_id)
 def test_region_facts_preserved_without_inferred_moscow(self):
  a=self.acquire();o=replace(self.observation(),region_status='incompatible',region_evidence='Самовывоз только вне Московской области',in_moscow_region=False)
  self.store.append_observation(a.product_id,o);resolved=self.store.resolve_observation(a.product_id,o.observation_id)['observation'];self.assertEqual(resolved['region_status'],'incompatible');self.assertFalse(resolved['in_moscow_region']);self.assertEqual(resolved['region_evidence'],o.region_evidence)
  for changed in (replace(o,in_moscow_region=1),replace(o,region_status='client_confirmed')):
   with self.assertRaises(ValueError):changed.payload()
 def test_source_provenance_amount_date_image_and_coverage_adversaries(self):
  a=self.acquire();o=self.observation()
  for field,value in [('raw_amount',True),('raw_amount',float('nan')),('raw_amount',0),('raw_amount',101),('observed_at',float('inf')),('observed_at',time.time()+100),('source_sha256','f'*64),('issuer','client'),('url','https://other.test/card')]:
   with self.subTest(field=field,value=value),self.assertRaises(ValueError):self.store.append_observation(a.product_id,replace(o,**{field:value}))
  for image in (replace(o.image,observed_at=o.observed_at-1),replace(o.image,local_path='../old.png'),replace(o.image,sha256='bad'),replace(o.image,size=True),replace(o.image,captured_at=o.observed_at-1),replace(o.image,context_evidence={'url':'https://foreign.test/'})):
   with self.assertRaises(ValueError):self.store.append_observation(a.product_id,replace(o,image=image))
  with self.assertRaises(ValueError):self.store.publish_coverage(a.product_id,'s0.test',status='no_match',observed_at=time.time(),product_presence='observed')
  with self.assertRaises(ValueError):self.store.publish_coverage(a.product_id,'s0.test',status='found',observed_at=time.time())
  self.store.publish_coverage(a.product_id,'s0.test',status='no_match',observed_at=time.time()-1);self.assertEqual(self.store.lookup(self.identity)['coverage']['s0.test']['product_presence'],'unknown')

 def test_normal_read_validates_heads_only_and_audit_retains_history(self):
  a=self.acquire();now=time.time()
  for n in range(40):self.store.append_observation(a.product_id,self.observation(100+n,stamp=now-100+n))
  with patch.object(self.store,'_observation',wraps=self.store._observation) as validate:
   normal=self.store.lookup(self.identity);self.assertEqual(validate.call_count,1)
  self.assertFalse(normal['history_included']);self.assertEqual(normal['history'],[])
  with patch.object(self.store,'_observation',wraps=self.store._observation) as validate:
   audit=self.store.lookup(self.identity,include_history=True);self.assertEqual(validate.call_count,40)
  self.assertTrue(audit['history_included']);self.assertEqual(len(audit['history']),40);self.assertEqual(audit['heads'],normal['heads']);self.assertEqual(audit['revision'],normal['revision'])
  current=self.observation(200,stamp=now-1,state='review');self.store.append_observation(a.product_id,current)
  with patch.object(self.store,'_observation',wraps=self.store._observation) as validate:
   newest=self.store.read_snapshot([self.identity])['products'][a.product_id];self.assertEqual(validate.call_count,2)
  self.assertEqual(newest['heads'][0]['last_good']['raw_amount'],139)
  self.assertEqual(self.store.all_snapshot()['products'][a.product_id]['heads'],newest['heads'])
  self.assertEqual(len(self.store.all_snapshot(include_history=True)['products'][a.product_id]['history']),41)
  self.assertEqual(normal['heads'][0]['current']['raw_amount'],139)
