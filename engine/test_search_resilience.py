"""Проверки устойчивости навигации по карточкам и запросов к SERP API."""
from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, sentinel

from engine.browser import Browser
from engine.requisites.supplier import (_electro_mpo_terms_seller,
                                        _etm_published_requisites,
                                        _requisites_paths, fetch_requisites_from_site)
from engine.search.agent import (_conflicting_full_model_code, _conflicting_numeric_article,
                                 _conflicting_network_cable_spec,
                                 _conflicting_page_article, _printed_page_article,
                                 _http_response_is_blocked, _needs_model_review,
                                 _save_product_screenshot)
from engine.search import search_engine


class ModelIdentityTests(unittest.TestCase):
    def test_cyrillic_article_letter_is_same_as_latin_on_card(self):
        self.assertFalse(_needs_model_review(
            "36003К", "Угол DKC 36003K 150х50", "https://supplier.ru/product/36003k"))

    def test_full_article_in_html_url_confirms_card(self):
        self.assertFalse(_needs_model_review(
            "UZM-XLK1-VN1-150240S", "Муфта ПКВтп 1х150/240 IEK",
            "https://supplier.ru/product/iek-uzm-xlk1-vn1-150240s.html"))

    def test_rating_units_and_script_are_normalized(self):
        self.assertFalse(_needs_model_review(
            "CT30 300/5A, CT30-300-0.5s-2.5",
            "DKC Трансформатор тока CT30 300/5А класс 0.5S 2.5ВА",
            "https://supplier.ru/product/ct30-300-0-5s-2-5"))

    def test_different_printed_numeric_article_is_rejected(self):
        self.assertTrue(_conflicting_numeric_article(
            "36217", "DKC Горизонтальный угол CPO 100х100 36017"))
        self.assertTrue(_conflicting_numeric_article(
            "30013K", "Пластина DKC 30013KHDZL"))
        self.assertFalse(_conflicting_numeric_article(
            "3526310", "Лоток 150х50 L3000 толщ.1,0 мм"))
        self.assertTrue(_conflicting_numeric_article(
            "3534310", "3534310HDZ DKC Лоток 200х100"))

    def test_different_article_in_url_is_not_exact_match(self):
        self.assertTrue(_needs_model_review(
            "UHH32-D036-200-99", "Хомут 3,6х200 (100 шт.)",
            "https://supplier.ru/product/uhh32-d036-200-100.html"))

    def test_exact_sku_in_product_page_title_confirms_card(self):
        self.assertTrue(_needs_model_review(
            "3526310", "DKC Лоток перфорированный 150х50 L3000",
            "https://elevel.ru/shop/lotok-perforirovannyy/"))
        self.assertFalse(_needs_model_review(
            "3526310", "DKC Лоток перфорированный 150х50 L3000",
            "https://elevel.ru/shop/lotok-perforirovannyy/",
            "3526310 DKC Лоток перфорированный 150х50 L3000 - Элевел"))

    def test_different_full_sku_suffix_in_title_is_conflict(self):
        self.assertTrue(_conflicting_full_model_code(
            "UHH31-D048-200-99", "Хомут IEK UHH31-D048-200-100 (100 шт.)"))
        self.assertTrue(_conflicting_full_model_code(
            "CT30 300/5A, CT30-300-0.5s-2.5",
            "Трансформатор CT30-300-0.5-2.5 DKC"))
        self.assertFalse(_conflicting_full_model_code(
            "UZM-XLK1-VN1-150240S", "Муфта IEK UZM-XLK1-VN1-150240S"))

    def test_ftp_cat6_does_not_accept_s_ftp_cat6a(self):
        name, mark = "Кабель сетевой, категория 6", "FTP 4x2x0,57"
        self.assertTrue(_conflicting_network_cable_spec(
            name, mark, "Кабель ParLan S/FTP Cat6A 4х2х0,57"))
        self.assertTrue(_conflicting_network_cable_spec(
            name, mark, "Кабель F/UTP Cat6A 4х2х0,57"))
        self.assertFalse(_conflicting_network_cable_spec(
            name, mark, "Кабель ParLan F/UTP Cat6 4х2х0,57"))

    def test_lanitnord_printed_article_overrides_generic_product_title(self):
        text = "Лоток 200х50\nАртикул: 3526410ZL\nПроизводитель DKC\n"
        article = _printed_page_article(text)
        self.assertEqual("3526410ZL", article)
        self.assertTrue(_conflicting_page_article("3526410", article))
        self.assertFalse(_conflicting_page_article("3526410ZL", article))
        self.assertFalse(_conflicting_page_article("36003К", "36003K"))
        self.assertEqual("36043K", _printed_page_article(
            "Угол DKC\nАртикул:\n36043K\nОсновные характеристики"))
        self.assertEqual("07-0200", _printed_page_article(
            "Стяжки\n[07-0200] Стяжка Rexant 200 x 3,6 мм",
            bracket_heading=True))

    def test_plain_pugv_does_not_accept_low_smoke_variant(self):
        from engine.search.agent import _conflicting_model_code_in_title
        self.assertTrue(_conflicting_model_code_in_title(
            "ПуГв (ПВ-3) 1×6", "Провод ПУГВнг(А)-LS 1х6"))
        self.assertFalse(_conflicting_model_code_in_title(
            "ПуГв (ПВ-3) 1×6", "Провод ПуГВ (ПВ-3) 6,0 ж/з"))


class _Response:
    def __init__(self):self.extensions={}
    status = 200
    status_code = 200
    text = "<response><url>https://supplier.example/product</url></response>"


class ScreenshotFallbackTests(unittest.TestCase):
    def test_font_wait_uses_browser_capture_instead_of_losing_offer(self):
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        page = MagicMock()
        page.screenshot.side_effect = PlaywrightTimeoutError("waiting for fonts to load")
        session = page.context.new_cdp_session.return_value
        session.send.return_value = {"data": "iVBORw0KGgo="}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "product.png"
            _save_product_screenshot(page, path)
            self.assertEqual(b"\x89PNG\r\n\x1a\n", path.read_bytes())
        session.detach.assert_called_once()


class _Page:
    def set_default_timeout(self, timeout):
        self.default_timeout = timeout

    def __init__(self):
        self.goto_kwargs = None
        self.closed = False
        self.close_kwargs = None
        self.content = None
        self.set_content_kwargs = None

    def goto(self, url, **kwargs):
        self.goto_kwargs = {"url": url, **kwargs}
        return _Response()

    def close(self, **kwargs):
        self.closed = True
        self.close_kwargs = kwargs

    def on(self, *_args):
        pass

    def wait_for_timeout(self, _milliseconds):
        pass

    def set_content(self, content, **kwargs):
        self.content = content
        self.set_content_kwargs = kwargs


class _SlowFirstPage(_Page):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def goto(self, url, **kwargs):
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        self.calls += 1
        if self.calls == 1:
            raise PlaywrightTimeoutError("slow navigation")
        return super().goto(url, **kwargs)


class _AlwaysSlowPage(_Page):
    def goto(self, url, **kwargs):
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        raise PlaywrightTimeoutError("slow navigation")


class _HttpResponse:
    status_code = 200
    text = "<html><head></head><body>Цена 100 руб.</body></html>"
    url = "https://supplier.example/product"


class _TransientXmlResponse(_Response):
    text = "<response><error>Выполните перезапрос. Ответ от поисковой системы не получен.</error></response>"


class _Context:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page


class _CreatedContext:
    def on(self, event, callback):
        self.close_callback = callback if event == 'close' else None

    def add_init_script(self, _script):
        pass

    def set_default_timeout(self, _timeout):
        pass


class _LaunchedBrowser:
    def __init__(self):
        self.context = _CreatedContext()
        self.context_kwargs = None

    def new_context(self, **kwargs):
        self.context_kwargs = kwargs
        return self.context

    def close(self):
        pass


class _Chromium:
    def __init__(self):
        self.launch_kwargs = None
        self.browser = _LaunchedBrowser()

    def launch(self, **kwargs):
        self.launch_kwargs = kwargs
        return self.browser


class _Playwright:
    def __init__(self):
        self.chromium = _Chromium()

    def stop(self):
        pass


class _PlaywrightStarter:
    def __init__(self, playwright):
        self.playwright = playwright

    def start(self):
        return self.playwright


class SearchResilienceTests(unittest.TestCase):
    def test_failed_browser_entry_stops_playwright_before_next_row(self):
        from unittest.mock import MagicMock
        browser = Browser()
        driver = MagicMock(spec=["stop"])

        def fail():
            browser._pw = driver
            raise ValueError("invalid cookie snapshot")

        with patch.object(browser, "_start", side_effect=fail), self.assertRaises(ValueError):
            browser.__enter__()
        driver.stop.assert_called_once()

    def test_cookie_cleanup_failure_does_not_leak_driver_or_mask_startup_error(self):
        browser = Browser()
        driver = MagicMock(spec=["stop"])

        def fail():
            browser._pw = driver
            raise ValueError("original startup failure")

        with patch.object(browser, "_start", side_effect=fail), \
             patch("engine.browser.Path.unlink", side_effect=PermissionError("temporary file locked")), \
             self.assertRaisesRegex(ValueError, "original startup failure"):
            browser.__enter__()
        driver.stop.assert_called_once()

        browser._pw = MagicMock(spec=["stop"])
        with patch.object(browser, "_save_cookies", side_effect=OSError("save failed")):
            browser.__exit__()
        browser._pw.stop.assert_called_once()

    def test_concurrent_cookie_saves_publish_complete_json_snapshots(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import MagicMock
        import json
        with TemporaryDirectory() as directory:
            cookie = Path(directory) / "cookies.json"
            cookie.write_text('{"cookies": []}')
            browsers = [Browser() for _ in range(3)]
            for i, browser in enumerate(browsers):
                browser.context = MagicMock()
                browser.context.storage_state.return_value = {"cookies": [{"name": str(i)}], "origins": []}
            with patch("engine.browser.COOKIE_FILE", cookie), ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(browser._save_cookies) for _ in range(20) for browser in browsers]
                while not all(f.done() for f in futures):
                    self.assertIn("cookies", json.loads(cookie.read_text()))
                for future in futures:
                    future.result()
            self.assertIn("cookies", json.loads(cookie.read_text()))
            self.assertEqual([cookie], list(Path(directory).iterdir()))

    def test_supplier_specific_requisites_page_is_checked_first(self):
        self.assertEqual("/index.php?sid=60", _requisites_paths("elektromehanika.ru")[0])
        self.assertEqual("/about/terms/", _requisites_paths("electro-mpo.ru")[0])
        self.assertEqual("/company/contacts/rekvizity/", _requisites_paths("etm.ru")[0])
        self.assertEqual("", _requisites_paths("other.ru")[0])

    def test_electro_mpo_uses_own_purchase_terms_to_identify_seller(self):
        terms = ("I. САМОВЫВОЗ\nПродавец: ЗАО «МПО Электромонтаж», "
                 "ИНН 7719079370, 125481, г. Москва, ул. Планерная.\n")
        self.assertIsNone(_electro_mpo_terms_seller(
            terms.replace("7719079370", "7719079371")))
        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page.inner_text.return_value = terms
        with patch.dict("engine.requisites.supplier._cache", {}, clear=True), \
             patch("engine.requisites.supplier.complete_json") as llm:
            seller = fetch_requisites_from_site(
                browser, "https://www.electro-mpo.ru/catalog/product/")
        self.assertEqual("ЗАО «МПО Электромонтаж»", seller.name)
        self.assertEqual("7719079370", seller.inn)
        browser.page.assert_called_once_with("https://www.electro-mpo.ru/about/terms/")
        llm.assert_not_called()

    def test_failed_requisites_attempt_does_not_poison_later_positions(self):
        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        terms = 'Продавец: ЗАО «МПО Электромонтаж», ИНН 7719079370, Москва.'
        with patch.dict("engine.requisites.supplier._cache", {}, clear=True), \
             patch("engine.requisites.supplier.complete_json", side_effect=TimeoutError):
            page.inner_text.return_value = "Временно недоступно"
            self.assertIsNone(fetch_requisites_from_site(browser, "https://www.electro-mpo.ru/product"))
            page.inner_text.return_value = terms
            seller = fetch_requisites_from_site(browser, "https://www.electro-mpo.ru/product")
            self.assertEqual("7719079370", seller.inn)
            calls = browser.page.call_count
            self.assertIs(seller, fetch_requisites_from_site(browser, "https://www.electro-mpo.ru/other"))
            self.assertEqual(calls, browser.page.call_count)

    def test_incomplete_seller_name_is_retryable(self):
        browser = MagicMock()
        browser.page.return_value.__enter__.return_value.inner_text.return_value = "ИНН 7719079370"
        with patch.dict("engine.requisites.supplier._cache", {}, clear=True), \
             patch("engine.requisites.supplier.complete_json", side_effect=[
                 {"inn": "7719079370", "name": " "},
                 {"inn": "7719079370", "name": "ЗАО МПО Электромонтаж"}]):
            self.assertIsNone(fetch_requisites_from_site(browser, "https://example.ru/product"))
            seller = fetch_requisites_from_site(browser, "https://example.ru/product")
            self.assertEqual("ЗАО МПО Электромонтаж", seller.name)

    def test_etm_uses_product_linked_requisites_page(self):
        page_text = ("Реквизиты АО «ТД ЭЛЕКТРОТЕХМОНТАЖ»\n"
                     "Полное наименование\nАкционерное общество «ТД Электротехмонтаж»\n"
                     "ИНН\n7842224734\nКПП\n784201001\n"
                     "Адрес юридического лица\n191144, г. Санкт-Петербург\n")
        self.assertIsNone(_etm_published_requisites(
            page_text.replace("7842224734", "7842224735")))
        browser = MagicMock()
        browser.page.return_value.__enter__.return_value.inner_text.return_value = page_text
        with patch.dict("engine.requisites.supplier._cache", {}, clear=True), \
             patch("engine.requisites.supplier.complete_json") as llm:
            seller = fetch_requisites_from_site(
                browser, "https://www.etm.ru/cat/nn/7492456")
        self.assertEqual("Акционерное общество «ТД Электротехмонтаж»", seller.name)
        self.assertEqual("7842224734", seller.inn)
        self.assertEqual("784201001", seller.kpp)
        browser.page.assert_called_once_with("https://www.etm.ru/company/contacts/rekvizity/")
        llm.assert_not_called()

    def test_artem_uses_dedicated_persistent_context_respecting_headless_mode(self):
        browser = Browser(headed=False)
        browser._pw = MagicMock()
        browser.context = sentinel.default_context
        with tempfile.TemporaryDirectory() as directory, \
             patch("engine.browser.CACHE_DIR", Path(directory)), \
             patch("engine.browser.settings.browser_channel", "chrome"), \
             patch("engine.browser.settings.browser_proxy_server", "http://127.0.0.1:2080"):
            context = browser._context_for_url("https://www.artem-tools.ru/catalog/product")
            self.assertIs(context, browser._context_for_url("https://www.artem-tools.ru/other"))
            self.assertIs(sentinel.default_context,
                          browser._context_for_url("https://www.kdds.ru/product"))

        launch = browser._pw.chromium.launch_persistent_context
        launch.assert_called_once()
        self.assertTrue(launch.call_args.kwargs["headless"])
        self.assertEqual({"server": "http://127.0.0.1:2080"},
                         launch.call_args.kwargs["proxy"])
        self.assertIn("browser_profiles/artem-tools",
                      launch.call_args.kwargs["user_data_dir"])

    def test_browser_passes_configured_proxy_to_chromium(self):
        playwright = _Playwright()
        with patch.object(__import__("playwright.sync_api", fromlist=["sync_playwright"]),
                          "sync_playwright",
                          return_value=_PlaywrightStarter(playwright)), \
                patch("engine.browser.COOKIE_FILE", Path("/nonexistent-kac-cookie")), \
                patch("engine.browser.settings.browser_proxy_server",
                      "http://127.0.0.1:2080"), \
                patch("engine.browser.settings.browser_channel", "chrome"):
            browser = Browser(headed=False)
            browser.__enter__()

        self.assertEqual(
            playwright.chromium.launch_kwargs["proxy"],
            {"server": "http://127.0.0.1:2080"},
        )
        self.assertEqual(playwright.chromium.launch_kwargs["channel"], "chrome")
        self.assertNotIn("user_agent", playwright.chromium.browser.context_kwargs)

    def test_rich_500_product_page_is_still_parseable(self):
        self.assertFalse(_http_response_is_blocked(500, "Карточка товара " * 500))
        self.assertTrue(_http_response_is_blocked(503, "Service unavailable"))
        self.assertTrue(_http_response_is_blocked(403, "Карточка товара " * 500))

    def test_browser_waits_only_for_navigation_commit(self):
        page = _Page()
        browser = Browser(headed=False)
        browser.context = _Context(page)

        with browser.page("https://supplier.example/product") as yielded:
            self.assertIs(yielded, page)
            self.assertEqual(page._kac_response_status, 200)

        self.assertEqual(page.goto_kwargs["wait_until"], "commit")
        self.assertEqual(page.goto_kwargs["timeout"], 10000)
        self.assertTrue(page.closed)
        self.assertEqual({"run_before_unload": True}, page.close_kwargs)

    def test_elektromehanika_static_html_avoids_blank_navigation(self):
        page = _Page()
        browser = Browser(headed=False)
        browser.context = _Context(page)
        response = MagicMock(status_code=200, text=(
            "<html><head><script src='https://slow.example/js'></script></head>"
            "<body>Цена</body></html>"
        ),
                             url="https://www.elektromehanika.ru/items_teh.php?id=1")
        with patch("engine.browser.httpx.get", return_value=response) as get:
            with browser.page("https://www.elektromehanika.ru/items_teh.php?id=1") as yielded:
                self.assertIs(yielded, page)
                self.assertEqual(200, yielded._kac_response_status)
        self.assertIsNone(page.goto_kwargs)
        self.assertIn("<base href=", page.content)
        self.assertNotIn("slow.example", page.content)
        self.assertEqual("domcontentloaded", page.set_content_kwargs["wait_until"])
        self.assertTrue(page.closed)
        get.assert_called_once()

    def test_xmlriver_allows_slow_tail_without_exposing_credentials(self):
        with patch.object(search_engine.settings, "xmlriver_user", "user"), \
                patch.object(search_engine.settings, "xmlriver_key", "key"), \
                patch("engine.search.search_engine.httpx.get", return_value=_Response()) as get:
            urls = search_engine.xmlriver_search("test query", 20)

        self.assertEqual(urls, ["https://supplier.example/product"])
        timeout = get.call_args.kwargs["timeout"]
        self.assertLessEqual(timeout.read, 30)
        self.assertGreater(timeout.read, 29)
        self.assertEqual(timeout.connect, 10)

    def test_xml_urls_decode_query_separators_before_navigation(self):
        xml = ("<doc><url>https://supplier.example/items_teh.php?g=1&amp;id=2</url></doc>"
               "<doc><url>https://supplier.example/card?a=1&#38;b=2</url></doc>")
        self.assertEqual(search_engine._urls_from_xml(xml), [
            "https://supplier.example/items_teh.php?g=1&id=2",
            "https://supplier.example/card?a=1&b=2",
        ])

    def test_browser_retries_one_navigation_timeout(self):
        page = _SlowFirstPage()
        browser = Browser(headed=False)
        browser.context = _Context(page)

        with browser.page("https://supplier.example/product") as yielded:
            self.assertEqual(yielded._kac_response_status, 200)

        self.assertEqual(page.calls, 2)

    def test_browser_uses_http_html_after_two_navigation_timeouts(self):
        page = _AlwaysSlowPage()
        browser = Browser(headed=False)
        browser.context = _Context(page)

        with patch("engine.browser.httpx.get", return_value=_HttpResponse()) as get:
            with browser.page("https://supplier.example/product") as yielded:
                self.assertEqual(yielded._kac_response_status, 200)
                self.assertIn("<base href=", yielded.content)

        self.assertEqual(get.call_count, 1)

    def test_browser_fallback_uses_configured_proxy(self):
        page = _AlwaysSlowPage()
        browser = Browser(headed=False)
        browser.context = _Context(page)

        with patch("engine.browser.settings.browser_proxy_server",
                   "http://127.0.0.1:2080"), \
                patch("engine.browser.httpx.get", return_value=_HttpResponse()) as get:
            with browser.page("https://supplier.example/product"):
                pass

        self.assertEqual(get.call_args.kwargs["proxy"], "http://127.0.0.1:2080")

    def test_xmlriver_retries_one_read_timeout(self):
        with patch.object(search_engine.settings, "xmlriver_user", "user"), \
                patch.object(search_engine.settings, "xmlriver_key", "key"), \
                patch("engine.search.search_engine.httpx.get", side_effect=[
                    search_engine.httpx.ReadTimeout("slow response"), _Response()
                ]) as get:
            urls = search_engine.xmlriver_search("test query", 20)

        self.assertEqual(urls, ["https://supplier.example/product"])
        self.assertEqual(get.call_count, 2)

    def test_xmlriver_does_not_retry_transient_xml_response_error(self):
        with patch.object(search_engine.settings, "xmlriver_user", "user"), \
                patch.object(search_engine.settings, "xmlriver_key", "key"), \
                patch("engine.search.search_engine.httpx.get", side_effect=[
                    _TransientXmlResponse(), _Response()
                ]) as get:
            from engine.search.failure_policy import SearchServiceUnavailable
            with self.assertRaises(SearchServiceUnavailable):
                search_engine.xmlriver_search("test query", 20)

        self.assertEqual(get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
