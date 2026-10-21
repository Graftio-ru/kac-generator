"""Offline checks for the ordered supplier search policy."""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from engine.models import PriceOffer, Requisites, SpecPosition
from engine.search import agent
from engine.search import search_engine
from engine.search.whitelist import fallback_hosts, requisites_for, source_type, whitelist_hosts


def _offer(url: str, price: float) -> PriceOffer:
    return PriceOffer(price_with_vat=price, url=url)


class SupplierListTests(unittest.TestCase):
    def test_cable_ru_minimum_price_is_not_a_quote(self) -> None:
        text = ("Кабель ППГнг(A)-HF 1х150\nот 1 695 руб./м с НДС\n"
                "Все цены, указанные в каталоге, представлены как минимальные "
                "(с пометкой «от»). Окончательная цена определяется индивидуально.")
        self.assertTrue(agent._site_declares_minimum_prices(
            "https://cable.ru/product", text
        ))
        self.assertFalse(agent._site_declares_minimum_prices(
            "https://other.ru/product", text
        ))
        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page._kac_response_status = 200
        page.title.return_value = "Кабель"
        with patch.object(agent, "_page_text_after_browser_check", return_value=text), \
             patch.object(agent, "_extract_from_page") as extract:
            offers, checked = agent._collect_offers(
                browser, ["https://cable.ru/product"], 1
            )
        self.assertEqual(([], 1), (offers, checked))
        extract.assert_not_called()

    def test_e_kc_category_listing_is_not_a_product_quote(self) -> None:
        browser = MagicMock()
        offers, checked = agent._collect_offers(
            browser,
            ["https://e-kc.ru/price/schetchik-elektroenergii?page=4"],
            1,
        )
        self.assertEqual(([], 0), (offers, checked))
        browser.page.assert_not_called()

    def test_offer_without_verified_seller_is_not_exported(self) -> None:
        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page._kac_response_status = 200
        page.title.return_value = "Товар"
        with patch.object(agent, "_page_text_after_browser_check", return_value="Товар 100 руб."), \
             patch.object(agent, "_extract_from_page", return_value={
                 "price_with_vat": 100,
                 "product_title": "Товар",
                 "matches_requested_product": True,
             }), \
             patch.object(agent, "wl_requisites_for", return_value=None), \
             patch.object(agent, "fetch_requisites_from_site", return_value=None), \
             patch.object(agent, "_save_product_screenshot") as screenshot:
            offers, checked = agent._collect_offers(
                browser, ["https://etm.ru/product/123"], 1, expected_name="Товар"
            )
        self.assertEqual(([], 1), (offers, checked))
        screenshot.assert_not_called()

    def test_etm_quotes_only_current_in_stock_jsonld_product(self) -> None:
        url = "https://www.etm.ru/cat/nn/7492456"
        product = {"@type": "Product", "sku": "7492456",
                   "offers": [{"url": url, "price": 2036.73,
                               "priceCurrency": "RUB",
                               "priceValidUntil": "2999-12-31",
                               "availability": "https://schema.org/InStock"}]}
        page = MagicMock()
        page.locator.return_value.all_text_contents.return_value = [json.dumps(product)]
        text = ("Кабель ППГнг(А)-HF 1х95\nКод товара: 7492456\n"
                "Ед.измерения:\tм\n2036.73 ₽/м\nХарактеристики\nАналоги и замены\n"
                "Чужой кабель 100 ₽/м")
        self.assertEqual((2036.73, "м"), agent._etm_main_product_price(page, url, text, "м"))
        self.assertIsNone(agent._etm_main_product_price(page, url, text, "шт"))
        for change in [
            {"sku": "7492457"},
            {"offers": [{**product["offers"][0], "url": "https://www.etm.ru/cat/nn/7492457"}]},
            {"offers": [{**product["offers"][0], "availability": "https://schema.org/OutOfStock"}]},
            {"offers": [{**product["offers"][0], "priceValidUntil": "2020-01-01"}]},
            {"offers": [{**product["offers"][0], "priceCurrency": "USD"}]},
        ]:
            with self.subTest(change=change):
                page.locator.return_value.all_text_contents.return_value = [
                    json.dumps({**product, **change})]
                self.assertIsNone(agent._etm_main_product_price(page, url, text, "м"))
        page.locator.return_value.all_text_contents.return_value = [json.dumps(product)]
        browser = MagicMock()
        browser.page.return_value.__enter__.return_value = page
        page._kac_response_status = 200
        page.title.return_value = "Кабель ППГнг(А)-HF 1х95"
        with patch.object(agent, "_page_text_after_browser_check", return_value=text), \
             patch.object(agent, "_extract_from_page", return_value={
                 "product_title": "Кабель ППГнг(А)-HF 1х95", "price_unit": "м",
                 "matches_requested_product": True, "moscow_pickup": True,
             }), \
             patch.object(agent, "wl_requisites_for", return_value=Requisites(
                 name="АО ТД Электротехмонтаж", inn="7842224734")), \
             patch.object(agent, "_save_product_screenshot"):
            offers, checked = agent._collect_offers(
                browser, [url], 1, expected_name="Кабель", expected_unit="м")
        self.assertEqual(1, checked)
        self.assertEqual(2036.73, offers[0].price_with_vat)
        self.assertEqual("м", offers[0].price_unit)

    def test_etm_minimum_order_subtotal_is_not_price_per_metre(self) -> None:
        url = "https://www.etm.ru/cat/nn/9862302"
        product = {"@type": "Product", "sku": "9862302", "offers": {
            "url": url, "price": 21579.84, "priceCurrency": "RUB",
            "priceValidUntil": "2999-12-31", "availability": "https://schema.org/InStock"}}
        page = MagicMock()
        page.locator.return_value.all_text_contents.return_value = [json.dumps(product)]
        text = ("Лоток 3526310\nЕд.измерения: м\n1 198.88 ₽/м\n"
                "1 102.26 ₽/м Для юрлиц\nЗаказ по 18 м\nАналоги и замены\n99 ₽/м")
        self.assertEqual((1198.88, "м"), agent._etm_main_product_price(page, url, text, "м"))
        self.assertIsNone(agent._etm_main_product_price(
            page, url, text.replace("Заказ по 18 м", "Заказ по 6 м"), "м"))
        self.assertIsNone(agent._etm_main_product_price(
            page, url, "Лоток\nЕд.измерения: м\nЗаказ по 18 м", "м"))

    def test_etm_live_sku_price_ignores_stale_order_total_and_analogue_prices(self):
        url = "https://www.etm.ru/cat/nn/9862301"
        product = {"@type": "Product", "sku": "9862301", "offers": {
            "url": url, "price": 23992.32, "priceCurrency": "RUB",
            "priceValidUntil": "2020-01-01", "availability": "https://schema.org/InStock"}}
        page, node, schema = MagicMock(), MagicMock(), MagicMock()
        expected_selector = '[data-testid="catalog-list-item-price-details-9862301-0-0"]'
        page.locator.side_effect = lambda selector: node if selector == expected_selector else schema
        node.count.return_value = 1
        node.nth.return_value = node.first
        node.first.is_visible.return_value = True
        node.first.inner_text.return_value = "999.68\n ₽/м"
        schema.all_text_contents.side_effect = lambda: [json.dumps(product)]
        text = "Лоток 3526210\nЕд.измерения: м\nАналоги и замены\n951.11 ₽/м\n999.68 ₽/м"
        self.assertEqual((999.68, "м"), agent._etm_main_product_price(page, url, text, "м"))
        for count, visible, price in [(0, True, "999.68 ₽/м"), (2, True, "999.68 ₽/м"),
                                       (1, False, "999.68 ₽/м"), (1, True, "999.68 ₽/шт"),
                                       (1, True, "от 999.68 ₽/м"), (1, True, "0 ₽/м")]:
            with self.subTest(count=count, visible=visible, price=price):
                node.count.return_value = count
                node.first.is_visible.return_value = visible
                node.first.inner_text.return_value = price
                self.assertIsNone(agent._etm_main_product_price(page, url, text, "м"))
        node.count.return_value = 1
        node.first.is_visible.return_value = True
        node.first.inner_text.return_value = "999.68 ₽/м"
        for key, value in [("availability", "https://schema.org/OutOfStock"),
                            ("url", "https://www.etm.ru/cat/nn/123"), ("priceCurrency", "USD")]:
            original = product["offers"][key]
            product["offers"][key] = value
            self.assertIsNone(agent._etm_main_product_price(page, url, text, "м"))
            product["offers"][key] = original
        product["offers"]["priceValidUntil"] = "2999-12-31"
        product["offers"]["price"] = 999.68
        node.count.return_value = 2
        self.assertIsNone(agent._etm_main_product_price(
            page, url, "Ед.измерения: м\n999.68 ₽/м\nАналоги и замены", "м"))
        hidden = MagicMock()
        hidden.is_visible.return_value = False
        node.count.return_value = 2
        node.nth.side_effect = lambda i: hidden if i == 0 else node.first
        self.assertEqual((999.68, "м"), agent._etm_main_product_price(page, url, text, "м"))
        product["sku"] = "123"
        self.assertIsNone(agent._etm_main_product_price(page, url, text, "м"))

    def test_aggregator_and_corporate_site_are_not_direct_quote_sources(self) -> None:
        self.assertEqual("aggregator", source_type("poisk.im"))
        self.assertEqual("corporate_site", source_type("russvet.ru"))
        browser = MagicMock()
        offers, checked = agent._collect_offers(
            browser, ["https://poisk.im/product/123", "https://russvet.ru/catalog/123"], 2
        )
        self.assertEqual(([], 0), (offers, checked))
        browser.page.assert_not_called()

    def test_one_active_list_contains_all_sites_and_legacy_reserve_is_empty(self):
        active=set(whitelist_hosts())
        self.assertEqual(len(active),36)
        self.assertTrue({'chipdip.ru','etm.ru','russvet.ru','minimaks.ru','electro-mpo.ru','220.ru','elektromehanika.ru','thermopat.com'} <= active)
        self.assertEqual(fallback_hosts(),[])

    def test_primary_requisites_apply_to_regional_subdomain(self) -> None:
        requisites = requisites_for("https://nsk.cable.ru/product")

        self.assertIsNotNone(requisites)
        self.assertEqual("9710008385", requisites.inn)
        self.assertEqual("ООО «Электропоставщик»", requisites.name)

    def test_site_search_discards_unlisted_domains_but_keeps_subdomains(self) -> None:
        results = [
            "https://ya.ru/products/card/product",
            "https://cable.ru/item",
            "https://tula.cable.ru/item",
        ]
        with patch.object(search_engine.settings, "xmlriver_user", "user"), \
             patch.object(search_engine.settings, "xmlriver_key", "key"), \
             patch.object(search_engine, "xmlriver_search", return_value=results):
            found = search_engine.search_on_sites("кабель", ["cable.ru"])

        self.assertEqual(results[1:], found)


class ProductModelTests(unittest.TestCase):
    def test_deepseek_object_queries_are_used_for_alias_search(self) -> None:
        position = SpecPosition(number=1, name="Кабель силовой", type_mark="ППГнг(А)-HF 1x50")
        with patch.object(agent, "complete_json", return_value={
            "queries": ["кабель безгалогенный 1х50 купить"]
        }):
            queries = agent.build_queries(position)

        self.assertIn("кабель безгалогенный 1х50 купить", queries)
        self.assertEqual('"ППГнг(А)-HF 1x50" цена', queries[0])

    def test_exact_mark_is_retained_before_semantic_alias_queries(self) -> None:
        position = SpecPosition(number=1, name="Длинное описание", type_mark="ППГнг(А)-HF 1x50")
        with patch.object(agent, "complete_json", return_value=[
            "безгалогенный силовой кабель 1х50 купить",
            "огнестойкий кабель ППГнг HF цена",
        ]) as llm_mock:
            queries = agent.build_queries(position)

        self.assertEqual('"ППГнг(А)-HF 1x50" цена', queries[0])
        self.assertIn("безгалогенный силовой кабель 1х50 купить", queries)
        self.assertIn("огнестойкий кабель ППГнг HF цена", queries)
        self.assertEqual(3, len(queries))
        llm_mock.assert_called_once()
        self.assertEqual(15, llm_mock.call_args.kwargs["timeout_seconds"])

    def test_comma_model_identity_is_not_split_into_equivalent_queries(self) -> None:
        position = SpecPosition(number=11, name="Трансформатор тока",
                                type_mark="CT30 300/5A, CT30-300-0.5-5")
        with patch.object(agent, "complete_json", return_value={
            "queries": ["трансформатор тока CT30 300/5 купить"]
        }):
            queries = agent.build_queries(position)

        self.assertEqual('"CT30 300/5A, CT30-300-0.5-5" цена', queries[0])
        self.assertEqual("трансформатор тока CT30 300/5 купить", queries[1])
        self.assertIn('CT30 300/5A, CT30-300-0.5-5', queries[2])

    def test_specific_alternative_model_code_confirms_matching_card(self) -> None:
        expected = "CT30 300/5A, CT30-300-0.5-5"
        self.assertIs(agent._type_mark_match_status(
            expected, "DKC трансформатор тока CT30-300-0.5-5"), True)
        self.assertIsNone(agent._type_mark_match_status(
            expected, "Трансформатор тока CT30 300/5А"))
        self.assertEqual(["ППГнг(А)-HF 3×4-0,66"],
                         agent._mark_variants("ППГнг(А)-HF 3×4-0,66"))

    def test_query_expansion_falls_back_and_deduplicates(self) -> None:
        position = SpecPosition(number=1, name="Кабель", type_mark="АБВ 1x50")
        with patch.object(agent, "complete_json", side_effect=RuntimeError("offline")):
            queries = agent.build_queries(position)

        self.assertEqual('"АБВ 1x50" цена', queries[0])
        self.assertIn('"АБВ 1x50" Кабель купить', queries)
        self.assertEqual(len(queries), len({query.casefold() for query in queries}))

    def test_dimension_variants_match(self) -> None:
        self.assertTrue(agent._type_mark_matches(
            "ППГнг(А)-HF 1x50", "Кабель ППГнг(А)-HF 1х50, 0,66 кВ"
        ))
        self.assertTrue(agent._type_mark_matches(
            "ППГнг(А)-HF 1x50", "Кабель ППГнг(А)-HF 1х50мк-1 ГОСТ"
        ))

    def test_different_dimension_is_rejected(self) -> None:
        self.assertFalse(agent._type_mark_matches(
            "ППГнг(А)-HF 1x50", "Кабель ППГнг(А)-HF 3х1,5"
        ))
        self.assertFalse(agent._type_mark_matches(
            "ППГнг(А)-HF 1x50", "Кабель ППГнг(А)-HF 1х120"
        ))

    def test_text_only_mark_must_match(self) -> None:
        self.assertTrue(agent._type_mark_matches(
            "THERMOPAT CAE", "Огнезащитное покрытие THERMOPAT CAE"
        ))
        self.assertFalse(agent._type_mark_matches(
            "THERMOPAT CAE", "Точка доступа QTECH QWP-82"
        ))
        self.assertFalse(agent._type_mark_matches(
            "THERMOPAT CAE", "Лента THERMOPAT BT-01"
        ))

    def test_catalog_code_may_be_present_in_url(self) -> None:
        self.assertTrue(agent._type_mark_matches(
            "3526210", "Лоток DKC https://shop.example/dkc-3526210"
        ))

    def test_multi_product_listing_urls_are_not_price_cards(self) -> None:
        self.assertTrue(agent._is_listing_url(
            "https://www.chipdip.ru/catalog-show/shpilki-rezbovye-2773?page=2"
        ))
        self.assertTrue(agent._is_listing_url(
            "https://e-kc.ru/price/shpilka-rezbovaya-sploshnaya-rezba"
        ))
        self.assertTrue(agent._is_listing_url(
            "https://www.elektro.ru/catalog/shpilki/?page=2"
        ))
        self.assertTrue(agent._is_listing_url(
            "https://www.elektromehanika.ru/items.php?id=29799"
        ))
        self.assertTrue(agent._is_listing_url(
            "https://www.minimaks.ru/catalog/kabeli-silovye/95-mm2/"
        ))
        self.assertTrue(agent._is_listing_url(
            "https://www.220.ru/kabel-kanaly-korob/minikanaly/40x40"
        ))
        self.assertTrue(agent._is_listing_url(
            "https://elektromehanika.ru/products.php?pid=39202"
        ))
        self.assertTrue(agent._is_listing_url("https://thermopat.com/products"))
        self.assertFalse(agent._is_listing_url("https://thermopat.com/product-fm2k"))
        self.assertFalse(agent._is_listing_url(
            "https://www.minimaks.ru/product/kabel-ppgng-hf-1x95/"
        ))
        self.assertFalse(agent._is_listing_url(
            "https://www.elektromehanika.ru/items_teh.php?g=1&id=2"
        ))
        self.assertFalse(agent._is_listing_url(
            "https://www.chipdip.ru/product/shpilka-m10h1000"
        ))
        self.assertFalse(agent._is_listing_url(
            "https://e-kc.ru/cena/shpilka-m10h1000-cm201001inox"
        ))
        browser = MagicMock()
        offers, checked = agent._collect_offers(browser, [
            "https://www.chipdip.ru/catalog-show/shpilki-rezbovye-2773?page=2"
        ], 5, "CM201001INOX", "Шпилька М10х1000", "шт")
        self.assertEqual(([], 0), (offers, checked))
        browser.page.assert_not_called()

    def test_elektromehanika_main_card_out_of_stock_is_not_a_quote(self) -> None:
        card = ("EXPERt1 Кабель ППГнг(А)-HF 1х95\n"
                "Артикул: EXPERt1Производитель: Эксперт-Кабель"
                "Нет в наличииОписание\n1155.7 ₽")
        self.assertTrue(agent._elektromehanika_card_out_of_stock(card))
        self.assertFalse(agent._elektromehanika_card_out_of_stock(
            "Артикул: EXPERt1Производитель: Эксперт-КабельДоступно: 12 м\n1155.7 ₽"))

    def test_dkc_market_waits_for_product_and_rejects_out_of_stock(self) -> None:
        card = ("Угол CS 45 в комплекте с крепежными элементами\n"
                "арт. 36722k\nНет в наличии\nцена за 1 шт\n2 835.34 ₽\nОписание")
        self.assertTrue(agent._dkc_market_card_out_of_stock(card))
        self.assertFalse(agent._dkc_market_card_out_of_stock(
            card.replace("Нет в наличии", "В корзину")))
        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page._kac_response_status = 200
        page.title.return_value = "36722K Угол CS 45"
        with patch.object(agent, "_page_text_after_browser_check", return_value=card), \
             patch.object(agent, "_extract_from_page") as extract:
            offers, checked = agent._collect_offers(
                browser, ["https://www.dkc.market/product/36722k-ugol"],
                1, "36722K", "Угол CS 45", "компл.",
            )
        self.assertEqual(([], 1), (offers, checked))
        page.locator.return_value.first.wait_for.assert_called_with(
            state="visible", timeout=8000,
        )
        extract.assert_not_called()

    def test_xcom_unavailable_card_does_not_quote_recommended_products(self) -> None:
        card = ("Код товара: 822805\nАртикул: DKC SSC90500K\n"
                "Нет в наличии\nОписание\nРекомендуем приобрести\n"
                "Кабель ППГнг(А)-HF\n130 ₽")
        self.assertTrue(agent._xcom_card_out_of_stock(card))
        self.assertFalse(agent._xcom_card_out_of_stock(
            card.replace("Нет в наличии", "3 458 ₽\nДобавить в корзину")))
        self.assertFalse(agent._xcom_card_out_of_stock(
            "Код товара: 822805\n3 458 ₽\nОписание\nНет в наличии"))
        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page._kac_response_status = 200
        page.title.return_value = "DKC SSC90500K"
        with patch.object(agent, "_page_text_after_browser_check", return_value=card), \
             patch.object(agent, "_extract_from_page") as extract, \
             agent.capture_card_diagnostics() as diagnostics:
            offers, checked = agent._collect_offers(
                browser, ["https://www.xcom-shop.ru/dkc_ssc90500k_822805.html"],
                1, "SSC90500K", "Перегородка SEP", "компл.",
            )
        self.assertEqual(([], 1), (offers, checked))
        self.assertEqual("out_of_stock", diagnostics[0]["code"])
        extract.assert_not_called()

    def test_elektromehanika_listing_resolves_only_exact_article(self) -> None:
        response = MagicMock(content=(
            '<div class="sa-strock"><p>CM201001INOX316L</p>'
            '<a href=./items_teh.php?g=1&amp;id=1>wrong</a>'
            '<div class="sa-strock"><p>CM201001INOX</p>'
            '<a href=./items_teh.php?g=29798&amp;id=113059>right</a>'
        ).encode())
        with patch.object(agent.httpx, "get", return_value=response) as get:
            self.assertEqual(
                "https://www.elektromehanika.ru/items_teh.php?g=29798&id=113059",
                agent._resolve_elektromehanika_listing(
                    "https://www.elektromehanika.ru/items.php?id=29799",
                    "CM201001INOX",
                ),
            )
        self.assertIn("artpro=CM201001INOX", get.call_args.args[0])

    def test_elektromehanika_direct_serp_hit_is_replaced_by_exact_article(self) -> None:
        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page._kac_response_status = 200
        page.title.return_value = "Другая карточка"
        wrong = "https://elektromehanika.ru/items_teh.php?g=1&id=2"
        exact = "https://www.elektromehanika.ru/items_teh.php?g=3&id=4"
        with patch.object(agent, "_resolve_elektromehanika_listing", return_value=exact), \
             patch.object(agent, "_page_text_after_browser_check", return_value="Другая карточка"), \
             patch.object(agent, "_extract_from_page", return_value={}):
            offers, checked = agent._collect_offers(
                browser, [wrong], 1, "3526210", "Лоток", "м"
            )
        self.assertEqual(([], 1), (offers, checked))
        browser.page.assert_called_once_with(exact)

    def test_elektromehanika_uses_main_price_not_warehouse_quote(self) -> None:
        text = ("Артикул: 3526210Производитель: DKCДоступно: 75 штОписание\n"
                "2248.73 ₽\nНаличие на складах:\nЭТМ: 48 шт 699.76 ₽/шт")
        self.assertEqual(
            (2248.73 / 3, "шт"),
            agent._elektromehanika_price_per_requested_unit(
                text, "3526210 Лоток перфорированный 100х50 L3000 мм", "м"
            ),
        )

    def test_catalog_code_with_variant_suffix_is_rejected(self) -> None:
        self.assertFalse(agent._type_mark_match_status(
            "3526210", "Лоток DKC 3526210HDZ https://shop.example/3526210hdz"
        ))

    def test_missing_catalog_code_is_unknown_for_semantic_check(self) -> None:
        self.assertIsNone(agent._type_mark_match_status(
            "3526210", "Лоток перфорированный DKC 100x50 L3000"
        ))

    def test_conflicting_model_with_same_prefix_is_rejected(self) -> None:
        self.assertFalse(agent._type_mark_match_status(
            "CE308 S31.543.OAA", "Счётчик CE208 S31.543.OAA"
        ))
        self.assertFalse(agent._type_mark_match_status(
            "CE308 1x50", "Счётчик CE208 1x50"
        ))

    def test_conflicting_article_in_url_overrides_extracted_title(self) -> None:
        url = "https://www.xcom-shop.ru/dkc_cm201001_265912.html"
        self.assertIs(agent._type_mark_match_status("CM201001INOX", url), False)
        self.assertIsNone(agent._type_mark_match_status(
            "CM201001INOX", "https://e-kc.ru/cena/shpilka-m10h1000"
        ))
        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page._kac_response_status = 200
        page.title.return_value = "Шпилька DKC"
        with patch.object(agent, "_page_text_after_browser_check", return_value="Шпилька DKC 327 руб."), \
             patch.object(agent, "_extract_from_page", return_value={
                 "price_with_vat": 327,
                 "product_title": "Шпилька DKC CM201001INOX",
                 "matches_requested_product": True,
             }):
            offers, checked = agent._collect_offers(
                browser, [url], 1, "CM201001INOX", "Шпилька", "шт"
            )
        self.assertEqual(([], 0), (offers, checked))
        browser.page.assert_not_called()

    def test_full_sku_conflict_names_observed_variant_without_quoting_it(self) -> None:
        url = "https://www.xcom-shop.ru/iek_uhh32-d036-200-100_463305.html"
        self.assertEqual(["UHH32-D036-200-100"], agent._different_full_skus_in_url(
            "UHH32-D036-200-99", url))
        browser = MagicMock()
        with agent.capture_card_diagnostics() as diagnostics:
            offers, checked = agent._collect_offers(
                browser, [url], 1, "UHH32-D036-200-99", "Хомут", "уп.")
        self.assertEqual(([], 0), (offers, checked))
        self.assertEqual("article_mismatch", diagnostics[0]["code"])
        self.assertEqual("UHH32-D036-200-100", diagnostics[0]["observed_article"])
        self.assertIn("вместо UHH32-D036-200-99", diagnostics[0]["reason"])
        browser.page.assert_not_called()

    def test_page_extraction_asks_for_semantic_match_in_same_llm_call(self) -> None:
        response = {
            "price_with_vat": 100,
            "product_title": "Эквивалентное название",
            "matches_requested_product": True,
            "match_reason": "синоним",
        }
        with patch.object(agent, "complete_json", return_value=response) as llm_mock:
            result = agent._extract_from_page(
                "Карточка товара", "Огнезащитное покрытие", "THERMOPAT CAE", "кг"
            )

        self.assertEqual(response, result)
        llm_mock.assert_called_once()
        prompt = llm_mock.call_args.args[0]
        self.assertIn("Огнезащитное покрытие", prompt)
        self.assertIn("THERMOPAT CAE", prompt)
        self.assertIn("Единица спецификации: кг", prompt)
        self.assertIn("matches_requested_product", llm_mock.call_args.kwargs["system"])
        self.assertEqual(120, llm_mock.call_args.kwargs["timeout_seconds"])

    def test_security_page_title_is_not_treated_as_product(self) -> None:
        self.assertTrue(agent._looks_like_bot_check(
            "Короткий текст без признаков капчи", "Доступ ограничен - Проверка безопасности"
        ))

    def test_unknown_mark_requires_positive_semantic_match(self) -> None:
        self.assertTrue(agent._product_match_is_accepted(None, True))
        self.assertFalse(agent._product_match_is_accepted(None, False))
        self.assertFalse(agent._product_match_is_accepted(None, None))

    def test_semantic_rejection_overrides_literal_match(self) -> None:
        self.assertFalse(agent._product_match_is_accepted(True, False))

    def test_other_explicit_article_overrides_semantic_match(self) -> None:
        self.assertTrue(agent._conflicting_explicit_article(
            "CM201001INOX", "IEK Шпилька М10х1000 DIN 975, CLW10-TM-10-1-R",
            "https://ie-moscow.ru/product/iek-clw10-tm-10-1-r/209548"
        ))
        self.assertFalse(agent._conflicting_explicit_article(
            "CM201001INOX", "Шпилька М10х1000 нерж. DIN975",
            "https://lanitnord.ru/product/shpilka-m10kh1000"
        ))
        self.assertFalse(agent._conflicting_explicit_article(
            "CT30 300/5A, CT30-300-0.5-5",
            "Трансформатор тока DKC CT30-300-0.5-5",
            "https://elevel.ru/product/dkc-ct30-300-0-5-5"
        ))
        self.assertFalse(agent._conflicting_explicit_article(
            "CT30 300/5A, CT30-300-0.5s-2.5",
            "Трансформатор тока CT30-300/5A класс 0.5s DKC",
            "https://chipdip.ru/product/transformator-toka-ct30-300-5a-klass-0.5s"
        ))
        self.assertFalse(agent._conflicting_explicit_article(
            "CT30 300/5A, CT30-300-0.5s-2.5",
            "Трансформатор тока DKC CT30-300-0,5S-2,5",
            "https://elevel.ru/product/ct30-300-0-5s-2-5"
        ))
        self.assertTrue(agent._conflicting_explicit_article(
            "CM201001INOX", "Шпилька DKC CM201001",
            "https://xcom.ru/product/dkc-cm201001"
        ))
        self.assertTrue(agent._conflicting_explicit_article(
            "CT30 300/5A, CT30-300-0.5s-2.5",
            "Трансформатор тока IEK TTI-30 300/5A",
            "https://spb.lanitnord.ru/product/transformator-toka-tti-30-300-5a"
        ))
        self.assertTrue(agent._conflicting_explicit_article(
            "CT30 300/5A, CT30-300-0.5s-2.5",
            "Трансформатор тока ТТИ-30 300/5А ИЭК",
            "https://spb.lanitnord.ru/product/transformator-toka-tti-30-300-5a"
        ))

    def test_exact_short_article_is_not_confused_with_family_label(self) -> None:
        for article, family in [("36041K", "CPO90"), ("30013K", "GSV50")]:
            with self.subTest(article=article):
                self.assertFalse(agent._conflicting_explicit_article(
                    article, f"Угол DKC {article} {family}",
                    f"https://supplier.ru/product/{article.lower()}-{family.lower()}"
                ))

    def test_short_article_override_requires_whole_token_on_both_surfaces(self) -> None:
        for title_article, url_article in [
            ("36042K", "36042k"), ("36041KX", "36041kx"),
            ("136041K", "136041k"), ("36041K", "36041kx"),
            ("36041KX", "36041k"),
        ]:
            with self.subTest(title=title_article, url=url_article):
                self.assertTrue(agent._conflicting_explicit_article(
                    "36041K", f"Угол {title_article} CPO90",
                    f"https://supplier.ru/{url_article}-cpo90"
                ))

    def test_short_article_does_not_rescue_compatibility_card_with_other_sku(self) -> None:
        self.assertEqual("abc123", agent._different_explicit_article(
            "36041K", "Другая деталь ABC123 совместима с 36041K CPO90",
            "https://supplier.ru/abc123-compatible-36041k-cpo90"
        ))

    def test_short_article_accepts_cyrillic_k_suffix_without_family_conflict(self) -> None:
        for expected in ["36041K", "36041К"]:
            with self.subTest(expected=expected):
                self.assertFalse(agent._conflicting_explicit_article(
                    expected, "Угол S5 36041К CPO90 90х100х100 (ДКС)",
                    "https://supplier.ru/36041k-cpo90-90kh100kh100"
                ))
                self.assertTrue(agent._conflicting_explicit_article(
                    expected, "Угол 36041КX CPO90",
                    "https://supplier.ru/36041kx-cpo90"
                ))

    def test_article_rejection_preserves_extracted_title_and_conflicting_token(self) -> None:
        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page._kac_response_status = 200
        page.title.return_value = "Карточка товара"
        title = "Угол DKC 36042K CPO90"
        with patch.object(agent, "_page_text_after_browser_check", return_value="Описание товара"), \
             patch.object(agent, "_extract_from_page", return_value={"product_title": title}), \
             agent.capture_card_diagnostics() as diagnostics:
            offers, _ = agent._collect_offers(
                browser, ["https://supplier.ru/36042k-cpo90"], 1,
                expected_type_mark="36041K"
            )
        self.assertEqual([], offers)
        self.assertEqual("article_mismatch", diagnostics[0]["code"])
        self.assertEqual("cpo90", diagnostics[0]["observed_article"])
        self.assertEqual(title, diagnostics[0]["product_title"])
        self.assertIn("36041K", diagnostics[0]["reason"])

    def test_package_price_without_sale_unit_cannot_be_normalised(self) -> None:
        data = {
            "price_with_vat": 102169,
            "listed_price_with_vat": 102169,
            "package_quantity": 20,
            "listed_price_covers_multiple_requested_units": True,
        }

        self.assertIsNone(agent._price_per_requested_unit(data))

    def test_already_normalised_unit_price_is_not_divided_again(self) -> None:
        data = {
            "price_with_vat": 5108.45,
            "listed_price_with_vat": 102169,
            "package_quantity": 20,
            "listed_price_covers_multiple_requested_units": False,
        }

        self.assertAlmostEqual(5108.45, agent._price_per_requested_unit(data))

    def test_listed_price_is_usable_when_matching_unit_price_is_missing(self) -> None:
        data = {"price_with_vat": None, "listed_price_with_vat": 1895.08,
                "price_unit": "шт"}
        self.assertAlmostEqual(1895.08, agent._price_per_requested_unit(data, "шт."))
        self.assertIsNone(agent._price_per_requested_unit(data, "м"))

    def test_package_price_needs_explicit_quantity_for_piece_quote(self) -> None:
        data = {"price_with_vat": 100, "listed_price_with_vat": 100,
                "price_unit": "уп", "package_quantity": 4,
                "listed_price_covers_multiple_requested_units": False}
        self.assertIsNone(agent._price_per_requested_unit(data, "шт"))
        data["listed_price_covers_multiple_requested_units"] = True
        self.assertEqual(25, agent._price_per_requested_unit(data, "шт"))
        data["price_unit"] = ""
        self.assertIsNone(agent._price_per_requested_unit(data, "шт"))

    def test_pack70_title_rejects_mislabeled_piece_price_and_preserves_pack_conversion(self):
        title = 'DKC Забивной анкер M10 (уп.70шт.)'
        data = {'price_with_vat': 4938, 'listed_price_with_vat': 4938,
                'price_unit': 'шт', 'package_quantity': 1,
                'listed_price_covers_multiple_requested_units': False}
        self.assertIsNone(agent._price_per_requested_unit(data, 'шт', title))
        self.assertFalse(agent._saved_price_unit_is_verified('шт', 'шт', 4938, 4938, title))
        data.update(price_unit='уп', package_quantity=70,
                    listed_price_covers_multiple_requested_units=True)
        self.assertAlmostEqual(4938 / 70, agent._price_per_requested_unit(data, 'шт', title))
        self.assertTrue(agent._saved_price_unit_is_verified('шт', 'уп', 4938, 4938 / 70, title))
        data['package_quantity'] = 7
        self.assertIsNone(agent._price_per_requested_unit(data, 'шт', title))
        conflict = title + ' (уп.100шт.)'
        self.assertIsNone(agent._price_per_requested_unit(dict(data, price_unit='шт'), 'шт', conflict))
        self.assertIsNone(agent._price_per_requested_unit(data, 'шт', conflict))
        self.assertFalse(agent._saved_price_unit_is_verified('шт', 'уп', 4938, 4938 / 70, conflict))

    def test_old_xcom_quote_needs_independent_selling_unit_evidence(self):
        url = 'https://www.xcom-shop.ru/dkc_3526310_123.html'
        self.assertFalse(agent._saved_price_unit_is_verified('м', 'шт', None, 5487,
                                                            'Лоток L3000', url))
        self.assertTrue(agent._saved_price_unit_is_verified('м', 'шт', 16461, 5487,
                            'Лоток L3000', url, 'Единица измерения: шт'))
        self.assertFalse(agent._saved_price_unit_is_verified('шт', 'уп', 4938, 4938 / 70,
                              'Анкер (уп.70шт.)', url))
        self.assertEqual('', agent._xcom_main_sale_unit('Единица измерения: шт\nЕд. изм.: м'))

    def test_pack_aliases_preserve_the_price_for_one_requested_pack(self) -> None:
        data = {"price_with_vat": 2.44, "listed_price_with_vat": 244,
                "price_unit": "упак", "package_quantity": 100,
                "listed_price_covers_multiple_requested_units": True}
        self.assertEqual(244, agent._price_per_requested_unit(data, "уп."))
        self.assertEqual(2.44, agent._price_per_requested_unit(data, "шт"))

    def test_same_sale_unit_does_not_divide_an_order_multiple(self) -> None:
        data = {"price_with_vat": 0.79255, "listed_price_with_vat": 158.51,
                "price_unit": "м", "package_quantity": 200,
                "listed_price_covers_multiple_requested_units": True}
        self.assertIsNone(agent._price_per_requested_unit(data, "м"))

    def test_piece_price_cannot_be_used_for_requested_pack(self) -> None:
        data = {"price_with_vat": 2.44, "listed_price_with_vat": 244,
                "price_unit": "шт", "package_quantity": 100,
                "listed_price_covers_multiple_requested_units": True}
        self.assertIsNone(agent._price_per_requested_unit(data, "уп."))
        self.assertFalse(agent._saved_price_unit_is_verified("уп", "шт", 244, 244))
        self.assertFalse(agent._saved_price_unit_is_verified("уп", "уп", None, 244))
        self.assertTrue(agent._saved_price_unit_is_verified("уп", "упак", 244, 244))

    def test_different_full_sku_in_url_is_rejected_before_opening_page(self) -> None:
        browser = MagicMock()
        url = "https://www.ie-moscow.ru/product/iek_uhh31-d048-200-100/11108"
        offers, checked = agent._collect_offers(
            browser, [url], 1, "UHH31-D048-200-99", "Хомут", "уп"
        )
        self.assertEqual(([], 0), (offers, checked))
        browser.page.assert_not_called()
        self.assertTrue(agent._conflicting_full_sku_in_url(
            "UHH31-D048-200-99", "https://cable.ru/uhh31_d048_200_100.php"))
        self.assertFalse(agent._conflicting_full_sku_in_url(
            "UHH31-D048-200-99", "https://cable.ru/uhh31_d048_200_99-item.php"))
        self.assertFalse(agent._conflicting_full_sku_in_url(
            "CT30 300/5A, CT30-300-0.5S-2.5", "https://shop.ru/ct30-3005a.html"))

    def test_complete_set_is_not_split_into_component_prices(self) -> None:
        data = {"price_with_vat": 50, "listed_price_with_vat": 200,
                "price_unit": "компл.", "package_quantity": 4,
                "listed_price_covers_multiple_requested_units": True}
        self.assertEqual(200, agent._price_per_requested_unit(data, "компл."))

    def test_pack_of_kit_components_is_not_four_complete_kits(self) -> None:
        data = {"price_with_vat": 205, "listed_price_with_vat": 820,
                "price_unit": "упак.", "package_quantity": 4,
                "listed_price_covers_multiple_requested_units": True}
        self.assertIsNone(agent._price_per_requested_unit(
            data, "компл.", "30013K, Пластина крепежная GSV H50 в комплекте с метизами"))

    def test_piece_price_is_one_set_only_when_title_names_included_hardware(self) -> None:
        data = {"price_with_vat": 3372.01, "listed_price_with_vat": 3372.01,
                "price_unit": "шт.",
                "listed_price_covers_multiple_requested_units": False}
        title = "Ответвитель DKC 36161K в комплекте с крепежными элементами"
        self.assertEqual(3372.01, agent._price_per_requested_unit(data, "компл.", title))
        self.assertIsNone(agent._price_per_requested_unit(
            data, "компл.", "Ответвитель DKC 36161K без крепежа"))

    def test_metre_price_for_one_metre_stud_can_be_checked_per_piece(self) -> None:
        data = {
            "price_with_vat": 1416.1,
            "listed_price_with_vat": 1416.1,
            "price_unit": "м.",
        }
        self.assertAlmostEqual(1416.1, agent._price_per_requested_unit(
            data, "шт", "Шпилька М10х1000 INOX - CM201001INOX"
        ))

    def test_metre_piece_conversion_requires_length_and_consistent_price(self) -> None:
        data = {
            "price_with_vat": 1416.1,
            "listed_price_with_vat": 1416.1,
            "price_unit": "м",
        }
        self.assertIsNone(agent._price_per_requested_unit(
            data, "шт", "Шпилька CM201001INOX"
        ))
        self.assertIsNone(agent._price_per_requested_unit(
            data, "шт", "Шпилька М10х2000 INOX"
        ))
        data["price_with_vat"] = 2832.2
        self.assertAlmostEqual(2832.2, agent._price_per_requested_unit(
            data, "шт", "Шпилька М10х2000 INOX"
        ))

    def test_piece_price_for_three_metre_tray_is_checked_per_metre(self) -> None:
        data = {
            "price_with_vat": 1000,
            "listed_price_with_vat": 3000,
            "price_unit": "шт",
        }
        self.assertAlmostEqual(1000, agent._price_per_requested_unit(
            data, "м", "Лоток DKC 100x50 L3000"
        ))

    def test_e_kc_reads_selling_unit_from_offer_row(self) -> None:
        page = MagicMock()
        rows = page.locator.return_value
        rows.count.return_value = 1
        row = rows.nth.return_value
        row.locator.return_value.inner_text.return_value = ""
        row.locator.side_effect = lambda selector: {
            "td.title": MagicMock(inner_text=lambda: "Шпилька М10х1000 INOX CM201001INOX"),
            'td.price meta[itemprop="price"]': MagicMock(
                get_attribute=lambda _name: "1416.1"
            ),
            "td.input .colWo": MagicMock(inner_text=lambda: "м."),
        }[selector]
        self.assertEqual((1416.1, "м"), agent._ekc_price_per_requested_unit(
            page, "CM201001INOX", "шт"
        ))
        self.assertIsNone(agent._ekc_price_per_requested_unit(
            page, "CM201001", "шт"
        ))

    def test_e_kc_rejects_metre_price_without_product_length(self) -> None:
        page = MagicMock()
        rows = page.locator.return_value
        rows.count.return_value = 1
        row = rows.nth.return_value
        row.locator.side_effect = lambda selector: {
            "td.title": MagicMock(inner_text=lambda: "Шпилька INOX CM201001INOX"),
            'td.price meta[itemprop="price"]': MagicMock(
                get_attribute=lambda _name: "1416.1"
            ),
            "td.input .colWo": MagicMock(inner_text=lambda: "м."),
        }[selector]
        self.assertIsNone(agent._ekc_price_per_requested_unit(
            page, "CM201001INOX", "шт"
        ))

    def test_e_kc_uses_full_variant_from_composite_transformer_mark(self) -> None:
        page = MagicMock()
        rows = page.locator.return_value
        rows.count.return_value = 1
        row = rows.nth.return_value
        title = MagicMock(inner_text=lambda: "DKC CT30-300-0.5-5 класс 0.5 5ВА")
        row.locator.side_effect = lambda selector: {
            "td.title": title,
            'td.price meta[itemprop="price"]': MagicMock(
                get_attribute=lambda _name: "2904.98"
            ),
            "td.input .colWo": MagicMock(inner_text=lambda: "шт."),
        }[selector]
        self.assertEqual((2904.98, "шт"), agent._ekc_price_per_requested_unit(
            page, "CT30 300/5A, CT30-300-0.5-5", "шт."
        ))
        self.assertIsNone(agent._ekc_price_per_requested_unit(
            page, "CT30 300/5A, CT30-300-0.5s-2.5", "шт."
        ))

    def test_e_kc_piece_can_be_the_specified_complete_set(self) -> None:
        page = MagicMock()
        rows = page.locator.return_value
        rows.count.return_value = 1
        row = rows.nth.return_value
        row.locator.side_effect = lambda selector: {
            "td.title": MagicMock(inner_text=lambda:
                                  "Ответвитель DKC 36161K в комплекте с крепеж. эл."),
            'td.price meta[itemprop="price"]': MagicMock(
                get_attribute=lambda _name: "3372.01"),
            "td.input .colWo": MagicMock(inner_text=lambda: "шт."),
        }[selector]
        self.assertEqual((3372.01, "шт"), agent._ekc_price_per_requested_unit(
            page, "36161K", "компл."))

    def test_e_kc_offer_table_checks_full_variant_before_extraction(self) -> None:
        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page._kac_response_status = 200
        page.title.return_value = "Трансформатор тока"
        rows = page.locator.return_value
        rows.count.return_value = 1
        rows.nth.return_value.locator.return_value.inner_text.return_value = (
            "DKC CT30-300-0.5-2.5 класс 0.5 2.5ВА"
        )
        url = "https://e-kc.ru/product/transformator-toka"
        with patch.object(agent, "_page_text_after_browser_check", return_value="Товар в наличии"), \
             patch.object(agent, "_extract_from_page") as extract, \
             agent.capture_card_diagnostics() as diagnostics:
            offers, checked = agent._collect_offers(
                browser, [url], 1, expected_name="Трансформатор тока",
                expected_type_mark="CT30 300/5A, CT30-300-0.5s-2.5",
                expected_unit="шт", supplier_hosts={"e-kc.ru"})

        self.assertEqual(([], 1), (offers, checked))
        self.assertEqual("article_mismatch", diagnostics[0]["code"])
        extract.assert_not_called()

    def test_rs24_uses_item_length_not_order_multiplicity_for_metres(self) -> None:
        page = MagicMock()
        page.locator.return_value.count.return_value = 1
        page.locator.return_value.first.inner_text.return_value = "3\u00a0151.26"
        page.locator.return_value.first.get_attribute.return_value = "EA"
        text = "Лоток L3000\nКратность для заказа\n8\nшт\nДлина\n3000 мм\n"

        self.assertAlmostEqual(1050.42, agent._rs24_price_per_requested_unit(page, text, "м"))
        self.assertAlmostEqual(3151.26, agent._rs24_price_per_requested_unit(page, text, "шт"))
        self.assertAlmostEqual(3151.26, agent._rs24_price_per_requested_unit(page, text, "шт."))
        page.locator.assert_any_call("button.js-add-to-cart[data-uom-code]")

    def test_rs24_numeric_piece_code_requires_one_piece_pack_text(self) -> None:
        page = MagicMock()
        page.locator.return_value.count.return_value = 1
        page.locator.return_value.first.inner_text.return_value = "4\u00a0150.44"
        page.locator.return_value.first.get_attribute.return_value = "001"
        self.assertEqual(4150.44, agent._rs24_price_per_requested_unit(
            page, "В 1 упаковке 1 шт.\nшт", "шт."
        ))
        self.assertIsNone(agent._rs24_price_per_requested_unit(
            page, "В 1 упаковке 10 шт.\nуп.", "шт."
        ))

    def test_rs24_complete_kit_sold_as_one_piece(self) -> None:
        page = MagicMock()
        price = MagicMock()
        price.count.return_value = 1
        price.first.inner_text.return_value = "2\u00a0422.92"
        button = MagicMock()
        button.count.return_value = 1
        button.first.get_attribute.return_value = "001"
        title = MagicMock()
        title.count.return_value = 1
        title.first.inner_text.return_value = (
            "Угол DKC 36722K в компл. с крепежн. элементами"
        )
        page.locator.side_effect = lambda selector: {
            ".js-product-price": price,
            "button.js-add-to-cart[data-uom-code]": button,
            "h1": title,
        }[selector]

        self.assertEqual(2422.92, agent._rs24_price_per_requested_unit(
            page, "В 1 упаковке 1 шт.", "компл."
        ))
        title.first.inner_text.return_value = "Угол DKC 36722K без крепежа"
        self.assertIsNone(agent._rs24_price_per_requested_unit(
            page, "В 1 упаковке 1 шт.", "компл."
        ))
        title.first.inner_text.return_value = "Угол DKC 36722K в компл. с крепежн. элементами"
        self.assertIsNone(agent._rs24_price_per_requested_unit(
            page, "В 1 упаковке 10 шт.", "компл."
        ))

    def test_rs24_reads_main_manufacturer_article(self) -> None:
        text = (
            "Артикул производителя\n36722K\nОтзывы\n"
            "Характеристики\nАртикул производителя\nOTHER123\n"
        )
        self.assertEqual("36722K", agent._rs24_manufacturer_article(text))
        self.assertTrue(agent._conflicting_page_article(
            "SSC41000K", agent._rs24_manufacturer_article(
                "Артикул производителя\nSSG01000K\nХарактеристики"
            )
        ))

    def test_rs24_rejects_wrong_manufacturer_article_before_llm(self) -> None:
        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page._kac_response_status = 200
        page.title.return_value = "Перегородка SEP DKC SSG01000K"
        page.locator.return_value.first.get_attribute.return_value = "001"
        text = "Перегородка SEP DKC SSG01000K\nАртикул производителя\nSSG01000K\nХарактеристики"
        with patch.object(agent, "_page_text_after_browser_check", return_value=text), \
             patch.object(agent, "_extract_from_page") as extract:
            offers, _ = agent._collect_offers(
                browser, ["https://rs24.ru/product/2453375"], 1,
                expected_type_mark="SSC41000K", expected_unit="компл.",
                supplier_hosts={"rs24.ru"},
            )
        self.assertEqual([], offers)
        extract.assert_not_called()

    def test_chipdip_uses_visible_product_price_for_sized_piece(self) -> None:
        page = MagicMock()
        price = page.locator.return_value
        price.count.return_value = 1
        price.first.get_attribute.return_value = "3780"
        title = "Лоток перфорированный DKC 3526310 150х50 L3000 сталь 1мм"
        self.assertEqual(1260, agent._chipdip_price_per_requested_unit(page, title, "м", "шт"))
        self.assertIsNone(agent._chipdip_price_per_requested_unit(page, "Лоток DKC 3526310", "м", "шт"))
        self.assertIsNone(agent._chipdip_price_per_requested_unit(page, title, "м", "м"))

    def test_lanitnord_uses_loaded_card_price_only_for_same_unit(self) -> None:
        page = MagicMock()
        price = page.locator.return_value
        price.count.return_value = 1
        price.first.get_attribute.return_value = "1895.08"
        self.assertEqual(1895.08, agent._lanitnord_price_per_requested_unit(page, "шт.", "шт"))
        self.assertIsNone(agent._lanitnord_price_per_requested_unit(page, "м", "шт"))
        price.count.return_value = 2
        self.assertIsNone(agent._lanitnord_price_per_requested_unit(page, "шт.", "шт"))

    def test_rs24_rejects_metre_price_without_explicit_length(self) -> None:
        page = MagicMock()
        page.locator.return_value.count.return_value = 1
        page.locator.return_value.first.inner_text.return_value = "3\u00a0151.26"
        page.locator.return_value.first.get_attribute.return_value = "EA"
        self.assertIsNone(agent._rs24_price_per_requested_unit(
            page, "Кратность для заказа\n8\nшт", "м"
        ))

    def test_rs24_does_not_guess_price_from_an_empty_product_page(self) -> None:
        page = MagicMock()
        page.locator.return_value.count.return_value = 0
        self.assertIsNone(agent._rs24_price_per_requested_unit(page, "", "м"))

    def test_rs24_blank_navigation_waits_for_product_after_reload(self) -> None:
        page = MagicMock()
        page.inner_text.return_value = "Кабель ППГнг(А)-HF 1х95\n480.68 ₽"
        self.assertEqual(page.inner_text.return_value,
                         agent._reload_empty_product_page(
                             page, "https://rs24.ru/product/420870"))
        page.reload.assert_called_once_with(wait_until="commit", timeout=10000)
        page.locator.assert_called_once_with("h1")
        page.locator.return_value.first.wait_for.assert_called_once_with(
            state="visible", timeout=8000)
        page.wait_for_timeout.assert_not_called()

    def test_xcom_converts_only_main_tray_piece_price_to_metres(self) -> None:
        page = MagicMock()
        meta = page.locator.return_value
        meta.count.return_value = 1
        meta.first.get_attribute.return_value = "20019"
        main = ("Код товара: 839809\nАртикул: DKC 3534310\n"
                "Длина (L)\n3000 мм\nЕдиница измерения: шт.\n20 019 ₽\nДобавить в корзину\n")
        self.assertEqual(6673, agent._xcom_main_metre_price(
            page, main + "Рекомендуем приобрести\n3 452 ₽", "м"
        ))
        self.assertIsNone(agent._xcom_main_metre_price(
            page, main.replace('Единица измерения: шт.\n', ''), 'м'))
        self.assertIsNone(agent._xcom_main_metre_price(
            page, main.replace("Длина (L)\n3000 мм", "Длина не указана"), "м"
        ))
        self.assertIsNone(agent._xcom_main_metre_price(
            page, main.replace("Добавить в корзину", "Нет в наличии\nОписание"), "м"
        ))
        meta.count.return_value = 2
        self.assertIsNone(agent._xcom_main_metre_price(page, main, "м"))

    def test_lanitnord_on_request_main_card_ignores_related_price(self) -> None:
        text = ("Муфта IEK\nАртикул: UZM-XLK1-VN1-70120S\nПО ЗАПРОСУ\n"
                "Похожие товары\nЦена:\n1 113.06 руб.\nВ корзину")
        self.assertTrue(agent._lanitnord_main_price_on_request(text))
        self.assertFalse(agent._lanitnord_main_price_on_request(
            text.replace("ПО ЗАПРОСУ", "Цена: 995.82 руб.")
        ))

        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page._kac_response_status = 200
        page.title.return_value = "Муфта IEK UZM-XLK1-VN1-70120S"
        with patch.object(agent, "_page_text_after_browser_check", return_value=text), \
             patch.object(agent, "_extract_from_page") as extract:
            offers, _ = agent._collect_offers(
                browser,
                ["https://www.lanitnord.ru/product/mufta-pkvtp-1kh70-120/"],
                1, expected_type_mark="UZM-XLK1-VN1-70120S",
                expected_unit="шт", supplier_hosts={"lanitnord.ru"},
            )
        self.assertEqual([], offers)
        extract.assert_not_called()

    def test_satro_cable_roll_is_not_price_denominator(self) -> None:
        text = ("Провод ПуГВ 6 мм² кратно 200 метр\nЕдиница измерения\nм\n"
                "Длина бухты, м\n200\nРОЗНИЧНАЯ ЦЕНА\n158,51 ₽\n"
                "ДОБАВИТЬ В КОРЗИНУ\nВся кабельная продукция продаётся бухтами")
        self.assertEqual(158.51, agent._satro_cable_metre_price(text))
        url = "https://satro-paladin.com/catalog/product/114716/"
        self.assertTrue(agent._satro_minimum_order_conflict(
            url, "Провод ПуГВ 6 мм² кратно 200 метр", "м", 100))
        self.assertFalse(agent._satro_minimum_order_conflict(
            url, "Провод ПуГВ 6 мм² кратно 200 метр", "м", 200))

        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page._kac_response_status = 200
        page.title.return_value = "Провод ПуГВ 6 мм² кратно 200 метр"
        with patch.object(agent, "_page_text_after_browser_check", return_value=text), \
             patch.object(agent, "_extract_from_page") as extract:
            offers, _ = agent._collect_offers(
                browser, [url], 1, expected_name="Провод ПуГВ 6 мм²",
                expected_unit="м", expected_qty=100,
                supplier_hosts={"satro-paladin.com"},
            )
        self.assertEqual([], offers)
        extract.assert_not_called()


class SupplierSearchOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.position = SpecPosition(number=1, name="Кабель", type_mark="ВВГнг")

    def test_regional_subdomains_count_as_one_supplier(self) -> None:
        urls = [
            "https://cable.ru/item-a",
            "https://nsk.cable.ru/item-b",
            "https://e-kc.ru/item-c",
        ]

        deduplicated = agent._dedup_by_host(
            urls, supplier_hosts={"cable.ru", "e-kc.ru"}
        )

        self.assertEqual([urls[0], urls[2]], deduplicated)

    def test_backup_card_is_kept_after_first_hit_on_same_supplier(self) -> None:
        urls = ["https://www.minimaks.ru/",
                "https://www.minimaks.ru/product/30013k",
                "https://www.minimaks.ru/product/gsv-h50"]
        self.assertEqual(urls[1:], agent._candidate_urls_by_host(
            urls, supplier_hosts={"minimaks.ru"}, expected_mark="30013K"))

    def test_primary_cards_from_all_sellers_precede_backup_cards(self) -> None:
        urls = ["https://minimaks.ru/product/a1", "https://minimaks.ru/product/a2",
                "https://220.ru/product/b1", "https://220.ru/product/b2"]
        self.assertEqual([urls[0], urls[2], urls[1], urls[3]],
                         agent._candidate_urls_by_host(urls))

    def test_exact_article_card_precedes_unrelated_search_hits(self) -> None:
        urls = [
            "https://first.example/product/another-item",
            "https://second.example/catalog/something-40500",
            "https://e-kc.ru/cena/peregorodka-sep-ssc40500k-dkc",
        ]
        self.assertEqual(urls[2], agent._candidate_urls_by_host(
            urls, expected_mark="SSC40500K")[0])

    def test_manufacturer_domain_requires_exact_brand(self) -> None:
        hosts = ["thermopat.com", "termopat-shop.ru", "etm.ru"]
        self.assertEqual(["thermopat.com"], agent._brand_direct_hosts(
            "THERMOPAT FM 2K", hosts))
        self.assertEqual([], agent._brand_direct_hosts("FM 2K", hosts))

    def test_unbranded_card_skips_llm_for_verified_manufacturer_mark(self) -> None:
        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page._kac_response_status = 200
        page.title.return_value = "Кронштейн настенный ONKRON FM2"
        with patch.object(agent, "_page_text_after_browser_check",
                          return_value="Кронштейн для телевизора 1000 рублей"), \
             patch.object(agent, "_extract_from_page") as extract, \
             agent.capture_card_diagnostics() as diagnostics:
            offers, checked = agent._collect_offers(
                browser, ["https://xcom-shop.ru/onkron_fm2.html"], 1,
                expected_type_mark="THERMOPAT FM 2K",
                expected_name="Противопожарная пена", expected_unit="шт",
                supplier_hosts={"xcom-shop.ru"},
            )
        self.assertEqual(([], 1), (offers, checked))
        self.assertEqual("brand_unconfirmed", diagnostics[0]["code"])
        extract.assert_not_called()

    def test_expired_phase_budget_skips_next_card_and_records_reason(self) -> None:
        browser = MagicMock()
        with patch.object(agent.time, "monotonic", return_value=100), \
             agent.capture_card_diagnostics() as diagnostics:
            offers, checked = agent._collect_offers(
                browser, ["https://supplier.example/item"], 1,
                deadline=99, budget_phase="основной список", budget_seconds=90,
            )
        self.assertEqual(([], 0), (offers, checked))
        self.assertEqual("search_budget", diagnostics[0]["code"])
        self.assertIn("90 с", diagnostics[0]["reason"])
        browser.page.assert_not_called()

    def test_unified_manufacturer_recovery_precedes_unrepresented_recovery(self):
        position=SpecPosition(number=93,name='Противопожарная пена',type_mark='THERMOPAT FM 2K',unit='шт')
        url='https://thermopat.com/product-fm2k';offer=_offer(url,6000)
        with patch.object(agent,'build_queries',return_value=['THERMOPAT FM 2K']), patch.object(agent,'whitelist_hosts',return_value=['primary.example','thermopat.com','other.example']), patch.object(agent,'_search_phase',return_value=([],0,[])) as phase, patch.object(agent,'search_on_sites',return_value=[url]) as targeted, patch.object(agent,'_collect_offers',return_value=([offer],1)), patch.object(agent,'_search_unrepresented_hosts') as missing, patch.object(agent.settings,'min_sources',1):
            offers,checked=agent.find_offers_for_position(object(),position)
        self.assertEqual((offers,checked),([offer],1))
        self.assertEqual(phase.call_args.args[3],['primary.example','thermopat.com','other.example'])
        self.assertEqual(targeted.call_args.args[1],['thermopat.com'])
        self.assertEqual(targeted.call_args.kwargs['deadline'],phase.call_args.args[5])
        missing.assert_not_called()

    def test_position_search_records_one_unified_literal_phase_time(self):
        offers=[_offer('https://primary.example/item',100),_offer('https://former-reserve.example/item',110)]
        with patch.object(agent,'build_queries',return_value=['literal']), patch.object(agent,'whitelist_hosts',return_value=['primary.example','former-reserve.example']), patch.object(agent,'_search_phase',return_value=(offers,2,[])), patch.object(agent.settings,'min_sources',2), agent.capture_card_diagnostics() as diagnostics:
            found,checked=agent.find_offers_for_position(object(),self.position)
        self.assertEqual((found,checked),(offers,2))
        self.assertEqual([item['phase'] for item in diagnostics if item['code']=='phase_timing'],['primary'])

    def test_model_tokens_rank_transformer_before_similar_named_tester(self) -> None:
        tester = "https://chipdip.ru/product/ct-30-tester-cem-9000269146"
        transformer = "https://chipdip.ru/product/transformator-toka-ct30-300-5a"
        candidates = agent._candidate_urls_by_host(
            [tester, transformer], expected_mark="CT30 300/5A, CT30-300-0.5-5")
        self.assertEqual([transformer, tester], candidates)

    def test_etm_search_catalog_is_not_a_single_product_card(self) -> None:
        self.assertTrue(agent._is_listing_url(
            "https://www.etm.ru/catalog/16301060_listovye_lotki_s_pokrytiem_cink_lamel"))
        self.assertTrue(agent._is_listing_url(
            "https://www.etm.ru/catalog?searchValue=трансформаторы тока"))
        self.assertFalse(agent._is_listing_url(
            "https://www.etm.ru/cat/nn/527354"))

    def test_different_named_model_overrides_semantic_match(self) -> None:
        self.assertTrue(agent._conflicting_model_code_in_title(
            "CT30-300-0.5-5", "Трансформатор тока ТШП-0,66 300/5А ИЭК"))
        self.assertFalse(agent._conflicting_model_code_in_title(
            "SSC91000K", "DKC Перегородка SEP для угла CS90 H100"))
        self.assertFalse(agent._conflicting_model_code_in_title(
            "CM201001INOX", "Шпилька М10х1000 DIN975"))

    def test_partial_cache_searches_only_missing_supplier(self) -> None:
        existing = [_offer("https://nsk.known.example/product/1", 100)]
        new = _offer("https://fresh.example/product/2", 110)
        urls = [existing[0].url, new.url]
        with patch.object(agent, "build_queries", return_value=["товар марка"]), \
             patch.object(agent, "whitelist_hosts",
                          return_value=["known.example", "fresh.example"]), \
             patch.object(agent, "fallback_hosts", return_value=[]), \
             patch.object(agent, "search_on_sites", return_value=urls), \
             patch.object(agent, "_collect_offers", return_value=([new], 1)) as collect, \
             patch.object(agent.settings, "min_sources", 2):
            offers, checked = agent.find_offers_for_position(
                object(), self.position, existing)

        self.assertEqual([existing[0], new], offers)
        self.assertEqual(1, checked)
        self.assertEqual([new.url], collect.call_args.args[1])
        self.assertEqual(1, collect.call_args.args[2])

    def test_complete_cache_does_not_search(self) -> None:
        existing = [_offer("https://known.example/product/1", 100)]
        with patch.object(agent, "build_queries") as queries, \
             patch.object(agent.settings, "min_sources", 1):
            offers, checked = agent.find_offers_for_position(
                object(), self.position, existing)
        self.assertEqual((existing, 0), (offers, checked))
        queries.assert_not_called()

    def test_second_card_is_checked_only_when_first_has_no_price(self) -> None:
        from engine.models import Requisites

        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page._kac_response_status = 200
        page.title.return_value = "Пластина GSV H50"
        urls = ["https://www.minimaks.ru/product/30013k",
                "https://www.minimaks.ru/product/gsv-h50"]
        first = {"price_with_vat": None, "price_unit": "шт",
                 "product_title": "Пластина GSV H50", "matches_requested_product": True}
        second = {"price_with_vat": 100, "price_unit": "шт",
                  "product_title": "Пластина GSV H50", "matches_requested_product": True}
        with patch.object(agent, "_page_text_after_browser_check", return_value="Пластина GSV H50"), \
             patch.object(agent, "_extract_from_page", side_effect=[first, second]), \
             patch.object(agent, "wl_requisites_for",
                          return_value=Requisites(name="Поставщик", inn="7707083893")), \
             patch.object(agent, "_save_product_screenshot"):
            offers, checked = agent._collect_offers(
                browser, urls, 2, expected_name="Пластина GSV H50",
                expected_unit="шт", supplier_hosts={"minimaks.ru"})

        self.assertEqual(2, checked)
        self.assertEqual(1, len(offers))
        self.assertEqual(urls[1], offers[0].url)

    def test_minimum_price_disclosure_skips_other_cards_on_same_site(self) -> None:
        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page._kac_response_status = 200
        page.title.return_value = "Кабель"
        urls = ["https://cable.ru/product/a", "https://cable.ru/product/b"]
        disclosure = "Цены указаны как минимальные. Окончательная цена по запросу."
        with patch.object(agent, "_page_text_after_browser_check", return_value=disclosure), \
             patch.object(agent, "_extract_from_page") as extract:
            offers, checked = agent._collect_offers(
                browser, urls, 2, expected_name="Кабель", expected_unit="м",
                supplier_hosts={"cable.ru"})

        self.assertEqual([], offers)
        self.assertEqual(1, checked)
        extract.assert_not_called()

    def test_unrepresented_recovery_uses_same_unified_list_and_deadline(self):
        first=_offer('https://primary.example/item',100);second=_offer('https://former-reserve.example/item',110)
        hosts=['primary.example','former-reserve.example']
        with patch.object(agent,'build_queries',return_value=['literal']), patch.object(agent,'whitelist_hosts',return_value=hosts), patch.object(agent,'_search_phase',return_value=([first],1,[first.url])) as phase, patch.object(agent,'_search_unrepresented_hosts',return_value=[second.url]) as recovery, patch.object(agent,'_collect_offers',return_value=([second],1)) as collect, patch.object(agent.settings,'min_sources',2):
            found,checked=agent.find_offers_for_position(object(),self.position)
        self.assertEqual((found,checked),([first,second],2))
        self.assertEqual(recovery.call_args.args[1],hosts)
        self.assertEqual(recovery.call_args.kwargs['deadline'],phase.call_args.args[5])
        self.assertEqual(collect.call_args.kwargs['deadline'],phase.call_args.args[5])
        self.assertEqual(collect.call_args.kwargs['supplier_hosts'],set(hosts))

    def test_targeted_search_recovers_host_missing_from_group_results(self) -> None:
        hosts = ["etm.ru", "elektromehanika.ru", "russvet.ru"]
        grouped = ["https://www.etm.ru/cat/nn/4570569"]
        direct = "https://www.elektromehanika.ru/items.php?id=29799"

        with patch.object(agent, "search_on_sites", return_value=[direct]) as search:
            found = agent._search_unrepresented_hosts(
                '"CM201001INOX" цена', hosts, grouped
            )

        self.assertEqual([direct], found)
        search.assert_called_once_with(
            '"CM201001INOX" цена', ["elektromehanika.ru"], n=10
        )

    def test_dedup_prefers_exact_product_over_earlier_listing(self) -> None:
        urls = [
            "https://www.chipdip.ru/catalog-show/shpilki-rezbovye-2773",
            "https://www.chipdip.ru/product/shpilka-m10-cm201001inox-123",
            "https://www.chipdip.ru/product/shpilka-m10-cm201001-456",
        ]

        self.assertEqual(
            [urls[1]], agent._dedup_by_host(
                urls, supplier_hosts={"chipdip.ru"},
                expected_mark="CM201001INOX",
            ),
        )

    def test_unified_recovery_skipped_after_sufficient_literal_sources(self):
        offers=[_offer('https://supplier%d.example/item'%i,100+i) for i in range(5)]
        with patch.object(agent,'build_queries',return_value=['literal']), patch.object(agent,'whitelist_hosts',return_value=['primary.example','former-reserve.example']), patch.object(agent,'_search_phase',return_value=(offers,5,[])) as phase, patch.object(agent,'_search_unrepresented_hosts') as recovery, patch.object(agent,'_search_queries_on_sites') as maker, patch.object(agent.settings,'min_sources',5):
            found,checked=agent.find_offers_for_position(object(),self.position)
        self.assertEqual((found,checked),(offers,5));phase.assert_called_once();recovery.assert_not_called();maker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
