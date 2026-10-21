"""Source-bound article + model + explicit VA/class obligations, no acquisition."""
import copy
import hashlib,json,uuid
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from .models import SpecPosition
from .search.catalogue import CatalogueStore,identity_for_position
from .search.catalogue_projection import _identity_reason,project_product,catalogue_scope
from .search.catalogue_producer import refresh_recorded,CataloguePublicationError
from .search.product_identity import (native_article_reason,native_source_spec_reason,
    valid_identity,_original_native_model_role,_resolved_model_role,_NATIVE_ARTICLE_INTENT)
from . import test_semantic_purchase_capture as fixtures


class NativeCompoundTests(unittest.TestCase):
    def position(self,article='CODE-5VA',power='5',accuracy='0.5'):
        name=f'Transformer, accuracy class {accuracy}, {power} VA'
        model='TR30 300/5A'
        return SpecPosition(number=1,name=name,type_mark=model+' / '+article,unit='шт',qty=6,
            source_evidence=dict(reader='native_pdf',cells=dict(name=name,model=model,article=article,maker='Maker')))

    def frame(self,p,*,article=None,power=None,accuracy=None,label=True,title_article=True):
        article=article if article is not None else p.source_evidence['cells']['article']
        power=power if power is not None else '5'
        accuracy=accuracy if accuracy is not None else '0.5'
        title='Transformer TR30 300/5A Maker'+(' '+article if title_article else '')
        if power:title+=' '+power+' VA'
        if accuracy:title+=' accuracy class '+accuracy
        text=title+'\nManufacturer: Maker\n'
        if label:text+='Manufacturer code: '+article+'\n'
        text+='100 ₽\nSale unit:\nшт'
        return dict(kind='complete',missing_fields=[],owner_bound=True,document_url='https://shop.test/product/TR30',
            document_title=title,main_title=title,article=article if label else '',owner_text=text,purchase_text=text,
            owner_truncated=False,body=text,body_truncated=False,observed_at=time.time())

    def proof(self,p,f):
        return dict(version=1,provenance='rendered_main_card',source_url=f['document_url'],canonical_url=f['document_url'],
            host='shop.test',expected_mark=p.type_mark,expected_name=p.name,main_title=f['main_title'],
            main_article=f['article'],observed_at=f['observed_at'])

    def capture(self,p,f):
        helper=fixtures.SemanticCaptureTests();helper.setUp();self.addCleanup(helper.doCleanups)
        return helper.run_capture(f,p)

    def test_native_binding_article_title_and_supplier_number(self):
        p=self.position();f=self.frame(p)
        self.assertTrue(valid_identity(self.proof(p,f),request_position=p,source_text=f['owner_text']))
        for change in ({'source_evidence':dict(p.source_evidence,cells=dict(p.source_evidence['cells'],article='FOREIGN'))},
                       {'review_fields':{'article':'uncertain'}},{'name':'Different immutable source name'}):
            bad=p.model_copy(update=change)
            self.assertTrue(native_article_reason(bad,f['main_title'],f['article']))
        for label in ('Supplier number','Номер поставщика'):
            no_article=self.frame(p,label=False,title_article=False)
            no_article['owner_text']+='\n'+label+': CODE-5VA'
            self.assertTrue(native_article_reason(p,no_article['main_title'],no_article['article']))
            self.assertFalse(valid_identity(self.proof(p,no_article),request_position=p))
        title_only=self.frame(p,label=False)
        self.assertEqual(native_article_reason(p,title_only['main_title'],''),'')
        wrong=self.frame(p,article='CODE-3VA')
        self.assertIn('противоречит',native_article_reason(p,wrong['main_title'],wrong['article']))

    def test_explicit_va_class_missing_conflicting_and_positive(self):
        for power,accuracy,expected in (('5','0.5',True),('5.0','0,5',True),
            ('3','0.5',False),('2.5','0.5',False),('5','0.5S',False),('','0.5',False),('5','',False)):
            with self.subTest(power=power,accuracy=accuracy):
                p=self.position();f=self.frame(p,power=power,accuracy=accuracy)
                reason=native_source_spec_reason(p,f['main_title'],f['owner_text'],f['article'])
                self.assertEqual(not reason,expected,reason)
        p=self.position(article='CODE-2.5VA',power='2.5',accuracy='0.5S')
        f=self.frame(p,power='2.5',accuracy='0.5S')
        self.assertEqual(native_source_spec_reason(p,f['main_title'],f['owner_text'],f['article']),'')
        f['owner_text']+='\nМощность: 3 ВА'
        self.assertIn('противоречит',native_source_spec_reason(p,f['main_title'],f['owner_text'],f['article']))

    def test_normal_capture_rejects_wrong_va_and_class_and_preserves_correct_generation(self):
        for power,accuracy,positive in (('5','0.5',True),('3','0.5',False),('5','0.5S',False),('','0.5',False)):
            with self.subTest(power=power,accuracy=accuracy):
                p=self.position();f=self.frame(p,power=power,accuracy=accuracy)
                try:store,identity,observed=self.capture(p,f)
                except CataloguePublicationError as error:
                    self.assertFalse(positive,error.code)
                    self.assertIn(error.code,{'catalogue_owner_unconfirmed','catalogue_source_binding_invalid'})
                    continue
                self.assertIsNotNone(observed)
                self.assertEqual((observed.raw_amount,observed.observed_at),(100,f['observed_at']))
                with catalogue_scope(store):
                    projected=project_product(store,identity,p).all_offers[0]
                    from .offer_selection import offer_is_comparable
                    self.assertEqual(offer_is_comparable(projected,p),positive,projected.unit_review_reason)
                self.assertEqual(observed.state=='observed',positive)
                if positive:self.assertEqual(observed.validation_version,_NATIVE_ARTICLE_INTENT)

    def test_normal_correct_two_point_five_va_and_contract_corruption_fail_closed(self):
        p=self.position(article='SKU2500',power='2.5',accuracy='0.5S')
        frame=self.frame(p,article='SKU2500',power='2.5',accuracy='0.5S')
        store,identity,original=self.capture(p,frame)
        self.assertEqual(original.state,'observed')
        self.assertEqual(original.native_request_contract['model'],p.source_evidence['cells']['model'])
        self.assertEqual(original.native_request_contract['requested'],p.name)
        self.assertEqual(original.native_request_contract['identity_sha256'],identity.key)
        with catalogue_scope(store):
            from .offer_selection import offer_is_comparable
            self.assertTrue(offer_is_comparable(project_product(store,identity,p).all_offers[0],p))
        for changes in ({'identity_sha256':'0'*64},{'article':'FOREIGN'}, {'mark':'TR30'},
                        {'issuer':'approved_import'},{'name':'Foreign native cell'},
                        {'version':True},{'extra':'untrusted'}):
            malformed=dict(original.native_request_contract,**changes)
            with self.assertRaises(ValueError):replace(original,native_request_contract=malformed).payload()
        current=original.payload()
        generic=SpecPosition(number=1,name=current['title'],type_mark=identity.code,unit='шт',qty=1)
        from .search.product_identity import _native_constraints
        for changed in (dict(current,native_request_contract=None),
                        dict(current,native_request_contract=dict(current['native_request_contract'],identity_sha256='0'*64))):
            with _resolved_model_role(changed,identity):self.assertIsNone(_native_constraints(generic))
        legacy=replace(original,native_request_contract=None,validation_version='catalogue-native-v1')
        self.assertNotIn('native_request_contract',legacy.payload())
        self.assertEqual((legacy.observed_at,legacy.raw_amount,legacy.source_sha256),
                         (original.observed_at,original.raw_amount,original.source_sha256))
        with _resolved_model_role(legacy.payload(),identity):self.assertIsNone(_native_constraints(generic))
        unknown=p.model_copy(update={'source_evidence':{}})
        self.assertIsNone(_native_constraints(unknown))

    def test_refresh_reload_uses_only_resolved_versioned_article_context(self):
        p=self.position();first=self.frame(p);store,identity,original=self.capture(p,first)
        self.assertEqual(original.validation_version,_NATIVE_ARTICLE_INTENT)
        # Use the same real producer capture entrypoint under owned refreshed context.
        helper=fixtures.SemanticCaptureTests();helper.setUp();self.addCleanup(helper.doCleanups)
        from .search import card_attachments,agent
        from .search.catalogue_images import CatalogueImageStore
        from .search.card_batch import CardSnapshot
        def callback(browser,position,url,**kwargs):
            fresh=self.frame(p);fresh['observed_at']=time.time()
            proof=self.proof(position,fresh)
            self.assertTrue(valid_identity(proof,request_position=position,source_text=fresh['owner_text']))
            root=Path(helper.temp.name);folder=root/'main_facts';folder.mkdir(exist_ok=True)
            owned=dict(fresh,version=1,role='main_card_facts',requested_url=url)
            path=folder/(uuid.uuid4().hex+'.json');data=json.dumps(owned,ensure_ascii=False).encode();path.write_bytes(data)
            ref=dict(ref=str(path.relative_to(root)),sha256=hashlib.sha256(data).hexdigest(),observed_at=fresh['observed_at'])
            return CardSnapshot.create(dict(url=url,requested_url=url,main_title=fresh['main_title'],page_article=fresh['article'],
                captured_at=fresh['observed_at'],text=fresh['owner_text'],purchase_text=fresh['purchase_text'],main_facts=ref,
                identity_evidence=proof,screenshot_context='',context_capture_evidence={})),None
        report=refresh_recorded(store,browser=object(),image_store=CatalogueImageStore(Path(helper.temp.name)),capture=callback)
        self.assertEqual(report[0]['outcome'],'observed',report)
        current=CatalogueStore(store.path).lookup(identity)['heads'][0]['current']
        self.assertGreater(current['observed_at'],original.observed_at)
        self.assertEqual(current['validation_version'],_NATIVE_ARTICLE_INTENT)
        self.assertNotEqual(current['observation_id'],original.observation_id)
        self.assertEqual(store.resolve_observation(identity.key,original.observation_id)['observation']['observed_at'],original.observed_at)
        generic=SpecPosition(number=1,name=current['title'],type_mark=identity.code,unit='шт',qty=1)
        negative=self.frame(p,power='3')
        with _resolved_model_role(current,identity):
            self.assertIn('противоречит',native_source_spec_reason(generic,negative['main_title'],negative['owner_text'],negative['article']))
        for forged in (dict(current,validation_version='unknown'),dict(current,identity=dict(current['identity'],code='OTHER'))):
            with _resolved_model_role(forged,identity):
                # No private article intent can be inherited from these records.
                from .search.product_identity import _native_constraints
                self.assertIsNone(_native_constraints(generic))
