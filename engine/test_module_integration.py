"""Offline integration adversaries; never use provider calls or saved health state."""
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
import httpx
from engine.models import SpecPosition
from engine.browser import SupplierProfileBusy
from engine.search import agent
from engine.search.session import (PersistentSupplierHealth, research_session, supplier_transport,
    transport_observed, SupplierDeferred, http_outcome)
from engine.search.supplier_health_store import SupplierHealthStore, Outcome
from engine.search.source_checks import source_checks
from engine.search.shared_pipeline import run_shared_pipeline


class ModuleIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SupplierHealthStore(Path(self.temp.name)/'health.db', lease_seconds=.15)
        self.health = PersistentSupplierHealth(self.store, ['s.ru'])

    def test_accessible_uncomparable_card_heals_and_alias_uses_registry_host(self):
        with research_session(self.health), supplier_transport('https://shop.s.ru/card'):
            transport_observed(Outcome.SUCCESS)
            agent.rejected('https://shop.s.ru/card', 'article_mismatch')
        self.assertEqual('healthy', self.store.snapshot('s.ru')['state'])
        self.assertIsNone(self.store.snapshot('shop.s.ru')['checked_at'])

    def test_long_operation_renews_lease_and_extraction_failure_preserves_access(self):
        with research_session(self.health), supplier_transport('https://s.ru/card'):
            transport_observed(Outcome.SUCCESS)
            time.sleep(.25)
            self.assertFalse(self.store.acquire('s.ru').allowed)
        self.assertEqual('healthy', self.store.snapshot('s.ru')['state'])

    def test_http_and_provider_errors_do_not_share_supplier_penalty(self):
        self.assertEqual(Outcome.HTTP_FORBIDDEN, http_outcome(403))
        self.assertEqual(Outcome.HTTP_RATE_LIMITED, http_outcome(429))
        self.assertEqual(Outcome.UNKNOWN, http_outcome(404))
        for exc in (RuntimeError('search provider'), SupplierProfileBusy('profile busy')):
            with self.assertRaises(type(exc)), research_session(self.health), supplier_transport('https://s.ru/c'):
                raise exc
            self.assertEqual('unknown', self.store.snapshot('s.ru')['state'])
        response=httpx.Response(403, request=httpx.Request('GET','https://s.ru/c'))
        with self.assertRaises(httpx.HTTPStatusError), research_session(self.health), supplier_transport('https://s.ru/c'):
            response.raise_for_status()
        self.assertEqual('http_forbidden',self.store.snapshot('s.ru')['reason'])

    def test_cooldown_dated_pair_payload_survives_source_checks(self):
        with research_session(self.health), supplier_transport('https://s.ru/card'):
            transport_observed(Outcome.HTTP_FORBIDDEN)
        with self.assertRaises(SupplierDeferred) as caught, research_session(self.health), supplier_transport('https://s.ru/other'):
            self.fail('Denied transport must not run')
        check=caught.exception.check
        row=source_checks(['s.ru'], {'s.ru':{'deferred_check':check}}, {}, terminal=True)[0]
        for key in ('checked_at','deferred_at','retry_at','failure_code','health_state'):
            self.assertEqual(check[key],row[key])
        self.assertEqual('unknown',row['product_presence'])
        self.assertIn('http_forbidden',row['reason'])
        self.assertIn('следующая попытка',row['reason'])

    def test_agent_profile_busy_is_deferred_and_not_health_failure(self):
        browser=MagicMock()
        browser.page.side_effect=SupplierProfileBusy('busy')
        with research_session(self.health), agent.capture_card_diagnostics() as diagnostics:
            result=agent._collect_offers(browser,['https://s.ru/product/a'],1)
        self.assertEqual([],result[0])
        self.assertEqual('profile_busy',diagnostics[0]['code'])
        self.assertEqual('unknown',self.store.snapshot('s.ru')['state'])

    def test_native_columns_reach_article_planner_and_stale_provenance_is_ignored(self):
        cells={'name':'Transformer','model':'MODEL-2','article':'ART-3','maker':'Factory','unit':'шт'}
        pos=SpecPosition(number=1,name='Transformer',type_mark='MODEL-2 / ART-3',unit='шт',
                         source_evidence={'reader':'native_pdf','cells':cells})
        with patch.object(agent,'complete_json') as paid:
            queries=agent.build_queries(pos,semantic=False)
        paid.assert_not_called()
        self.assertEqual('"ART-3" "Factory" цена',queries[0])
        self.assertIn('"MODEL-2"',queries[1])
        pos.name='Edited product'
        self.assertNotIn('Factory',' '.join(agent.build_queries(pos,semantic=False)))

    def pipeline(self, cards, *, cancelled=lambda:False, contention_seconds=2):
        finals={}
        run_shared_pipeline([0,1,2], seed=lambda i:({'status':'failed','found':0},[f'https://artem-tools.ru/{i}']),
            plan=lambda i:[], discover=lambda i,t:([],[]), cards=cards,
            publish=lambda i,r,d,done:finals.update({i:r}) if done else None,
            deadline=time.monotonic()+4,cancelled=cancelled,target=5,exhaustive=True,
            profile_host='artem-tools.ru',contention_seconds=contention_seconds)
        return finals

    def test_profile_host_all_rows_use_one_worker_and_no_overlap(self):
        threads=[]
        active=[0]
        def cards(i,urls,previous):
            active[0]+=1
            self.assertEqual(1,active[0])
            threads.append(threading.get_ident())
            time.sleep(.01)
            active[0]-=1
            return dict(previous,diagnostics=[])
        finals=self.pipeline(cards)
        self.assertEqual(3,len(finals))
        self.assertEqual(1,len(set(threads)))

    def test_contention_requeues_then_recovers_instead_of_terminalizing(self):
        calls={}
        def cards(i,urls,previous):
            calls[i]=calls.get(i,0)+1
            return dict(previous, diagnostics=[{'code':'supplier_probe_in_progress','url':urls[0]}]
                        if calls[i]==1 else [],found=0)
        self.assertEqual(3,len(self.pipeline(cards)))
        self.assertEqual({0:2,1:2,2:2},calls)

    def test_permanent_profile_busy_is_finite_and_cancel_aware(self):
        calls=[]
        def cards(i,urls,previous):
            calls.append(i)
            return dict(previous,diagnostics=[{'code':'profile_busy','url':urls[0]}])
        self.assertEqual(3,len(self.pipeline(cards,contention_seconds=0)))
        self.assertEqual(3,len(calls))
        calls.clear()
        self.assertEqual(3,len(self.pipeline(cards,cancelled=lambda:bool(calls))))
        self.assertLessEqual(len(calls),3)

    def test_external_provider_http_errors_inside_card_do_not_penalize_supplier(self):
        for observed in (Outcome.UNKNOWN, Outcome.SUCCESS):
            response=httpx.Response(403,request=httpx.Request('POST','https://provider.example/chat'))
            with self.assertRaises(httpx.HTTPStatusError), research_session(self.health), supplier_transport('https://s.ru/card'):
                transport_observed(observed)
                response.raise_for_status()
            self.assertIsNone(self.store.snapshot('s.ru')['reason'])
        error=httpx.ReadTimeout('provider',request=httpx.Request('GET','https://provider.example/data'))
        with self.assertRaises(httpx.ReadTimeout), research_session(self.health), supplier_transport('https://s.ru/card'):
            raise error
        self.assertIsNone(self.store.snapshot('s.ru')['reason'])

    def test_retry_after_is_finite_and_caps_cooldown(self):
        from engine.search.session import retry_after_seconds
        for value in ('nan','inf','-1','0','bad',True,None):
            self.assertIsNone(retry_after_seconds(value))
        response=httpx.Response(429,headers={'retry-after':'99999'},request=httpx.Request('GET','https://s.ru/c'))
        before=time.time()
        with self.assertRaises(httpx.HTTPStatusError), research_session(self.health), supplier_transport('https://s.ru/c'):
            response.raise_for_status()
        self.assertAlmostEqual(before+3600,self.store.snapshot('s.ru')['retry_at'],delta=1)

    def test_app_force_discovery_respects_cooldown_and_exports_dated_unknown_pair(self):
        from engine.test_all_sites import AllSitesTests
        from engine.schemas import PositionIn
        from engine.search.source_registry import SourceRegistry
        with research_session(self.health), supplier_transport('https://s.ru/card'):
            transport_observed(Outcome.HTTP_FORBIDDEN)
        position=PositionIn(number=1,name='Thing',unit='шт',qty=1)
        registry=SourceRegistry(Path(self.temp.name)/'mappings.db')
        with patch('engine.search.session.PersistentSupplierHealth',return_value=self.health), \
             patch('engine.search.source_registry.SourceRegistry',return_value=registry):
            job=AllSitesTests().run_job([position],['s.ru'],lambda *a,**kw:self.fail('Cooldown is not bypassed'),
                research=lambda p:{'status':'failed','found':0,'candidates':[]},refresh_first=True,force_discovery=True)
        row=job['rows'][0]
        check=row['source_checks'][0]
        self.assertEqual(('incomplete','unknown'),(check['status'],check['product_presence']))
        self.assertEqual('http_forbidden',check['failure_code'])
        self.assertGreater(check['retry_at'],check['checked_at'])
        self.assertEqual({},registry.load(position))
        from fastapi.testclient import TestClient
        from engine.app import app
        from openpyxl import load_workbook
        from io import BytesIO
        response=TestClient(app).post('/research/report',json={'positions':[
            {'number':1,'name':'Thing','unit':'шт','qty':1,'source_checks':[check]}]})
        self.assertEqual(200,response.status_code)
        workbook=load_workbook(BytesIO(response.content))
        values=[str(c.value) for sheet in workbook for cells in sheet for c in cells]
        self.assertTrue(any('http_forbidden' in value and 'следующая попытка' in value for value in values))

    def test_health_denial_does_not_open_browser_or_count_card_transport(self):
        with research_session(self.health), supplier_transport('https://s.ru/card'):
            transport_observed(Outcome.BOT_PROTECTION)
        browser=MagicMock()
        with research_session(self.health), agent.capture_card_diagnostics() as diagnostics:
            offers,checked=agent._collect_offers(browser,['https://s.ru/product/a'],1)
        self.assertEqual(([],0),(offers,checked))
        browser.page.assert_not_called()
        self.assertEqual(('supplier_cooldown',0), (diagnostics[0]['code'], diagnostics[0]['cards']))

    def test_current_negative_is_dated_and_ttl_is_explicit_without_absence(self):
        check=source_checks(['s.ru'],{'s.ru':{'queries':1,'checked_at':100}}, {},terminal=True)[0]
        self.assertEqual('no_match',check['status'])
        self.assertEqual((100,86400,86500),(check['checked_at'],check['ttl_seconds'],check['expires_at']))
        self.assertEqual('unknown',check['product_presence'])
        from engine.search.source_registry import SourceRegistry
        registry=SourceRegistry(Path(self.temp.name)/'negative.db')
        position=SpecPosition(number=1,name='Thing',unit='шт')
        registry.save(position,check)
        self.assertEqual(100,registry.load(position)['s.ru']['outcome']['checked_at'])
