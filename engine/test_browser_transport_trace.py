"""Offline attribution/privacy/bounds tests; no browser or supplier calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from engine.browser_transport_trace import ArrivalTrace, safe_url, trace_diagnostics


class ArrivalTraceTests(unittest.TestCase):
    def trace(self):
        trace = ArrivalTrace('https://supplier.example/card?token=SECRET#SECRET')
        trace.main_frame = 'main'
        trace.begin_attempt()
        return trace

    def request(self, trace, rid='doc', kind='Document', frame='main', **extra):
        trace.request(dict(requestId=rid, type=kind, frameId=frame,
            request={'url': 'https://supplier.example/card?token=SECRET', 'headers': {'cookie': 'SECRET'}}, **extra))

    def test_retry_document_keeps_original_attempt_and_unknown_bytes(self):
        trace = self.trace()
        self.request(trace)
        trace.attempt_error(TimeoutError('timeout SECRET'))
        trace.begin_attempt()
        trace.response(dict(requestId='doc', response={'status': 200, 'url': 'https://supplier.example/card?token=SECRET'}))
        trace.commit('https://supplier.example/card?token=SECRET', 200)
        doc = trace.data['documents'][0]
        self.assertEqual(1, doc['attempt'])
        self.assertEqual('pending', doc['completion'])
        self.assertIsNone(doc['encoded_bytes'])
        self.assertIsNone(trace.data['resources']['Document']['known_encoded_bytes'])
        self.assertEqual(2, trace.data['attempts'][1]['id'])
        self.assertNotIn('SECRET', json.dumps(trace.data))

    def test_redirect_links_generations_and_real_completion(self):
        trace = self.trace()
        self.request(trace)
        self.request(trace, redirectResponse={'status': 302, 'encodedDataLength': 120})
        trace.response(dict(requestId='doc', response={'status': 200, 'url': 'https://supplier.example/final'}))
        trace.finished(dict(requestId='doc', encodedDataLength=300))
        first, second = trace.data['documents']
        self.assertEqual('redirect', first['completion'])
        self.assertEqual(1, second['redirect_from'])
        self.assertEqual(2, second['generation'])
        self.assertEqual(300, second['encoded_bytes'])
        self.assertEqual('finished', second['completion'])

    def test_subframe_and_failed_dependency_never_become_main_document(self):
        trace = self.trace()
        self.request(trace, 'frame', frame='subframe')
        self.request(trace, 'script', kind='Script')
        trace.failed(dict(requestId='script', errorText='net::ERR_CONNECTION_RESET https://token:SECRET@host/'))
        self.request(trace)
        trace.finished(dict(requestId='doc', encodedDataLength=0))
        self.assertEqual(1, len(trace.data['documents']))
        self.assertEqual('ERR_CONNECTION_RESET', trace.data['failures'][0]['reason'])
        self.assertEqual(0, trace.data['documents'][0]['encoded_bytes'])
        self.assertEqual(1, trace.data['resources']['Script']['failed'])
        self.assertNotIn('SECRET', json.dumps(trace.data))

    def test_first_final_facts_only_counts_refs_and_same_generation(self):
        trace = self.trace()
        self.request(trace)
        ref = dict(ref='main_facts/owned.json', sha256='a'*64, observed_at=123)
        trace.facts('before_wait', {'body': '', 'main_title': '', 'observation_status': 'observed'}, ref)
        trace.facts('coherent_main_facts', {'body': 'SECRET', 'main_title': 'SECRET', 'kind': 'complete', 'missing_fields': []}, ref)
        self.assertEqual(0, trace.data['first_observation']['body_length'])
        self.assertEqual(6, trace.data['final_facts']['body_length'])
        self.assertEqual(1, trace.data['final_facts']['generation'])
        self.assertNotIn('SECRET', json.dumps(trace.data))

    def test_memory_and_serialized_data_are_bounded(self):
        trace = self.trace()
        for index in range(1000):
            self.request(trace, str(index), kind='Script')
            trace.failed(dict(requestId=str(index), errorText='SECRET'))
            trace.point('event')
        self.assertEqual(trace.MAX_REQUESTS, len(trace.requests))
        self.assertEqual(trace.MAX_POINTS, len(trace.data['points']))
        self.assertEqual(trace.MAX_FAILURES, len(trace.data['failures']))
        self.assertGreater(trace.data['dropped'], 0)
        self.assertLess(len(json.dumps(trace.data)), 15000)

    def test_immutable_owned_file_ref_survives_cancel_and_unsupported_cdp(self):
        sink = []
        with tempfile.TemporaryDirectory() as folder, trace_diagnostics(sink):
            trace = ArrivalTrace('https://supplier.example/card')
            page = Mock()
            page.context.new_cdp_session.side_effect = NotImplementedError('SECRET')
            trace.attach(page)
            trace.begin_attempt()
            cancel = RuntimeError('SECRET')
            trace.finalize(Path(folder), cancel)
            trace.finalize(Path(folder), cancel)
            files = list(Path(folder).glob('browser_transport_traces/*.json'))
            self.assertEqual(1, len(files))
            record = json.loads(files[0].read_text())
            self.assertEqual('unavailable', record['capture'])
            self.assertEqual('NotImplementedError', record['errors'][0]['error_type'])
            self.assertEqual('error', record['outcome'])
            self.assertNotIn('SECRET', files[0].read_text())
            self.assertEqual(page._kac_browser_transport_trace, cancel._kac_browser_transport_trace)
            self.assertEqual(1, len(sink))

    def test_retention_io_failure_is_diagnostic_without_raising(self):
        sink = []
        with tempfile.TemporaryDirectory() as folder, trace_diagnostics(sink):
            trace = ArrivalTrace('https://supplier.example/card')
            path = Path(folder) / 'file'
            path.write_text('owned')
            trace.finalize(path, ValueError('original'))
        self.assertEqual('browser_transport_trace_unavailable', sink[0]['code'])

    def test_local_cdp_only_commands_and_events(self):
        trace = self.trace()
        page = Mock()
        channel = page.context.new_cdp_session.return_value
        channel.send.return_value = {'frameTree': {'frame': {'id': 'main'}}}
        trace.attach(page)
        self.assertEqual(['Page.getFrameTree', 'Network.enable'], [call.args[0] for call in channel.send.call_args_list])
        self.assertEqual(4, channel.on.call_count)
        self.assertEqual('cdp_network', trace.data['capture'])
        page.goto.assert_not_called()
        page.wait_for_timeout.assert_not_called()

    def test_safe_url(self):
        self.assertEqual('', safe_url('https://user:SECRET@host/card'))
        self.assertEqual('', safe_url('data:text/html,SECRET'))
        self.assertEqual('https://host/card', safe_url('https://host/card?SECRET#SECRET'))


class BrowserTraceIntegrationTests(unittest.TestCase):
    def browser(self):
        from engine.browser import Browser
        from unittest.mock import MagicMock
        browser = Browser(headed=False)
        page = MagicMock()
        page.goto.return_value = MagicMock(status=200, url='https://supplier.example/card')
        browser.context = MagicMock()
        browser.context.new_page.return_value = page
        page.context.new_cdp_session.return_value.send.return_value = {'frameTree': {'frame': {'id': 'main'}}}
        return browser, page

    def test_control_survives_with_trace_and_exact_close(self):
        from engine.search.failure_policy import ResearchControlError
        from unittest.mock import patch
        browser, page = self.browser()
        sink = []
        control = ResearchControlError('original control')
        with tempfile.TemporaryDirectory() as folder, patch('engine.browser.CACHE_DIR', Path(folder)), trace_diagnostics(sink):
            with self.assertRaises(ResearchControlError) as caught:
                with browser.page('https://supplier.example/card'):
                    raise control
            self.assertIs(control, caught.exception)
            page.close.assert_called_once_with(run_before_unload=True)
            page.goto.assert_called_once()
            page.wait_for_timeout.assert_not_called()
            self.assertEqual('browser_transport_trace', sink[-1]['code'])
            record = json.loads((Path(folder)/sink[-1]['browser_transport_trace']['ref']).read_text())
            self.assertEqual('ResearchControlError', record['primary_error_type'])

    def test_setup_failure_and_cleanup_failure_keep_original_errors(self):
        from unittest.mock import patch
        for setup in (True, False):
            browser, page = self.browser()
            if setup:
                page.set_default_timeout.side_effect = ValueError('setup')
            page.close.side_effect = RuntimeError('close')
            sink = []
            with tempfile.TemporaryDirectory() as folder, patch('engine.browser.CACHE_DIR', Path(folder)), trace_diagnostics(sink):
                with self.assertRaises(ValueError if setup else RuntimeError):
                    with browser.page('https://supplier.example/card'):
                        pass
                record = json.loads((Path(folder)/sink[-1]['browser_transport_trace']['ref']).read_text())
                self.assertEqual('ValueError' if setup else 'RuntimeError', record['primary_error_type'])
                page.close.assert_called_once()

    def test_retry_and_http_fallback_are_separate_delivery_modes(self):
        from playwright.sync_api import Error
        from unittest.mock import patch, MagicMock
        browser, page = self.browser()
        page.goto.side_effect = Error('net::ERR_TIMED_OUT')
        response = MagicMock(status_code=200, text='<html>owned</html>', url='https://supplier.example/card')
        sink = []
        with tempfile.TemporaryDirectory() as folder, patch('engine.browser.CACHE_DIR', Path(folder)), trace_diagnostics(sink), patch('engine.browser.httpx.get', return_value=response):
            with browser.page('https://supplier.example/card'):
                pass
            record = json.loads((Path(folder)/sink[-1]['browser_transport_trace']['ref']).read_text())
            self.assertEqual(['native_goto', 'native_goto', 'http_set_content_fallback'], [a['mode'] for a in record['attempts']])
            self.assertIsNone(record['documents'][-1]['encoded_bytes'])
            self.assertEqual('unknown', record['documents'][-1]['completion'])
            self.assertIsNotNone(record['documents'][-1]['content_applied_at'])
            self.assertEqual(2, page.goto.call_count)
            self.assertEqual(1, page.wait_for_timeout.call_count)  # Existing retry only.
            page.set_content.assert_called_once()

    def test_observation_io_failure_does_not_replace_consumer_error(self):
        from unittest.mock import patch
        browser, page = self.browser()
        sink = []
        with tempfile.TemporaryDirectory() as folder, trace_diagnostics(sink):
            cache = Path(folder)/'file'
            cache.write_text('owned')
            with patch('engine.browser.CACHE_DIR', cache), self.assertRaisesRegex(ValueError, 'consumer'):
                with browser.page('https://supplier.example/card'):
                    raise ValueError('consumer')
            page.close.assert_called_once()
        self.assertEqual('browser_transport_trace_unavailable', sink[-1]['code'])

    def test_acquisition_failure_retains_trace_without_page(self):
        from unittest.mock import patch
        browser, page = self.browser()
        browser.context.new_page.side_effect = ValueError('acquisition')
        sink = []
        with tempfile.TemporaryDirectory() as folder, patch('engine.browser.CACHE_DIR', Path(folder)), trace_diagnostics(sink):
            with self.assertRaisesRegex(ValueError, 'acquisition'):
                with browser.page('https://supplier.example/card'):
                    pass
            record = json.loads((Path(folder)/sink[-1]['browser_transport_trace']['ref']).read_text())
            self.assertEqual([], record['attempts'])
            self.assertEqual('ValueError', record['primary_error_type'])
            page.goto.assert_not_called()


if __name__ == '__main__':
    unittest.main()
