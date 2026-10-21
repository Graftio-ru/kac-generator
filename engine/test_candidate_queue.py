import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from engine.models import SpecPosition, PriceOffer
from engine.search.candidate_queue import CandidateQueue, candidate_scope
from engine.search import agent
from engine.search.session import ResearchProgressError


class CandidateQueueTests(unittest.TestCase):
    def test_manual_retry_keeps_uploaded_position_identity(self):
        from fastapi.testclient import TestClient
        from engine.app import app
        from engine.models import PositionResult
        with patch('engine.search.base_db.lookup', return_value=None), \
                patch('engine.search.base_db.upsert', side_effect=lambda pos,result:result), \
                patch('engine.app._browser_factory', return_value=nullcontext(None)), \
                patch('engine.search.candidate_queue.candidate_scope', return_value=nullcontext()) as scope, \
                patch.object(agent, 'research_position', side_effect=lambda browser,pos,*a,**kw:PositionResult(position=pos)) as research:
            response = TestClient(app).post('/search_position', json={
                'number':24, 'qty':16, 'name':'Лоток', 'type_mark':'3526310', 'unit':'м', 'all_suppliers':False})
            self.assertEqual(response.status_code, 200)
            pos = research.call_args.args[1]
            self.assertEqual((pos.number,pos.qty), (24,16))
            self.assertEqual(scope.call_args.args[0], pos)

    def test_card_failure_is_not_replaced_by_cleanup_failure(self):
        class Batches:
            def __iter__(self): return iter([['https://etm.ru/item']])
            def close(self): raise ResearchProgressError('disk failure')
        with patch.object(agent, '_iter_search_batches', return_value=Batches()), \
                patch.object(agent, '_candidate_urls_by_host', side_effect=lambda urls, **kw: urls), \
                patch.object(agent, '_collect_offers', side_effect=ValueError('primary failure')):
            with self.assertRaisesRegex(ValueError, 'primary failure') as caught:
                agent._search_phase(None, self.position(), ['query'], ['etm.ru'], [],
                                    time.monotonic()+5, 'test', 5, 10)
            self.assertTrue(any('очередь' in note for note in caught.exception.__notes__))

    def test_rejected_urls_are_reconsidered_after_one_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = CandidateQueue(self.position(), Path(tmp)/'queue.sqlite3')
            q.add(['https://wrong.ru/item'])
            q.reject('https://wrong.ru/item', 'article_mismatch', 'wrong')
            self.assertEqual(q.urls(), [])
            with patch('engine.search.candidate_queue.time.time', return_value=time.time()+86401):
                self.assertEqual(q.urls(), ['https://wrong.ru/item'])

    def test_early_close_reports_background_persistence_failure(self):
        release = threading.Event()
        background_started = threading.Event()
        def search(query, group, **kwargs):
            if group[0] == 's0.ru':
                self.assertTrue(background_started.wait(2))
                return ['https://good.ru/item']
            background_started.set()
            self.assertTrue(release.wait(2))
            return ['https://bad.ru/item']
        with tempfile.TemporaryDirectory() as tmp:
            with candidate_scope(self.position(), Path(tmp)/'queue.sqlite3') as q:
                def save(urls):
                    if 'https://bad.ru/item' in urls:
                        raise ResearchProgressError('disk failure')
                with patch.object(agent, 'search_on_sites', side_effect=search), patch.object(q, 'add', side_effect=save):
                    batches = agent._iter_search_batches('query', [f's{i}.ru' for i in range(16)], 10, 'test', time.monotonic()+5)
                    self.assertEqual(next(batches), ['https://good.ru/item'])
                    release.set()
                    with self.assertRaises(ResearchProgressError):
                        batches.close()

    def position(self, unit='м'):
        return SpecPosition(number=24, name='Лоток', type_mark='3526310', unit=unit, qty=16)

    def test_restart_preserves_unvisited_and_isolates_changed_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'queue.sqlite3'
            CandidateQueue(self.position(), path).add(['https://etm.ru/item', 'https://rs24.ru/item'])
            self.assertEqual(len(CandidateQueue(self.position(), path).urls()), 2)
            self.assertEqual(CandidateQueue(self.position('шт'), path).urls(), [])

    def test_terminal_mismatch_and_temporary_failure_differ(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = CandidateQueue(self.position(), Path(tmp)/'queue.sqlite3')
            q.add(['https://wrong.ru/item', 'https://blocked.ru/item', 'https://valid.ru/item'])
            q.reject('https://wrong.ru/item', 'article_mismatch', 'wrong')
            q.reject('https://blocked.ru/item', 'captcha', 'temporary')
            q.mark('https://valid.ru/item', 'accepted')
            self.assertNotIn('https://wrong.ru/item', q.urls())
            self.assertIn('https://blocked.ru/item', q.urls())
            self.assertIn('https://valid.ru/item', q.urls())

    def test_deadline_before_visiting_saves_all_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            with candidate_scope(self.position(), Path(tmp)/'queue.sqlite3') as q, \
                    patch.object(agent.time, 'monotonic', return_value=20):
                offers, checked = agent._collect_offers(None, ['https://etm.ru/item'], 5, deadline=10)
                self.assertEqual((offers, checked), ([], 0))
                self.assertEqual(q.urls(), ['https://etm.ru/item'])

    def test_saved_cards_can_satisfy_target_without_new_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            urls = [f'https://seller{i}.ru/item' for i in range(5)]
            offers = [PriceOffer(price_with_vat=100+i, url=u) for i, u in enumerate(urls)]
            with candidate_scope(self.position(), Path(tmp)/'queue.sqlite3') as q:
                q.add(urls)
                with patch.object(agent, 'settings', agent.settings.model_copy(update={'min_sources': 5})), \
                        patch.object(agent, '_candidate_urls_by_host', side_effect=lambda urls, **kw: urls), \
                        patch.object(agent, '_collect_offers', return_value=(offers,5)), \
                        patch.object(agent, 'build_queries', side_effect=AssertionError('rediscovery')):
                    result, checked = agent.find_offers_for_position(None, self.position())
                self.assertEqual(result, offers)
                self.assertEqual(checked, 5)
