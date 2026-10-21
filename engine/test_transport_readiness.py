"""Offline pins for configured supplier routing and existing DKC admission."""
import time
import unittest
from unittest.mock import MagicMock, patch

import httpx
from playwright.sync_api import Error as PlaywrightError

from engine import browser as bm
from engine.models import Requisites
from engine.search import agent
from engine.search.session import deadline_scope


class TransportTests(unittest.TestCase):
    def browser(self, fallback=False):
        browser = bm.Browser(headed=False)
        page = MagicMock()
        browser.context = MagicMock()
        browser.context.new_page.return_value = page
        if fallback:
            page.goto.side_effect = PlaywrightError('navigation timeout')
        return browser, page

    def response(self, url):
        return httpx.Response(200, text='<html><head></head><body>Card</body></html>',
                              request=httpx.Request('GET', url))

    def test_all_three_paths_share_explicit_route_redirect_and_timeout_policy(self):
        for proxy in ('  http://127.0.0.1:2080  ', '', '   '):
            for route in ('static', 'fallback', 'article'):
                with self.subTest(proxy=bool(proxy.strip()), route=route):
                    url = ('https://www.elektromehanika.ru/items_teh.php?id=1'
                           if route == 'static' else 'https://supplier.example/card')
                    with patch.object(bm.settings, 'browser_proxy_server', proxy), \
                            patch.object(bm.httpx, 'get', return_value=self.response(url)) as get:
                        if route == 'article':
                            agent._resolve_elektromehanika_listing(url, 'ABC')
                        else:
                            browser, page = self.browser(route == 'fallback')
                            with browser.page(url):
                                pass
                            page.close.assert_called_once()
                        get.assert_called_once()
                        self.assertEqual({'headers': {'User-Agent': bm.UA},
                            'follow_redirects': True, 'timeout': 15, 'trust_env': False,
                            **({'proxy': proxy.strip()} if proxy.strip() else {})},
                            get.call_args.kwargs)

    def test_proxy_failure_never_retries_direct_or_yields_success(self):
        for route in ('static', 'fallback', 'article'):
            with self.subTest(route=route), \
                    patch.object(bm.settings, 'browser_proxy_server', 'http://127.0.0.1:2080'), \
                    patch.object(bm.httpx, 'get', side_effect=httpx.ProxyError('proxy unavailable')) as get:
                url = ('https://www.elektromehanika.ru/items_teh.php?id=1'
                       if route == 'static' else 'https://supplier.example/card')
                if route == 'article':
                    with agent.capture_card_diagnostics() as diagnostics:
                        self.assertIsNone(agent._resolve_elektromehanika_listing(url, 'ABC'))
                    self.assertEqual('article_search_error', diagnostics[0]['code'])
                else:
                    browser, page = self.browser(route == 'fallback')
                    with self.assertRaises(httpx.ProxyError if route == 'static' else PlaywrightError):
                        with browser.page(url):
                            self.fail('failed proxy must not yield')
                    page.close.assert_called_once()
                get.assert_called_once()
                self.assertEqual('http://127.0.0.1:2080', get.call_args.kwargs['proxy'])

    def test_deadline_clips_common_http_timeout(self):
        with deadline_scope(time.monotonic() + 1), patch.object(bm.httpx, 'get') as get:
            bm.supplier_http_get('https://supplier.example/card')
        self.assertGreater(get.call_args.kwargs['timeout'], 0)
        self.assertLessEqual(get.call_args.kwargs['timeout'], 1)

    def test_http_error_and_retry_after_preserved(self):
        browser, page = self.browser(fallback=True)
        response = httpx.Response(429, text='retry later', headers={'retry-after': '60'},
            request=httpx.Request('GET', 'https://supplier.example/card'))
        with patch.object(bm.httpx, 'get', return_value=response):
            with self.assertRaises(PlaywrightError) as caught:
                with browser.page(str(response.url)):
                    self.fail('429 must not yield')
        self.assertEqual(429, caught.exception._kac_http_status)
        self.assertEqual('60', caught.exception._kac_retry_after)


class DkcAdmissionTests(unittest.TestCase):
    URL = 'https://www.dkc.market/product/3526410-lotok'

    def test_initial_template_is_rejected_before_text_extraction_and_screenshot(self):
        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page.title.return_value = 'Product – купить за 1 732.58 ₽'
        page.inner_text.return_value = 'Название товара\n100 ₽\n1 шт.\nРекомендации 500 ₽'
        page.locator.return_value.first.wait_for.side_effect = PlaywrightError('h1 unavailable')
        with patch.object(agent, '_page_text_after_browser_check') as text, \
                patch.object(agent, '_extract_from_page') as extract, \
                patch.object(agent, '_save_product_screenshot') as shot, \
                agent.capture_card_diagnostics() as diagnostics:
            offers, checked = agent._collect_offers(browser, [self.URL], 1)
        self.assertEqual(([], 1), (offers, checked))
        self.assertEqual('site_error', diagnostics[0]['code'])
        page.locator.assert_called_once_with('h1')
        text.assert_not_called()
        extract.assert_not_called()
        shot.assert_not_called()

    def test_loaded_card_gate_precedes_extraction_and_screenshot(self):
        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page._kac_response_status = 200
        page.title.return_value = 'DKC Лоток'
        order = []
        page.locator.return_value.first.wait_for.side_effect = lambda **kw: order.append('ready')
        data = dict(product_title='DKC Лоток', price_with_vat=123,
                    listed_price_with_vat=123, price_unit='шт', matches_requested_product=True)
        with patch.object(agent, '_page_text_after_browser_check', return_value='DKC Лоток 123 ₽'), \
                patch.object(agent, '_extract_from_page', side_effect=lambda *a: order.append('extract') or data), \
                patch.object(agent, '_dkc_market_main_offer', return_value=(123, 'шт')), \
                patch.object(agent, 'wl_requisites_for', return_value=Requisites(name='Seller', inn='7719079370')), \
                patch.object(agent, '_save_product_screenshot', side_effect=lambda *a: order.append('screenshot')):
            offers, checked = agent._collect_offers(browser, [self.URL], 1, expected_unit='шт')
        self.assertEqual((1, 1), (len(offers), checked))
        self.assertEqual(['ready', 'extract', 'screenshot'], order)


if __name__ == '__main__':
    unittest.main()
