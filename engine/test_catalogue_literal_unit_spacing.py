"""Literal same-price slash denomination whitespace; no inferred sale unit."""
import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from . import config
from .models import SpecPosition
from .search.catalogue import CatalogueStore,ProductIdentity
from .search.catalogue_projection import _facts,project_product,catalogue_scope
from .offer_selection import offer_is_comparable
from .source_quote import quote_review_reason,selected_price_context,primary_text
from . import test_catalogue_projection as fixtures

ROOT=Path(__file__).resolve().parents[2]
FRESH=ROOT/'sources/kac-generator/native-compound-recorded-cards-2026-10-14'
OID='4c2f7bc4027e44079fe01aec7d1112f2'

class LiteralUnitTests(unittest.TestCase):
    def test_same_cash_horizontal_slash_whitespace_only(self):
        helper=fixtures.CatalogueProjectionTests();helper.setUp();self.addCleanup(helper.doCleanups)
        for space in ('',' ','\t','\u00a0','\u202f','  '):
            observation=helper.observe(text='Изделие ABC123\n100 руб. /'+space+'шт')
            self.assertEqual(_facts(observation.payload(),helper.position)[0]['price'],100)
        for suffix in ('/ м','/ 10 шт','/','шт','/\nшт'):
            observation=helper.observe(text='Изделие ABC123\n100 руб. '+suffix)
            self.assertIsNone(_facts(observation.payload(),helper.position)[0],suffix)
        observation=helper.observe(text='Изделие ABC123\nот 100 руб. / шт')
        self.assertIsNone(_facts(observation.payload(),helper.position)[0])
        observation=helper.observe(text='Изделие ABC123\n100 руб. без НДС / шт',vat='excluded')
        self.assertIsNone(_facts(observation.payload(),helper.position)[0])
        observation=helper.observe(text='Изделие ABC123\n100 руб. / компл',sale='компл')
        self.assertIsNone(_facts(observation.payload(),helper.position)[0])
        observation=helper.observe(text='Изделие ABC123\n100 руб. / 10 шт',sale='10 шт',lot=10)
        self.assertEqual(_facts(observation.payload(),helper.position)[0]['price'],10)
        observation=helper.observe(text='Изделие ABC123\n100 руб. / шт\nПохожие товары\n200 руб. / м')
        self.assertEqual(_facts(observation.payload(),helper.position)[0]['price'],100)

    def test_noncatalogue_consumer_uses_same_literal_unit_proof(self):
        from . import test_source_quote as noncatalogue
        fixture=noncatalogue.SourceQuoteTests()
        for suffix,expected in (('/шт',True),('/ шт',True),('/\tшт',True),('/\u00a0шт',True),
                                ('/ м',False),('/ 10 шт',False),('шт',False),('/\nшт',False)):
            page,pos,offer=fixture.produce(price=100,sale='шт',unit='шт',title='Изделие 3526410',unit_text=suffix)
            self.assertEqual(not quote_review_reason(offer,pos),expected,suffix)
        _,pos,offer=fixture.produce(price=100,listed=1000,quantity=10,sale='10 шт',unit='шт',
            title='Изделие 3526410',unit_text='/ 10 шт')
        self.assertEqual(quote_review_reason(offer,pos),'')

    def test_actual_fresh_observation_normal_read_preserves_source_date_hash_image(self):
        snapshot=json.loads((FRESH/'catalogue-after.json').read_text())
        observed=next(h['current'] for h in snapshot['heads'] if h['current']['observation_id']==OID)
        p=SpecPosition.model_validate(json.loads((FRESH/'projection.json').read_text())['position'])
        price,unit=selected_price_context(primary_text(observed['source_text']),observed['raw_amount'])
        self.assertEqual(unit,'/ шт')
        self.assertIn('10 767.34 руб.',price)
        self.assertEqual(_facts(observed,p)[0]['price'],10767.34)
        source_db=FRESH/'catalogue.sqlite3';before_hash=hashlib.sha256(source_db.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'catalogue.sqlite3'
            with sqlite3.connect('file:'+str(source_db)+'?mode=ro',uri=True) as source,sqlite3.connect(path) as target:source.backup(target)
            store=CatalogueStore(path);before=store.all_snapshot()
            data=snapshot['identity'];identity=ProductIdentity(**{**data,'features':tuple(tuple(v) for v in data['features'])})
            with patch.object(config,'CACHE_DIR',FRESH/'cache'),catalogue_scope(store):
                result=project_product(store,identity,p)
                offer=next(v for v in result.all_offers if v.catalogue_ref['observation_id']==OID)
                self.assertEqual(quote_review_reason(offer,p),'')
                self.assertTrue(offer_is_comparable(offer,p))
                self.assertEqual(offer.price_with_vat,10767.34)
                self.assertEqual(offer.catalogue_observed_at,observed['observed_at'])
                self.assertEqual(offer.catalogue_source_sha256,observed['source_sha256'])
                self.assertEqual(offer.catalogue_image,observed['image'])
            self.assertEqual(store.all_snapshot(),before)
        self.assertEqual(hashlib.sha256(source_db.read_bytes()).hexdigest(),before_hash)
