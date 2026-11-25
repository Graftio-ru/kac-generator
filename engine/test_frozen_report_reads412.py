"""Frozen read reuse boundaries; native96 equivalence is separate acceptance."""
import unittest
from types import SimpleNamespace
from .search import catalogue_projection as p

class Store:
 def __init__(self):self.lookups=[];self.resolves=[];self.revision=1
 def lookup(self,identity):self.lookups.append(identity.key);return {'product_id':identity.key,'revision':self.revision}
 def resolve_observation(self,product,observation):self.resolves.append((product,observation));return {'product_id':product,'observation':{'observation_id':observation},'revision':self.revision}

class FrozenReadTests(unittest.TestCase):
 def test_live_calls_do_not_reuse_or_hide_revision(self):
  store=Store();identity=SimpleNamespace(key='a')
  self.assertEqual(p._lookup_product(store,identity)['revision'],1);store.revision=2
  self.assertEqual(p._lookup_product(store,identity)['revision'],2);self.assertEqual(len(store.lookups),2)
  with p.catalogue_scope(store):
   p._resolve_observation(store,'a','one');p._resolve_observation(store,'a','one')
  self.assertEqual(len(store.resolves),2)
 def test_frozen_native_reads_once_per_exact_key(self):
  store=Store()
  with p.catalogue_scope(store,snapshot={'revision':1}):
   a=p._lookup_product(store,SimpleNamespace(key='a'));self.assertIs(a,p._lookup_product(store,SimpleNamespace(key='a')))
   p._lookup_product(store,SimpleNamespace(key='alias'))
   p._resolve_observation(store,'a','one');p._resolve_observation(store,'a','one');p._resolve_observation(store,'a','two');p._resolve_observation(store,'b','one')
  self.assertEqual(store.lookups,['a','alias']);self.assertEqual(store.resolves,[('a','one'),('a','two'),('b','one')])
 def test_other_store_never_uses_scoped_authority(self):
  one=Store();two=Store()
  with p.catalogue_scope(one,snapshot={'revision':1}):
   p._lookup_product(two,SimpleNamespace(key='a'));two.revision=2
   self.assertEqual(p._lookup_product(two,SimpleNamespace(key='a'))['revision'],2)
  self.assertEqual(len(two.lookups),2)
 def test_nested_scope_and_exception_restore_parent(self):
  store=Store();identity=SimpleNamespace(key='a')
  with p.catalogue_scope(store,snapshot={'revision':1}):
   outer=p._lookup_product(store,identity)
   with self.assertRaises(RuntimeError):
    with p.catalogue_scope(store,snapshot={'revision':2}):
     self.assertIsNot(p._lookup_product(store,identity),outer);raise RuntimeError('fixture')
   self.assertIs(p._lookup_product(store,identity),outer)
  store.revision=2
  with p.catalogue_scope(store,snapshot={'revision':2}):self.assertEqual(p._lookup_product(store,identity)['revision'],2)
  self.assertEqual(len(store.lookups),3)
 def test_failed_native_read_is_not_cached(self):
  store=Store();identity=SimpleNamespace(key='a');calls=[]
  def read():
   calls.append(1)
   if len(calls)==1:raise ValueError('native_refusal')
   return {'product_id':'a'}
  with p.catalogue_scope(store,snapshot={'revision':1}):
   with self.assertRaises(ValueError):p._scoped_read(store,'product',identity.key,read)
   self.assertEqual(p._scoped_read(store,'product',identity.key,read),{'product_id':'a'})
  self.assertEqual(len(calls),2)
if __name__=='__main__':unittest.main()
