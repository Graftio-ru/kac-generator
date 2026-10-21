"""Offline adversarial identity/quote split pins; no live claims."""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from engine.models import PriceOffer, Requisites
from engine.schemas import PositionIn, OfferIn
from engine.app import _offer_to_dict
from engine.offer_selection import select_top_offers
from engine.search import agent
from engine.search.product_identity import rendered_identity, valid_identity, main_card_article
from engine.search.source_registry import SourceRegistry, product_key, confirmed_card
from engine.search.session import (research_session, observed_quote, observed_identity,
                                  batch_card_urls, ResearchProgressError, SupplierHealth)
from engine import test_all_sites


class ProductIdentityTests(unittest.TestCase):
    def test_delayed_main_card_sku_is_read_from_rendered_body(self):
        page = MagicMock()
        page.locator.return_value.count.return_value = 1
        page.inner_text.return_value = 'Tray\nарт. 3526410\nПохожие товары\nарт. 9999999'
        text = agent._identity_card_text(page, 'Tray')
        self.assertEqual('3526410', main_card_article(text))
        page.wait_for_function.assert_called_once()
        self.assertIn("closest('form')", page.wait_for_function.call_args.args[0])
        self.assertLessEqual(page.wait_for_function.call_args.kwargs['timeout'], 20000)

    def test_no_unique_primary_sku_never_uses_hidden_or_related_metadata(self):
        for count in (0, 2):
            page = MagicMock()
            page.locator.return_value.count.return_value = count
            text = 'Tray\nПохожие товары\nарт. 3526410'
            self.assertEqual(text, agent._identity_card_text(page, text))
            page.wait_for_function.assert_not_called()
            page.inner_text.assert_not_called()
            self.assertEqual('', main_card_article(text))

    def test_sku_wait_timeout_preserves_unconfirmed_or_already_printed_article(self):
        page = MagicMock()
        page.locator.return_value.count.return_value = 1
        page.wait_for_function.side_effect = TimeoutError()
        page.inner_text.return_value = 'Tray'
        self.assertEqual('Tray', agent._identity_card_text(page, 'Tray'))
        page.inner_text.assert_called_once_with('body')
        page.inner_text.return_value = 'Tray\nарт. 3526410'
        self.assertEqual('3526410', main_card_article(agent._identity_card_text(page, 'Tray')))
        page.reset_mock()
        self.assertEqual('Tray\nарт. WRONG', agent._identity_card_text(page, 'Tray\nарт. WRONG'))
        page.locator.assert_not_called()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.position = PositionIn(number=1, name='Cable', type_mark='ABC123', unit='м')
        self.url = 'https://supplier.ru/product/abc123'
        self.registry = SourceRegistry(Path(self.tmp.name)/'sources.sqlite3')
        for scoped in (patch('engine.config.CACHE_DIR',Path(self.tmp.name)),
                       patch('engine.schemas.CACHE_DIR',Path(self.tmp.name)),
                       patch('engine.search.base_db.BASE_FILE',Path(self.tmp.name)/'base.json')):
            scoped.start();self.addCleanup(scoped.stop)

    def proof(self, **changes):
        evidence = rendered_identity(self.url, self.url, 'Cable ABC123', 'ABC123',
                                     'ABC123', 'Cable', 'supplier.ru')
        return {**evidence, **changes}

    def quote(self, **changes):
        from engine.test_price_capture import fixture_capture
        return fixture_capture(PriceOffer(**{**dict(url=self.url, product_title='Cable ABC123',
            price_with_vat=100, price_unit='м', listed_price_with_vat=changes.get('price_with_vat',100),
            identity_evidence=self.proof()), **changes}))

    def run_job(self, research, search=None):
        def producer_adapter(p):
            # Replacing _research_row also replaces its trusted receipt boundary.
            from engine.search import base_db
            from engine.models import PositionResult
            with base_db.capture_current_observations() as produced:
                row=research(p)
            if produced:
                pos=p.to_result().position
                base_db.upsert(pos,PositionResult(position=pos,offers=produced,all_offers=produced),
                               confirmed_observations=produced)
            return row
        with patch('engine.search.source_registry.SourceRegistry', return_value=self.registry):
            return test_all_sites.AllSitesTests().run_job([self.position], ['supplier.ru'],
                search or (lambda *a, **kw: self.fail('Known identity must precede discovery')),
                producer_adapter, refresh_first=True)

    def test_identity_survives_unit_region_price_refusal_and_history_is_separate(self):
        self.assertTrue(self.registry.remember_identity(self.position, self.proof()))
        for status in ('review', 'no_match', 'error'):
            self.registry.save(self.position, dict(host='supplier.ru',status=status),
                               quotes=[_offer_to_dict(self.quote(unit_review_required=True))])
        saved = self.registry.load(self.position)['supplier.ru']
        self.assertEqual([self.url], saved['urls'])
        self.assertEqual('error', saved['outcome']['status'])
        history = self.registry.history(self.position, 'supplier.ru')
        self.assertEqual(['review','no_match','error'], [h['outcome']['status'] for h in history])
        self.assertTrue(all(h['checked_at'] > 0 for h in history))
        self.assertTrue(history[0]['outcome']['quotes'][0]['unit_review_required'])
        # No-price mapping uses the independent identity producer, never a
        # fabricated zero-price capture or a selectable quotation.
        from engine.search.product_identity import capture_current_identity
        page=MagicMock();page.url=self.url;page._kac_document_url=self.url;page._kac_response_status=200
        raw=dict(url=self.url,title='Cable ABC123',text='Cable ABC123\nАртикул: ABC123')
        page.evaluate.return_value=raw;page.inner_text.return_value=raw['text'];page.title.return_value=raw['title']
        page.screenshot.side_effect=lambda **kw:Path(kw['path']).write_bytes(b'offline identity-only pixels')
        identity=capture_current_identity(page,Path(self.tmp.name)/'no-price-identity.png',
                                        self.url,'ABC123','Cable','supplier.ru','')
        no_price=PriceOffer(url=self.url,price_with_vat=0,product_title='Cable ABC123',identity_evidence=identity)
        self.assertTrue(confirmed_card(self.url,dict(candidates=[_offer_to_dict(no_price)]),self.position))
        self.assertEqual({},no_price.price_capture_evidence)
        self.assertEqual([],select_top_offers([no_price],3,self.position))

    def test_missing_boolean_only_wrong_mark_and_price_mapping_are_not_proof(self):
        for evidence in ({}, {'confirmed': True}, self.proof(main_article='XYZ999'),
                         self.proof(main_title='Cable XYZ999',main_article=''),
                         self.proof(expected_mark='DIFFERENT'), self.proof(provenance='reviewed_quote')):
            self.assertFalse(self.registry.remember_identity(self.position, evidence))
        self.registry.save(self.position, dict(host='supplier.ru',status='found'), [self.url])
        self.assertEqual([], self.registry.load(self.position)['supplier.ru']['urls'])
        self.assertEqual([self.url], self.registry.load(self.position)['supplier.ru']['legacy_urls'])

    def test_additive_legacy_migration_preserves_raw_urls_and_recovers_only_explicit_proof(self):
        path=Path(self.tmp.name)/'legacy.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE source_registry(product TEXT,host TEXT,urls TEXT,outcome TEXT,PRIMARY KEY(product,host))')
            db.execute('INSERT INTO source_registry VALUES (?,?,?,?)',
                       (product_key(self.position),'supplier.ru',json.dumps([self.url]),json.dumps({'status':'review'})))
        registry=SourceRegistry(path)
        self.assertEqual([],registry.load(self.position)['supplier.ru']['urls'])
        self.assertTrue(registry.remember_identity(self.position,self.proof()))
        self.assertEqual([self.url],SourceRegistry(path).load(self.position)['supplier.ru']['urls'])
        with sqlite3.connect(path) as db:
            self.assertEqual([self.url],json.loads(db.execute('SELECT urls FROM source_registry').fetchone()[0]))

    def test_redirect_noncard_challenge_unknown_and_recommendation_never_confirmed(self):
        for url in ('https://evil.ru/product/abc123','https://supplier.ru/login',
                    'https://supplier.ru/search?q=ABC123','https://supplier.ru/catalog/',
                    'https://supplier.ru/challenge','https://supplier.ru/category/abc123',
                    'https://supplier.ru/categories/abc123','https://supplier.ru/product/xyz999'):
            self.assertFalse(valid_identity(self.proof(canonical_url=url)))
        self.assertFalse(valid_identity(self.proof(main_title='Cable', main_article='')))
        self.assertEqual('',main_card_article('Cable\nРекомендуем\nАртикул: ABC123'))
        self.assertEqual('ABC123',main_card_article('Cable\nАртикул:\nABC123\nРекомендуем\nАртикул: XYZ999'))

    def test_dates_serialization_position_binding_and_roundtrip(self):
        evidence=self.proof()
        for date in (0,-1,float('nan'),time.time()+1000,'bad',str(time.time()),True):
            self.assertFalse(valid_identity({**evidence,'observed_at':date}))
        data=_offer_to_dict(self.quote(identity_evidence=evidence))
        restored=PositionIn.model_validate({**self.position.model_dump(),'offers':[data]}).to_result().offers[0]
        self.assertEqual(evidence['main_article'],restored.identity_evidence['main_article'])
        self.assertEqual(data['identity_evidence'],OfferIn.model_validate(json.loads(json.dumps(data))).identity_evidence)
        self.assertFalse(self.registry.remember_identity(self.position.model_copy(update={'name':'Other'}),evidence))

    def test_definitive_invalidation_cannot_be_recovered_from_old_draft_but_fresh_can(self):
        proof=self.proof()
        self.registry.remember_identity(self.position,proof)
        self.registry.invalidate(self.position,'supplier.ru',self.url,'wrong item')
        self.assertEqual([],self.registry.load(self.position)['supplier.ru']['urls'])
        self.assertFalse(self.registry.remember_identity(self.position,proof))
        self.assertTrue(self.registry.remember_identity(self.position,self.proof()))
        self.assertEqual([self.url],self.registry.load(self.position)['supplier.ru']['urls'])

    def test_review_callback_preserves_identity_but_refuses_unconfirmed_price_refresh(self):
        from engine.search import base_db
        from engine.models import PositionResult
        visits=[]
        self.registry.remember_identity(self.position,self.proof())
        pos=self.position.to_result().position
        old=self.quote()
        base_db.upsert(pos,PositionResult(position=pos,offers=[old],all_offers=[old]),
                       confirmed_observations=[old])
        self.assertEqual(1,base_db.lookup(pos,'2026','4',exact_only=True).sources_found)
        def research(p):
            urls=batch_card_urls() or []
            if not urls:return dict(status='failed',candidates=[])
            visits.extend(urls)
            offer=self.quote(unit_review_required=True,in_moscow_region=None)
            observed_identity(offer.identity_evidence)
            observed_quote(offer)
            return dict(status='failed',candidates=[_offer_to_dict(offer)], diagnostics=[
                dict(url=self.url,code='price_or_unit_unconfirmed',reason='unit review')])
        row=self.run_job(research)['rows'][0]
        self.assertEqual([self.url],visits)
        self.assertEqual([],row['offers'])
        self.assertTrue(row['candidates'][0]['unit_review_required'])
        self.assertTrue(row['candidates'][0]['refresh_failed'])
        self.assertIn('unit review',row['candidates'][0]['refresh_failure_reason'])
        receipts=base_db.refresh_receipt_snapshot()
        self.assertEqual(['refused'],[r['status'] for entry in receipts.values()
            for context in entry['refresh_receipts'].values() for r in context.values()])
        self.assertEqual(0,base_db.lookup(pos,'2026','4',exact_only=True).sources_found)
        self.assertEqual([self.url],self.registry.load(self.position)['supplier.ru']['urls'])
        self.assertTrue(valid_identity(row['candidates'][0]['identity_evidence']))
        from engine.region_policy import region_notice
        self.assertIn('Регион поставки не подтверждён',region_notice(row['candidates'][0]))
        self.assertEqual('review',row['source_checks'][0]['status'])
        self.assertEqual(0,row['source_checks'][0].get('errors',0))
        self.assertEqual(0,row['source_checks'][0].get('card_errors',0))

    def test_higher_lower_fresh_replaces_history_and_failed_refresh_excludes_old(self):
        from engine.search import base_db
        from engine.models import PositionResult
        pos=self.position.to_result().position
        old=self.quote()
        base_db.upsert(pos,PositionResult(position=pos,offers=[old],all_offers=[old]),
                       confirmed_observations=[old])
        self.assertEqual([100],[o.price_with_vat for o in base_db.lookup(pos,'2026','4',exact_only=True).offers])
        for price in (50,150):
            self.registry.remember_identity(self.position,self.proof())
            def research(p):
                if not batch_card_urls():return dict(status='partial',candidates=[_offer_to_dict(self.quote(price_with_vat=999))])
                fresh=self.quote(price_with_vat=price)
                observed_quote(fresh)
                return dict(status='partial',candidates=[_offer_to_dict(fresh)])
            row=self.run_job(research)['rows'][0]
            self.assertEqual([price],[o['price_with_vat'] for o in row['offers']])
            self.assertEqual([price],[o.price_with_vat for o in base_db.lookup(pos,'2026','4',exact_only=True).offers])
        def failed(p):
            return dict(status='partial',candidates=[_offer_to_dict(self.quote(price_with_vat=999))],diagnostics=[
                dict(url=self.url,code='captcha',reason='challenge')])
        row=self.run_job(failed,lambda *a,**kw:[])['rows'][0]
        self.assertEqual([],row['offers'])
        self.assertTrue(row['candidates'][0]['refresh_failed'])
        self.assertEqual([self.url],self.registry.load(self.position)['supplier.ru']['urls'])

    def test_callback_storage_failures_are_progress_not_supplier_failure(self):
        for emitter in (observed_identity,observed_quote):
            with research_session(on_observed=lambda x: (_ for _ in ()).throw(OSError()),
                                  on_identity=lambda x: (_ for _ in ()).throw(OSError())), self.assertRaises(ResearchProgressError):
                emitter(self.proof())
        observed_identity(self.proof())  # context reset

    def test_actual_collect_callback_review_and_redirect_protection_offline(self):
        browser=MagicMock();page=browser.page.return_value.__enter__.return_value
        page.url=self.url;page._kac_response_status=200
        page.title.return_value='Cable ABC123'
        page.locator.return_value.first.inner_text.return_value='Cable ABC123'
        identities=[];quotes=[]
        with research_session(on_identity=identities.append,on_observed=quotes.append), \
             patch.object(agent,'_page_text_after_browser_check',return_value='Cable ABC123\nАртикул: ABC123'), \
             patch.object(agent,'_extract_from_page',return_value=dict(product_title='Cable ABC123',matches_requested_product=True,
                 listed_price_with_vat=120,price_with_vat=120,price_unit='упак')), \
             patch.object(agent,'wl_requisites_for',return_value=Requisites(name='Seller',inn='7842224734')), \
             patch.object(agent,'_save_product_screenshot'):
            offers,count=agent._collect_offers(browser,[self.url],1,'ABC123','Cable','м')
        self.assertEqual(1,len(identities));self.assertTrue(valid_identity(identities[0]))
        self.assertEqual(1,len(quotes));self.assertTrue(quotes[0].unit_review_required)
        page.url='https://evil.ru/product/abc123'
        with agent.capture_card_diagnostics() as diagnostics, \
             patch.object(agent,'_page_text_after_browser_check',return_value='Cable ABC123'), \
             patch.object(agent,'_extract_from_page') as extract:
            offers,count=agent._collect_offers(browser,[self.url],1,'ABC123','Cable','м')
        extract.assert_not_called();self.assertEqual('redirect_unconfirmed',diagnostics[0]['code'])

    def test_delayed_sku_redirect_or_challenge_is_rechecked_before_identity(self):
        for challenge in (False, True):
            browser = MagicMock()
            page = browser.page.return_value.__enter__.return_value
            page.url = self.url
            page._kac_document_url = self.url  # Older set_content provenance cannot mask a new navigation.
            page._kac_response_status = 200
            page.title.return_value = 'Cable ABC123'
            page.locator.return_value.first.inner_text.return_value = 'Cable ABC123'
            text = 'Cable ABC123\nАртикул: ABC123'
            if challenge:
                text = 'Checking your browser\nverify you are human\n' + text
            page.inner_text.return_value = text
            def delayed(page, original):
                if not challenge:
                    page.url = 'https://evil.ru/product/abc123'
                return text
            identities = []
            with research_session(on_identity=identities.append), \
                 agent.capture_card_diagnostics() as diagnostics, \
                 patch.object(agent, '_page_text_after_browser_check', return_value='Cable'), \
                 patch.object(agent, '_identity_card_text', side_effect=delayed), \
                 patch.object(agent, '_extract_from_page') as extract:
                offers, checked = agent._collect_offers(browser, [self.url], 1, 'ABC123', 'Cable', 'м')
            self.assertEqual([], identities)
            extract.assert_not_called()
            self.assertEqual('captcha' if challenge else 'redirect_unconfirmed', diagnostics[0]['code'])

    def test_supplier_health_cooldown_still_prevents_card_and_mapping_does_not_bypass_it(self):
        self.registry.remember_identity(self.position,self.proof())
        health=SupplierHealth(limit=1);health.reject(self.url,'captcha');browser=MagicMock()
        with research_session(health),agent.capture_card_diagnostics() as diagnostics:
            offers,count=agent._collect_offers(browser,[self.url],1,'ABC123','Cable','м')
        browser.page.assert_not_called();self.assertEqual('supplier_paused',diagnostics[0]['code'])
        self.assertEqual([self.url],self.registry.load(self.position)['supplier.ru']['urls'])

    def test_current_url_cannot_mask_wrong_http_document_provenance(self):
        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page.url = self.url
        page._kac_document_url = 'https://evil.ru/product/abc123'
        page._kac_response_status = 200
        page.title.return_value = 'Cable ABC123'
        page.locator.return_value.first.inner_text.return_value = 'Cable ABC123'
        identities = []
        with research_session(on_identity=identities.append), \
             agent.capture_card_diagnostics() as diagnostics, \
             patch.object(agent, '_page_text_after_browser_check', return_value='Cable ABC123\nАртикул: ABC123'), \
             patch.object(agent, '_extract_from_page') as extract:
            agent._collect_offers(browser, [self.url], 1, 'ABC123', 'Cable', 'м')
        self.assertEqual([], identities)
        extract.assert_not_called()
        self.assertEqual('redirect_unconfirmed', diagnostics[0]['code'])

    def test_document_title_or_recommendation_cannot_replace_missing_main_heading(self):
        browser=MagicMock();page=browser.page.return_value.__enter__.return_value
        page.url=self.url;page._kac_response_status=200
        page.title.return_value='Cable ABC123'
        page.locator.return_value.first.inner_text.side_effect=RuntimeError('no heading')
        identities=[]
        with research_session(on_identity=identities.append), \
             patch.object(agent,'_page_text_after_browser_check',return_value='Generic store\nРекомендуем\nАртикул: ABC123'), \
             patch.object(agent,'_extract_from_page',return_value=dict(product_title='Cable ABC123',matches_requested_product=False)), \
             agent.capture_card_diagnostics() as diagnostics:
            offers,count=agent._collect_offers(browser,[self.url],1,'ABC123','Cable','м')
        self.assertEqual([],identities)
        self.assertEqual('product_mismatch',diagnostics[0]['code'])

    def test_edited_candidate_url_cannot_reuse_another_cards_identity(self):
        data=_offer_to_dict(self.quote())
        data['url']='https://supplier.ru/product/xyz999'
        self.assertFalse(confirmed_card(data['url'],dict(candidates=[data]),self.position))
        self.assertFalse(confirmed_card(self.url,dict(candidates=[data]),self.position))

    def test_generic_primary_article_abbreviation_and_inline_fields(self):
        self.assertEqual('3526410',main_card_article('Tray\nарт. 3526410\nРекомендуем\nарт. 9999999'))
        self.assertEqual('36662K',main_card_article('Angle\nАртикул: 36662K Производитель: Brand'))
        self.assertEqual('',main_card_article('Tray\nПохожие товары\nарт. 3526410'))

    def test_actual_http_document_url_metadata_survives_about_blank_and_wrong_host_rejected(self):
        from engine.browser import Browser
        browser=Browser(headed=False);browser.context=MagicMock()
        page=browser.context.new_page.return_value
        page.url='about:blank'
        from playwright.sync_api import Error as PlaywrightError
        page.goto.side_effect=PlaywrightError('timeout')
        response=MagicMock(status_code=200,text='<html><h1>Cable ABC123</h1></html>',url=self.url)
        with patch('engine.browser.supplier_http_get',return_value=response):
            with browser.page(self.url) as rendered:
                self.assertEqual(self.url,rendered._kac_document_url)
                self.assertEqual('about:blank',rendered.url)
        page.close.assert_called_once()
        response.url='https://evil.ru/product/abc123'
        with patch('engine.browser.supplier_http_get',return_value=response):
            with browser.page(self.url) as rendered:
                self.assertEqual(response.url,rendered._kac_document_url)
        # Consumer validates metadata exactly like Chromium redirects.
        self.assertFalse(valid_identity(self.proof(canonical_url=response.url)))

    def test_known_card_resolver_never_requests_article_rediscovery(self):
        url='https://www.elektromehanika.ru/items_teh.php?g=29747&id=223304'
        with patch.object(agent,'supplier_http_get',side_effect=AssertionError('Known card caused rediscovery')) as http:
            self.assertEqual(url,agent._resolve_elektromehanika_listing(url,'36662K'))
            self.assertEqual(url,agent._resolve_elektromehanika_listing(url,''))
        http.assert_not_called()
