"""Exact source-contract regressions; actual captured facts are read-only."""
import copy
import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from .models import SpecPosition
from .schemas import PositionIn
from .search.manufacturer_aliases import manufacturer_equal
from .search.catalogue import CatalogueStore, ProductIdentity
from .search.catalogue_projection import _identity_reason, _facts, project_product
from .search.catalogue_producer import capture_and_publish
from .search.catalogue_images import CatalogueImageStore
from . import test_catalogue_producer as producer_fixtures

ROOT = Path(__file__).resolve().parents[2]
ORIGINAL = ROOT/'sources/kac-generator/full-service-2026-10-14-typed-supplier-boundary-slice-1'
PUBLIC = ROOT/'sources/kac-generator/original-native96-resume-2026-10-14/step-1/public.json'
TAXONOMY = ROOT/'sources/kac-generator/candidate-taxonomy-2026-10-14/result.json'


class TwoComparatorTests(unittest.TestCase):
    def test_complete_verified_composites_only(self):
        for label in ('DKC (ДКС)', ' ДКС\t( dkc ) ', 'DKC (ДКС) (DKC)'):
            self.assertTrue(manufacturer_equal('DKC', label), label)
        for label in ('DKC (IEK)', 'DKC (unknown)', 'DKC (ДКС fake)',
                      'DKC / ДКС', 'DKC (ДКС) GROUP', 'DKC ((ДКС))',
                      'DKC ()', '(DKC)', 'DKC (ДКС, IEK)', 'DKC123 (ДКС)',
                      'DKC (ДКС) ' + 'x'*513):
            self.assertFalse(manufacturer_equal('DKC', label), label)
        self.assertTrue(manufacturer_equal('Unregistered', 'Unregistered'))
        self.assertFalse(manufacturer_equal('Unregistered', 'Unregistered (Unregistered)'))
        self.assertFalse(manufacturer_equal('IEK', 'ИЭК'))

    def test_whitespace_does_not_normalize_substantive_title(self):
        pos = SpecPosition(number=1,name='Изделие',type_mark='ABC123',unit='шт',qty=1)
        observation = dict(title='Изделие ABC123',manufacturer_code='ABC123',
            url='https://shop.test/product/abc123',identity={'manufacturer':'DKC'},
            source_text='Изделие\n\t ABC123\nПроизводитель: DKC (ДКС)\n100 руб. за 1 шт')
        self.assertEqual(_identity_reason(observation,pos),'')
        for text in ('Изделие ABC124','изделие ABC123','Изделие ABC-123'):
            changed=dict(observation,source_text=observation['source_text'].replace('Изделие\n\t ABC123',text))
            self.assertIn('сохранённым текстом',_identity_reason(changed,pos))
        for maker in ('DKC (IEK)','DKC (unknown)'):
            changed=dict(observation,source_text=observation['source_text'].replace('DKC (ДКС)',maker))
            self.assertIn('Производитель',_identity_reason(changed,pos))

    def test_all_eleven_retained_candidates_and_original_conflict_states(self):
        baseline=json.loads(TAXONOMY.read_text())
        positions={v['number']:v for v in json.loads((ORIGINAL/'parsed.json').read_text())['positions']}
        public=json.loads(PUBLIC.read_text())
        db=ORIGINAL/'catalogue/catalogue.sqlite3'
        original_hash=hashlib.sha256(db.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'catalogue.sqlite3'
            with sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True) as source,sqlite3.connect(target) as destination:
                source.backup(destination)
            store=CatalogueStore(target);before=store.all_snapshot();seen=[]
            for row in public['rows']:
                for value in row.get('candidates',[]):
                    pos=PositionIn.model_validate(positions[row['number']]).to_result().position
                    ref=value['catalogue_ref']
                    observation=store.resolve_observation(ref['product_id'],ref['observation_id'])['observation']
                    self.assertNotIn('native_request_contract',observation) # Original legacy payloads unchanged.
                    expected=next(v for v in baseline if v['url']==observation['url'])
                    reason=_identity_reason(observation,pos);seen.append(observation['url'])
                    if 'favorit-el.ru' in observation['url'] or 'electro-mpo.ru' in observation['url']:
                        self.assertEqual(reason,'',observation['url'])
                        self.assertEqual(observation['state'],'identity_conflict')
                        self.assertIsNone(_facts(observation,pos)[0]) # No old-state promotion.
                    elif 'dkc.market' in observation['url']:
                        self.assertTrue(reason) # Never becomes eligible after whitespace repair.
                    elif 'chipdip.ru' in observation['url']:
                        self.assertIn('Класс точности',reason) # Owned property 0.5 contradicts title/native 0.5S.
                    else:
                        if expected['identity_reason']:
                            self.assertTrue(reason,observation['url']) # Same refusal, possibly more specific.
                        else:self.assertEqual(reason,'',observation['url'])
                    self.assertEqual(_facts(observation,pos)[1],expected['facts_reason'],observation['url'])
            self.assertEqual(len(seen),11)
            self.assertEqual(store.all_snapshot(),before)
        self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(),original_hash)

    def test_normal_fresh_capture_appends_without_rewriting_old_conflict(self):
        fixture=producer_fixtures.CatalogueProducerTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        identity=ProductIdentity('model','ABC123','DKC','Изделие')
        fixture.store.acquire_unknown(identity,['shop.test'],owner='test')
        fixture.identity=identity
        fixture.pos=fixture.pos.model_copy(update={'source_evidence':dict(reader='native_pdf',cells=dict(name='Изделие',model='ABC123',article='',maker='DKC'))})
        old_stamp=time.time()-30
        old_snapshot,old_offer=fixture.capture(stamp=old_stamp,
            text='Изделие\n ABC123\nАртикул: ABC123\nПроизводитель: DKC (ДКС)\n100 руб. с НДС\nза 1 шт')
        # Recreate the prior immutable reviewed generation, not a price authority.
        from .search.catalogue_producer import publish_snapshot
        prior=publish_snapshot(fixture.store,identity,fixture.pos,old_snapshot,image_store=fixture.images,offer=old_offer)
        with tempfile.TemporaryDirectory() as directory:
            store=CatalogueStore(Path(directory)/'catalogue.sqlite3');store.acquire_unknown(identity,['shop.test'],owner='test')
            prior=replace(prior,state='identity_conflict');store.append_observation(identity.key,prior)
            frozen=copy.deepcopy(store.resolve_observation(identity.key,prior.observation_id)['observation'])
            new_snapshot,new_offer=fixture.capture(text='Изделие\n\tABC123\nАртикул: ABC123\nПроизводитель: DKC (ДКС)\n90 руб. с НДС\nза 1 шт',price=90)
            current=capture_and_publish(store,identity,fixture.pos,fixture.url,browser=object(),
                image_store=fixture.images,capture=lambda *a,**k:(new_snapshot,new_offer))
            self.assertEqual(current.state,'observed');self.assertEqual(current.raw_amount,90)
            self.assertGreater(current.observed_at,prior.observed_at)
            self.assertNotEqual(current.observation_id,prior.observation_id)
            old=store.resolve_observation(identity.key,prior.observation_id)
            self.assertFalse(old['current']);self.assertEqual(old['observation'],frozen)
            self.assertEqual(project_product(store,identity,fixture.pos).offers[0].price_with_vat,90)


class NativeCombinedVABlocker(unittest.TestCase):
    """Separate failing prerequisite: not included in comparator acceptance command."""
    def test_legitimate_maker_cannot_make_actual_wrong_3va_match_native_5va(self):
        public=json.loads(PUBLIC.read_text())
        row=next(v for v in public['rows'] if v['number']==11)
        value=next(v for v in row['candidates'] if 'dkc.market' in v['url'])
        original=next(v for v in json.loads((ORIGINAL/'parsed.json').read_text())['positions'] if v['number']==11)
        pos=PositionIn.model_validate(original).to_result().position
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'catalogue.sqlite3'
            with sqlite3.connect('file:'+str(ORIGINAL/'catalogue/catalogue.sqlite3')+'?mode=ro',uri=True) as source,sqlite3.connect(target) as destination:
                source.backup(destination)
            ref=value['catalogue_ref']
            observation=CatalogueStore(target).resolve_observation(ref['product_id'],ref['observation_id'])['observation']
            self.assertTrue(_identity_reason(observation,pos)) # Exact saved source remains refused.
            changed=dict(observation,source_text=observation['source_text']+'\nПроизводитель: DKC')
            self.assertTrue(_identity_reason(changed,pos), 'Native 5VA must refuse actual 3VA even with genuine manufacturer label')
            from .search.catalogue import ProductIdentity,identity_for_position
            from .search.catalogue_producer import CataloguePublicationError
            from .search.card_batch import CardSnapshot
            from .search.product_identity import valid_identity
            proof=dict(version=1,provenance='rendered_main_card',source_url=changed['url'],canonical_url=changed['url'],
                host='dkc.market',expected_mark=pos.type_mark,expected_name=pos.name,main_title=changed['title'],
                main_article=changed['manufacturer_code'],observed_at=changed['observed_at'])
            self.assertFalse(valid_identity(proof,request_position=pos,source_text=changed['source_text']))
            isolated=CatalogueStore(Path(directory)/'publication.sqlite3');identity=identity_for_position(pos)
            isolated.acquire_unknown(identity,['dkc.market'],owner='test')
            images=CatalogueImageStore(Path(directory)/'images')
            snapshot=CardSnapshot.create(dict(url=changed['url'],requested_url=changed['url'],main_title=changed['title'],
                page_article=changed['manufacturer_code'],captured_at=changed['observed_at'],text=changed['source_text'],
                identity_evidence=proof,screenshot_context='',context_capture_evidence={}))
            with self.assertRaises(CataloguePublicationError) as error:
                capture_and_publish(isolated,identity,pos,changed['url'],browser=object(),image_store=images,
                    capture=lambda *a,**kw:(snapshot,None),supplier_host='dkc.market')
            self.assertEqual(error.exception.code,'catalogue_owner_unconfirmed')
            self.assertEqual(isolated.lookup(identity)['heads'],[])

