"""Exact same-seller native price ties reuse genuine stored evidence only."""
import copy,hashlib,tempfile,time,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from .models import SpecPosition
from .search.catalogue import CatalogueStore,SourceObservation,ImageEvidence,identity_for_position
from .search.catalogue_projection import catalogue_scope,project_product
from .search.selected_proofs import proof_ready
from .offer_selection import independent_offers

class NativePhotoTie341Tests(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name);self.store=CatalogueStore(self.root/'catalogue.sqlite3');self.position=SpecPosition(number=1,name='Изделие',type_mark='ABC123',unit='шт',qty=12);self.identity=identity_for_position(self.position);self.admission=self.store.acquire_unknown(self.identity,['seller.test','other.test'],owner='fixture')
  p=patch('engine.config.CACHE_DIR',self.root);p.start();self.addCleanup(p.stop)
 def source(self,price=100,photo=False,inn='7707083893'):
  oid=uuid.uuid4().hex;url='https://seller.test/product/'+oid;stamp=time.time();title='Изделие ABC123';text=f'{title}\nАртикул: ABC123\nЦена {price} руб. с НДС\nза 1 шт'
  image=ImageEvidence(oid,url,stamp,'missing',error='not_selected_price_observation')
  if photo:
   p=self.root/(oid+'.png');Image.new('RGB',(20,10),'white').save(p);data=p.read_bytes();digest=hashlib.sha256(data).hexdigest();context=dict(version=1,role='review_context',filename=p.name,sha256=digest,url=url,captured_at=stamp,width=20,height=10,document_width=20,document_height=10,truncated=False)
   image=ImageEvidence(oid,url,stamp,'captured',local_path=p.name,sha256=digest,size=len(data),captured_at=stamp,context_evidence=context)
  source=SourceObservation(oid,self.identity,'seller.test',url,url,stamp,'supplier_rendered',title,'ABC123',text,hashlib.sha256(text.encode()).hexdigest(),price,'RUB','included','шт',1,None,'','in_stock','observed','Seller',inn,image,'catalogue-v1')
  self.store.append_observation(self.identity.key,source,acquisition=self.admission,coverage_status='found');return source
 def pool(self):return project_product(self.store,self.identity,self.position).all_offers
 def test_exact_native_tie_prefers_real_ready_in_both_orders_without_mutation(self):
  missing=self.source();ready=self.source(photo=True);before=copy.deepcopy(self.store.lookup(self.identity))
  with catalogue_scope(self.store):
   offers=self.pool();a=next(o for o in offers if o.url==missing.url);b=next(o for o in offers if o.url==ready.url)
   self.assertFalse(proof_ready(a,self.position));self.assertTrue(proof_ready(b,self.position))
   for pool in ([a,b],[b,a]):self.assertIs(independent_offers(pool,self.position)[0],b)
  self.assertEqual(self.store.lookup(self.identity),before)
 def test_equal_both_missing_or_both_ready_remain_stable(self):
  for photo in (False,True):
   self.source(photo=photo);self.source(photo=photo)
   with catalogue_scope(self.store):
    offers=[o for o in self.pool()if proof_ready(o,self.position)==photo][-2:]
    for pool in (offers,list(reversed(offers))):self.assertIs(independent_offers(pool,self.position)[0],pool[0])
 def test_higher_missing_beats_lower_ready_and_near_equal_is_not_tie(self):
  a=self.source(100.01);b=self.source(100,photo=True)
  with catalogue_scope(self.store):
   pool=self.pool();chosen=independent_offers(pool,self.position);self.assertEqual(chosen[0].url,a.url);self.assertFalse(proof_ready(chosen[0],self.position))
   missing=self.source(100);offers=self.pool();near=next(o for o in offers if o.url==missing.url).model_copy(update={'price_with_vat':100.00000000001});ready=next(o for o in offers if o.url==b.url)
   self.assertFalse(proof_ready(near,self.position));self.assertIs(independent_offers([ready,near],self.position)[0],near)
 def test_different_sellers_keep_independent_cohort(self):
  a=self.source();b=self.source(photo=True,inn='7842224734')
  with catalogue_scope(self.store):self.assertEqual({o.url for o in independent_offers(self.pool(),self.position)},{a.url,b.url})
 def test_fake_flags_paths_and_corrupt_png_cannot_win(self):
  a=self.source();b=self.source(photo=True)
  with catalogue_scope(self.store):
   pool=self.pool();first=next(o for o in pool if o.url==a.url);later=next(o for o in pool if o.url==b.url)
   (self.root/b.image.local_path).write_bytes(b'bad')
   forged=later.model_copy(update={'screenshot_context':'fake.png','catalogue_image':{'status':'captured'}})
   self.assertFalse(proof_ready(forged,self.position));self.assertIs(independent_offers([first,forged],self.position)[0],first)
 def test_no_position_and_manual_letter_ties_keep_original(self):
  self.source();self.source(photo=True)
  with catalogue_scope(self.store):
   pool=self.pool()
   with patch('engine.search.selected_proofs.proof_ready',side_effect=AssertionError('no tie proof check allowed')):
    self.assertEqual(independent_offers(pool),[])
    letters=[o.model_copy(update={'from_letter':True})for o in pool];self.assertIs(independent_offers(letters,self.position)[0],letters[0])
    manual=[dict(price_with_vat=100,price_unit='шт',from_letter=True,inn='7707083893',url='https://seller.test/'+str(i))for i in range(2)];self.assertIs(independent_offers(manual,self.position)[0],manual[0])
 def test_noncomparable_cannot_replace_and_empty_pool_stays_empty(self):
  self.source();self.source(photo=True)
  with catalogue_scope(self.store):
   pool=self.pool();bad=pool[-1].model_copy(update={'refresh_failed':True,'refresh_failure_reason':'source_failed'})
   self.assertIs(independent_offers([pool[0],bad],self.position)[0],pool[0]);self.assertEqual(independent_offers([bad],self.position),[])

if __name__=='__main__':unittest.main()
