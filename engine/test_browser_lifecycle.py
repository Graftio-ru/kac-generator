"""Offline browser ownership and cleanup pin tests; no supplier/provider calls."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from playwright.sync_api import Error as PlaywrightError

from engine.browser import Browser, SupplierProfileBusy, _lock_profile
from engine.search.worker_scope import borrow_browser, worker_browser_scope


class BrowserLifecycleTests(unittest.TestCase):
    def page_browser(self):
        browser = Browser(headed=False)
        page = MagicMock()
        page.goto.return_value = MagicMock(status=200)
        browser.context = MagicMock()
        browser.context.new_page.return_value = page
        return browser, page

    def supplier_browser(self):
        browser = Browser(headed=False)
        browser._pw = MagicMock()
        browser._browser = MagicMock()
        browser.context = MagicMock()
        browser._context_closed = False
        supplier = browser._pw.chromium.launch_persistent_context.return_value
        supplier.pages = [MagicMock(), MagicMock()]
        return browser, supplier

    def test_page_setup_failure_still_closes_acquired_page(self):
        for operation in ('set_default_timeout', 'on'):
            with self.subTest(operation=operation):
                browser, page = self.page_browser()
                getattr(page, operation).side_effect = ValueError('page setup failed')
                with self.assertRaisesRegex(ValueError, 'page setup failed'):
                    with browser.page('https://supplier.example/card'):
                        self.fail('failed setup must not yield')
                page.close.assert_called_once_with(run_before_unload=True)

    def test_navigation_records_one_result_after_supplier_method_validation(self):
        """Record a refused page only once, even when its transport returned HTTP 200."""
        from engine.telemetry import capture_metrics

        url = 'https://supplier.example/card'
        response = MagicMock(status_code=200, url=url, text='<html>Product</html>')
        for method, denied_gate in (('http', 'response'), ('browser', 'current')):
            for refused in (False, True):
                with self.subTest(method=method, refused=refused):
                    browser, page = self.page_browser()
                    validation_error = RuntimeError('supplier method refused') if refused else None
                    with patch('engine.supplier_methods.selected', return_value={'method': method}), \
                            patch('engine.supplier_methods.response',
                                  side_effect=validation_error if denied_gate == 'response' else None), \
                            patch('engine.supplier_methods.current',
                                  side_effect=validation_error if denied_gate == 'current' else None), \
                            patch('engine.browser.supplier_http_get', return_value=response), \
                            capture_metrics() as metrics:
                        if refused:
                            with self.assertRaisesRegex(RuntimeError, 'supplier method refused'):
                                with browser.page(url):
                                    self.fail('A refused supplier page must not yield')
                        else:
                            with browser.page(url) as opened:
                                self.assertIs(opened, page)
                    events = [event for event in metrics.events if event['stage'] == 'navigation']
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0]['ok'], not refused)
                    self.assertEqual(events[0]['label'], '')
                    page.close.assert_called_once_with(run_before_unload=True)

    def test_consumer_error_survives_close_failure_and_invalidates_lease(self):
        browser, page = self.page_browser()
        browser._browser = MagicMock()
        browser._context_closed = False
        page.close.side_effect = RuntimeError('close failed')
        with self.assertRaisesRegex(ValueError, 'card extraction failed') as caught:
            with browser.page('https://supplier.example/card'):
                raise ValueError('card extraction failed')
        self.assertFalse(browser.is_usable())
        self.assertIn('RuntimeError', caught.exception.__notes__[0])

    def test_successful_consumer_close_failure_is_visible(self):
        browser, page = self.page_browser()
        page.close.side_effect = RuntimeError('close failed')
        with self.assertRaisesRegex(RuntimeError, 'close failed'):
            with browser.page('https://supplier.example/card'):
                pass
        self.assertTrue(browser._context_closed)

    def test_timeout_failed_fallback_and_close_preserve_navigation_error(self):
        browser, page = self.page_browser()
        failure = PlaywrightError('navigation timeout')
        page.goto.side_effect = failure
        page.close.side_effect = RuntimeError('close failed')
        with patch('engine.browser.httpx.get', side_effect=OSError('network failed')):
            with self.assertRaises(PlaywrightError) as caught:
                with browser.page('https://supplier.example/card'):
                    self.fail('failed navigation must not yield')
        self.assertIs(failure, caught.exception)
        self.assertEqual(2, page.goto.call_count)
        page.close.assert_called_once()

    def test_unsuccessful_or_empty_fallback_never_yields_blank_success(self):
        for status, content in [(403, 'Access denied'), (503, 'Unavailable'), (200, '')]:
            with self.subTest(status=status):
                browser, page = self.page_browser()
                page.goto.side_effect = PlaywrightError('navigation timeout')
                response = MagicMock(status_code=status, text=content)
                with patch('engine.browser.httpx.get', return_value=response):
                    with self.assertRaises(PlaywrightError) as caught:
                        with browser.page('https://supplier.example/card'):
                            self.fail('failed fallback must not yield')
                self.assertIn(f'status {status}', caught.exception.__notes__[0])
                page.close.assert_called_once()

    def test_failed_fallback_set_content_keeps_navigation_failure(self):
        browser, page = self.page_browser()
        page.goto.side_effect = PlaywrightError('navigation timeout')
        page.set_content.side_effect = PlaywrightError('render timeout')
        response = MagicMock(status_code=200, text='<html>card</html>', url='https://supplier.example/card')
        with patch('engine.browser.httpx.get', return_value=response):
            with self.assertRaisesRegex(PlaywrightError, 'navigation timeout'):
                with browser.page('https://supplier.example/card'):
                    self.fail('failed rendering must not yield')
        page.close.assert_called_once()

    def test_supplier_launch_cleans_initial_tabs_preserves_settings_and_closes_once(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('engine.browser.CACHE_DIR', Path(directory)), \
                patch('engine.browser.settings.browser_channel', 'chrome'), \
                patch('engine.browser.settings.browser_proxy_server', 'http://127.0.0.1:2080'):
            for headed in (False, True):
                browser, supplier = self.supplier_browser()
                browser.headed = headed
                initial_pages = list(supplier.pages)
                context = browser._context_for_url('https://www.artem-tools.ru/card')
                self.assertIs(supplier, context)
                self.assertIs(context, browser._context_for_url('https://artem-tools.ru/other'))
                for initial in initial_pages:
                    initial.close.assert_called_once()
                kwargs = browser._pw.chromium.launch_persistent_context.call_args.kwargs
                self.assertEqual(not headed, kwargs['headless'])
                self.assertEqual('chrome', kwargs['channel'])
                self.assertEqual({'server': 'http://127.0.0.1:2080'}, kwargs['proxy'])
                self.assertEqual(30000, kwargs['timeout'])
                driver = browser._pw
                browser.__exit__()
                browser.__exit__()
                supplier.close.assert_called_once()
                driver.stop.assert_called_once()
                self.assertIsNone(browser._supplier_context)
                self.assertIsNone(browser._profile_lock)
                self.assertFalse(browser.is_usable())

    def test_supplier_closed_event_forces_worker_restart(self):
        with tempfile.TemporaryDirectory() as directory, patch('engine.browser.CACHE_DIR', Path(directory)):
            browser, supplier = self.supplier_browser()
            replacement, _ = self.supplier_browser()
            try:
                with worker_browser_scope(), patch.object(browser, '__enter__', return_value=browser), \
                        patch.object(replacement, '__enter__', return_value=replacement):
                    with borrow_browser(lambda: browser) as first:
                        first._context_for_url('https://artem-tools.ru/card')
                        supplier.on.call_args.args[1]()
                        self.assertFalse(first.is_usable())
                    with borrow_browser(lambda: replacement) as second:
                        self.assertIs(second, replacement)
            finally:
                browser.__exit__()
                replacement.__exit__()

    def test_concurrent_supplier_profile_is_busy_before_chromium_launch(self):
        with tempfile.TemporaryDirectory() as directory, patch('engine.browser.CACHE_DIR', Path(directory)):
            first, _ = self.supplier_browser()
            second, _ = self.supplier_browser()
            try:
                first._context_for_url('https://artem-tools.ru/card')
                with self.assertRaisesRegex(SupplierProfileBusy, 'profile busy'):
                    second._context_for_url('https://artem-tools.ru/card')
                second._pw.chromium.launch_persistent_context.assert_not_called()
                first.__exit__()
                second._context_for_url('https://artem-tools.ru/card')
                second._pw.chromium.launch_persistent_context.assert_called_once()
            finally:
                first.__exit__()
                second.__exit__()

    def test_profile_lock_is_process_safe_and_survives_stale_file(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / 'profile'
            lock = _lock_profile(profile)
            code = ('import sys; from pathlib import Path; '
                    'from engine.browser import _lock_profile; _lock_profile(Path(sys.argv[1]))')
            try:
                child = subprocess.run([sys.executable, '-c', code, str(profile)],
                                       capture_output=True, text=True, timeout=10)
                self.assertNotEqual(0, child.returncode)
                self.assertIn('SupplierProfileBusy', child.stderr)
            finally:
                lock.close()
            child = subprocess.run([sys.executable, '-c', code, str(profile)],
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(0, child.returncode, child.stderr)
            self.assertTrue(profile.with_suffix('.lock').exists())
            reacquired = _lock_profile(profile)
            reacquired.close()

    def test_supplier_setup_or_launch_failure_releases_profile_for_next_worker(self):
        with tempfile.TemporaryDirectory() as directory, patch('engine.browser.CACHE_DIR', Path(directory)):
            for operation in ('launch', 'set_default_timeout', 'initial_close'):
                with self.subTest(operation=operation):
                    browser, supplier = self.supplier_browser()
                    if operation == 'launch':
                        browser._pw.chromium.launch_persistent_context.side_effect = ValueError('supplier failed')
                    elif operation == 'initial_close':
                        supplier.pages[0].close.side_effect = ValueError('supplier failed')
                    else:
                        supplier.set_default_timeout.side_effect = ValueError('supplier failed')
                    with self.assertRaisesRegex(ValueError, 'supplier failed'):
                        browser._context_for_url('https://artem-tools.ru/card')
                    self.assertIsNone(browser._supplier_context)
                    self.assertIsNone(browser._profile_lock)
                    replacement, _ = self.supplier_browser()
                    try:
                        replacement._context_for_url('https://artem-tools.ru/card')
                    finally:
                        replacement.__exit__()

    def test_scope_final_cleanup_preserves_primary_error(self):
        browser, _ = self.page_browser()
        with patch.object(browser, '__enter__', return_value=browser), \
                patch.object(browser, '__exit__', side_effect=RuntimeError('cleanup failed')):
            with self.assertRaisesRegex(ValueError, 'scope primary') as caught:
                with worker_browser_scope():
                    with borrow_browser(lambda: browser):
                        pass
                    raise ValueError('scope primary')
        self.assertIn('RuntimeError', caught.exception.__notes__[0])

    def test_budget_failure_after_lock_acquisition_releases_lease(self):
        with tempfile.TemporaryDirectory() as directory, patch('engine.browser.CACHE_DIR', Path(directory)):
            browser, _ = self.supplier_browser()
            with patch('engine.browser._budget_ms', side_effect=ValueError('budget expired')):
                with self.assertRaisesRegex(ValueError, 'budget expired'):
                    browser._context_for_url('https://artem-tools.ru/card')
            self.assertIsNone(browser._profile_lock)
            replacement, _ = self.supplier_browser()
            try:
                replacement._context_for_url('https://artem-tools.ru/card')
            finally:
                replacement.__exit__()

    def test_unacknowledged_supplier_and_driver_shutdown_quarantines_profile(self):
        with tempfile.TemporaryDirectory() as directory, patch('engine.browser.CACHE_DIR', Path(directory)), \
                patch('engine.browser._FAILED_PROFILE_LOCKS', []):
            from engine.browser import _FAILED_PROFILE_LOCKS as quarantined
            browser, supplier = self.supplier_browser()
            browser._context_for_url('https://artem-tools.ru/card')
            supplier.close.side_effect = RuntimeError('context close failed')
            browser._pw.stop.side_effect = RuntimeError('driver stop failed')
            primary = ValueError('original extraction failure')
            try:
                browser.__exit__(ValueError, primary, None)
                self.assertIn('profile remains locked', primary.__notes__[0])
                self.assertEqual(1, len(quarantined))
                replacement, _ = self.supplier_browser()
                with self.assertRaises(SupplierProfileBusy):
                    replacement._context_for_url('https://artem-tools.ru/card')
                replacement.__exit__()
            finally:
                for lock in quarantined:
                    lock.close()

    def test_failed_supplier_close_releases_profile_only_after_driver_stops(self):
        with tempfile.TemporaryDirectory() as directory, patch('engine.browser.CACHE_DIR', Path(directory)):
            browser, supplier = self.supplier_browser()
            browser._context_for_url('https://artem-tools.ru/card')
            supplier.close.side_effect = RuntimeError('context close failed')
            browser.__exit__()
            browser._pw.stop.assert_called_once()
            replacement, _ = self.supplier_browser()
            try:
                replacement._context_for_url('https://artem-tools.ru/card')
            finally:
                replacement.__exit__()


if __name__ == '__main__':
    unittest.main()

    def test_capture_resize_permission_only_for_owned_headless_pages(self):
        for headed in (False,True):
            browser,page=self.page_browser();browser.headed=headed
            with browser.page('https://supplier.example/card') as owned:
                self.assertIs(owned._kac_capture_resize_allowed,not headed)
