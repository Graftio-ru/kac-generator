"""Offline manufacturer-registry identity resolution, no acquisition/network execution."""
import hashlib,tempfile,time,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from .models import SpecPosition
from .search.catalogue import CatalogueStore,ProductIdentity,SourceObservation,ImageEvidence
from .search.catalogue_projection import project_product,catalogue_scope
from .offer_selection import offer_is_comparable

class CatalogueMakerAliasTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=CatalogueStore(self.root/'catalogue.sqlite3')
        self.identity=ProductIdentity('article','ABC123','DKC')
        self.resources=[f's{i}.test' for i in range(36)]
    def aliases(self):
        return [ProductIdentity('article','ABC123',maker) for maker in ('DKC','ДКС','dkc','дкс','DkC')]
    def position(self,maker,qty=1):
        return SpecPosition(number=1,name='Изделие',type_mark='ABC123',unit='шт',qty=qty,
            source_evidence={'reader':'native_pdf','cells':{'name':'Изделие','model':'','article':'ABC123','maker':maker}})
    def observe(self,identity=None):
        from PIL import Image
        identity=identity or self.identity;stamp=time.time()-30;captured=stamp+1;oid=uuid.uuid4().hex
        path=self.root/'source.png';Image.new('RGB',(20,10),'white').save(path);data=path.read_bytes()
        url='https://s0.test/card?id=7';text='Изделие DKC ABC123\nЦена 100 руб.\nза 1 шт'
        image=ImageEvidence(oid,url,stamp,'captured',sha256=hashlib.sha256(data).hexdigest(),local_path=path.name,
            size=len(data),captured_at=captured,context_evidence=dict(version=1,role='review_context',filename=path.name,
            sha256=hashlib.sha256(data).hexdigest(),url=url,captured_at=captured,width=20,height=10,
            document_width=20,document_height=10,truncated=False))
        observed=SourceObservation(oid,identity,'s0.test',url,url,stamp,'supplier_rendered','Изделие DKC ABC123','ABC123',
            text,hashlib.sha256(text.encode()).hexdigest(),100,'RUB','unknown','шт',None,None,'','unknown','observed',
            'Сбербанк','7707083893',image,'offline-fixture-v1')
        self.store.append_observation(identity.key,observed)
        return observed
    def test_known_empty_aliases_are_reads_no_new_reservation(self):
        first=self.store.acquire_unknown(self.identity,self.resources,owner='first')
        revision=self.store.lookup(self.identity)['revision']
        for alias in self.aliases():
            known=self.store.lookup(alias)
            self.assertEqual(known['product_id'],self.identity.key)
            self.assertEqual(known['identity']['manufacturer'],'DKC')
            self.assertEqual(known['heads'],[]);self.assertEqual(len(known['coverage']),36)
            self.assertFalse(self.store.acquire_unknown(alias,self.resources,owner='another').allowed)
            self.assertEqual(self.store.lookup(alias)['revision'],revision)
        view=self.store.read_snapshot(self.aliases())
        self.assertEqual(set(view['products']),{a.key for a in self.aliases()})
        self.assertEqual(len(self.store.all_snapshot()['products']),1)
    def test_partial_original_issuer_date_image_ref_and_qty_survive_alias_specs(self):
        self.store.acquire_unknown(self.identity,self.resources,owner='first');observed=self.observe()
        from . import config
        with patch.object(config,'CACHE_DIR',self.root):
            for alias in self.aliases():
                for qty in (1,150):
                    position=self.position(alias.manufacturer,qty)
                    snapshot=self.store.read_snapshot([alias]);record=snapshot['products'][alias.key]
                    result=project_product(self.store,alias,position,snapshot=record)
                    offer=result.offers[0]
                    self.assertEqual(offer.price_with_vat,100)
                    self.assertEqual(offer.catalogue_ref,{'product_id':self.identity.key,'observation_id':observed.observation_id})
                    self.assertEqual(offer.catalogue_observed_at,observed.observed_at)
                    self.assertEqual(offer.catalogue_image['sha256'],observed.image.sha256)
                    self.assertEqual(offer.screenshot_product,str(self.root.resolve()/'source.png'))
                    with catalogue_scope(self.store,snapshot=snapshot):self.assertTrue(offer_is_comparable(offer,position))
        self.assertEqual(len(self.store.lookup(self.identity,include_history=True)['history']),1)
    def test_concurrent_first_alias_reservation_has_only_one_product_and_lease(self):
        identities=self.aliases()*3
        def reserve(identity):return CatalogueStore(self.store.path).acquire_unknown(identity,self.resources,owner='concurrent')
        with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(reserve,identities))
        self.assertEqual(sum(a.allowed for a in results),1)
        actual=next(a.product_id for a in results if a.allowed)
        self.assertTrue(all(a.product_id==actual for a in results))
        self.assertEqual(len(self.store.all_snapshot()['products']),1)
        self.assertEqual(len(self.store.lookup(self.identity)['coverage']),36)
    def test_existing_duplicate_exact_key_is_ambiguous_known_and_never_reacquired(self):
        self.store.acquire_unknown(self.identity,self.resources,owner='old-one');self.observe()
        alternate=ProductIdentity('article','ABC123','ДКС')
        # Simulate already existing pre-registry records, not a runtime migration.
        with patch.object(self.store,'_identity_candidates',return_value=[]):
            self.store.acquire_unknown(alternate,self.resources,owner='old-two')
        self.observe(alternate)
        for identity in self.aliases():
            record=self.store.lookup(identity)
            self.assertTrue(record['known']);self.assertTrue(record['identity_ambiguous'])
            self.assertEqual(record['product_id'],'');self.assertEqual(record['heads'],[])
            self.assertEqual(record['conflicting_product_ids'],sorted([self.identity.key,alternate.key]))
            self.assertTrue(all(v['status']=='unknown' and v['observed_at'] is None for v in record['coverage'].values()))
            acquisition=self.store.acquire_unknown(identity,self.resources,owner='must-not-acquire')
            self.assertFalse(acquisition.allowed);self.assertEqual(acquisition.reason,'known_ambiguous')
            self.assertEqual(project_product(self.store,identity,self.position(identity.manufacturer)).offers,[])
        self.assertEqual(len(self.store.all_snapshot()['products']),2)
        self.assertTrue(all(len(p['heads'])==1 for p in self.store.all_snapshot()['products'].values()))
    def test_no_unknown_maker_or_changed_neutral_identity_widening(self):
        self.store.acquire_unknown(self.identity,self.resources,owner='first')
        for other in (ProductIdentity('article','ABC124','ДКС'),ProductIdentity('article','ABC123','IEK'),
                      ProductIdentity('article','ABC123','DKC GROUP'),ProductIdentity('model','ABC123','DKC','Изделие')):
            self.assertIsNone(self.store.lookup(other))
        unknown=ProductIdentity('article','CODE','Maker')
        self.store.acquire_unknown(unknown,self.resources,owner='unknown-maker')
        self.assertIsNone(self.store.lookup(ProductIdentity('article','CODE','maker')))
        model=ProductIdentity('model','MODEL','DKC','Изделие',(('rating','10'),))
        self.store.acquire_unknown(model,self.resources,owner='model')
        self.assertEqual(self.store.lookup(ProductIdentity('model','MODEL','ДКС','Изделие',(('rating','10'),)))['product_id'],model.key)
        self.assertIsNone(self.store.lookup(ProductIdentity('model','MODEL','ДКС','Другое',(('rating','10'),))))
        self.assertIsNone(self.store.lookup(ProductIdentity('model','MODEL','ДКС','Изделие',(('rating','20'),))))
        self.assertIsNone(self.store.lookup(ProductIdentity('model','MODEL','','Изделие',(('rating','10'),))))
