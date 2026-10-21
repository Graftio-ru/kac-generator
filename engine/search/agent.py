"""Поисковый агент: по позиции спецификации находит ≥MIN_SOURCES предложений,
сохраняет сведения о самовывозе, извлекает цену и реквизиты, отбирает TOP_PRICES максимальных.

Извлечение цены/региона/ИНН со страницы товара делает Claude (по тексту страницы),
скриншот карточки — Playwright. Реквизиты добиваются через DaData по ИНН.
"""
from __future__ import annotations

from .failure_policy import ResearchControlError
from ..offer_selection import select_top_offers, source_count, offer_is_comparable
from ..region_policy import extracted_region, region_status
from .whitelist import fallback_hosts  # Deprecated inert compatibility symbol; never called.

import base64
import json
import math
import re
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import date
from html import unescape
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import httpx

from ..config import CACHE_DIR, settings
from ..llm import complete_json, LLMAccountError, LLMUnavailable, LLMOperationFailed, ModelOutputInvalid, MODEL_OPERATION_TIMEOUT_SECONDS
from .failure_policy import SearchServiceUnavailable, SearchQueryFailed, FailureContext
from ..models import PositionResult, PriceOffer, Requisites, SpecPosition
from ..telemetry import timed_call
from ..requisites.dadata import fetch_requisites
from ..requisites.supplier import fetch_requisites_from_site, valid_inn
from .search_engine import search_on_sites
from .candidate_queue import current_queue
from .session import (ResearchProgressError, SearchBudgetExceeded, accepted,
                      rejected, supplier_paused, research_deadline, bounded_timeout, deadline_scope,
                      supplier_transport, transport_observed, http_outcome, SupplierDeferred)
from .supplier_health_store import Outcome
from ..browser import SupplierProfileBusy, supplier_http_get
from .whitelist import requisites_for as wl_requisites_for
from .whitelist import source_type, whitelist_hosts

QUERY_SYSTEM = (
    "Сформируй до 3 коротких поисковых запросов для покупки товара в РФ. Используй распространённые "
    "синонимы, альтернативные торговые наименования и назначение товара; марку записывай в тех "
    "вариантах, которые реально встречаются у поставщиков. Не подменяй модель или типоразмер. "
    "Верни JSON-объект вида {\"queries\": [\"запрос 1\", \"запрос 2\"]} без пояснений."
)
EXTRACT_SYSTEM = (
    "Со страницы интернет-магазина извлеки JSON: "
    "price_with_vat (число — цена за единицу в рублях С НДС, без разделителей), "
    "listed_price_with_vat (число — цена ровно как указана на странице), "
    "package_quantity (число — сколько запрошенных единиц входит в упаковку/бухту, минимум 1), "
    "listed_price_covers_multiple_requested_units (bool — указанная цена относится ко всей "
    "упаковке/бухте из нескольких запрошенных единиц), "
    "price_unit (строка — единица цены на странице: м, шт, упаковка, бухта и т.п.), "
    "product_title (строка), "
    "moscow_pickup (bool/null — явно подтверждён/отрицается самовывоз в Москве/МО; отсутствие сведений = null), "
    "region_status (unknown/confirmed/incompatible — только наблюдения карточки, не решение о допуске сайта), "
    "region_evidence (точный текст карточки о складе/самовывозе/доставке, иначе пусто). "
    "Не выводи регион из юридического адреса или списка поставщиков. Отсутствие склада или "
    "данных о доставке = unknown; incompatible только при явных несовместимых условиях поставки. "
    "city (строка — город/регион склада или продавца, если указан, иначе пусто), "
    "seller_inn (строка — ИНН продавца, если указан на странице, иначе пусто), "
    "matches_requested_product (bool — соответствует ли карточка запрошенному товару), "
    "match_reason (короткая строка с причиной решения). "
    "Учитывай синонимы, альтернативные наименования и разные способы записи марки. "
    "Явно другая модель, типоразмер или назначение означает false. "
    "price_with_vat всегда пересчитывай в единицу, указанную в запросе. Например, если бухта "
    "20 м стоит 100000 рублей, а запрошенная единица — м, price_with_vat должно быть 5000, "
    "listed_price_with_vat — 100000, package_quantity — 20. "
    "Кратность или минимальное количество для заказа НЕ означает, что показанная цена относится "
    "ко всей этой партии. Если лоток длиной 3000 мм продаётся поштучно, а запрошены метры, "
    "дели цену одной штуки на 3, а не на кратность заказа. "
    "Цена 'от', минимальная/ориентировочная цена, диапазон и 'по запросу' не являются "
    "подтверждённой ценой покупки: в этих случаях price_with_vat: null. "
    "Если цена не найдена — price_with_vat: null. Только JSON."
)



def _budget_ms(milliseconds):
    return max(1, int(bounded_timeout(milliseconds / 1000) * 1000))

def _log(msg: str) -> None:
    """Диагностика поиска в окне движка (start.command)."""
    print(f"[поиск] {msg}", file=sys.stderr, flush=True)


_CARD_DIAGNOSTICS: ContextVar[list[dict] | None] = ContextVar(
    "card_diagnostics", default=None
)


@contextmanager
def capture_card_diagnostics():
    """Collect per-card rejection reasons for one position, even in parallel jobs."""
    diagnostics: list[dict] = []
    token = _CARD_DIAGNOSTICS.set(diagnostics)
    try:
        yield diagnostics
    finally:
        _CARD_DIAGNOSTICS.reset(token)


def _record_query_failure(failure, hosts=()):
    diagnostics=_CARD_DIAGNOSTICS.get()
    if diagnostics is not None:
        diagnostics.extend({**failure.diagnostic(),'url':f'https://{host}/' if host else ''}
                           for host in (hosts or ['']))


def _reject_card(url: str, code: str, reason: str, *, skipped: bool = False,
                 observed_article: str = "", product_title: str = "", failure_context=None,
                 capture_stage: str = "", identity_phase_diagnostics=None,
                 capture_error_type: str = "", capture_error_cause: str = "",
                 font_readiness_diagnostics=None, native_visibility_diagnostics=None) -> None:
    queue = current_queue()
    if queue is not None and url:
        queue.reject(url, code, reason)
    marker = "↷" if skipped else "✗"
    rejected(url, code)
    _log(f"  {marker} {url[:60]} → {reason}")
    diagnostics = _CARD_DIAGNOSTICS.get()
    if diagnostics is not None:
        item = {"url": url, "code": code, "reason": reason}
        if capture_stage:
            item['capture_stage'] = capture_stage
        if isinstance(capture_error_type,str) and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,79}',capture_error_type):
            item['capture_error_type']=capture_error_type
        if capture_error_cause in ('font_readiness_timeout','clip_outside_page','screenshot_timeout','unknown'):
            item['capture_error_cause']=capture_error_cause
        if isinstance(font_readiness_diagnostics,dict):
            from ..price_capture import _safe_font_diagnostics
            item['font_readiness_diagnostics']=_safe_font_diagnostics(font_readiness_diagnostics)
        if native_visibility_diagnostics is not None:
            try:
                from ..native_geometry import safe_native_visibility_diagnostics
                item['native_visibility_diagnostics'] = safe_native_visibility_diagnostics(
                    native_visibility_diagnostics)
            except ResearchControlError:
                raise
            except Exception:
                pass  # Optional observation must not replace the actual refusal.
        if isinstance(identity_phase_diagnostics, dict):
            item['identity_phase_diagnostics'] = identity_phase_diagnostics
        if failure_context is None and code in {'captcha','empty_page'}:
            failure_context = FailureContext('supplier_navigation','local',
                'bot_protection' if code == 'captcha' else 'empty_document',time.time(),
                host=(urlparse(url).hostname or '').lower().removeprefix('www.'))
        if failure_context is not None:
            item.update(failure_context.diagnostic(reason, code=code))
            item['url'] = url
        if observed_article:
            item["observed_article"] = observed_article
        if product_title:
            item["product_title"] = product_title
        diagnostics.append(item)


def _record_search_error(query: str, reason: str) -> None:
    diagnostics = _CARD_DIAGNOSTICS.get()
    if diagnostics is not None:
        diagnostics.append({"url": "", "query": query, "code": "search_error",
                            "reason": reason})


def _record_search_budget(phase: str, seconds: int) -> None:
    reason = f"этап «{phase}» остановлен после лимита {seconds} с"
    diagnostics = _CARD_DIAGNOSTICS.get()
    if diagnostics is not None and any(
        item.get("code") == "search_budget" and item.get("phase") == phase
        for item in diagnostics
    ):
        return
    _log(f"  ↷ {reason}")
    if diagnostics is not None:
        diagnostics.append({"url": "", "code": "search_budget", "phase": phase,
                            "reason": reason})


def _record_phase_timing(phase: str, seconds: float) -> None:
    diagnostics = _CARD_DIAGNOSTICS.get()
    if diagnostics is not None:
        diagnostics.append({"url": "", "code": "phase_timing",
                            "phase": phase, "seconds": round(seconds, 2)})


@timed_call("screenshot")
def _save_product_screenshot(page, path: Path) -> None:
    """Capture a card even when Playwright stalls waiting for web fonts."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        page.screenshot(
            path=str(path), clip={"x": 0, "y": 0, "width": 1366, "height": 900},
            timeout=max(1, int(bounded_timeout(8) * 1000)),
        )
    except PlaywrightTimeoutError as exc:
        if "fonts" not in str(exc).lower():
            raise
        session = page.context.new_cdp_session(page)
        try:
            capture = session.send("Page.captureScreenshot", {
                "format": "png", "captureBeyondViewport": False,
            })
            path.write_bytes(base64.b64decode(capture["data"]))
        finally:
            session.detach()


def build_queries(pos: SpecPosition, *, semantic: bool = True) -> list[str]:
    from .article_query_plan import build_article_query_plan
    evidence = pos.source_evidence or {}
    cells = evidence.get('cells', {})
    clean = lambda value: ' '.join(str(value or '').split())
    native = (evidence.get('reader') == 'native_pdf' and isinstance(cells, dict)
              and clean(cells.get('name')) == clean(pos.name)
              and clean(cells.get('unit')) == clean(pos.unit)
              and clean(' / '.join(dict.fromkeys(clean(cells.get(k)) for k in ('model','article')
                                                if clean(cells.get(k))))) == clean(pos.type_mark))
    fields = pos.review_fields or {}
    native = native and not any(k in fields for k in ('name','type_mark','model','article','maker'))
    plan = build_article_query_plan(pos.name, clean(cells.get('model')) if native else pos.type_mark,
        article=clean(cells.get('article')) if native else '',
        manufacturer=clean(cells.get('maker')) if native else '', unit=pos.unit)
    exact_queries = list(plan.queries)
    if not semantic:
        return exact_queries

    semantic_queries: list[str] = []
    if semantic:
        try:
            qs = complete_json(
                f"Наименование из спецификации: {pos.name}\nМарка/тип: {pos.type_mark}",
                system=QUERY_SYSTEM,
                timeout_seconds=15,
            )
            if isinstance(qs, dict):
                qs = qs.get("queries")
            if not isinstance(qs,list) or any(not isinstance(q,str)for q in qs):
                raise ModelOutputInvalid('invalid_schema')
            if qs:
                semantic_queries = [str(q) for q in qs[:3]]
        except ResearchControlError:
            raise
        except Exception:
            pass

    queries: list[str] = []
    seen: set[str] = set()
    # Semantic expansion must retain its bounded slot after literal queries;
    # deterministic script aliases cannot consume the entire paid LLM result.
    for query in exact_queries[:1] + semantic_queries + exact_queries[1:]:
        clean = " ".join(query.split()).strip()
        key = clean.casefold()
        if clean and key not in seen:
            seen.add(key)
            queries.append(clean)
    return queries[:3]


# Антибот/капча-интерстишелы (Yandex SmartCaptcha, Cloudflare, «я не робот»). На таких
# страницах нет товара, а LLM может выдумать цену/ИНН — оффер отбрасываем.
# STRONG — фразы, которых на реальной карточке товара практически не бывает: срабатывают
# при любой длине (капча Яндекса бывает многословной — «Разверните картинку» и пр.).
_BOT_MARKERS_STRONG = (
    "подтвердите, что вы", "подтвердите, что запросы", "запросы отправляли вы",
    "вы не робот", "я не робот", "это не я", "разверните картинку",
    "именно реальный человек", "получить доступ к сайту", "доступ ограничен",
    "проверка безопасности", "checking your browser", "verify you are human",
    "необычный трафик", "unusual traffic", "showcaptcha", "smartcaptcha",
    "проверка пользователя", "killbot",
)
# WEAK — общие слова, которые изредка встречаются и в нормальном тексте: только на коротких
# страницах (на реальной карточке товара текста много).
_BOT_MARKERS_WEAK = ("captcha", "капча", "robot")


def _looks_like_bot_check(text: str, title: str = "") -> bool:
    """Похоже ли на страницу-заглушку антибота/капчи (а не карточку товара)."""
    low = f"{title}\n{text}".lower()
    if any(m in low for m in _BOT_MARKERS_STRONG):
        return True
    return len(text) < 1500 and any(m in low for m in _BOT_MARKERS_WEAK)


def _protection_reason(text: str, title: str = "", status: int | None = None) -> str:
    """Name only an observed protection marker on an already rejected page."""
    low = f"{title}\n{text}".casefold()
    providers = (
        (("killbot",), "KillBot"),
        (("turnstile",), "Cloudflare Turnstile"),
        (("cloudflare",), "Cloudflare"),
        (("smartcaptcha", "smart captcha"), "Yandex SmartCaptcha"),
        (("recaptcha",), "Google reCAPTCHA"),
        (("hcaptcha",), "hCaptcha"),
        (("ddos-guard", "ddos guard"), "DDoS-Guard"),
        (("qrator",), "Qrator"),
    )
    observed = next((name for markers, name in providers if any(m in low for m in markers)), None)
    suffix = f" (HTTP {status})" if status else ""
    if observed:
        return f"Карточка недоступна: на странице обнаружен маркер {observed}{suffix}; цена не получена"
    if _looks_like_bot_check(text, title):
        return f"Доступ заблокирован: проверка пользователя/антибот, тип защиты не определён{suffix}; цена не получена"
    return f"Карточка недоступна{suffix}; причина не установлена, защита от ботов не подтверждена"


def _http_response_is_blocked(status: int | None, text: str) -> bool:
    """Отбрасывает реальную HTTP-ошибку, но сохраняет богатую карточку с неверным 5xx."""
    if not status or status < 400:
        return False
    return not (500 <= status < 600 and len(text) >= 2000
                and not _looks_like_bot_check(text))


def _page_text_after_browser_check(page, status: int | None) -> str:
    """Даёт JS-проверке время заменить короткий 5xx-интерстишел карточкой товара."""
    text = page.inner_text("body")
    if ((status and status >= 400) or not text.strip() or _looks_like_bot_check(text)):
        page.wait_for_timeout(_budget_ms(4500))
        try:
            text = page.inner_text("body")
        except ResearchControlError:
            raise
        except Exception:
            pass
    return text


def _identity_card_text(page, text: str) -> str:
    """Wait for a main-card SKU placeholder to render, without metadata fallback."""
    from .product_identity import main_card_article
    if main_card_article(text):
        return text
    try:
        # A product widget can render its heading before its printed article.
        # Scope the placeholder to the form containing the real primary h1;
        # recommendation widgets and hidden JSON are not identity evidence.
        if page.locator('form:has(h1) [data-product-card-sku]').count() != 1:
            return text
        try:
            page.wait_for_function(
                "() => Boolean(document.querySelector('h1')?.closest('form')"
                "?.querySelector('[data-product-card-sku]')?.innerText.trim())",
                timeout=_budget_ms(20000))
        except ResearchControlError:
            raise
        except Exception:
            pass  # Recheck actual rendered text even when the placeholder wait expires.
        rendered = page.inner_text('body')
        return rendered if isinstance(rendered, str) else text
    except ResearchControlError:
        raise
    except Exception:
        return text


def _reload_empty_product_page(page, url: str) -> str:
    """Wait for a product body after a blank first RS24 navigation."""
    try:
        page.reload(wait_until="commit", timeout=_budget_ms(10000))
        if _canonical_host(url) == "rs24.ru":
            try:
                page.locator("h1").first.wait_for(state="visible", timeout=_budget_ms(8000))
            except ResearchControlError:
                raise
            except Exception:
                pass
        else:
            page.wait_for_timeout(_budget_ms(1500))
        return page.inner_text("body")
    except ResearchControlError:
        raise
    except Exception:
        return ""


def _validate_extraction_fields(value):
    """Same strict field rules, with privacy-safe known-field type categories."""
    from .card_batch import CardBatchSchemaInvalid,json_type
    if not isinstance(value,dict):raise CardBatchSchemaInvalid('extraction',received=json_type(value),expected='object')
    boolean=('matches_requested_product','listed_price_covers_multiple_requested_units')
    numeric=('price_with_vat','listed_price_with_vat','package_quantity')
    text=('price_unit','product_title','region_evidence','city','seller_inn','match_reason')
    for field in boolean:
        if field in value and type(value[field]) is not bool:raise CardBatchSchemaInvalid('field_type',field=field,received=json_type(value[field]),expected='boolean')
    if 'moscow_pickup' in value and value['moscow_pickup'] is not None and type(value['moscow_pickup']) is not bool:raise CardBatchSchemaInvalid('field_type',field='moscow_pickup',received=json_type(value['moscow_pickup']),expected='boolean')
    for field in numeric:
        if field in value and value[field] is not None and (type(value[field]) not in (int,float) or not math.isfinite(value[field])):raise CardBatchSchemaInvalid('field_type',field=field,received=json_type(value[field]),expected='finite_number')
    for field in text:
        if field in value and not isinstance(value[field],str):raise CardBatchSchemaInvalid('field_type',field=field,received=json_type(value[field]),expected='string')
    if 'region_status' in value and (not isinstance(value['region_status'],str) or value['region_status'] not in {'unknown','confirmed','incompatible'}):raise CardBatchSchemaInvalid('field_type',field='region_status',received=json_type(value['region_status']),expected='valid_enum')
    return value


def _extract_from_page(page_text: str, expected_name: str = "",
                       expected_type_mark: str = "", expected_unit: str = "") -> dict | None:
    try:
        product_request = (
            f"Запрошенный товар: {expected_name}\n"
            if expected_name else
            "Контрольная проверка магазина: выбери один конкретный товар, для которого на "
            "странице видны название и числовая цена. Если такого товара нет, верни "
            "price_with_vat: null.\n"
        )
        request = (
            product_request +
            f"Марка/тип: {expected_type_mark or 'не указана'}\n\n"
            f"Единица спецификации: {expected_unit or 'не указана'}\n\n"
            f"Текст страницы:\n\n{page_text[:12000]}"
        )
        value=complete_json(request, system=EXTRACT_SYSTEM,
                             timeout_seconds=MODEL_OPERATION_TIMEOUT_SECONDS)
        return _validate_extraction_fields(value)
    except ResearchControlError:
        raise
    except Exception as e:
        if isinstance(e, (SearchBudgetExceeded, LLMUnavailable, SearchServiceUnavailable)):
            raise
        _log(f"    извлечение LLM упало: {type(e).__name__}: {e}")
        return None


def _normalise_model_text(value: str) -> str:
    """Нормализация обозначений: 1х50, 1×50 и 1x50 считаются одинаковыми."""
    return (value.lower().replace("ё", "е").replace("х", "x").replace("×", "x")
            .replace(",", "."))


def _distinctive_model_tokens(value: str) -> set[str]:
    """Числовые части марки, несовпадение которых означает другой товар/типоразмер."""
    normalised = _normalise_model_text(value)
    dimensions = set(re.findall(r"\d+(?:\.\d+)?x\d+(?:\.\d+)?", normalised))
    tokens = (token.strip(".") for token in re.findall(r"[a-zа-я0-9.]+", normalised))
    confusables = str.maketrans({"а": "a", "в": "v", "к": "k"})
    model_tokens = set()
    for token in tokens:
        if not any(char.isdigit() for char in token) or len(token) < 2:
            continue
        token = token.translate(confusables)
        # The drawing may say 2.5 while the seller writes 2.5ВА.
        if re.fullmatch(r"\d+\.\d+(?:va|kv)", token):
            token = re.sub(r"(?:va|kv)$", "", token)
        model_tokens.add(token)
    return dimensions | model_tokens


def _mark_variants(mark: str) -> list[str]:
    """Split comma-separated model aliases without splitting decimal commas."""
    return re.split(r",\s+(?=[A-Za-zА-Яа-я]+\d)", mark)


def _single_type_mark_match_status(expected: str, product_identity: str) -> bool | None:
    if not expected.strip():
        return True

    expected_tokens = _distinctive_model_tokens(expected)
    identity_tokens = _distinctive_model_tokens(product_identity)

    # The x in an article suffix such as INOX is not a dimension separator.
    is_dimension = lambda token: bool(re.match(r"^\d+(?:\.\d+)?x\d", token))
    expected_dimensions = {token for token in expected_tokens if is_dimension(token)}
    identity_dimensions = {token for token in identity_tokens if is_dimension(token)}
    if (expected_dimensions and identity_dimensions
            and not expected_dimensions.issubset(identity_dimensions)):
        return False

    non_dimension_tokens = expected_tokens - expected_dimensions
    token_match = expected_tokens.issubset(identity_tokens)
    if not token_match:
        # Numeric article 3526210HDZ is a different variant from 3526210,
        # even if an LLM calls the two products semantically equivalent.
        for expected_token in non_dimension_tokens:
            if (expected_token.isdigit() and expected_token not in identity_tokens
                    and any(token.startswith(expected_token) for token in identity_tokens)):
                return False
        # CE308 против CE208 — явный конфликт. Отсутствующий в заголовке артикул
        # (например, 3526210) остаётся неясным случаем для семантической проверки.
        for expected_token in non_dimension_tokens:
            prefix_match = re.match(r"[a-zа-я]+", expected_token)
            prefix = prefix_match.group(0) if prefix_match else ""
            if len(prefix) < 2:
                continue
            comparable = {
                token for token in identity_tokens
                if token != expected_token and token.startswith(prefix)
            }
            if comparable:
                return False

    expected_words = {
        token for token in re.findall(r"[a-zа-я]+", _normalise_model_text(expected))
        if len(token) >= 3
    }
    identity_words = set(re.findall(
        r"[a-zа-я]+", _normalise_model_text(product_identity)
    ))
    words_match = expected_words.issubset(identity_words)
    if token_match and words_match:
        return True
    if not expected_tokens and words_match:
        return True
    return None


def _type_mark_match_status(expected: str, product_identity: str) -> bool | None:
    """Compare alternative model codes; prefer the most specific exact code."""
    variants = _mark_variants(expected)
    if len(variants) == 1:
        return _single_type_mark_match_status(expected, product_identity)
    statuses = [(_single_type_mark_match_status(variant, product_identity),
                 (len(_distinctive_model_tokens(variant)),
                  sum(map(len, _distinctive_model_tokens(variant)))))
                for variant in variants]
    specificity = max(score for _, score in statuses)
    if any(status is True and score == specificity for status, score in statuses):
        return True
    if all(status is False for status, _ in statuses):
        return False
    # A generic alias alone does not prove the full article and variant.
    return None


def _type_mark_matches(expected: str, product_identity: str) -> bool:
    """Совместимый бинарный интерфейс: неясный случай не считается точным совпадением."""
    return _type_mark_match_status(expected, product_identity) is True


def _needs_model_review(expected: str, product_title: str, url: str,
                        page_title: str = "", page_article: str = "") -> bool:
    """A semantic LLM match without an exact model code needs user verification."""
    if _conflicting_page_article(expected, page_article):
        return True
    return _type_mark_match_status(
        expected, _model_identity(product_title, url) + " " + page_title + " " + page_article
    ) is not True


def _printed_page_article(page_text: str, *, bracket_heading: bool = False) -> str:
    """Read the main card's printed article before related-product sections."""
    match = re.search(r"(?m)^\s*Артикул:\s*([A-Za-zА-Яа-я0-9._-]+)", page_text)
    if match:
        return match.group(1)
    if not bracket_heading:
        return ""
    # Lanmark prefixes the primary product heading with [manufacturer code].
    match = re.search(r"(?m)^\s*\[([A-Za-zА-Яа-я0-9][A-Za-zА-Яа-я0-9._-]{2,})\]\s",
                      page_text)
    return match.group(1) if match else ""


def _rs24_manufacturer_article(page_text: str) -> str:
    """Read RS24's manufacturer SKU from the main product details."""
    product_details = page_text.split("Характеристики", 1)[0]
    match = re.search(
        r"(?m)^\s*Артикул производителя\s*\n\s*([A-Za-zА-Яа-я0-9._-]{3,})\s*$",
        product_details,
    )
    return match.group(1) if match else ""


def _conflicting_page_article(expected: str, article: str, *, request_role=None) -> bool:
    """A separately printed seller article must equal an unambiguous spec SKU."""
    from .product_identity import _active_model_role
    if request_role is None and _active_model_role(expected):return False
    wanted = expected.strip()
    if not article or not re.fullmatch(r"[A-Za-zА-Яа-я0-9._-]{3,}", wanted):
        return False
    fold = str.maketrans({"а": "a", "в": "b", "с": "c", "е": "e", "к": "k",
                          "м": "m", "н": "h", "о": "o", "р": "p", "т": "t",
                          "х": "x", "у": "y"})
    compact = lambda value: re.sub(r"[^a-zа-я0-9]", "", _normalise_model_text(value)).translate(fold)
    return compact(wanted) != compact(article)


def _model_identity(product_title: str, url: str) -> str:
    """Use the URL path as evidence without treating .html as an article suffix."""
    path = re.sub(r"\.html?$", "", urlparse(url).path, flags=re.IGNORECASE)
    return f"{product_title} {path}"


def _conflicting_numeric_article(expected: str, product_title: str) -> bool:
    """A different printed numeric manufacturer article is not a semantic alias."""
    article = expected.strip().casefold().replace("к", "k")
    if not re.fullmatch(r"\d{5,8}[a-z]?", article):
        return False
    if not article.isdigit():
        return any(token.startswith(article) and token != article
                   for token in _distinctive_model_tokens(product_title))
    if re.search(rf"(?<![a-z0-9]){re.escape(article)}[a-z]{{2,}}(?![a-z0-9])",
                 _normalise_model_text(product_title)):
        return True
    printed = set(re.findall(rf"(?<!\d)\d{{{len(article)}}}(?!\d)", product_title))
    return bool(printed and article not in printed)


def _conflicting_full_model_code(expected: str, product_title: str) -> bool:
    """Reject a different full SKU from the same model family printed on the card."""
    pattern = r"(?<![a-zа-я0-9])([a-zа-я]+\d+(?:[-_][a-zа-я0-9]+(?:[.,][a-zа-я0-9]+)?){2,})(?![a-zа-я0-9])"
    confusables = str.maketrans({"а": "a", "в": "v", "к": "k"})

    def codes(value: str) -> list[tuple[str, str]]:
        found = []
        for code in re.findall(pattern, _normalise_model_text(value)):
            parts = re.split(r"[-_]", code)
            first = parts[0].translate(confusables)
            compact = re.sub(r"[^a-zа-я0-9]", "", code).translate(confusables)
            found.append((first, compact))
        return found

    wanted = codes(expected)
    printed = codes(product_title)
    for first, compact in printed:
        same_family = [item for item in wanted if item[0] == first]
        if same_family and all(item[1] != compact for item in same_family):
            if not any(item[1] == compact for item in wanted):
                return True
    return False


def _different_full_skus_in_url(expected: str, url: str) -> list[str]:
    """Return conflicting same-family SKUs from the URL, never a price match."""
    normalized = _normalise_model_text(expected)
    wanted = re.fullmatch(r"([a-z]+\d+(?:-[a-z0-9]+){3,})", normalized)
    if wanted is None:
        return []
    expected_code = wanted.group(1)
    family = expected_code.split("-", 1)[0]
    path = _normalise_model_text(urlparse(url).path).replace("_", "-")
    segment_count = expected_code.count("-")
    pattern = rf"(?<![a-z0-9]){re.escape(family)}(?:-[a-z0-9]+){{{segment_count}}}(?![a-z0-9])"
    return list(dict.fromkeys(code.upper() for code in re.findall(pattern, path)
                              if code != expected_code))


def _conflicting_full_sku_in_url(expected: str, url: str) -> bool:
    """Catch a different terminal SKU variant without interpreting prose slugs as SKUs."""
    return bool(_different_full_skus_in_url(expected, url))


def _conflicting_model_code_in_title(expected: str, product_title: str) -> bool:
    """A card naming a different model cannot be rescued by a semantic LLM match."""
    expected_wire = _normalise_model_text(expected)
    printed_wire = _normalise_model_text(product_title)
    # ПУГВнг(А)-LS is a different insulation/fire-performance variant from
    # the specified ordinary ПУГВ (ПВ-3), despite the shared ПУГВ prefix.
    if (re.search(r"\bпугв\b", expected_wire)
            and re.search(r"\bпугвнг", printed_wire)):
        return True
    pattern = r"(?<![a-zа-я0-9])([a-zа-я]{2,})[-_]?([0-9]+)"
    confusables = str.maketrans({
        "а": "a", "в": "b", "с": "c", "е": "e", "н": "h", "к": "k",
        "м": "m", "о": "o", "р": "p", "т": "t", "х": "x", "у": "y",
    })

    def codes(value: str) -> set[str]:
        normalised = _normalise_model_text(value)
        found: set[str] = set()
        for match in re.finditer(pattern, normalised):
            prefix, number = match.groups()
            if prefix in {"din", "iso", "гост", "мм", "mm"}:
                continue
            if "для угла" in normalised[max(0, match.start() - 20):match.start()]:
                continue
            found.add(f"{prefix.translate(confusables)}{number}")
        return found

    expected_codes = codes(expected)
    title_codes = codes(product_title)
    return bool(expected_codes and title_codes and expected_codes.isdisjoint(title_codes))


def _conflicting_network_cable_spec(expected_name: str, expected_mark: str,
                                    product_title: str) -> bool:
    """An explicitly different shielding or category is not an FTP Cat6 alias."""
    mark = _normalise_model_text(expected_mark)
    name = _normalise_model_text(expected_name)
    title = _normalise_model_text(product_title)
    if re.search(r"(?<![a-z])ftp(?![a-z])", mark) and re.search(
            r"(?<![a-z])s\s*/\s*ftp(?![a-z])", title):
        return True
    expected_category = re.search(r"категори[яи]\s*(\d+)(a?)\b", name)
    if expected_category:
        printed_categories = re.findall(
            r"(?:cat|категори[яи])\s*(\d+)(a?)\b", title)
        if any(category != expected_category.groups()
               for category in printed_categories):
            return True
    return False


def _different_explicit_article(expected: str, product_title: str,
                                url: str) -> str:
    """Return the competing code printed in both a product title and its URL."""
    from .product_identity import _active_model_interface, _active_request_model_code
    if not expected.strip():
        return ""
    article_pattern = r"(?<![a-zа-я0-9])([a-zа-я]{2,}[-_]?\d{2,}[a-zа-я0-9]*(?:[-_][a-zа-я0-9]+)*)"
    expected_articles = {
        re.sub(r"[^a-zа-я0-9]", "", article)
        for article in re.findall(article_pattern, _normalise_model_text(expected))
    }
    expected_compact = re.sub(r"[^a-zа-я0-9]", "", _normalise_model_text(expected))
    title_compact = re.sub(r"[^a-zа-я0-9]", "", _normalise_model_text(product_title))
    url_compact = re.sub(r"[^a-zа-я0-9]", "", _normalise_model_text(url))
    latin_article = str.maketrans({
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l",
        "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s",
        "т": "t", "у": "u", "ф": "f", "ц": "ts", "ч": "ch", "ш": "sh",
        "щ": "shch", "ы": "y", "э": "e", "ю": "yu", "я": "ya",
    })
    # A full manufacturer article can contain punctuation (for example 0.5S).
    # Its exact normalized occurrence in both surfaces outweighs shorter codes
    # such as CT30 that a token regex may find along the way.
    full_articles = [
        re.sub(r"[^a-zа-я0-9]", "", _normalise_model_text(part))
        for part in expected.split(",")
    ]
    if any(len(article) >= 8 and article in title_compact and article in url_compact
           for article in full_articles):
        return ""
    # Short DKC articles such as 36041K are accompanied by family labels
    # such as CPO90. Require a complete token on both surfaces, so 36041KX
    # and 136041K cannot masquerade as the requested article.
    exact_short_article = False
    short_article = expected_compact.replace("к", "k")
    if re.fullmatch(r"\d{5,}[a-zа-я]?", short_article):
        exact_article = rf"(?<![a-zа-я0-9]){re.escape(short_article)}(?![a-zа-я0-9])"
        exact_short_article = bool(
            re.search(exact_article, _normalise_model_text(product_title).replace("к", "k"))
            and re.search(exact_article, _normalise_model_text(url).replace("к", "k")))
    for article in re.findall(article_pattern, _normalise_model_text(product_title)):
        compact = re.sub(r"[^a-zа-я0-9]", "", article)
        if compact in expected_articles or _active_request_model_code(expected,compact):
            continue
        if _active_model_interface(expected,compact) and _type_mark_match_status(expected,product_title) is True:
            # A required, verified communication interface is a technical role,
            # not a competing manufacturer SKU. Other competing codes remain.
            continue
        if "," in expected and compact in expected_compact:
            continue
        # Exempt only these DKC family/geometry labels. A compatibility card
        # naming the requested article alongside an unrelated SKU must still
        # be rejected, even when both codes appear in its URL.
        if exact_short_article and re.fullmatch(r"(?:cpo(?:45|90)|gsv(?:50|100))", compact):
            continue
        if compact in url_compact or compact.translate(latin_article) in url_compact:
            return article
    return ""


def _conflicting_explicit_article(expected: str, product_title: str,
                                  url: str) -> bool:
    """Reject a different article printed in both a product title and its URL."""
    return bool(_different_explicit_article(expected, product_title, url))


def _product_match_is_accepted(mark_status: bool | None,
                               semantic_match: object) -> bool:
    """Неясное совпадение марки принимается только после явного подтверждения LLM."""
    if mark_status is False or semantic_match is False:
        return False
    return mark_status is True or semantic_match is True


def _sale_unit(value: object) -> str:
    from ..source_quote import _sale_unit as shared
    return shared(value)

def _explicit_piece_pack_size(title: str) -> int | None:
    from ..source_quote import _explicit_piece_pack_size as shared
    return shared(title)

def _saved_price_unit_is_verified(requested_unit: str, price_unit: str,
                                  listed_price: object, quoted_price: object,
                                  product_title: str = "", url: str = "",
                                  price_unit_evidence: str = "") -> bool:
    """Require pack evidence before reusing a saved quote as a pack price."""
    # Historical manual quotes have no raw card price; preserve that workflow.
    # A captured raw card price without its selling unit is unresolved evidence.
    if not price_unit and listed_price is not None:
        return False
    if _canonical_host(url) == "xcom-shop.ru":
        # Old LLM unit labels are not independent evidence of the selling unit.
        if not price_unit_evidence or _xcom_main_sale_unit(price_unit_evidence) != _sale_unit(price_unit):
            return False
    pack = _explicit_piece_pack_size(product_title)
    if price_unit and _sale_unit(requested_unit) == "компл" and _sale_unit(price_unit) != "компл":
        if (_sale_unit(price_unit) != "шт" or not _piece_is_complete_set(product_title)
                or pack not in {None, 1}):
            return False
        try:
            listed, quoted = float(listed_price), float(quoted_price)
            return (math.isfinite(listed) and listed > 0 and math.isfinite(quoted)
                    and math.isclose(listed, quoted, rel_tol=1e-6, abs_tol=0.02))
        except (TypeError, ValueError):
            return False
    if _sale_unit(requested_unit) == "шт" and pack is not None and pack != 1:
        if pack == 0:
            return False
        if _sale_unit(price_unit) != "уп":
            return False
        try:
            return float(listed_price) > 0 and math.isclose(
                float(listed_price) / pack, float(quoted_price), rel_tol=0.02, abs_tol=0.02)
        except (TypeError, ValueError):
            return False
    if price_unit and _sale_unit(price_unit) != _sale_unit(requested_unit):
        try:
            listed, quoted = float(listed_price), float(quoted_price)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(listed) or listed <= 0 or not math.isfinite(quoted):
            return False
        lot = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(?:м|метр[аов]*)\.?", price_unit.strip().casefold())
        if lot and _sale_unit(requested_unit) == "м":
            quantity = float(lot[1].replace(",", "."))
            converted = listed / quantity if quantity > 0 else None
        else:
            converted = _price_per_requested_unit(
                {"price_unit": price_unit, "price_with_vat": quoted,
                 "listed_price_with_vat": listed}, requested_unit, product_title)
        return (converted is not None and math.isfinite(converted)
                and math.isclose(converted, quoted, rel_tol=1e-6, abs_tol=0.02))
    if _sale_unit(requested_unit) != "уп":
        return True
    if _sale_unit(price_unit) != "уп":
        return False
    try:
        listed = float(listed_price)
        quoted = float(quoted_price)
    except (TypeError, ValueError):
        return False
    return listed > 0 and math.isclose(listed, quoted, rel_tol=0.02, abs_tol=0.02)


def _product_length_metres(title: str) -> float | None:
    from ..source_quote import _product_length_metres as shared
    return shared(title)

def _piece_is_complete_set(title: str) -> bool:
    from ..source_quote import _piece_is_complete_set as shared
    return shared(title)

def _price_per_requested_unit(data: dict, requested_unit: str = "",
                              product_title: str = "", *, owned_unit_context: str = "", identity_metadata: tuple = (), factory_proof=None,request_position=None) -> float | None:
    from ..source_quote import _price_per_requested_unit as shared
    return shared(data, requested_unit, product_title, owned_unit_context=owned_unit_context, identity_metadata=identity_metadata,factory_proof=factory_proof,request_position=request_position)

def _rs24_price_per_requested_unit(page, page_text: str, requested_unit: str) -> float | None:
    """Read RS24's selected selling unit; order multiplicity is not package size."""
    try:
        price_node = page.locator(".js-product-price")
        unit_node = page.locator("button.js-add-to-cart[data-uom-code]")
        if price_node.count() != 1 or unit_node.count() != 1:
            return None
        raw = price_node.first.inner_text()
        price = float(re.sub(r"\s", "", raw).replace(",", "."))
        selling_unit = unit_node.first.get_attribute("data-uom-code")
    except ResearchControlError:
        raise
    except Exception:
        return None
    if price <= 0:
        return None
    requested = _sale_unit(requested_unit)
    one_piece_pack = bool(re.search(
        r"В 1 упаковке\s+1\s+шт\.?", page_text, re.IGNORECASE
    ))
    if selling_unit == "EA" and requested == "шт":
        return price
    if selling_unit == "001" and requested == "шт" and one_piece_pack:
        return price
    if requested == "компл" and selling_unit in {"EA", "001"}:
        if selling_unit == "001" and not one_piece_pack:
            return None
        try:
            title_node = page.locator("h1")
            if title_node.count() == 1 and _piece_is_complete_set(title_node.first.inner_text()):
                return price
        except ResearchControlError:
            raise
        except Exception:
            return None
    if selling_unit == "M" and requested == "м":
        return price
    if selling_unit == "EA" and requested == "м":
        length = re.search(r"(?:^|\n)Длина\s*\n\s*(\d+(?:[.,]\d+)?)\s*(мм|см|м)\b",
                           page_text, re.IGNORECASE)
        if length is None:
            return None
        metres = float(length.group(1).replace(",", ".")) * {
            "мм": 0.001, "см": 0.01, "м": 1.0,
        }[length.group(2).lower()]
        return price / metres if metres > 0 else None
    return None


def _chipdip_price_per_requested_unit(page, product_title: str,
                                      requested_unit: str, sale_unit: str) -> float | None:
    """Use ChipDip's product price when a clearly sized piece is priced per metre."""
    if _sale_unit(requested_unit) != "м" or _sale_unit(sale_unit) != "шт":
        return None
    length = _product_length_metres(product_title)
    if length is None:
        return None
    try:
        prices = page.locator('meta[itemprop="price"][content]')
        if prices.count() != 1:
            return None
        listed = float(prices.first.get_attribute("content"))
    except ResearchControlError:
        raise
    except Exception:
        return None
    return listed / length if listed > 0 else None


def _lanitnord_price_per_requested_unit(page, requested_unit: str,
                                        sale_unit: str) -> float | None:
    """Read the loaded product card's price, never a related-product price."""
    if not requested_unit or _sale_unit(sale_unit) != _sale_unit(requested_unit):
        return None
    try:
        prices = page.locator('meta[itemprop="price"][content]')
        if prices.count() != 1:
            return None
        listed = float(prices.first.get_attribute("content"))
    except ResearchControlError:
        raise
    except Exception:
        return None
    return listed if listed > 0 else None


def _elektro_main_metre_offer(page, requested_unit: str) -> tuple[float, str, float] | None:
    """Read the main card's explicit total 'за N м', not retail/related prices."""
    if _sale_unit(requested_unit) != "м":
        return None
    try:
        blocks = page.locator('.active_price_st')
        if blocks.count() != 1:
            return None
        block = blocks.first
        labels = block.locator('.class__price_label_pack')
        spans = block.locator(':scope > span')
        if labels.count() != 1 or spans.count() != 3:
            return None
        if spans.nth(1).inner_text().strip() != '₽':
            return None
        price_node = spans.first
        label = labels.inner_text()
        match = re.fullmatch(r"\s*за\s+(\d+(?:[.,]\d+)?)\s*м\.?\s*", label, re.IGNORECASE)
        if not match:
            return None
        quantity = float(match.group(1).replace(',', '.'))
        raw = price_node.inner_text()
        price = float(re.sub(r'\s', '', raw).replace(',', '.'))
        if not math.isfinite(price) or not math.isfinite(quantity) or price <= 0 or quantity <= 0:
            return None
        return price / quantity, f"{quantity:g} м", price
    except (AttributeError, TypeError, ValueError):
        return None


def _dkc_market_main_offer(page, product_title: str, requested_unit: str) -> tuple[float, str] | None:
    """One sold piece is a complete kit only when its card names the contents."""
    try:
        blocks = page.locator('.product__price')
        if blocks.count() != 1:
            return None
        block = blocks.first
        text = block.inner_text()
        if not re.search(r'цена\s+за\s+1\s+шт\b', text, re.IGNORECASE):
            return None
        prices = block.locator('[data-product-card-price-from-cart]')
        if prices.count() != 1:
            return None
        raw = prices.first.inner_text()
        price = float(re.sub(r'[\s₽]', '', raw).replace(',', '.'))
        requested = _sale_unit(requested_unit)
        if not math.isfinite(price) or price <= 0:
            return None
        if requested == 'шт' or (requested == 'компл' and _piece_is_complete_set(product_title)):
            pack = _explicit_piece_pack_size(product_title)
            if pack is None or pack == 1:
                return price, 'шт'
    except (AttributeError, TypeError, ValueError):
        pass
    return None


def _lanitnord_main_price_on_request(page_text: str) -> bool:
    """The primary card has no quotable price even if related items are priced."""
    product = page_text.split("Похожие товары", 1)[0]
    article = re.search(r"(?m)^\s*Артикул:\s*[A-Za-zА-Яа-я0-9._-]+\s*$", product)
    if not article:
        return False
    return bool(re.search(r"(?m)^\s*ПО ЗАПРОСУ\s*$", product[article.end():]))


def _ekc_price_per_requested_unit(page, expected_mark: str,
                                  requested_unit: str) -> tuple[float, str] | None:
    """Use the actual E-KC offer row, not an LLM guess about its selling unit."""
    try:
        # A drawing may state both the CT30 family and its full variant code.
        # The last comma-separated code distinguishes 0.5/5VA from 0.5S/2.5VA.
        exact_mark = _mark_variants(expected_mark)[-1].strip()
        rows = page.locator('tr[itemprop="offers"]')
        for index in range(rows.count()):
            row = rows.nth(index)
            title = row.locator("td.title").inner_text()
            if exact_mark and _type_mark_match_status(exact_mark, title) is not True:
                continue
            raw_price = row.locator('td.price meta[itemprop="price"]').get_attribute(
                "content"
            )
            price = float(raw_price)
            sale_unit = _sale_unit(row.locator("td.input .colWo").inner_text())
            requested = _sale_unit(requested_unit)
            if price <= 0 or not sale_unit or not requested:
                continue
            if sale_unit == requested:
                return price, sale_unit
            if (sale_unit == "шт" and requested == "компл"
                    and _piece_is_complete_set(title)):
                return price, sale_unit
            length = _product_length_metres(title)
            if length is None:
                continue
            if sale_unit == "м" and requested == "шт":
                return price * length, sale_unit
            if sale_unit == "шт" and requested == "м":
                return price / length, sale_unit
    except (AttributeError, TypeError, ValueError):
        pass
    return None


def _ekc_matching_offer_row(page, expected_mark: str) -> bool | None:
    """Whether E-KC's loaded offer table has the required full variant."""
    try:
        rows = page.locator('tr[itemprop="offers"]')
        count = rows.count()
        if not count:
            return None
        exact_mark = expected_mark.split(",")[-1].strip()
        return any(_type_mark_match_status(
            exact_mark, rows.nth(index).locator("td.title").inner_text()) is True
                   for index in range(count))
    except (AttributeError, TypeError, ValueError):
        return None


def _elektromehanika_price_per_requested_unit(
    page_text: str, product_title: str, requested_unit: str,
) -> tuple[float, str] | None:
    """Use the store's main card price, not third-party warehouse prices below it."""
    if "Доступно:" not in page_text:
        return None
    main = page_text.split("Доступно:", 1)[1].split("Наличие на складах:", 1)[0]
    unit_match = re.search(r"\d+\s*(шт|м)(?=Описание|\s|$)", main, re.IGNORECASE)
    price_match = re.search(r"(\d[\d\s\u00a0]*[.,]?\d*)\s*₽", main)
    if not unit_match or not price_match:
        return None
    try:
        price = float(re.sub(r"\s", "", price_match.group(1)).replace(",", "."))
    except ValueError:
        return None
    if price <= 0:
        return None
    sale_unit = _sale_unit(unit_match.group(1))
    requested = _sale_unit(requested_unit)
    if sale_unit == requested:
        return price, sale_unit
    length = _product_length_metres(product_title)
    if not length:
        return None
    if sale_unit == "шт" and requested == "м":
        return price / length, sale_unit
    if sale_unit == "м" and requested == "шт":
        return price * length, sale_unit
    return None


def _canonical_host(url: str, supplier_hosts: set[str] | None = None) -> str:
    host = urlparse(url).netloc.replace("www.", "").lower()
    for supplier_host in supplier_hosts or ():
        if host == supplier_host or host.endswith(f".{supplier_host}"):
            return supplier_host
    return host


def _dedup_by_host(urls: list[str], skip_hosts: set[str] | None = None,
                   supplier_hosts: set[str] | None = None,
                   expected_mark: str = "") -> list[str]:
    """Prefer a matching product card over a category or conflicting article."""
    seen = set(skip_hosts or ())
    best: dict[str, tuple[int, str]] = {}
    for u in urls:
        h = _canonical_host(u, supplier_hosts)
        if not h or h in seen:
            continue
        mark_status = _type_mark_match_status(expected_mark, u) if expected_mark else None
        score = (0 if _is_listing_url(u) else 2) + {
            True: 3, False: -3, None: 0,
        }[mark_status]
        if h not in best or score > best[h][0]:
            best[h] = (score, u)
    return [url for _, url in best.values()]


def _candidate_urls_by_host(urls: list[str], skip_hosts: set[str] | None = None,
                            supplier_hosts: set[str] | None = None,
                            expected_mark: str = "", per_host: int = 2) -> list[str]:
    """Keep a backup card per seller when the first search hit has no usable price."""
    groups: dict[str, list[tuple[int, int, str]]] = {}
    for index, url in enumerate(urls):
        host = _canonical_host(url, supplier_hosts)
        if not host or host in (skip_hosts or set()):
            continue
        mark_status = _type_mark_match_status(expected_mark, url) if expected_mark else None
        token_overlap = len(_distinctive_model_tokens(expected_mark)
                            & _distinctive_model_tokens(url)) if expected_mark else 0
        score = (0 if _is_listing_url(url) else 2) + {
            True: 3, False: -3, None: 0,
        }[mark_status] + token_overlap
        group = groups.setdefault(host, [])
        if all(existing_url != url for _, _, existing_url in group):
            group.append((score, index, url))
    selected_by_host: list[tuple[int, int, list[str]]] = []
    for group in groups.values():
        ranked = sorted(group, key=lambda item: (-item[0], item[1]))
        cards = [item for item in ranked if not _is_listing_url(item[2])]
        selected = (cards or ranked)[:per_host]
        selected_by_host.append((selected[0][0], selected[0][1],
                                 [url for _, _, url in selected]))
    selected_by_host.sort(key=lambda item: (-item[0], item[1]))
    # Try each seller's best card first. Backup cards should cost time only if
    # five distinct sellers were not found in that first round.
    return [cards[round_index] for round_index in range(per_host)
            for _, _, cards in selected_by_host if len(cards) > round_index]


def _site_declares_minimum_prices(url: str, page_text: str) -> bool:
    """Cable.ru labels its catalog prices as minimums, subject to a final quote."""
    return (_canonical_host(url) == "cable.ru"
            and "как минимальные" in page_text.casefold()
            and "окончательная цена" in page_text.casefold())


def _is_listing_url(url: str) -> bool:
    """Category search results can contain a matching SKU and another item's price."""
    host = _canonical_host(url)
    path = urlparse(url).path.casefold()
    return (path in {"", "/"}
            # These URLs show many SKUs/prices, never one quotable card.
            or (host == "minimaks.ru" and path.startswith("/catalog/"))
            or (host == "220.ru" and path.split("/")[1] in {
                "rozetki-i-vyklyuchateli", "kabel-kanaly-korob",
                "truby-dlya-elektroprovodki", "kabel-i-provod",
                "okl", "korobki",
            })
            or (host == "elektromehanika.ru" and path in {"/index.php", "/products.php"})
            or (host == "e-kc.ru" and path.startswith("/price/"))
            or (host == "thermopat.com" and path == "/products")
            or (host == "chipdip.ru" and path.startswith("/catalog-show/"))
            or (host == "etm.ru" and (path == "/catalog" or path.startswith("/catalog/")))
            or (host == "elektro.ru" and path.startswith("/catalog/"))
            or (host == "lanitnord.ru" and path.startswith("/catalog/"))
            or (host == "elektromehanika.ru" and path == "/items.php"))


def _elektromehanika_card_out_of_stock(page_text: str) -> bool:
    """Detect the availability of the main card, not related-product snippets."""
    return bool(re.search(
        r"Производитель:[^\n]{0,120}Нет в наличии\s*Описание",
        page_text, re.IGNORECASE,
    ))


def _dkc_market_card_out_of_stock(page_text: str) -> bool:
    """Read availability from the product block, before the description and footer."""
    article = re.search(r"\bарт\.\s*[A-Za-zА-Яа-я0-9_-]+", page_text, re.IGNORECASE)
    if not article:
        return False
    product_block = page_text[article.end():].split("Описание", 1)[0]
    return "нет в наличии" in product_block.casefold()


def _etm_main_product_price(page, url: str, page_text: str,
                            requested_unit: str) -> tuple[float, str] | None:
    """Verify the current ETM offer against its visible public unit price."""
    path = urlparse(url).path.rstrip("/")
    card = re.fullmatch(r"/cat/nn/(\d+)", path)
    if not card:
        return None
    main_text = page_text.split("Аналоги и замены", 1)[0]
    unit = re.search(r"Ед\.измерения:\s*([^\n]{1,30})", main_text, re.IGNORECASE)
    if not unit:
        return None
    sale_unit = unit.group(1).strip()
    if not _sale_unit(sale_unit) or _sale_unit(sale_unit) != _sale_unit(requested_unit):
        return None
    try:
        blocks = page.locator('script[type="application/ld+json"]').all_text_contents()
    except ResearchControlError:
        raise
    except Exception:
        return None
    products = []
    for block in blocks:
        try:
            product = json.loads(block)
        except (TypeError, ValueError):
            continue
        if isinstance(product, dict) and product.get("@type") == "Product" \
                and str(product.get("sku")) == card.group(1):
            products.append(product)
    stock_states = set()
    for product in products:
        offers = product.get("offers", [])
        for offer in offers if isinstance(offers, list) else [offers]:
            if not isinstance(offer, dict):
                continue
            offered_url = str(offer.get("url") or "")
            if (offer.get("priceCurrency") == "RUB"
                    and _canonical_host(offered_url) == "etm.ru"
                    and urlparse(offered_url).path.rstrip("/") == path):
                stock_states.add(str(offer.get("availability") or ""))
    # The price sidebar occurs after the analogue section in DOM text order.
    # Its exact SKU test id separates the public price from crossed-out retail,
    # company-only discounts, recommendations and JSON-LD order subtotals.
    try:
        node = page.locator(f'[data-testid="catalog-list-item-price-details-{card.group(1)}-0-0"]')
        visible_nodes = [node.nth(i) for i in range(node.count())
                         if node.nth(i).is_visible()]
        if len(visible_nodes) > 1 or len(stock_states) > 1:
            return None
        if (stock_states == {"https://schema.org/InStock"}
                and len(visible_nodes) == 1):
            visible = re.fullmatch(
                r"\s*(\d[\d \u00a0\u202f]*(?:[.,]\d{1,2})?)\s*₽\s*/\s*([А-Яа-яA-Za-z.]+)\s*",
                visible_nodes[0].inner_text())
            if visible and _sale_unit(visible.group(2)) == _sale_unit(requested_unit):
                price = float(re.sub(r"\s", "", visible.group(1)).replace(",", "."))
                if math.isfinite(price) and price > 0:
                    return price, sale_unit
    except ResearchControlError:
        raise
    except Exception:
        pass
    matches: list[float] = []
    for block in blocks:
        try:
            product = json.loads(block)
        except (TypeError, ValueError):
            continue
        if not isinstance(product, dict) or product.get("@type") != "Product":
            continue
        if str(product.get("sku")) != card.group(1):
            continue
        offers = product.get("offers", [])
        for offer in offers if isinstance(offers, list) else [offers]:
            if not isinstance(offer, dict) or offer.get("priceCurrency") != "RUB":
                continue
            try:
                offered_url = urlparse(str(offer.get("url") or ""))
            except ValueError:
                continue
            if (_canonical_host(str(offer.get("url") or "")) != "etm.ru"
                    or offered_url.path.rstrip("/") != path):
                continue
            if not str(offer.get("availability") or "").endswith("/InStock"):
                continue
            try:
                valid_until = date.fromisoformat(str(offer.get("priceValidUntil")))
                price = float(offer.get("price"))
            except (TypeError, ValueError):
                continue
            if valid_until < date.today() or not math.isfinite(price) or price <= 0:
                continue
            matches.append(price)
    if len(matches) != 1:
        return None
    # ETM's JSON-LD may quote the subtotal for the minimum order instead of
    # the visible per-metre price. Never label that subtotal as a unit price.
    visible = re.search(
        r"(\d[\d \u00a0\u202f]*(?:[.,]\d{1,2})?)\s*₽\s*/\s*([А-Яа-яA-Za-z.]+)",
        main_text)
    if not visible or _sale_unit(visible.group(2)) != _sale_unit(requested_unit):
        return None
    price = float(re.sub(r"\s", "", visible.group(1)).replace(",", "."))
    if not math.isfinite(price) or price <= 0:
        return None
    if math.isclose(matches[0], price, rel_tol=1e-6, abs_tol=0.02):
        return price, sale_unit
    multiple = re.search(r"Заказ\s+по\s+(\d+(?:[.,]\d+)?)\s*([А-Яа-яA-Za-z.]+)",
                         main_text, re.IGNORECASE)
    if multiple and _sale_unit(multiple.group(2)) == _sale_unit(requested_unit):
        qty = float(multiple.group(1).replace(",", "."))
        if qty > 0 and math.isclose(matches[0], price * qty, rel_tol=1e-6, abs_tol=0.02):
            return price, sale_unit
    return None


def _xcom_card_out_of_stock(page_text: str) -> bool:
    """Ignore prices of recommendations below an unavailable XCOM product."""
    product_start = re.search(r"Код товара:\s*\d+", page_text, re.IGNORECASE)
    if not product_start:
        return False
    product_block = page_text[product_start.end():].split("Описание", 1)[0]
    return "нет в наличии" in product_block.casefold()


def _xcom_main_sale_unit(page_text: str) -> str:
    main = page_text.split("Рекомендуем приобрести", 1)[0]
    units = {_sale_unit(unit) for unit in re.findall(
        r"(?:Единица измерения|Ед\.?\s*изм\.?)\s*:?\s*(шт\.?|м)(?=\s|$)",
        main, re.IGNORECASE)}
    return units.pop() if len(units) == 1 else ""


def _xcom_main_metre_price(page, page_text: str, requested_unit: str) -> float | None:
    """Convert the main in-cart tray's piece price using its stated length."""
    if _sale_unit(requested_unit) != "м":
        return None
    main = page_text.split("Рекомендуем приобрести", 1)[0]
    if _xcom_main_sale_unit(main) != "шт":
        return None
    if "Добавить в корзину" not in main or _xcom_card_out_of_stock(page_text):
        return None
    lengths = {float(value.replace(",", ".")) / 1000 for value in re.findall(
        r"Длина\s*\(L\)\s*:?[\s\S]{0,12}?(\d+(?:[.,]\d+)?)\s*мм\b",
        main, re.IGNORECASE,
    )}
    if len(lengths) != 1 or next(iter(lengths)) <= 0:
        return None
    price = _xcom_main_listed_price(page)
    return price / next(iter(lengths)) if price is not None else None


def _xcom_main_listed_price(page) -> float | None:
    try:
        price_node = page.locator('meta[itemprop="price"][content]')
        if price_node.count() != 1:
            return None
        price = float(price_node.first.get_attribute("content"))
    except ResearchControlError:
        raise
    except Exception:
        return None
    return price if math.isfinite(price) and price > 0 else None


def _satro_cable_roll_length(page_text: str) -> int | None:
    if "вся кабельная продукция" not in page_text.casefold() or "продаётся бухтами" not in page_text.casefold():
        return None
    match = re.search(r"Длина бухты,\s*м\s*\n\s*(\d+)\b", page_text, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _satro_minimum_order_conflict(url: str, product_title: str,
                                  requested_unit: str, requested_qty: float) -> bool:
    if (_canonical_host(url) != "satro-paladin.com"
            or _sale_unit(requested_unit) != "м" or requested_qty <= 0):
        return False
    match = re.search(r"кратно\s+(\d+)\s+метр", product_title, re.IGNORECASE)
    if not match:
        return False
    roll = int(match.group(1))
    return roll > 0 and not math.isclose(requested_qty / roll,
                                         round(requested_qty / roll), abs_tol=1e-9)


def _satro_cable_metre_price(page_text: str) -> float | None:
    """Satro cable prices are per metre; roll length is an order multiple."""
    if _satro_cable_roll_length(page_text) is None:
        return None
    unit = re.search(r"Единица измерения\s*\n\s*м\s*(?:\n|$)", page_text, re.IGNORECASE)
    retail = re.search(r"РОЗНИЧНАЯ ЦЕНА\s*\n\s*([\d\s]+(?:[.,]\d{1,2})?)\s*₽",
                       page_text, re.IGNORECASE)
    if not unit or not retail or "ДОБАВИТЬ В КОРЗИНУ" not in page_text:
        return None
    try:
        price = float(re.sub(r"\s", "", retail.group(1)).replace(",", "."))
    except ValueError:
        return None
    return price if math.isfinite(price) and price > 0 else None


def _resolve_elektromehanika_listing(url: str, expected_mark: str) -> str | None:
    """Resolve a listing; a known product card must never rediscover its article."""
    if urlparse(url).path.casefold() == "/items_teh.php":
        return url  # Identity is verified on the card, not by another search.
    mark = expected_mark.strip()
    if not mark or not re.fullmatch(r"[A-Za-zА-Яа-я0-9._-]+", mark):
        return None
    article = re.compile(rf"(?<![A-Za-zА-Яа-я0-9]){re.escape(mark)}(?![A-Za-zА-Яа-я0-9])",
                         re.IGNORECASE)
    # The no-www host is indexed by search, but its browser navigation is flaky.
    site_root = "https://www.elektromehanika.ru/"
    search_url = urljoin(site_root, f"art_search_items.php?artpro={quote(mark)}")
    try:
        response = supplier_http_get(search_url)
        response.raise_for_status()
        transport_observed(Outcome.SUCCESS)
        html = response.content.decode("latin1")  # article and href are ASCII
        cards = re.split(r'<div\s+class=["\']sa-strock["\']>', html, flags=re.IGNORECASE)
        for card in cards[1:]:
            if not article.search(card):
                continue
            link = re.search(r'<a\s+href\s*=\s*["\']?([^"\' >]*items_teh\.php[^"\' >]*)',
                             card, re.IGNORECASE)
            if link:
                return urljoin(search_url, unescape(link.group(1)))
    except (SearchBudgetExceeded, LLMUnavailable, SearchServiceUnavailable):
        raise
    except ResearchControlError:
        raise
    except Exception as exc:
        if isinstance(exc, httpx.HTTPStatusError):
            transport_observed(http_outcome(exc.response.status_code),
                               retry_after=exc.response.headers.get('retry-after'))
        elif isinstance(exc, httpx.TimeoutException):
            transport_observed(Outcome.NETWORK_TIMEOUT)
        elif isinstance(exc, httpx.NetworkError):
            transport_observed(Outcome.NETWORK_ERROR)
        _reject_card(url, "article_search_error",
                     f"поиск по артикулу не ответил: {type(exc).__name__}")
    return None


def _search_queries_on_sites(queries: list[str], hosts: list[str], n: int,
                             label: str, *, deadline: float | None = None) -> list[str]:
    """Выполняет варианты запроса параллельно, чтобы таймауты SERP не складывались."""
    def run(query: str) -> tuple[str, list[str], Exception | None]:
        try:
            if deadline is not None:
                return query, search_on_sites(query, hosts, n=n, deadline=deadline), None
            return query, search_on_sites(query, hosts, n=n), None
        except SearchQueryFailed as exc:
            return query, [], exc
        except ResearchControlError:
            raise
        except Exception as exc:
            return query, [], exc

    urls: list[str] = []
    with ThreadPoolExecutor(max_workers=min(3, len(queries) or 1)) as pool:
        futures = [pool.submit(copy_context().run, run, query) for query in queries]
        for query, found, error in (future.result() for future in futures):
            if error is not None:
                if isinstance(error, SearchQueryFailed):
                    _record_query_failure(error,hosts)
                    continue
                if isinstance(error, ResearchProgressError):
                    raise error
                if isinstance(error, SearchBudgetExceeded):
                    _reject_card("", "discovery_deferred", str(error))
                    continue
                _log(f"  ({label}) запрос «{query[:40]}» → "
                     f"сбой: {type(error).__name__}: {error}")
                _record_search_error(query, f"{label}: {type(error).__name__}")
                continue
            _log(f"  ({label}) запрос «{query[:40]}» → {len(found)} ссылок")
            urls.extend(found)
    return urls


def _search_unrepresented_hosts(query: str, hosts: list[str], found_urls: list[str],
                                n: int = 10, *, deadline: float | None = None) -> list[str]:
    """Recover suppliers hidden by a crowded multi-site SERP batch."""
    represented = {_canonical_host(url, set(hosts)) for url in found_urls}
    missing = [host for host in hosts
               if host not in represented and source_type(host) == "direct_supplier"
               and not supplier_paused(f"https://{host}/")]
    if not query or not missing:
        return []

    def search_host(host: str) -> tuple[list[str], Exception | None]:
        try:
            if deadline is not None:
                return search_on_sites(query, [host], n=n, deadline=deadline), None
            return search_on_sites(query, [host], n=n), None
        except SearchQueryFailed as exc:
            return [], exc
        except ResearchControlError:
            raise
        except Exception as exc:
            return [], exc

    urls: list[str] = []
    with ThreadPoolExecutor(max_workers=min(3, len(missing))) as pool:
        futures = [pool.submit(copy_context().run, search_host, host) for host in missing]
        for host, (found, error) in zip(missing, (future.result() for future in futures)):
            if error is not None:
                if isinstance(error, SearchQueryFailed):
                    _record_query_failure(error,[host])
                    continue
                if isinstance(error, ResearchProgressError):
                    raise error
                if isinstance(error, SearchBudgetExceeded):
                    _reject_card(f"https://{host}/", "discovery_deferred", str(error))
                    continue
                _log(f"  (точечный поиск) {host} → {type(error).__name__}: {error}")
                _record_search_error(f"{host}: {query}", type(error).__name__)
            _log(f"  (точечный поиск) {host} → {len(found)} ссылок")
            urls.extend(found)
    return urls


def _search_phase(browser, pos: SpecPosition, queries: list[str], hosts: list[str],
                  existing: list[PriceOffer], deadline: float, budget_phase: str,
                  budget_seconds: int, per_query: int) -> tuple[list[PriceOffer], int, list[str]]:
    """Inspect an exact query before spending time on semantic alternatives."""
    offers: list[PriceOffer] = []
    checked = 0
    discovered: list[str] = []
    seen_urls: set[str] = set()
    host_set = set(hosts)
    for query in queries:
        if source_count(existing + offers) >= settings.min_sources or time.monotonic() >= deadline:
            break
        used_hosts = {_canonical_host(offer.url, host_set) for offer in existing + offers}
        remaining_hosts = []
        for host in hosts:
            if host in used_hosts:
                continue
            if supplier_paused(f"https://{host}/"):
                _reject_card(f"https://{host}/", "supplier_paused",
                             "сайт временно пропущен после повторных сбоев в этом прогоне", skipped=True)
            else:
                remaining_hosts.append(host)
        if not remaining_hosts:
            break
        batches = _iter_search_batches(query, remaining_hosts, per_query,
                                       budget_phase, deadline)
        try:
            for urls in batches:
                discovered.extend(urls)
                used_hosts = {_canonical_host(offer.url, host_set)
                              for offer in existing + offers}
                selected = _candidate_urls_by_host(
                    urls, skip_hosts=used_hosts, supplier_hosts=host_set,
                    expected_mark=pos.type_mark)
                selected = [url for url in selected if url not in seen_urls]
                seen_urls.update(selected)
                fresh, visited = _collect_offers(
                    browser, selected, settings.min_sources - source_count(existing + offers),
                    pos.type_mark, pos.name, pos.unit, supplier_hosts=host_set,
                    deadline=deadline, budget_phase=budget_phase, budget_seconds=budget_seconds,
                    expected_qty=pos.qty, existing_offers=existing + offers)
                offers.extend(fresh)
                checked += visited
                if (source_count(existing + offers) >= settings.min_sources
                        or time.monotonic() >= deadline):
                    break
        finally:
            primary_error = sys.exc_info()[1]
            try:
                batches.close()
            except ResearchControlError as error:
                if primary_error is None:
                    raise
                primary_error.add_note(f"Дополнительно не удалось сохранить очередь: {error}")
    return offers, checked, discovered


def _iter_search_batches(query: str, hosts: list[str], n: int, label: str,
                         deadline: float):
    """Keep a bounded rolling window and reserve time for validated cards."""
    groups = [hosts[i:i + 8] for i in range(0, len(hosts), 8)]
    remaining = max(0.0, deadline - time.monotonic())
    discovery_deadline = deadline - min(12.0, remaining * 0.4)
    width = min(2, max(1, settings.search_api_workers), len(groups) or 1)
    pool = ThreadPoolExecutor(max_workers=width)
    pending = {}
    next_group = 0
    queue = current_queue()
    def discover(group):
        urls = search_on_sites(query, group, n=n, deadline=discovery_deadline)
        if queue is not None:
            queue.add(urls)
        return urls
    try:
        while pending or next_group < len(groups):
            # Do not queue every supplier group at once. Refill only when the
            # caller has finished inspecting the previous result's cards.
            while (len(pending) < width and next_group < len(groups)
                   and time.monotonic() < discovery_deadline):
                group = groups[next_group]
                future = pool.submit(copy_context().run, discover, group)
                pending[future] = next_group
                next_group += 1
            if not pending:
                break
            ready = {future for future in pending if future.done()}
            if not ready:
                ready, _ = wait(pending, timeout=max(0, discovery_deadline-time.monotonic()),
                                return_when=FIRST_COMPLETED)
            if not ready or time.monotonic() >= deadline:
                break
            for future in sorted(ready, key=pending.get):
                group_index=pending.pop(future)
                try:
                    urls = future.result()
                except SearchQueryFailed as error:
                    _record_query_failure(error,groups[group_index])
                    continue
                except ResearchControlError:
                    raise
                except Exception as error:
                    if isinstance(error, ResearchProgressError):
                        raise
                    if isinstance(error, SearchBudgetExceeded):
                        _reject_card("", "discovery_deferred", str(error))
                        continue
                    _record_search_error(query, f"{label}: {type(error).__name__}")
                    continue
                _log(f"  ({label}) группа сайтов, запрос «{query[:40]}» → {len(urls)} ссылок")
                yield urls
        deferred = len(pending) + len(groups) - next_group
        if deferred:
            _reject_card("", "discovery_deferred",
                         f"этап «{label}»: поиск ещё {deferred} групп поставщиков "
                         "отложен, оставлено время для проверки карточек")
    finally:
        active_error = sys.exc_info()[1]
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
        for future in pending:
            if future.done() and not future.cancelled():
                failure = future.exception()
                if isinstance(failure,SearchQueryFailed):
                    _record_query_failure(failure,groups[pending[future]])
                    continue
                if isinstance(failure, ResearchControlError) and (
                        active_error is None or isinstance(active_error, GeneratorExit)):
                    raise failure


def _brand_direct_hosts(mark: str, hosts: list[str]) -> list[str]:
    """Find a manufacturer's own domain only when it exactly names the mark's brand."""
    brand = re.split(r"[^a-zа-я0-9]+", mark.casefold().strip())[0]
    if not brand or len(brand) < 4:
        return []
    return [host for host in hosts if host.split(".")[0].casefold() == brand
            and source_type(host) == "direct_supplier"]


def _verified_manufacturer_brand(mark: str) -> str:
    """Only require a brand when a listed direct manufacturer owns that domain."""
    hosts = _brand_direct_hosts(mark, whitelist_hosts())
    return hosts[0].split(".")[0].casefold() if hosts else ""


def _admit_card_text(url,text,title,expected_type_mark,expected_unit,expected_qty,required_brand,blocked_hosts,supplier_host):
    if (_canonical_host(url) == "elektromehanika.ru"
            and _elektromehanika_card_out_of_stock(text)):
        _reject_card(url, "out_of_stock", "карточка помечена «Нет в наличии», цена не котируется")
        return None
    if (_canonical_host(url) == "dkc.market"
            and _dkc_market_card_out_of_stock(text)):
        _reject_card(url, "out_of_stock", "товар отсутствует в наличии у поставщика")
        return None
    if (_canonical_host(url) == "xcom-shop.ru"
            and _xcom_card_out_of_stock(text)):
        _reject_card(url, "out_of_stock", "товар отсутствует в наличии у поставщика")
        return None
    if _site_declares_minimum_prices(url, text):
        _reject_card(url, "minimum_price", "только минимальная цена «от», нужна котировка")
        # This disclosure applies to the site's catalog, not only
        # the current card; more cards cannot provide a firm quote.
        blocked_hosts.add(supplier_host)
        return None
    if (_canonical_host(url) == "lanitnord.ru"
            and _lanitnord_main_price_on_request(text)):
        _reject_card(url, "price_on_request",
                     "основной товар продаётся по запросу, числовая цена относится к другим товарам")
        return None
    roll_length = (_satro_cable_roll_length(text)
                   if _canonical_host(url) == "satro-paladin.com" else None)
    if (roll_length and _sale_unit(expected_unit) == "м" and expected_qty > 0
            and not math.isclose(expected_qty / roll_length,
                                 round(expected_qty / roll_length), abs_tol=1e-9)):
        _reject_card(url, "minimum_order",
                     f"требуется {expected_qty:g} м, продаётся только бухтами по {roll_length} м")
        return None
    if _conflicting_numeric_article(expected_type_mark, title):
        _reject_card(url, "article_mismatch",
                     "заголовок карточки содержит другой артикул")
        return None
    if required_brand and required_brand not in f"{url} {title} {text}".casefold():
        _reject_card(url, "brand_unconfirmed",
                     "на странице нет марки указанного производителя")
        return None
    page_article = (_printed_page_article(
        text, bracket_heading=_canonical_host(url) == "lanmark.ru")
                    if _canonical_host(url) in {"lanitnord.ru", "elevel.ru",
                                                "lanmark.ru"}
                    else _rs24_manufacturer_article(text)
                    if _canonical_host(url) == "rs24.ru" else "")
    if _conflicting_page_article(expected_type_mark, page_article):
        _reject_card(url, "article_mismatch",
                     f"на карточке указан другой артикул: {page_article}")
        return None
    return {'roll_length':roll_length,'page_article':page_article}


# Existing DOM readers below require row unit/mark/title; their observations
# cannot be replayed as neutral facts until their producer is separated.
_REQUEST_BOUND_RENDER_PROFILES={'xcom-shop.ru','elektro.ru','dkc.market','rs24.ru','chipdip.ru','lanitnord.ru','e-kc.ru','elektromehanika.ru','etm.ru','satro-paladin.com'}

def _snapshot_from_raw_rendered(document,requested_url,supplier_host,mark,name,unit,qty,brand,blocked,deadline):
    from .card_batch import CardSnapshot
    from .product_identity import rendered_identity
    from .session import observed_identity,research_deadline
    raw=document.facts();data=raw['data'];coherent=data['coherent'];observer=data['identity_observation']
    source=data['source'];reference=source.get('ref','')
    # Integrity of the owned neutral text observation, not image-content proof.
    if not re.fullmatch(r'main_facts/[a-f0-9]{32}\.json',reference):
        _reject_card(requested_url,'raw_document_invalid','Ссылка на исходное наблюдение не подтверждена');return None
    path=CACHE_DIR/reference
    try:
        if path.is_symlink() or path.resolve().parent!=(CACHE_DIR/'main_facts').resolve() or path.stat().st_size>1024*1024:raise ValueError('invalid observation')
        payload=path.read_bytes();bounded_timeout(1)
        if sha256(payload).hexdigest()!=source.get('sha256'):raise ValueError('observation changed')
        stored=json.loads(payload)
        if any(stored.get(k)!=coherent.get(k) for k in ('main_title','article','owner_text','purchase_text','body','observed_at','owner_bound','owner_truncated','body_truncated','document_url','rendered_source')):raise ValueError('observation changed')
    except (OSError,ValueError,TypeError):
        _reject_card(requested_url,'raw_document_invalid','Исходное наблюдение повреждено; сохранённая цена не подтверждается');return None
    if coherent['kind']=='explicit_price_on_request':
        _reject_card(requested_url,'price_on_request','На основной карточке цена предоставляется по запросу');return None
    text=coherent['owner_text'];title=coherent['document_title'] or coherent['main_title']
    if _conflicting_numeric_article(mark,coherent['main_title']) or _conflicting_full_model_code(mark,coherent['main_title']) or _conflicting_model_code_in_title(mark,coherent['main_title']):
        _reject_card(requested_url,'article_mismatch','Наблюдаемый основной товар имеет другое исполнение');return None
    identity=rendered_identity(requested_url,raw['actual_url'],observer['main_title'],observer['main_article'],mark,name,supplier_host,observed_at=raw['captured_at'])
    if identity:observed_identity(identity)
    admission=_admit_card_text(raw['actual_url'],text,title,mark,unit,qty,brand,blocked,supplier_host)
    if admission is None:return None
    active=research_deadline();owned=min(d for d in (deadline,active) if d is not None) if deadline is not None or active is not None else None
    facts=dict(url=raw['actual_url'],requested_url=requested_url,supplier_host=supplier_host,expected_type_mark=mark,expected_name=name,expected_unit=unit,expected_qty=qty,text=text,purchase_text=coherent.get('purchase_text',''),title=title,main_title=coherent['main_title'],page_article=admission['page_article'] or identity.get('main_article',''),identity_evidence=identity,adapter={},roll_length=admission['roll_length'],captured_at=raw['captured_at'],main_facts=data['source'],readiness_kind=coherent['kind'],missing_fields=list(coherent['missing_fields']),document_body=coherent['body'],screenshot_context='',context_capture_evidence={},context_capture_review_reason='Снимок карточки создаётся после отбора предложений')
    # Revalidate the immutable financial DOM for this request, independently of
    # optional images and of the caller's neutral cache packet.
    if identity and coherent.get('rendered_source'):
        from .http_card import rendered_source,read_source
        typed=rendered_source(coherent,data['source'])
        if typed and read_source(typed,allow_rendered=True):
            facts['rendered_source']=typed
            facts['identity_evidence']={**identity,'rendered_source':typed}
    return CardSnapshot.create(facts,owned)

def _live_render_profile(browser):
    """No eager browser start or cookie inspection; only actual live contexts."""
    context=getattr(browser,'_context',None)
    if context is None or getattr(browser,'_context_closed',True) or getattr(browser,'_shutdown',True):return None
    signature=(context,getattr(browser,'_supplier_context',None))
    previous=getattr(browser,'_kac_raw_contexts',None)
    if previous is None or any(a is not b for a,b in zip(signature,previous)):
        browser._kac_raw_contexts=signature
        browser._kac_raw_cohort=__import__('uuid').uuid4().hex
    if not isinstance(getattr(browser,'_kac_raw_cohort',None),str):browser._kac_raw_cohort=__import__('uuid').uuid4().hex
    return 'rendered_owner_v1:'+browser._kac_raw_cohort

def _capture_card(browser,url,requested_url,supplier_host,expected_type_mark,expected_name,expected_unit,expected_qty,required_brand,blocked_hosts,deadline,*,http_consumer=None,render_consumer=None):
    from .session import current_document_cache
    from .card_attachments import capturing_attachment
    cache=current_document_cache()
    args=(browser,url,requested_url,supplier_host,expected_type_mark,expected_name,expected_unit,expected_qty,required_brand,blocked_hosts,deadline)
    if cache is None or capturing_attachment():
        sink=(lambda record,source:http_consumer(record,source,reused=False)) if http_consumer is not None else None
        return _capture_card_live(*args,http_sink=sink,render_sink=render_consumer)
    from .document_snapshot import RawDocument
    from .http_card import main_product
    from ..browser import supplier_http_configuration_hash
    import base64,uuid
    route=supplier_http_configuration_hash();http_profile='public_http_v1'
    # Rendered pages share only the same actual Browser/session owner; never
    # assume cookie/auth equality between worker contexts or disclose cookies.
    render_profile=_live_render_profile(browser)
    http_key=cache.key(requested_url,supplier_host,route,http_profile);render_key=cache.key(requested_url,supplier_host,route,render_profile) if render_profile else None
    generic=supplier_host not in _REQUEST_BOUND_RENDER_PROFILES
    def evaluate(document,reused=True):
        bounded_timeout(1);raw=document.facts();diagnostics=_CARD_DIAGNOSTICS.get()
        if reused and diagnostics is not None:diagnostics.append({'url':requested_url,'code':'raw_document_reused','reason':'Исходный документ повторно проверяется для этой позиции; дата получения сохранена','raw_document':{**document.provenance(True),'cache_epoch':cache.epoch}})
        if raw['kind']=='rendered':return _snapshot_from_raw_rendered(document,requested_url,supplier_host,expected_type_mark,expected_name,expected_unit,expected_qty,required_brand,blocked_hosts,deadline)
        record=raw['data']['record'];source=raw['data']['source'];response=httpx.Response(record['status'],headers={'content-type':record['content_type']},content=base64.b64decode(record['body_base64'],validate=True),request=httpx.Request('GET',record['url']))
        response._kac_raw_delivery=(record,source)
        sink=(lambda record,source:http_consumer(record,source,reused=reused)) if http_consumer is not None else None
        return _capture_card_live(*args,neutral_response=response,http_sink=sink,render_sink=render_consumer)
    hit=cache.peek(http_key) or (cache.peek(render_key) if generic and render_key is not None else None)
    if hit is not None:return evaluate(hit)
    owner_result=[None];captured=[None];render_captured=[None]
    owner_http=[None];owner_render=[]
    class NeutralHttpDelivered(Exception):
        """Producer-only stop: publish neutral source before any row consumer."""
    def http_sink(record,source):
        owner_http[0]=(record,source)
        if supplier_http_configuration_hash()==route:
            try:observed=main_product(base64.b64decode(record['body_base64'],validate=True).decode(record['encoding'],errors='replace'),record['url'])
            except (ValueError,LookupError):observed=None
            if observed is not None and not _looks_like_bot_check(observed['text'],observed['title']):
                captured[0]=RawDocument.create('http',requested_url,record['url'],supplier_host,route,http_profile,{'record':record,'source':source},record['observed_at'])
        if http_consumer is not None:raise NeutralHttpDelivered()
    def render_sink(data):
        if http_consumer is not None:
            owner_render.append(data);return  # Row callback runs after shared capture.
        if not generic or supplier_http_configuration_hash()!=route:return
        live_profile=_live_render_profile(browser)
        if live_profile is None:return
        raw=RawDocument.create('rendered',requested_url,data['coherent']['document_url'],supplier_host,route,live_profile,data,data['coherent']['observed_at'])
        render_captured[0]=raw
    def capture():
        try:owner_result[0]=_capture_card_live(*args,http_sink=http_sink,render_sink=render_sink if generic or http_consumer is not None else None)
        except NeutralHttpDelivered:pass
        bounded_timeout(1)
        if render_captured[0] is not None and _live_render_profile(browser)==render_captured[0].facts()['presentation']:cache.publish(render_captured[0])
        return captured[0]
    document,reused=cache.capture(http_key,capture)
    if not reused:
        if http_consumer is not None and owner_http[0] is not None:
            # Owner-local uncacheable deliveries are never handed to followers.
            record,source=owner_http[0]
            response=httpx.Response(record['status'],headers={'content-type':record['content_type']},content=base64.b64decode(record['body_base64'],validate=True),request=httpx.Request('GET',record['url']))
            response._kac_raw_delivery=(record,source)
            return _capture_card_live(*args,neutral_response=response,http_sink=lambda record,source:http_consumer(record,source,reused=False),render_sink=render_consumer)
        if render_consumer is not None:
            for data in owner_render:render_consumer(data)
        return owner_result[0]
    document=document or (cache.peek(render_key) if generic and render_key is not None else None)
    if document is None:
        _reject_card(requested_url,'raw_document_unavailable','Общее получение не предоставило совместимый документ; наличие неизвестно')
        return None
    return evaluate(document)

def _capture_card_live(browser,url,requested_url,supplier_host,expected_type_mark,expected_name,expected_unit,expected_qty,required_brand,blocked_hosts,deadline,*,neutral_response=None,http_sink=None,render_sink=None):
    """One supplier lease/deadline across HTTP and one possible render fallback."""
    from ..browser import SupplierHttpLimit,SupplierHttpRepresentation
    from .http_card import main_product,save_response
    from .card_batch import CardSnapshot
    from .product_identity import http_identity
    from .session import research_deadline,retry_after_seconds,http_capability_advice,finish_http_capability,failed_primary_http
    from ..browser import supplier_http_configuration_hash
    capability=None
    failed_http=False
    route_hash=supplier_http_configuration_hash()
    def capability_finish(outcome):
        bounded_timeout(1)
        return finish_http_capability(capability,outcome if supplier_http_configuration_hash()==route_hash else 'unknown')
    def render(attempt=None):
        bounded_timeout(1)  # Parent/card allocation and cancellation remain authoritative.
        if attempt is not None:attempt['fallback_performed']=True
        try:
            from contextlib import nullcontext
            with failed_primary_http(url) if failed_http else nullcontext():
                with supplier_transport(url) if neutral_response is not None else nullcontext():
                    snapshot=_capture_rendered_card(browser,url,requested_url,supplier_host,expected_type_mark,expected_name,expected_unit,expected_qty,required_brand,blocked_hosts,deadline,neutral_sink=render_sink)
        except BaseException as error:
            if attempt is not None:attempt['fallback_error_type']=type(error).__name__
            raise
        if attempt is not None:
            attempt['fallback_completed']=True
            attempt['recovered']=snapshot is not None
            if snapshot is not None:
                facts=snapshot.facts();facts['http_transport_attempt']=dict(attempt)
                snapshot=CardSnapshot.create(facts,snapshot.deadline)
        return snapshot
    from contextlib import nullcontext
    with deadline_scope(deadline),supplier_transport(url) if neutral_response is None else nullcontext():
        bounded_timeout(1)
        capability=http_capability_advice(supplier_host,route_hash) if neutral_response is None else None
        if capability is not None and not capability[2]['allowed']:
            advice=capability[2];failed_http=True
            diagnostics=_CARD_DIAGNOSTICS.get()
            if diagnostics is not None:
                diagnostics.append({'url':url,'code':'http_capability_browser_first','reason':'HTTP пропущен по временному состоянию транспорта; проверяется браузером',
                    'http_capability':{'reason':advice['reason'],'checked_at':advice['checked_at'],'retry_at':advice['retry_at'],
                        'stored_cause':advice.get('failure_code'),'remaining_ttl':max(0,(advice['retry_at'] or time.time())-time.time()),'http_wire_attempted':False,'configured_route_hash':route_hash}})
            return render()
        try:response=neutral_response if neutral_response is not None else supplier_http_get(url,max_bytes=2*1024*1024,max_redirects=3)
        except SupplierHttpRepresentation:
            capability_finish('unknown')
            return render()
        except SupplierHttpLimit as error:
            capability_finish('unknown')
            _reject_card(url,'extract_failed',str(error));return None
        except (httpx.TimeoutException,httpx.NetworkError) as error:
            context=getattr(error,'_kac_supplier_failure',None)
            if (not isinstance(context,FailureContext) or context.source!='supplier_http'
                    or context.scope!='local' or context.cause not in {'network_timeout','network_error'}
                    or context.host!=_canonical_host(url) or isinstance(error,httpx.ProxyError)):
                raise
            bounded_timeout(1)
            failed_http=True
            capability_finish('read_timeout' if isinstance(error,httpx.ReadTimeout) else 'unknown')
            attempt=context.diagnostic('HTTP transport: '+type(error).__name__)
            attempt.update(url=url,error_type=type(error).__name__,fallback_performed=False,recovered=False)
            diagnostics=_CARD_DIAGNOSTICS.get()
            if diagnostics is not None:
                diagnostics.append({'url':url,'code':'http_transport_attempt','reason':'HTTP не завершён; проверяется браузером в оставшееся время','http_transport_attempt':attempt})
            return render(attempt)
        if neutral_response is None:capability_finish('delivered')
        text=response.text;title=''
        if response.status_code==429:
            transport_observed(Outcome.HTTP_RATE_LIMITED,retry_after=retry_after_seconds(response.headers.get('retry-after')))
            _reject_card(url,'http_blocked','HTTP429: поставщик ограничил запросы; проверка отложена',failure_context=FailureContext('supplier_http','local','http_rate_limited',time.time(),429,supplier_host));return None
        if response.status_code==200 and 'html' in response.headers.get('content-type','').lower():
            record,source=(response._kac_raw_delivery if neutral_response is not None else save_response(response,requested_url))
            from .card_link_discovery import delivered
            delivered(record,source)
            diagnostics=_CARD_DIAGNOSTICS.get()
            if diagnostics is not None:
                diagnostics.append({'url':url,'code':'http_capture_observation','reason':'Датированный HTTP-документ повторно проверяется' if neutral_response is not None else 'HTTP200 сохранён до разбора карточки','http_response_source':source,'http_status':200,'observed_at':record['observed_at'],'http_wire_attempted':neutral_response is None})
            if http_sink is not None:http_sink(record,source)
            try:observed=main_product(text,str(response.url))
            except ValueError:observed=None
            if observed is None:
                try:frame=main_product(text,str(response.url),admission_only=True)
                except ValueError:frame=None
                if frame and (_conflicting_model_code_in_title(expected_type_mark,frame['title']) or bool(re.search(r'(?:Артикул производителя|Код производителя|Manufacturer code|MPN)\s*[:：]?\s*'+re.escape(frame['article'])+r'(?![\w.-])',frame['text'],re.I)) and _conflicting_page_article(expected_type_mark,frame['article'])):
                    _reject_card(url,'article_mismatch','Основной товар HTTP-карточки имеет другое обозначение');transport_observed(Outcome.SUCCESS);return None
                if frame and _admit_card_text(url,frame['text'],frame['title'],expected_type_mark,expected_unit,expected_qty,required_brand,blocked_hosts,supplier_host) is None:
                    transport_observed(Outcome.SUCCESS)
                    return None
            if observed and _conflicting_model_code_in_title(expected_type_mark,observed['title']):
                _reject_card(url,'article_mismatch','Наблюдаемый основной товар HTTP-карточки имеет другое обозначение');transport_observed(Outcome.SUCCESS);return None
            if observed and not _looks_like_bot_check(observed['text'],observed['title']):
                admission=_admit_card_text(url,observed['text'],observed['title'],expected_type_mark,expected_unit,expected_qty,required_brand,blocked_hosts,supplier_host)
                if admission is None:
                    transport_observed(Outcome.SUCCESS)
                    return None
                if _canonical_host(url)=='e-kc.ru':
                    return render()
                identity=http_identity(record,observed,source,expected_type_mark,expected_name,supplier_host)
                if identity:
                    active=research_deadline();owned=min(d for d in (deadline,active) if d is not None) if deadline is not None or active is not None else None
                    facts=dict(url=record['url'],requested_url=requested_url,supplier_host=supplier_host,expected_type_mark=expected_type_mark,expected_name=expected_name,expected_unit=expected_unit,expected_qty=expected_qty,text=observed['text'],title=observed['title'],main_title=observed['title'],page_article=observed['article'],identity_evidence=identity,adapter={},roll_length=None,captured_at=record['observed_at'],screenshot_context='',context_capture_evidence={},context_capture_review_reason='Снимок будет собран после отбора предложения',merchant_requisites=observed['merchant_requisites'],capture_transport='http',http_source=source)
                    snapshot=CardSnapshot.create(facts,owned)
                    # Static HTML may only bypass rendering when its actual literal
                    # quote is independently usable; missing scope/unit stays render-needed.
                    if _deterministic_card_data(snapshot) is not None:
                        transport_observed(Outcome.SUCCESS)
                        from .session import observed_identity
                        observed_identity(identity)
                        return snapshot
        # A shell/ambiguous/protection page gets one existing noninteractive render
        # under remaining budget; proxy/cancel errors above never switch route.
        return render()


_RENDER_OBSERVATION_JS="""(() => {
 const all=Array.from(document.querySelectorAll('h1'));const sample=all.slice(0,64);
 const excluded=e=>{for(let p=e,n=0;p&&n<48;p=p.parentElement,n++){
  if(p.hidden||p.getAttribute('aria-hidden')==='true'||p.matches('nav,footer,aside,dialog,[role="dialog"],template,[class*="recommend"],[class*="related"],[class*="modal"],[class*="old-price"]'))return true;
  const style=getComputedStyle(p);if(style.display==='none'||style.visibility==='hidden')return true;
 }return false};
 const visible=sample.filter(e=>!excluded(e)&&(e.innerText||'').trim());
 const scoped=visible.filter(e=>e.closest('main,[itemtype*="schema.org/Product"],form'));
 const chosen=scoped.length===1?scoped[0]:(!scoped.length&&visible.length===1?visible[0]:null);
 const body=document.body?.innerText||'';const title=document.title||'';
 return {document_url:document.URL.slice(0,4096),title:title.slice(0,512),body:body.slice(0,12000),body_truncated:body.length>12000,title_truncated:title.length>512,
  all_h1:Math.min(all.length,10000),visible_h1:visible.length,main_h1:scoped.length,headings_truncated:all.length>64,
  main_title:chosen&&all.length<=64?(chosen.innerText||'').trim().slice(0,512):''};
})()"""

def _render_observation(page,requested_url,phase):
    """Bounded current DOM observation, never screenshot or price qualification."""
    from urllib.parse import urlsplit,urlunsplit
    from hashlib import sha256
    import json,uuid
    def public_url(value):
        if not isinstance(value,str):return ''
        if value in {'about:blank','chrome-error://chromewebdata/'}:return value
        try:
            parsed=urlsplit(value)
            if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password:return ''
            return urlunsplit((parsed.scheme,parsed.netloc,parsed.path,'',''))[:4096]
        except ValueError:return ''
    bounded_timeout(1)
    record={'version':1,'role':'render_observation','phase':phase,'requested_url':public_url(requested_url),'url':public_url(getattr(page,'url','')),'document_url':public_url(getattr(page,'_kac_document_url','')),'observed_at':time.time()}
    before_url=getattr(page,'url','');response_document_url=getattr(page,'_kac_document_url','')
    status=getattr(page,'_kac_response_status',None)
    record['http_status']=status if type(status) is int and 100<=status<=599 else None
    record['status_url_bound']=bool(record['url'] and record['url']==record['document_url'])
    channel=None;limit=time.monotonic()+1
    try:
        channel=page.context.new_cdp_session(page)
        value=channel.send('Runtime.evaluate',{'expression':_RENDER_OBSERVATION_JS,'returnByValue':True,'timeout':max(1,int(bounded_timeout(max(0,limit-time.monotonic()))*1000))})
        if type(value) is not dict or value.get('exceptionDetails'):raise ValueError('observation_unavailable')
        fields=value.get('result',{}).get('value')
        if type(fields) is not dict:raise ValueError('observation_unavailable')
        for key,cap in (('title',512),('body',12000),('main_title',512),('document_url',4096)):
            if type(fields.get(key)) is not str or len(fields[key])>cap:raise ValueError('observation_unavailable')
        for key in ('body_truncated','title_truncated','headings_truncated'):
            if type(fields.get(key)) is not bool:raise ValueError('observation_unavailable')
        for key in ('all_h1','visible_h1','main_h1'):
            if type(fields.get(key)) is not int or not 0<=fields[key]<=10000:raise ValueError('observation_unavailable')
        record.update({k:fields[k] for k in ('title','body','main_title','body_truncated','title_truncated','headings_truncated','all_h1','visible_h1','main_h1')})
        after_url=getattr(page,'url','')
        changed=not (isinstance(before_url,str) and before_url==after_url==fields['document_url'])
        record.update(url=public_url(after_url),document_url=public_url(fields['document_url']),response_document_url=public_url(response_document_url),url_changed=changed,status_url_bound=not changed and response_document_url==fields['document_url'])
        record['observation_status']='navigation_changed' if changed else 'observed'
    except ResearchControlError:raise
    except Exception as error:record.update(observation_status='unavailable',observation_error_type=type(error).__name__)
    finally:
        if channel is not None:
            try:channel.detach()
            except ResearchControlError:raise
            except Exception as error:record['observation_cleanup_error_type']=type(error).__name__
    bounded_timeout(1)
    folder=CACHE_DIR/'render_observations';folder.mkdir(parents=True,exist_ok=True);path=folder/(uuid.uuid4().hex+'.json')
    raw=json.dumps(record,ensure_ascii=False,allow_nan=False).encode()
    with path.open('xb') as file:file.write(raw)
    reference={'ref':str(path.relative_to(CACHE_DIR)),'sha256':sha256(raw).hexdigest(),'observed_at':record['observed_at'],'phase':phase}
    diagnostics=_CARD_DIAGNOSTICS.get()
    trace=getattr(page,'_kac_arrival_trace',None)
    if trace is not None:trace.facts(phase,record,reference)
    if diagnostics is not None:diagnostics.append({'url':requested_url,'code':'render_observation','reason':'Текущее состояние страницы сохранено для разбора','render_observation':reference,'http_status':record['http_status'],'observed_at':record['observed_at']})
    return record,reference

def _observed_main_title(page,record):
    if record.get('observation_status')=='observed':return record.get('main_title','')
    if record.get('observation_status')=='navigation_changed':return ''
    # Unsupported CDP keeps legacy strict single-heading admission, never a
    # guess among hidden/cart/recommendation headings.
    try:
        headings=page.locator('h1')
        return headings.first.inner_text(timeout=_budget_ms(500)).strip() if headings.count()==1 else ''
    except ResearchControlError:raise
    except Exception:return ''

def _readiness_failure(url,host,record):
    text=record.get('body','');title=record.get('title','');status=record.get('http_status') if record.get('status_url_bound') else None
    actual=record.get('document_url') or record.get('url') or ''
    if record.get('observation_status')=='navigation_changed' or (actual.startswith(('http://','https://')) and _canonical_host(actual)!=_canonical_host(url)):
        _reject_card(url,'redirect_unconfirmed','Состояние документа не связано с исходной карточкой поставщика; причина доступа не подтверждена')
        return
    if _looks_like_bot_check(text,title) or _http_response_is_blocked(status,text):
        cause='bot_protection' if _looks_like_bot_check(text,title) else http_outcome(status).value
        transport_observed(Outcome.BOT_PROTECTION if cause=='bot_protection' else http_outcome(status))
        _reject_card(url,'http_blocked',_protection_reason(text,title,status),failure_context=FailureContext('supplier_navigation','local',cause,time.time(),status,host))
    elif record.get('url') in {'about:blank','chrome-error://chromewebdata/'}:
        _reject_card(url,'empty_page','Браузер показал пустой документ или внутреннюю страницу ошибки; карточка не подтверждена')
    elif record.get('observation_status')=='observed' and not text.strip():
        _reject_card(url,'empty_page','Документ не содержит видимого текста; карточка не подтверждена')
    else:_reject_card(url,'site_error','Основной заголовок карточки не готов или неоднозначен; защита от ботов не подтверждена')


_MAIN_FACTS_READINESS_JS="""(args => new Promise(resolve => {
 const limit=typeof args==='number'?args:args.limit,previousPreviewKey=typeof args==='number'?null:args.previous_preview_key;
 let observer=null,timer=null,finished=false;const initial=document.URL;
 const computedInvisible=e=>{for(let p=e;p;p=p.parentElement){
  const style=getComputedStyle(p),opacity=Number(style.opacity);
  if(style.display==='none'||style.visibility==='hidden'||style.visibility==='collapse'||!Number.isFinite(opacity)||opacity<=0)return true;
 }return false};
 const excluded=e=>{for(let p=e;p;p=p.parentElement){
  if(p.matches('aside,nav,footer,dialog,[role="dialog"],[hidden],[aria-hidden="true"],[data-related-products],[data-recommendations]'))return true;
  if(Array.from(p.classList||[]).some(c=>/^(?:related[-_]products|recommendations|recommended[-_]products|product[-_]recommendations|cart[-_]modal)$/i.test(c)))return true;
 }return Boolean(e&&computedInvisible(e))};
 const relatedHeading=/^(?:Рекомендуем|Рекомендации|Похожие товары|Сопутствующие товары|С этим товаром|Аналогичные товары|Related products|Recommended products)\\s*[:：]?$/i;
 const text=n=>{if(n.nodeType===3)return n.textContent;if(n.nodeType!==1||excluded(n)||n.matches('script,style,template,del,s,[class*="oldPrice"],[class*="old-price"],[class*="economy"],[class*="saving"]'))return '';
  let result='',boundary=null;
  for(const child of n.childNodes){
   if(child.nodeType===1){
    if(excluded(child))continue;
    const level=/^H[1-6]$/.test(child.tagName)?Number(child.tagName[1]):null;
    const control=['tab','button'].includes(child.getAttribute('role'))||n.getAttribute('role')==='tablist';
    if(level&&!control&&relatedHeading.test(Array.from(child.childNodes).map(text).join('').trim())){boundary=level;continue;}
    if(boundary!==null){if(level&&level<=boundary)boundary=null;else continue;}
   }else if(boundary!==null)continue;
   result+=text(child);
  }
  const block=/^(?:DIV|P|SECTION|ARTICLE|H[1-6]|BR|LI|DT|DD|TR|TD|TH)$/.test(n.tagName);return(block?'\\n':'')+result+(block?'\\n':'');};
 const read=()=>{
  const all=Array.from(document.querySelectorAll('h1'));const headings=all.filter(h=>!excluded(h)&&(h.innerText||'').trim());
  const h=all.length===1&&headings.length===1?headings[0]:null;
  const semanticProducts=Array.from(document.querySelectorAll('[itemscope][itemtype*="schema.org/Product"]')).filter(e=>!e.closest('aside,nav,footer,dialog,[role="dialog"],[data-related-products],[data-recommendations]'));
  let owner=h?.closest('[itemscope][itemtype*="schema.org/Product"],form,main');
  // A generic layout wrapper cannot bypass contradictory declared identity.
  if(semanticProducts.length>1||owner&&semanticProducts.length&& !owner.matches('[itemscope][itemtype*="schema.org/Product"]'))owner=null;
  let purchaseText='',purchaseUnit=false,identityRoot=null;
  // A declared Product can be a metadata sibling, never a body-wide owner.
  // Its actual name/MPN join a printed heading/article without requested data.
  if(!owner&&h){
   const norm=s=>(s||'').trim().replace(/\\s+/g,' ');
   const properties=(p,k)=>Array.from(p.querySelectorAll('[itemprop="'+k+'"]')).filter(e=>e.closest('[itemscope]')===p).map(e=>norm(e.getAttribute('content')||e.getAttribute('href')||e.innerText));
   const unique=vs=>Array.from(new Set(vs.filter(Boolean)));
   const products=semanticProducts;
   if(products.length===1){
    const product=products[0],names=unique(properties(product,'name')),mpns=unique(properties(product,'mpn')),urls=unique(properties(product,'url'));
    let urlMatch=urls.length===0;
    if(urls.length===1){try{urlMatch=new URL(urls[0],document.URL).href.split('#')[0]===document.URL.split('#')[0]}catch{}}
    let common=h.parentElement;while(common&&!common.contains(product)&&!common.matches('body,html'))common=common.parentElement;
    if(common&&!common.matches('body,html')&&!excluded(common)&&common.querySelectorAll('h1').length===1&&names.length===1&&names[0]===norm(h.innerText)&&mpns.length===1&&urlMatch){
     let card=h;while(card.parentElement!==common&&card.parentElement)card=card.parentElement;
     const rows=(root,label)=>{let values=[];for(const e of root.querySelectorAll('*')){
      if(excluded(e)||e.matches('script,style,template,input')||e.children.length>4)continue;
      const s=norm(e.innerText);if(!s||s.length>256)continue;
      const m=s.match(label);if(m)values.push({e,value:m[1]});
     }return values.filter(r=>!values.some(child=>child.e!==r.e&&r.e.contains(child.e)))};
     const articleLabel=/^(?:Артикул производителя|Код производителя|Manufacturer code|MPN)\\s*[:：]?\\s*([A-Za-zА-Яа-я0-9._-]{3,})$/i;
     const unitLabel=/^(?:Единица измерения|Ед\\.?\\s*изм\\.?|Sale unit|Unit of sale)\\s*[:：]?\\s*((?:\\d+(?:[.,]\\d+)?\\s*)?(?:штука|штуки|шт\\.?|метр|м|компл(?:ект)?|упаковка))$/i;
     const articles=rows(card,articleLabel),codes=unique(articles.map(r=>r.value));
     if(codes.length===1&&codes[0].toLowerCase()===mpns[0].toLowerCase()){
      const brands=Array.from(product.querySelectorAll('[itemscope][itemprop="brand"]')).flatMap(b=>properties(b,'name'));
      const printedBrands=unique(rows(card,/^(?:Производитель|Manufacturer|Brand)\\s*[:：]?\\s*(.{1,128})$/i).filter(r=>!articleLabel.test(norm(r.e.innerText))).map(r=>r.value));
      const bs=unique(brands);const brandMatch=!bs.length||(bs.length===1&&printedBrands.length===1&&bs[0].toLowerCase()===printedBrands[0].toLowerCase());
      if(brandMatch){
       owner=card;identityRoot=common;purchaseText=h.innerText.trim()+'\\n'+text(articles[0].e);
       for(let panel=articles[0].e;panel&&card.contains(panel);panel=panel.parentElement){
        const units=unique(rows(panel,unitLabel).map(r=>r.value));const ptext=text(panel);
        const cart=Array.from(panel.querySelectorAll('*')).some(e=>!excluded(e)&&/^(?:В корзину|Добавить в корзину|Add to cart|Buy)$/i.test(norm(e.innerText)));
        const localCodes=unique(rows(panel,articleLabel).map(r=>r.value));
        if(cart&&localCodes.length===1&&/\\d[\\d .,\\u00a0\\u202f]*\\s*(?:₽|руб\\.?|RUB)/i.test(ptext)){
         purchaseText=h.innerText.trim()+'\\n'+ptext;purchaseUnit=units.length===1;break;
        }
       }
      }
     }
    }
   }
  }
  let siblingOwner=false;
  if(!owner&&h&&!semanticProducts.length){
   const declared=Array.from(document.querySelectorAll('[data-product-id]')).filter(e=>!excluded(e)&&e.getAttribute('data-product-id')?.trim());
   if(declared.length===1){
    const candidate=declared[0];const local=text(candidate);
    const codes=Array.from(local.matchAll(/(?:^|\\n)\\s*(?:Артикул(?: производителя| товара)?|Код производителя|Article|SKU|Manufacturer code)\\s*[:：]?\\s*\\n?\\s*([A-Za-zА-Яа-я0-9._-]{3,})(?=\\s|$)/gi)).map(m=>m[1]);
    const unique=Array.from(new Set(codes));const title=h.innerText.trim();
    if(!candidate.querySelector('h1')&&unique.length===1&&title.split(/[^A-Za-zА-Яа-я0-9._-]+/).some(t=>t.toLowerCase()===unique[0].toLowerCase())){owner=candidate;siblingOwner=true}
   }
  }
  // Only explicit Product itemref can extend technical ownership outside the
  // identity/card branch. Retain the complete common scope, not a selected
  // affirmative paragraph; Python independently rederives the same join.
  let technicalBinding='not_declared';
  if(owner&&semanticProducts.length===1&&semanticProducts[0].hasAttribute('itemref')){
   const ids=(semanticProducts[0].getAttribute('itemref')||'').trim().split(/\\s+/).filter(Boolean);
   const refs=ids.map(id=>Array.from(document.querySelectorAll('[id]')).filter(n=>n.id===id));
   let common=owner;
   if(!ids.length||ids.length>16||new Set(ids).size!==ids.length||refs.some(ns=>ns.length!==1||excluded(ns[0])||!(ns[0].getAttribute('itemprop')||'').split(/\\s+/).includes('description')))technicalBinding='ambiguous';
   else{
    for(const [ref] of refs)while(common&&!common.contains(ref))common=common.parentElement;
    if(!common||common.matches('body,html')||excluded(common))technicalBinding='ambiguous';
    else{owner=common;identityRoot=common;technicalBinding='bound'}
   }
  }
  const bound=Boolean(technicalBinding!=='ambiguous'&&owner&&!excluded(owner)&&(siblingOwner||owner.querySelectorAll('h1').length===1));
  const raw=bound?(siblingOwner?h.innerText.trim()+'\\n':'')+text(owner):'';
  const primary=raw;const articles=Array.from(primary.matchAll(/(?:^|\\n)\\s*(?:Артикул(?: производителя| товара)?|арт\\.|Код производителя|Article|SKU|Manufacturer code)\\s*[:：]?\\s*\\n?\\s*([A-Za-zА-Яа-я0-9._-]{3,})(?=\\s|$)/gi)).map(m=>m[1]);
  const unique=Array.from(new Set(articles));const article=unique.length===1?unique[0]:'';
  const price=Boolean(primary.match(/\\d[\\d .,\\u00a0\\u202f]*\\s*(?:₽|руб\\.?|RUB)/i));
  const unit=Boolean(primary.match(/(?:цена\\s+)?за\\s*(?:\\d+(?:[.,]\\d+)?\\s*)?(?:метр(?:а|ов)?|м(?![A-Za-zА-Яа-я])|шт\\.?|штук[аи]?|компл(?:ект)?|упаковку)|\\/\\s*(?:шт\\.?|м(?![A-Za-zА-Яа-я])|компл)/i))||purchaseUnit;
  const body=document.body?.innerText||'';const title=h?.innerText.trim()||'';
  const missing=[];if(!title)missing.push('title');if(!article)missing.push('article');if(!price)missing.push('price');if(!unit)missing.push('sale_unit');
  let kind=!bound?'unsupported_scope':!missing.length?'complete':'partial';
  if(document.URL!==initial)kind='navigation_changed';
  else if(/captcha|recaptcha|hcaptcha|turnstile|smartcaptcha|verify you are human|проверка пользователя|подтвердите.{0,30}человек/i.test(body.slice(0,12000)))kind='observed_protection';
  else if(bound&&!price&&/(?:цена|стоимость)\\s+(?:по\\s+запросу|уточняется)/i.test(primary))kind='explicit_price_on_request';
  // One declared primary Chipdip Product/Offer, never a global/cart cash guess.
  // Local proof visibility includes every ancestor, including outside Product.
  const previewVisible=e=>Boolean(e&&!excluded(e));
  const previewText=n=>{
   if(n.nodeType===3)return previewVisible(n.parentElement)?n.textContent:'';
   if(n.nodeType!==1||!previewVisible(n)||n.matches('script,style,template,del,s,[class*="oldPrice"],[class*="old-price"],[class*="economy"],[class*="saving"]'))return '';
   const block=/^(?:DIV|P|SECTION|ARTICLE|H[1-6]|BR|LI|DT|DD|TR|TD|TH)$/.test(n.tagName);
   return (block?'\\n':'')+Array.from(n.childNodes).map(previewText).join('')+(block?'\\n':'');
  };
  let preview_cash='',preview_text='';
  if(new URL(document.URL).hostname.replace(/^www\\./i,'')==='chipdip.ru'&&bound&&owner.matches('[itemscope][itemtype*="schema.org/Product"]')&&h.closest('[itemscope][itemtype*="schema.org/Product"]')===owner){
   const offers=Array.from(owner.querySelectorAll('.product__ordering[itemprop="offers"][itemscope][itemtype*="schema.org/AggregateOffer"]')).filter(e=>previewVisible(e)&&e.closest('[itemscope][itemtype*="schema.org/Product"]')===owner);
   if(offers.length===1){
    const offer=offers[0],cash=Array.from(offer.querySelectorAll('.ordering-mainoffer-price-w')).filter(e=>previewVisible(e)&&e.closest('.product__ordering')===offer);
    const preview=Array.from(offer.querySelectorAll('.ordering-tocart-info')).filter(e=>previewVisible(e)&&e.closest('.product__ordering')===offer);
    if(cash.length===1&&preview.length===1&&Array.from(offer.querySelectorAll('button,a,[role="button"]')).some(e=>previewVisible(e)&&/^(?:Добавить в корзину|В корзину|Купить|Купить в 1 клик)$/i.test(previewText(e).trim()))){
     const value=previewText(cash[0]).trim(),previewValue=previewText(preview[0]).trim();
     if(value.length<=256&&previewValue.length<=4096){preview_cash=value;preview_text=previewValue;}
    }
   }
  }
  const control_captions=bound?Array.from(new Set(Array.from(owner.querySelectorAll('a,button,[role="button"]')).filter(e=>!excluded(e)&&!e.querySelector('a,button,[role="button"]')).map(e=>(e.innerText||'').replace(/\\s+/g,' ').trim()).filter(v=>/^(?:В корзину|Добавить в корзину|В корзине|Add to cart|Added to cart|In cart|Buy)$/i.test(v)))).slice(0,32):[];
  let rendered_dom='',rendered_capture_status=typeof args==='object'&&args.capture_dom?'owner_unbound':'not_requested';
  const rendered_capture_budget={nodes:0,bytes:0};
  if(typeof args==='object'&&args.capture_dom&&bound){
   // Preserve structural ancestry and every declared identity conflict. Only
   // exterior subtrees unrelated to this scope are omitted. Never crop owned
   // claims or keep only a description that happened to look affirmative.
   const roots=new Set([identityRoot||owner,...document.querySelectorAll('[itemscope][itemtype*="schema.org/Product"],h1,script[type="application/ld+json"],link[rel="canonical"],head>title,head>meta')]);
   const ancestors=new Set();for(const root of roots)for(let n=root;n;n=n.parentElement)ancestors.add(n);
   let stopped='';
   const project=(node,inside=false)=>{
    if(stopped)return null;
    const deep=inside||roots.has(node);
    if(!deep&&!ancestors.has(node))return null;
    if(node.nodeType===1&&node.matches('style,script:not([type="application/ld+json"])')&&!semanticProducts.includes(node))return null;
    if(++rendered_capture_budget.nodes>40000){stopped='node_limit';return null}
    if(node.nodeType!==1&&node.nodeType!==3)return null;
    const copy=node.cloneNode(false);
    if(node.nodeType===1){
     // Execution/asset payloads have no primary identity/technical authority.
     for(const attribute of Array.from(copy.attributes))if(/^on/i.test(attribute.name)||['src','srcset','nonce','integrity'].includes(attribute.name))copy.removeAttribute(attribute.name);
     if(!node.matches('head,meta,script,link,title')&&computedInvisible(node))copy.setAttribute('hidden','');
     if(copy.hasAttribute('style')&&copy.getAttribute('style').trim()==='')copy.removeAttribute('style');
    }
    let cost=node.nodeType===3?new TextEncoder().encode(copy.nodeValue||'').length:32+new TextEncoder().encode(copy.outerHTML).length;
    rendered_capture_budget.bytes=Math.min(2*1024*1024+1,rendered_capture_budget.bytes+cost);
    if(rendered_capture_budget.bytes>2*1024*1024){stopped='byte_limit';return null}
    for(const child of node.childNodes){const next=project(child,deep);if(next)copy.appendChild(next);if(stopped)break}
    return copy;
   };
   const clone=project(document.documentElement);
   if(stopped)rendered_capture_status=stopped;
   else if(!clone)rendered_capture_status='owner_unbound';
   else{
    const serialized=clone.outerHTML,bytes=new TextEncoder().encode(serialized).length;
    rendered_capture_budget.bytes=bytes;
    if(bytes>2*1024*1024)rendered_capture_status='byte_limit';
    else{rendered_dom=serialized;rendered_capture_status='captured'}
   }
  }
  const value={rendered_dom,rendered_capture_status,rendered_capture_budget,preview_cash,preview_text,control_captions,kind,missing_fields:missing,owner_bound:bound,document_url:document.URL.slice(0,4096),document_title:document.title.slice(0,512),main_title:title.slice(0,512),article,owner_text:primary.slice(0,40000),purchase_text:purchaseText.slice(0,20000),owner_truncated:primary.length>40000||purchaseText.length>20000,body:body.slice(0,200000),body_truncated:body.length>200000,observed_at:Date.now()/1000};
  // Neutral source snapshot, never a JS unit/financial acceptance decision.
  // Timestamps and unrelated body/footer changes cannot wake Python repeatedly.
  value.preview_change_key=JSON.stringify([kind,missing,bound,value.document_url,value.main_title,article,value.owner_text,value.purchase_text,value.owner_truncated,value.body_truncated,preview_cash,preview_text]);
  return value;
 };
 const finish=value=>{if(finished)return;finished=true;observer?.disconnect();if(timer!==null)clearTimeout(timer);resolve(value)};
 const check=()=>{const value=read();
  if(['complete','explicit_price_on_request','observed_protection','navigation_changed'].includes(value.kind))finish(value);
  else if(previousPreviewKey!==null&&value.preview_change_key!==previousPreviewKey){value.preview_changed=true;finish(value)}
 };
 observer=new MutationObserver(check);observer.observe(document.documentElement,{subtree:true,childList:true,characterData:true,attributes:true});
 timer=setTimeout(()=>finish(read()),Math.max(1,limit));check();
}))"""

def _owned_readiness_preview(result,requested_url):
    """Reuse native denomination proof (including packages) solely for readiness, never kit/price admission."""
    from hashlib import sha256
    from urllib.parse import urlsplit
    from ..source_quote import owned_purchase_facts,observe_captured_quote,primary_text
    from .purchase_preview import derive_unit_basis,valid_unit_basis
    actual=urlsplit(result['document_url']);requested=urlsplit(requested_url)
    if (actual.scheme not in {'http','https'} or actual.hostname not in {'chipdip.ru','www.chipdip.ru'}
            or actual.username or actual.password or actual.query or actual.fragment
            or requested.hostname not in {'chipdip.ru','www.chipdip.ru'} or requested.path!=actual.path
            or result['kind']!='partial' or result['missing_fields']!=['sale_unit']
            or not result['owner_bound'] or result['owner_truncated'] or result['body_truncated']
            or not result['main_title'] or not result['article']
            or _explicit_piece_pack_size(result['main_title']) not in (None,1)):
        return None
    cash_text=result.get('preview_cash','')
    if type(cash_text) is not str or len(cash_text)>256 or not cash_text:return None
    selected=owned_purchase_facts(cash_text)['raw_amount']
    if selected is None:return None
    text=primary_text(result['owner_text']);stamp=result['observed_at'];url=result['document_url']
    cash=observe_captured_quote(text,actual_url=url,url=url,observed_at=stamp,title=result['main_title'],
        article=result['article'],listed=selected,normalized=selected,sale_unit='',requested_unit='',data={})
    observation=dict(source_text=text,source_sha256=sha256(text.encode()).hexdigest(),raw_amount=selected,
        currency='RUB',observed_at=stamp,url=url,title=result['main_title'],purchase_terms='',state='review')
    basis=derive_unit_basis(observation,cash)
    if not valid_unit_basis(observation,basis):return None
    preview_text=result.get('preview_text','')
    if type(preview_text) is not str or not preview_text or len(preview_text)>4096:return None
    normalized=lambda value:' '.join(value.split())
    selected_cash=text[slice(*basis['cash_span'])]
    selected_quantity=text[slice(*basis['quantity_span'])]
    selected_subtotal=text[slice(*basis['subtotal_span'])]
    # The native proof must describe these exact separately selected DOM panes,
    # never an unrelated quantity/subtotal elsewhere in the main Product.
    if (normalized(cash_text)!=normalized(selected_cash)
            or normalized(preview_text)!=normalized(selected_quantity)+' '+normalized(selected_subtotal)):return None
    return basis


def _wait_main_card_facts(page,requested_url,*,max_wait=8,immediate=False):
    """One finite mutation wait; its result is source data, not quote admission."""
    from hashlib import sha256
    import json,uuid
    from .product_identity import canonical_card_url
    bounded_timeout(1)
    allocation=bounded_timeout(max_wait);before=getattr(page,'url','');channel=None;result=None
    try:
        channel=page.context.new_cdp_session(page)
        until=time.monotonic()+allocation
        last_preview_key=None;preview_changed=False;proof_key=None;preview=None
        def evaluate(milliseconds,previous_key=None):
            nonlocal last_preview_key,preview_changed,proof_key,preview
            bounded_timeout(1)
            # Financial source retention is independent of optional photographs.
            arguments=json.dumps(dict(limit=milliseconds,previous_preview_key=previous_key,capture_dom=True))
            reply=channel.send('Runtime.evaluate',{'expression':'('+_MAIN_FACTS_READINESS_JS+')('+arguments+')','returnByValue':True,'awaitPromise':True,'timeout':max(1,int(allocation*1000))})
            bounded_timeout(1)
            if type(reply) is not dict or reply.get('exceptionDetails'):raise ValueError('facts_wait_unavailable')
            result=reply.get('result',{}).get('value')
            if type(result) is not dict or result.get('kind') not in {'complete','partial','explicit_price_on_request','observed_protection','navigation_changed','unsupported_scope'}:raise ValueError('facts_wait_unavailable')
            result.setdefault('rendered_dom','')
            if type(result['rendered_dom']) is not str or len(result['rendered_dom'].encode())>2*1024*1024:raise ValueError('rendered_dom_bound')
            result.setdefault('rendered_capture_status','legacy_unspecified')
            if result['rendered_capture_status'] not in {'legacy_unspecified','evaluation_unavailable','not_requested','owner_unbound','node_limit','byte_limit','captured'}:raise ValueError('rendered_status')
            budget=result.setdefault('rendered_capture_budget',{'nodes':0,'bytes':0})
            if type(budget) is not dict or set(budget)!={'nodes','bytes'} or any(type(budget[k]) is not int or not 0<=budget[k]<= (40001 if k=='nodes' else 16*1024*1024) for k in budget):raise ValueError('rendered_budget')
            result.setdefault('purchase_text','')
            result.setdefault('control_captions',[])
            if type(result['control_captions']) is not list or len(result['control_captions'])>32 or any(type(v) is not str or not re.fullmatch(r'(?:В корзину|Добавить в корзину|В корзине|Add to cart|Added to cart|In cart|Buy)',v,re.I) for v in result['control_captions']):raise ValueError('facts_wait_unavailable')
            for key,cap in (('document_url',4096),('document_title',512),('main_title',512),('article',256),('owner_text',40000),('purchase_text',20000),('body',200000)):
                if type(result.get(key)) is not str or len(result[key])>cap:raise ValueError('facts_wait_unavailable')
            if type(result.get('missing_fields')) is not list or len(result['missing_fields'])>4 or any(k not in {'title','article','price','sale_unit'} for k in result['missing_fields']):raise ValueError('facts_wait_unavailable')
            if any(type(result.get(k)) is not bool for k in ('owner_bound','owner_truncated','body_truncated')):raise ValueError('facts_wait_unavailable')
            if type(result.get('observed_at')) not in (int,float) or not math.isfinite(result['observed_at']) or not 0<result['observed_at']<=time.time()+300:raise ValueError('facts_wait_unavailable')
            last_preview_key=result.get('preview_change_key')
            if last_preview_key is not None and (type(last_preview_key) is not str or len(last_preview_key)>500000):raise ValueError('facts_wait_unavailable')
            preview_changed=result.get('preview_changed',False)
            if type(preview_changed) is not bool:raise ValueError('facts_wait_unavailable')
            # One operation-local comparison only; no cross-operation cache/ledger.
            key=tuple(json.dumps(result.get(k),sort_keys=True) for k in ('kind','missing_fields','owner_bound','document_url','main_title','article','owner_text','purchase_text','owner_truncated','body_truncated','preview_cash','preview_text'))
            if key!=proof_key:
                preview=_owned_readiness_preview(result,requested_url);proof_key=key
            result={k:result[k] for k in ('rendered_dom','rendered_capture_status','rendered_capture_budget','kind','missing_fields','owner_bound','document_url','document_title','main_title','article','owner_text','purchase_text','owner_truncated','body','body_truncated','observed_at','control_captions')}
            if result['purchase_text']:
                from ..source_quote import owned_purchase_facts
                purchase=owned_purchase_facts(result['purchase_text'])
                for key,missing in (('raw_amount','price'),('sale_unit','sale_unit')):
                    if not purchase[key] and missing not in result['missing_fields']:result['missing_fields'].append(missing)
                if result['kind']=='complete' and result['missing_fields']:result['kind']='partial'
            if preview is not None:
                result['kind']='complete';result['missing_fields']=[]
                result['readiness_unit_basis']=preview
            return result
        from urllib.parse import urlsplit
        chipdip=(urlsplit(requested_url).hostname or '').removeprefix('www.')=='chipdip.ru'
        result=evaluate(0 if immediate or chipdip else max(1,int(allocation*1000)-25))
        changes=0
        while chipdip and not immediate and result['kind'] not in {'complete','explicit_price_on_request','observed_protection','navigation_changed'}:
            allocation=bounded_timeout(max(0,until-time.monotonic()))
            # At most31 change rechecks plus initial/final native checks. After
            # saturation keep the full remaining legacy wait and late-valid facts.
            wake=changes<31
            result=evaluate(max(1,int(allocation*1000)-25),last_preview_key if wake else None)
            if not preview_changed or not wake:break
            changes+=1
        if before!=getattr(page,'url','') or before!=result['document_url']:
            result['kind']='navigation_changed';result.pop('readiness_unit_basis',None)
    except ResearchControlError:raise
    except Exception:
        bounded_timeout(1)
        body=page.inner_text('body',timeout=_budget_ms(500));titles=page.locator('h1')
        title=titles.first.inner_text(timeout=_budget_ms(500)).strip() if titles.count()==1 else ''
        result=dict(kind='unsupported_scope',missing_fields=['article','price','sale_unit'] if title else ['title','article','price','sale_unit'],owner_bound=False,document_url=getattr(page,'url',''),document_title='',main_title=title,article='',owner_text='',owner_truncated=False,body=body[:200000],body_truncated=len(body)>200000,observed_at=time.time())
    finally:
        if channel is not None:
            try:channel.detach()
            except ResearchControlError:raise
            except Exception:pass
    bounded_timeout(1)
    from urllib.parse import urlsplit,urlunsplit
    public=canonical_card_url(result['document_url'])
    if not public and result['document_url'] in {'about:blank','chrome-error://chromewebdata/'}:public=result['document_url']
    parsed=urlsplit(public)
    result.setdefault('rendered_capture_status','evaluation_unavailable')
    result.setdefault('rendered_capture_budget',{'nodes':0,'bytes':0})
    dom=result.pop('rendered_dom','')
    result['rendered_writer_status']='not_attempted'
    if dom and result.get('owner_bound') and not result.get('owner_truncated'):
        from .http_card import save_rendered_dom
        try:
            result['rendered_source']=save_rendered_dom(dom,result,requested_url)
            result['rendered_writer_status']='stored'
        except ValueError:result['rendered_writer_status']='validation_refused'
        except OSError:result['rendered_writer_status']='storage_failed'
    stored={**result,'document_url':urlunsplit((parsed.scheme,parsed.netloc,parsed.path,'','')),'requested_url':canonical_card_url(requested_url).split('?')[0],'version':1,'role':'main_card_facts'}
    folder=CACHE_DIR/'main_facts';folder.mkdir(parents=True,exist_ok=True);path=folder/(uuid.uuid4().hex+'.json');raw=json.dumps(stored,ensure_ascii=False,allow_nan=False).encode()
    with path.open('xb') as file:file.write(raw)
    ref={'ref':str(path.relative_to(CACHE_DIR)),'sha256':sha256(raw).hexdigest(),'observed_at':result['observed_at']}
    diagnostics=_CARD_DIAGNOSTICS.get()
    trace=getattr(page,'_kac_arrival_trace',None)
    if trace is not None:trace.facts('coherent_main_facts',result,ref)
    if diagnostics is not None:diagnostics.append({'url':requested_url,'code':'main_facts_readiness','reason':'Основные исходные данные: '+result['kind'],'readiness_kind':result['kind'],'missing_fields':list(result['missing_fields']),'main_facts':ref})
    return result,ref


def _same_card_generation(before,after):
    """Preserve substantive source text; discard only observed cart-control captions."""
    from ..source_quote import owned_purchase_facts
    if any(before.get(k)!=after.get(k) for k in ('document_url','main_title','article','owner_bound','owner_truncated')):return False
    if not before.get('owner_bound') or before.get('owner_truncated'):return False
    if before.get('rendered_source',{}).get('body_sha256')!=after.get('rendered_source',{}).get('body_sha256'):return False
    def substantive(frame,key):
        captions=frame.get('control_captions',[])
        if type(captions) is not list or any(type(v) is not str or not re.fullmatch(r'(?:В корзину|Добавить в корзину|В корзине|Add to cart|Added to cart|In cart|Buy)',v,re.I) for v in captions):return None
        captions={v.casefold() for v in captions}
        # Whole stand-alone caption only: no substring, price or source clause removal.
        lines=[line for line in frame.get(key,'').splitlines() if re.sub(r'\s+',' ',line).strip().casefold() not in captions]
        return re.sub(r'\s+',' ',' '.join(lines)).strip()
    for key in ('owner_text','purchase_text'):
        old,new=substantive(before,key),substantive(after,key)
        if old is None or new is None or old!=new:return False
    try:return owned_purchase_facts(before.get('purchase_text',''))==owned_purchase_facts(after.get('purchase_text',''))
    except ValueError:return False


def _coherent_identity(page,requested_url,expected_type_mark,expected_name,supplier_host,coherent):
    from ..source_quote import observe_identity
    from .product_identity import valid_identity
    if not coherent['owner_bound']:return {}
    evidence=observe_identity(page,requested_url,expected_type_mark,expected_name,supplier_host)
    if not evidence or evidence.get('main_title')!=coherent['main_title']:return {}
    owned=coherent['article'] if coherent['owner_bound'] else ''
    if owned:
        if evidence.get('main_article')!=owned:
            current,_=_wait_main_card_facts(page,requested_url,max_wait=.5,immediate=True)
            if not _same_card_generation(coherent,current):return {}
            evidence={**evidence,'main_article':current['article']}
        return {**evidence,'observed_at':coherent['observed_at']} if valid_identity(evidence,expected_type_mark,expected_name,supplier_host) else {}
    # An unowned body/recommendation label cannot repair main identity. Exact
    # independently observed title identity remains valid without that label.
    if evidence.get('main_article'):
        evidence={**evidence,'main_article':''}
        if not valid_identity(evidence,expected_type_mark,expected_name,supplier_host):return {}
    return {**evidence,'observed_at':coherent['observed_at']} if evidence else {}

def _capture_rendered_card(browser,url,requested_url,supplier_host,expected_type_mark,expected_name,expected_unit,expected_qty,required_brand,blocked_hosts,deadline,*,neutral_sink=None):
    from .card_batch import CardSnapshot
    from .session import research_deadline
    from ..browser_transport_trace import trace_diagnostics
    neutral_photo=None
    with deadline_scope(deadline), trace_diagnostics(_CARD_DIAGNOSTICS.get()), browser.page(url) as page:
        _render_observation(page,requested_url,'before_product_wait')
        coherent,coherent_ref=_wait_main_card_facts(page,requested_url)
        if coherent['kind']=='navigation_changed':
            _reject_card(url,'redirect_unconfirmed','Документ изменился во время ожидания основных данных')
            return None
        response_status = getattr(page, "_kac_response_status", None)
        text=coherent['owner_text'] if coherent['owner_bound'] else coherent['body']
        title=coherent['document_title'] or coherent['main_title']
        if _http_response_is_blocked(response_status, text):
            transport_observed(Outcome.BOT_PROTECTION if _looks_like_bot_check(text,title)
                else http_outcome(response_status), retry_after=getattr(page,'_kac_retry_after',None))
            _reject_card(url, "http_blocked", _protection_reason(text, title, response_status),
                failure_context=FailureContext('supplier_navigation','local',
                    'bot_protection' if _looks_like_bot_check(text,title) else http_outcome(response_status).value,
                    time.time(), response_status if isinstance(response_status,int) else None,supplier_host))
            blocked_hosts.add(supplier_host)
            return None
        if not text.strip():
            _reject_card(url,'empty_page','Карточка не предоставила видимый текст в выделенное время; наличие товара не установлено')
            return None
        if _looks_like_bot_check(text, title):
            _reject_card(url, "captcha", _protection_reason(text, title, response_status))
            blocked_hosts.add(supplier_host)
            return None
        from .product_identity import canonical_card_url, card_host
        current_url = canonical_card_url(getattr(page, 'url', ''))
        document_url = canonical_card_url(getattr(page, '_kac_document_url', ''))
        if current_url and document_url and current_url != document_url:
            _reject_card(requested_url, "redirect_unconfirmed", "адрес страницы не совпадает с адресом загруженного документа")
            return None
        actual_url = document_url or current_url
        if not current_url or urlparse(str(getattr(page,'url',''))).scheme not in {'http','https'}:
            _reject_card(requested_url,'redirect_unconfirmed','страница не является HTTP-карточкой поставщика')
            return None
        if actual_url:
            if (card_host(actual_url) != card_host(url) or _is_listing_url(actual_url)
                    or _type_mark_match_status(expected_type_mark, actual_url) is False
                    or _conflicting_model_code_in_title(expected_type_mark, urlparse(actual_url).path)
                    or _different_full_skus_in_url(expected_type_mark, actual_url)
                    or re.search(r'/(?:login|signin|auth|captcha|challenge|search)(?:[/?.]|$)', actual_url, re.I)):
                _reject_card(requested_url, "redirect_unconfirmed", "переход не ведёт на карточку этого поставщика")
                return None
            url = actual_url
        if isinstance(response_status, int) and 200 <= response_status < 400 and text.strip():
            transport_observed(Outcome.SUCCESS)
        from .product_identity import rendered_identity, main_card_article
        from .session import observed_identity
        ready_now,_=_render_observation(page,requested_url,'identity_before_capture')
        main_title=coherent['main_title']
        if ready_now.get('observation_status')=='navigation_changed':
            _reject_card(url,'redirect_unconfirmed','Адрес документа изменился во время чтения основных данных')
            return None
        if neutral_sink is not None and coherent['owner_bound']:
            titles=page.locator('h1')
            count=titles.count()
            observed_title=titles.first.inner_text(timeout=_budget_ms(500)).strip() if count==1 else ''
            observed_article=main_card_article(__import__('engine.source_quote',fromlist=['primary_text']).primary_text(page.inner_text('body',timeout=_budget_ms(500)))) if count==1 else ''
            if count==1 and coherent['article'] and observed_article!=coherent['article']:
                # Navigation/related labels in full body cannot certify a code.
                # Reobserve the declared owner without requested values instead.
                current,_=_wait_main_card_facts(page,requested_url,max_wait=.5,immediate=True)
                same_owner=_same_card_generation(coherent,current)
                observed_article=current['article'] if same_owner else ''
            valid_observer=count==1 and observed_title==coherent['main_title'] and (not coherent['article'] or observed_article==coherent['article']) and canonical_card_url(page.url)==actual_url
            if valid_observer:
                record={'coherent':coherent,'source':coherent_ref,'status':response_status,'requested_url':requested_url,'identity_observation':{'main_title':observed_title,'main_article':coherent['article'],'canonical_url':canonical_card_url(page.url),'observed_at':coherent['observed_at']}}
                from .whitelist import capture_seller_reference
                record['seller_reference_snapshot'] = capture_seller_reference(coherent['document_url'],coherent['observed_at'])
                from .card_attachments import capturing_attachment
                if capturing_attachment():
                    from ..review_context import capture_context
                    shot=CACHE_DIR / ('prod_'+__import__('uuid').uuid4().hex+'.png')
                    evidence,reason=capture_context(page,shot,url)
                    after,after_ref=_wait_main_card_facts(page,requested_url,max_wait=.5,immediate=True)
                    unchanged=_same_card_generation(coherent,after)
                    # Body equality is not authority for a corrupted after
                    # receipt. Validate the typed current source independently
                    # even when its declared body hash happens to match.
                    from .http_card import rendered_source,read_source
                    requires_current=not unchanged
                    if unchanged and coherent.get('rendered_source'):
                        previous=rendered_source(coherent,coherent_ref)
                        requires_current=bool(previous and read_source(previous,allow_rendered=True))
                    if requires_current:
                        current=rendered_source(after,after_ref)
                        if (not after.get('owner_bound') or after.get('owner_truncated')
                                or after.get('kind')=='navigation_changed'
                                or canonical_card_url(after.get('document_url',''))!=actual_url
                                or not current or not read_source(current,allow_rendered=True)):
                            _reject_card(url,'source_generation_changed','Новое поколение основной карточки не подтверждено; старый снимок не использован')
                            return None
                    if not unchanged:
                        # The old photograph and cash cannot repair a changed
                        # generation; downstream checks derive the new source.
                        coherent,coherent_ref=after,after_ref
                        text=coherent['owner_text'];main_title=coherent['main_title']
                        title=coherent['document_title'] or main_title
                        neutral_photo=('',{},'Карточка изменилась во время снимка; сохранены только новые финансовые данные')
                        record=None # Never publish the prephoto neutral packet.
                    else:
                        neutral_photo=(shot,evidence,reason)
                        record.update(screenshot_context=str(shot) if evidence else '',context_capture_evidence=evidence,context_capture_review_reason=reason)
                if record is not None:neutral_sink(record)
        if coherent['kind']=='explicit_price_on_request':
            transport_observed(Outcome.SUCCESS)
            _reject_card(url,'price_on_request','На основной карточке цена предоставляется по запросу; числовая котировка не подтверждена')
            return None
        if main_title and (_conflicting_numeric_article(expected_type_mark,main_title) or _conflicting_full_model_code(expected_type_mark,main_title) or _conflicting_model_code_in_title(expected_type_mark,main_title)):
            _reject_card(url,'article_mismatch','В наблюдаемом основном заголовке указан другой артикул или исполнение',product_title=main_title[:200])
            return None
        from ..source_quote import observe_identity
        # Readiness selection cannot broaden the independent identity observer
        # or promote a labelled article borrowed from unrelated page content.
        identity_evidence=_coherent_identity(page,requested_url,expected_type_mark,expected_name,supplier_host,coherent)
        if identity_evidence:
            observed_identity(identity_evidence)
        admission=_admit_card_text(url,text,title,expected_type_mark,expected_unit,expected_qty,required_brand,blocked_hosts,supplier_host)
        if admission is None:return None
        roll_length=admission['roll_length'];page_article=admission['page_article']
        if (_canonical_host(url) == "e-kc.ru" and expected_type_mark
                and _ekc_matching_offer_row(page, expected_type_mark) is False):
            _reject_card(url, "article_mismatch",
                         "в таблице предложений нет требуемой марки и исполнения")
            return None
        # Capture all existing page adapters before closing the page. Values are
        # observations, not fabricated pages or expected-price/model answers.
        host=_canonical_host(url);adapter={}
        if host=='xcom-shop.ru':
            adapter['sale_unit']=_xcom_main_sale_unit(text)
            adapter['listed']=_xcom_main_listed_price(page)
            adapter['metre']=_xcom_main_metre_price(page,text,expected_unit) if adapter['sale_unit']=='шт' and _sale_unit(expected_unit)=='м' else None
        elif host=='elektro.ru' and _sale_unit(expected_unit)=='м':adapter['offer']=_elektro_main_metre_offer(page,expected_unit)
        elif host=='dkc.market' and _sale_unit(expected_unit) in {'шт','компл'}:adapter['offer']=_dkc_market_main_offer(page,title,expected_unit)
        elif host=='rs24.ru':adapter['unit_price']=_rs24_price_per_requested_unit(page,text,expected_unit)
        elif host in {'chipdip.ru','lanitnord.ru'}:
            reader=_chipdip_price_per_requested_unit if host=='chipdip.ru' else _lanitnord_price_per_requested_unit
            unit='шт' if host=='chipdip.ru' else _sale_unit(expected_unit)
            adapter['unit_prices']={unit:(reader(page,title,expected_unit,unit) if host=='chipdip.ru' else reader(page,expected_unit,unit))}
        elif host=='e-kc.ru':adapter['offer']=_ekc_price_per_requested_unit(page,expected_type_mark,expected_unit)
        elif host=='elektromehanika.ru':adapter['offer']=_elektromehanika_price_per_requested_unit(text,title,expected_unit)
        elif host=='etm.ru':adapter['offer']=_etm_main_product_price(page,url,text,expected_unit)
        elif host=='satro-paladin.com' and roll_length and _sale_unit(expected_unit)=='м':adapter['unit_price']=_satro_cable_metre_price(text)
        from .card_attachments import capturing_attachment
        after=after_ref=None
        shot='';context_evidence={};context_reason='Снимок карточки создаётся после отбора предложений'
        if capturing_attachment():
            from ..review_context import capture_context
            if neutral_photo is not None:
                shot,context_evidence,context_reason=neutral_photo
            else:
                shot=CACHE_DIR / ('prod_'+__import__('uuid').uuid4().hex+'.png')
                context_evidence,context_reason=capture_context(page,shot,url)
            if context_evidence:
                after,after_ref=_wait_main_card_facts(page,requested_url,max_wait=.5,immediate=True)
                if not _same_card_generation(coherent,after):
                    context_evidence={};context_reason='Сведения карточки изменились во время снятия; снимок не связан с новой ценой'
                    coherent,coherent_ref=after,after_ref
                    main_title=coherent['main_title'];title=coherent['document_title'] or main_title
                    adapter={} # Earlier adapter facts cannot repair a newer DOM generation.
        from ..source_quote import observe_identity
        late_observation,_=_render_observation(page,requested_url,'identity_after_capture')
        if late_observation.get('observation_status')=='navigation_changed':
            _reject_card(url,'redirect_unconfirmed','Адрес документа изменился во время сохранения основных данных')
            return None
        identity_evidence=_coherent_identity(page,requested_url,expected_type_mark,expected_name,supplier_host,coherent) if expected_type_mark else identity_evidence
        # Keep the exact same mutation-read epoch; no later body mixture.
        text=coherent['owner_text'] if coherent['owner_bound'] else coherent['body']
        # Every condition belongs to this generation, not the prephoto card.
        if coherent['kind']=='navigation_changed':
            _reject_card(url,'redirect_unconfirmed','Документ изменился во время снятия карточки')
            return None
        if _looks_like_bot_check(coherent['body'],title):
            _reject_card(url,'captcha',_protection_reason(coherent['body'],title,response_status))
            return None
        if coherent['kind']=='explicit_price_on_request':
            _reject_card(url,'price_on_request','Текущая карточка предоставляет цену по запросу')
            return None
        if main_title and (_conflicting_numeric_article(expected_type_mark,main_title) or _conflicting_full_model_code(expected_type_mark,main_title) or _conflicting_model_code_in_title(expected_type_mark,main_title)):
            _reject_card(url,'article_mismatch','Основной товар изменился во время снятия карточки')
            return None
        current_admission=_admit_card_text(url,text,title,expected_type_mark,expected_unit,expected_qty,required_brand,blocked_hosts,supplier_host)
        if current_admission is None:return None
        roll_length=current_admission['roll_length'];page_article=current_admission['page_article']
        if not page_article and identity_evidence.get('main_article')==coherent['article'] and coherent['owner_bound']:
            page_article=identity_evidence['main_article']
        if canonical_card_url(page.url)!=canonical_card_url(url):
            _reject_card(requested_url,'redirect_unconfirmed','адрес карточки изменился во время сохранения')
            return None
        if len(text)>200000:
            _reject_card(url,'extract_failed','Текст карточки превышает ограничение снимка данных; цена не подтверждена')
            return None
        facts=dict(url=url,requested_url=requested_url,supplier_host=supplier_host,expected_type_mark=expected_type_mark,expected_name=expected_name,expected_unit=expected_unit,expected_qty=expected_qty,text=text,purchase_text=coherent.get('purchase_text',''),title=title,main_title=main_title,page_article=page_article,identity_evidence=identity_evidence,adapter=adapter,roll_length=roll_length,captured_at=coherent['observed_at'],main_facts=coherent_ref,readiness_kind=coherent['kind'],missing_fields=list(coherent['missing_fields']),document_body=coherent['body'],screenshot_context=str(shot) if context_evidence else '',context_capture_evidence=context_evidence,context_capture_review_reason=context_reason)
        if coherent.get('rendered_source'):
            from .http_card import rendered_source,read_source,rendered_photo_valid
            source=rendered_source(coherent,coherent_ref,after=after,after_facts=after_ref,context=context_evidence or None)
            if source and read_source(source,allow_rendered=True):
                facts['rendered_source']=source
                facts['identity_evidence']={**identity_evidence,'rendered_source':source}
                if context_evidence and not rendered_photo_valid(source,context_evidence,facts['screenshot_context']):
                    facts.update(screenshot_context='',context_capture_evidence={},context_capture_review_reason='Текущий снимок не связан с сохранёнными сведениями карточки')
        active=research_deadline();owned=min(d for d in (deadline,active) if d is not None) if deadline is not None or active is not None else None
        snapshot=CardSnapshot.create(facts,owned)
    return snapshot


def _literal_card_fields(snapshot, *, financial_outcome=None):
    """Only one independently usable current literal price with exact identity."""
    from ..source_quote import primary_text,verified_owned_text,selected_price_context,observe_captured_quote,quote_review_reason
    from ..price_capture import visible_prices,_sale_lot
    facts=snapshot.facts();title=facts['main_title'];identity=facts['identity_evidence']
    source=facts.get('http_source') or facts.get('rendered_source')
    try:text=verified_owned_text(facts['text'],source,url=facts['url'],observed_at=facts['captured_at']) if source else primary_text(facts['text'])
    except (ValueError,TypeError,KeyError,OSError):return {}
    choices=[];raw_choices=[]
    for listed in dict.fromkeys(visible_prices(text)):
        price_text,unit_text=selected_price_context(text,listed)
        if re.search(r'(?:розничная|клубная|оптовая)\s+цена',price_text,re.I):continue
        units=re.findall(r'(?:\bцена\s+)?\bза(?=\s|\d|$)\s*(\d+(?:[.,]\d+)?)?\s*(метр(?:а|ов)?|м|шт\.?|штук[аи]?|компл(?:ект)?)(?![а-яa-z])',unit_text,re.I)
        units+=re.findall(r'/\s*(\d+(?:[.,]\d+)?)?\s*(метр(?:а|ов)?|м|шт\.?|штук[аи]?|компл(?:ект)?)(?![а-яa-z])',unit_text,re.I)
        units={(float(q.replace(',','.')) if q else 1,_sale_unit(u)) for q,u in units}
        if len(units)!=1:continue
        quantity,unit=next(iter(units));sale=unit if quantity==1 else f'{quantity:g} {unit}'
        data=dict(product_title=title,price_unit=sale,listed_price_with_vat=listed,price_with_vat=listed/quantity,
                  package_quantity=quantity,listed_price_covers_multiple_requested_units=quantity!=1,matches_requested_product=True,match_reason='Точное обозначение основной карточки и однозначная текстовая цена')
        normalized=listed/quantity if unit==_sale_unit(facts['expected_unit']) else _price_per_requested_unit(data,facts['expected_unit'],title)
        # Preserve the selected source denomination before requested-unit loss.
        # Reuse the same quote/role checks in the source unit; this does not
        # make the raw quotation admissible for the requested unit.
        if financial_outcome is not None:
            raw_data=dict(data)
            raw_evidence=observe_captured_quote(text,actual_url=facts['url'],observed_at=facts['captured_at'],url=facts['url'],title=title,article=identity.get('main_article',''),listed=listed,normalized=listed/quantity,sale_unit=sale,requested_unit=unit,data=raw_data,owned_source=source)
            raw_view=dict(raw_data,url=facts['url'],identity_evidence=identity,source_quote_evidence=raw_evidence,normalized_price_unit=unit)
            if raw_evidence and not quote_review_reason(raw_view,{'unit':unit,'type_mark':'','name':facts['expected_name']}):
                raw_choices.append((raw_data,normalized))
        if normalized is None:continue
        data['price_with_vat']=normalized
        evidence=observe_captured_quote(text,actual_url=facts['url'],observed_at=facts['captured_at'],url=facts['url'],title=title,article=identity.get('main_article',''),listed=listed,normalized=normalized,sale_unit=sale,requested_unit=facts['expected_unit'],data=data,owned_source=facts.get('http_source') or facts.get('rendered_source'))
        view=dict(data,url=facts['url'],identity_evidence=identity,source_quote_evidence=evidence,normalized_price_unit=facts['expected_unit'])
        if evidence and not quote_review_reason(view,{'unit':facts['expected_unit'],'type_mark':'','name':facts['expected_name']}):choices.append(data)
    fields=choices[0] if len(choices)==1 else {'product_title':title}
    if not identity and (facts['expected_type_mark'] or _normalise_model_text(title)!=_normalise_model_text(facts['expected_name'])):
        fields.pop('matches_requested_product',None);fields.pop('match_reason',None)
    elif identity:
        fields.update(matches_requested_product=True,match_reason='Точное обозначение основной карточки')
    if financial_outcome is not None and identity and len(set(visible_prices(text)))==1 and len(raw_choices)==1 and not choices and raw_choices[0][1] is None:
        from .financial_outcome import RawFinancialRejection
        raw=dict(raw_choices[0][0],price_with_vat=None)
        financial_outcome(RawFinancialRejection(raw,'requested_unit_unresolved'))
    return fields

def _deterministic_card_data(snapshot, *, financial_outcome=None):
    facts=snapshot.facts()
    from .owned_purchase_row import from_facts
    row=from_facts(facts)
    if row:
        data=dict(product_title=facts['main_title'],matches_requested_product=bool(facts['identity_evidence']),
                  match_reason='Текущая строка покупки с явной денежной суммой и единицей',
                  listed_price_with_vat=row['raw_amount'],price_unit=row['sale_unit'],
                  price_with_vat=row['raw_amount'],package_quantity=1)
        normalized=_price_per_requested_unit(data,facts['expected_unit'],facts['main_title'],owned_unit_context=row['unit_text'])
        data['price_with_vat']=normalized
        if financial_outcome is not None and normalized is None:
            from .financial_outcome import RawFinancialRejection
            financial_outcome(RawFinancialRejection(dict(data),'requested_unit_unresolved'))
        return data
    if facts.get('http_source') or facts.get('rendered_source'):
        from ..source_quote import labelled_sale_units
        if labelled_sale_units(facts['text']):
            from .literal_card_quote import current_literal_quote
            return current_literal_quote(facts) if financial_outcome is None else current_literal_quote(facts,financial_outcome=financial_outcome)
    if facts.get('purchase_text'):
        from ..source_quote import owned_purchase_facts
        purchase=owned_purchase_facts(facts['purchase_text'])
        data=dict(product_title=facts['main_title'],matches_requested_product=bool(facts['identity_evidence']),match_reason='Независимые сведения основного предложения; неподтверждённые единицы остаются для разбора',listed_price_with_vat=purchase['raw_amount'],price_unit=purchase['sale_unit'],price_with_vat=None)
        if purchase['raw_amount'] is not None and not purchase['reason']:
            data['price_with_vat']=_price_per_requested_unit(dict(data,price_with_vat=purchase['raw_amount']),facts['expected_unit'],facts['main_title'])
            if financial_outcome is not None and data['price_with_vat'] is None:
                from .financial_outcome import RawFinancialRejection
                financial_outcome(RawFinancialRejection(dict(data),'requested_unit_unresolved'))
        return data  # Complete raw source facts never need a guessed conversion.
    fields=_literal_card_fields(snapshot) if financial_outcome is None else _literal_card_fields(snapshot,financial_outcome=financial_outcome)
    if fields.get('matches_requested_product') is True and fields.get('listed_price_with_vat') is not None:return fields
    facts=snapshot.facts();adapter=facts['adapter'];identity=facts['identity_evidence']
    if not identity:return None
    offer=adapter.get('offer'); listed=adapter.get('listed')
    unit=adapter.get('sale_unit')
    if offer:
        normalized,unit=offer[:2];listed=offer[2] if len(offer)>2 else normalized
    elif listed is not None:normalized=adapter.get('metre') or listed
    else:return None
    from ..source_quote import observe_captured_quote,quote_review_reason
    data=dict(product_title=facts['main_title'],matches_requested_product=True,match_reason='Подтверждённые сведения основной карточки',price_with_vat=normalized,listed_price_with_vat=listed,price_unit=unit)
    evidence=observe_captured_quote(facts['text'],actual_url=facts['url'],observed_at=facts['captured_at'],url=facts['url'],title=facts['main_title'],article=identity.get('main_article',''),listed=listed,normalized=normalized,sale_unit=unit,requested_unit=facts['expected_unit'],data=data,owned_source=facts.get('http_source') or facts.get('rendered_source'))
    view=dict(data,url=facts['url'],identity_evidence=identity,source_quote_evidence=evidence,normalized_price_unit=facts['expected_unit'])
    return data if evidence and not quote_review_reason(view,{'unit':facts['expected_unit'],'type_mark':facts['expected_type_mark'],'name':facts['expected_name']}) else None


def _finish_captured_card(browser,snapshot,data,*,enrich_seller=True):
    from ..source_quote import observe_captured_quote
    from .product_identity import main_card_article
    from .session import observed_identity
    facts=snapshot.facts()
    url=facts['url'];requested_url=facts['requested_url'];supplier_host=facts['supplier_host']
    expected_type_mark=facts['expected_type_mark'];expected_name=facts['expected_name'];expected_unit=facts['expected_unit']
    text=facts['text'];title=facts['title'];main_title=facts['main_title'];page_article=facts['page_article']
    identity_evidence=facts['identity_evidence'];adapter=facts['adapter'];roll_length=facts['roll_length']
    from .product_identity import native_source_spec_reason, native_comparison_mark
    native_reason=native_source_spec_reason(None,main_title,text,identity_evidence.get('main_article') or page_article,mark=expected_type_mark,name=expected_name)
    if native_reason:
        _reject_card(url,'native_spec_unconfirmed',native_reason)
        return None
    comparison_type_mark=native_comparison_mark(mark=expected_type_mark,name=expected_name)
    queue=current_queue()
    product_title = str(data.get("product_title", ""))
    if _conflicting_model_code_in_title(comparison_type_mark, product_title):
        _reject_card(url, "article_mismatch",
                     "в названии товара указана другая модель")
        return None
    if _conflicting_network_cable_spec(expected_name, comparison_type_mark,
                                       product_title + " " + title):
        _reject_card(url, "product_mismatch",
                     "другой тип экрана или категория сетевого кабеля")
        return None
    conflicting_article = _different_explicit_article(
        comparison_type_mark, product_title, url)
    if conflicting_article:
        _reject_card(url, "article_mismatch",
                     f"конфликтующее обозначение {conflicting_article} в названии и адресе; "
                     f"ожидалось {expected_type_mark}: «{product_title[:200]}»",
                     observed_article=conflicting_article,
                     product_title=product_title)
        return None
    if _conflicting_numeric_article(comparison_type_mark, product_title):
        _reject_card(url, "article_mismatch", "в названии товара указан другой цифровой артикул")
        return None
    if _conflicting_full_model_code(expected_type_mark, product_title):
        _reject_card(url, "article_mismatch", "в названии товара другой полный артикул")
        return None
    mark_status = _type_mark_match_status(
        expected_type_mark, _model_identity(product_title, url)
    )
    semantic_match = data.get("matches_requested_product")
    match_reason = str(data.get("match_reason") or "без пояснения")
    if not _product_match_is_accepted(mark_status, semantic_match):
        if mark_status is None and semantic_match is not False:
            _reject_card(url, "product_unconfirmed",
                         f"соответствие не подтверждено: {match_reason[:90]}")
            return None
        _reject_card(url, "product_mismatch",
                     f"другая марка/типоразмер: «{product_title[:70]}»")
        return None
    # Validate the dated captured text, never a synthetic/reopened page.
    text=facts['text']
    price_snapshot={'text':text,'title':main_title,'url':url}
    host = _canonical_host(url)
    etm_offer = None
    elektro_offer = None
    dkc_market_offer = None
    unit_evidence = ""
    verified_listed_price = None
    if host == "xcom-shop.ru":
        xcom_unit = adapter.get('sale_unit','')
        unit_evidence = f"Единица измерения: {xcom_unit}" if xcom_unit else ""
        if xcom_unit == "шт" and _sale_unit(expected_unit) == "м":
            unit_price = adapter.get('metre')
            verified_listed_price = adapter.get('listed')
        elif xcom_unit == _sale_unit(expected_unit) and xcom_unit:
            verified_listed_price = adapter.get('listed')
            unit_price = _price_per_requested_unit(
                dict(data, price_unit=xcom_unit, price_with_vat=verified_listed_price,
                     listed_price_with_vat=verified_listed_price), expected_unit, product_title)
        else:
            unit_price = None
        data["price_unit"] = xcom_unit
    elif host == "elektro.ru" and _sale_unit(expected_unit) == "м":
        elektro_offer = adapter.get('offer')
        unit_price = elektro_offer[0] if elektro_offer else None
        verified_listed_price = elektro_offer[2] if elektro_offer else None
    elif host == "dkc.market" and _sale_unit(expected_unit) in {"шт", "компл"}:
        dkc_market_offer = adapter.get('offer')
        unit_price = dkc_market_offer[0] if dkc_market_offer else None
        verified_listed_price = unit_price
    elif host == "rs24.ru":
        unit_price = adapter.get('unit_price')
    elif host == "chipdip.ru":
        unit_price = adapter.get('unit_prices',{}).get(_sale_unit(data.get('price_unit') or '')) or _price_per_requested_unit(data, expected_unit, product_title)
    elif host == "lanitnord.ru":
        unit_price = adapter.get('unit_prices',{}).get(_sale_unit(data.get('price_unit') or '')) or _price_per_requested_unit(data, expected_unit, product_title)
    elif host == "e-kc.ru":
        ekc_offer = adapter.get('offer')
        unit_price = ekc_offer[0] if ekc_offer else None
    elif host == "elektromehanika.ru":
        elektromehanika_offer = adapter.get('offer')
        unit_price = elektromehanika_offer[0] if elektromehanika_offer else None
    elif host == "etm.ru":
        etm_offer = adapter.get('offer')
        unit_price = etm_offer[0] if etm_offer else None
    elif host == "satro-paladin.com" and roll_length and _sale_unit(expected_unit) == "м":
        unit_price = adapter.get('unit_price')
    else:
        unit_price = _price_per_requested_unit(
            data, expected_unit, product_title
        )
    unit_review_reason = ""
    if unit_price is None:
        # Explicit printed sale denominator can distinguish a unit
        # quotation from the product title's package size.
        from ..source_quote import observe_captured_quote,quote_review_reason
        literal=observe_captured_quote(text,actual_url=url,observed_at=facts['captured_at'],url=url,title=main_title,article=identity_evidence.get('main_article') or main_card_article(text),listed=data.get('listed_price_with_vat'),normalized=data.get('price_with_vat'),sale_unit=data.get('price_unit'),requested_unit=expected_unit,data=data,expected_name=expected_name,expected_mark=expected_type_mark,owned_source=facts.get('http_source') or facts.get('rendered_source'))
        view=dict(data,url=url,product_title=main_title,identity_evidence=identity_evidence,source_quote_evidence=literal,normalized_price_unit=expected_unit,unit_review_required=False)
        if literal and not quote_review_reason(view,{'unit':expected_unit,'type_mark':expected_type_mark,'name':expected_name}):
            unit_price=literal['normalized_price'];verified_listed_price=literal['listed_price']
    if unit_price is None:
        unit_review_reason = (
            "Не подтверждено соответствие единицы продажи спецификации: "
            f"на сайте {data.get('price_unit') or 'не указана'}, требуется {expected_unit}")
        _reject_card(
            url, "price_or_unit_unconfirmed",
            "цена или единица продажи не подтверждена "
            f"(указано: {data.get('price_unit')!r}, "
            f"{data.get('listed_price_with_vat')!r}; "
            f"запрошено: {expected_unit!r})",
        )
        # Keep a real listed price as evidence, not as a converted quote.
        try:
            raw_price = float(data.get("listed_price_with_vat"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(raw_price) or raw_price <= 0:
            return None
        unit_price = raw_price
        verified_listed_price = raw_price
    if verified_listed_price is None:
        generic_price = _price_per_requested_unit(data, expected_unit, product_title)
        if (generic_price is not None and
                math.isclose(generic_price, unit_price, rel_tol=1e-6, abs_tol=0.02)
                and data.get("listed_price_with_vat") is not None):
            verified_listed_price = float(data["listed_price_with_vat"])
    # регион НЕ фильтруем жёстко: страница часто не пишет про самовывоз явно.
    regional = extracted_region(data)
    in_region = regional["in_moscow_region"]
    # Cached seller hints are cheap; remote enrichment must wait
    # for current price, unit, capture and late identity authority.
    req = wl_requisites_for(url) or Requisites()
    if (not req.name.strip() or not valid_inn(req.inn)) and facts.get('merchant_requisites'):
        req=Requisites.model_validate(facts['merchant_requisites'])
    from ..requisites.supplier import captured_seller_evidence, seller_evidence_requisites, seller_evidence_reason
    seller_evidence = captured_seller_evidence(facts)
    req = seller_evidence_requisites(seller_evidence, req)
    source_unit = (elektro_offer[1] if elektro_offer
                   else dkc_market_offer[1] if dkc_market_offer
                   else ekc_offer[1] if host == "e-kc.ru" and ekc_offer
                   else elektromehanika_offer[1]
                   if host == "elektromehanika.ru" and elektromehanika_offer
                   else etm_offer[1]
                   if host == "etm.ru" and etm_offer
                   else str(data.get("price_unit") or "не указана"))
    model_pending = _needs_model_review(
        expected_type_mark, product_title, url, title, page_article)
    if unit_review_reason:
        _log(f"  ? {url[:60]} → сохранена цена {unit_price:g} за {source_unit}; пересчёт не подтверждён")
    else:
        _log(f"  {'?' if model_pending else '✓'} {url[:60]} → цена {unit_price:g} за {expected_unit or 'ед.'} "
             f"(на карточке: {source_unit})")
    from copy import deepcopy
    from .session import observed_quote
    from ..source_quote import observe_captured_quote,observe_identity
    identity_evidence=facts['identity_evidence']
    if identity_evidence:observed_identity(identity_evidence)
    source_evidence=observe_captured_quote(text,actual_url=url,observed_at=facts['captured_at'],url=url,title=identity_evidence.get("main_title",main_title),article=identity_evidence.get('main_article') or main_card_article(text),listed=verified_listed_price or unit_price,normalized=unit_price,sale_unit=source_unit,requested_unit=expected_unit,data=data,expected_name=expected_name,expected_mark=expected_type_mark,owned_source=facts.get('http_source') or facts.get('rendered_source'))
    if not source_evidence and not unit_review_reason:unit_review_reason='Цена/единица карточки не подтверждены текстовыми сведениями'
    early_candidate=PriceOffer(price_with_vat=unit_price,url=url,product_title=product_title,page_title=title,page_article=page_article,identity_evidence=deepcopy(identity_evidence),price_unit=source_unit,normalized_price_unit="" if unit_review_reason else expected_unit,listed_price_with_vat=verified_listed_price or unit_price,unit_review_required=bool(unit_review_reason),unit_review_reason=unit_review_reason,match_review_required=model_pending,source_quote_evidence=source_evidence,requisites=req,seller_evidence=seller_evidence,**regional)
    observed_quote(deepcopy(early_candidate))
    shot=facts['screenshot_context']
    context_evidence=facts['context_capture_evidence'];context_reason=facts['context_capture_review_reason']
    early_candidate.screenshot_context=str(shot) if context_evidence else ""
    early_candidate.context_capture_evidence=context_evidence
    early_candidate.context_capture_review_reason=context_reason
    observed_quote(deepcopy(early_candidate))
    capture_evidence={};capture_reason='';late_identity=identity_evidence
    candidate = PriceOffer(
        price_with_vat=unit_price,
        url=url,
        product_title=product_title,
        page_title=title,
        page_article=page_article,
        identity_evidence=identity_evidence,
        price_unit=source_unit,
        normalized_price_unit="" if unit_review_reason else expected_unit,
        price_unit_evidence=unit_evidence,
        source_quote_evidence=source_evidence,
        price_capture_evidence={},
        price_capture_review_reason=capture_reason,
        unit_review_required=bool(unit_review_reason),
        unit_review_reason=unit_review_reason,
        match_review_required=model_pending,
        listed_price_with_vat=(unit_price if _sale_unit(source_unit)
                               == _sale_unit(expected_unit) else verified_listed_price),
        **regional,
        requisites=req,
        seller_evidence=seller_evidence,
        screenshot_product=str(shot) if context_evidence else None,
        screenshot_context=early_candidate.screenshot_context,context_capture_evidence=context_evidence,context_capture_review_reason=context_reason,
    )
    position = {'unit': expected_unit, 'type_mark': expected_type_mark,
                'name': expected_name}
    prerequisites = (not expected_type_mark or bool(late_identity)) and \
        offer_is_comparable(candidate, position, require_seller=False)
    if prerequisites:
        seller_reason = ""
        try:
            if enrich_seller and (not req.name.strip() or not valid_inn(req.inn)):
                inn = re.sub(r"\D", "", str(data.get("seller_inn") or ""))
                remote_req = fetch_requisites(inn) if valid_inn(inn) else None
                if (remote_req is None or not remote_req.name.strip()
                        or not valid_inn(remote_req.inn)):
                    try:remote_req = fetch_requisites_from_site(browser, url, capture_context=dict(product_url=url,captured_at=facts['captured_at']))
                    finally:browser._kac_raw_cohort=__import__('uuid').uuid4().hex
                if remote_req is not None:
                    req = remote_req
        except ResearchControlError as error:
            # A seller-stage deadline/global failure cannot erase
            # the current card observation or become a green quote.
            candidate.price_capture_review_reason = (
                f"Реквизиты продавца не подтверждены: {type(error).__name__}")
            _reject_card(url, "seller_unconfirmed", candidate.price_capture_review_reason)
            from .session import observed_quote
            observed_quote(candidate)
            if queue is not None:
                queue.mark(queue.active_url, 'pending')
            raise
        except Exception as error:
            seller_reason = f"Реквизиты продавца не подтверждены: {type(error).__name__}"
        if not req.name.strip() or not valid_inn(req.inn):
            seller_reason = seller_reason or "Реквизиты продавца не подтверждены"
        if seller_reason:
            _reject_card(url, "seller_unconfirmed", seller_reason)
            # Anonymous-host seller fallback must never admit this
            # reviewed candidate. Keep the current proof and pixels.
            candidate.price_capture_review_reason = seller_reason
        candidate.requisites = req
    seller_evidence = captured_seller_evidence(facts, req.seller_source)
    candidate.seller_evidence = seller_evidence
    candidate.requisites = seller_evidence_requisites(seller_evidence, req)
    seller_reason = seller_evidence_reason(seller_evidence)
    if seller_reason:
        candidate.price_capture_review_reason = seller_reason
    if str(data.get("city") or "").strip():
        candidate.requisites.city = str(data["city"])
    return candidate


def _collect_offers(browser, urls: list[str], want: int,
                    expected_type_mark: str = "", expected_name: str = "",
                    expected_unit: str = "",
                    supplier_hosts: set[str] | None = None,
                    deadline: float | None = None,
                    budget_phase: str = "", budget_seconds: int = 0,
                    expected_qty: float = 0,
                    existing_offers: list[PriceOffer] | None = None,
                    ) -> tuple[list[PriceOffer], int]:
    """Обходит список URL, извлекает цену/реквизиты, возвращает (офферы, обойдено)."""
    offers: list[PriceOffer] = []
    existing_offers = existing_offers or []
    initial_sources = source_count(existing_offers)
    checked = 0
    successful_hosts: set[str] = set()
    blocked_hosts: set[str] = set()
    required_brand = _verified_manufacturer_brand(expected_type_mark)
    queue = current_queue()
    if queue is not None:
        queue.add(urls)
    for url in urls:
        capture_ref=None
        if source_count(existing_offers + offers) - initial_sources >= want:
            break
        if deadline is not None and time.monotonic() >= deadline:
            _record_search_budget(budget_phase or "поиск", budget_seconds)
            break
        requested_url = url
        from .card_batch import replay_for
        replay_value=replay_for(requested_url)
        if replay_value is not None:url=replay_value[0].facts()['url']
        if queue is not None:
            queue.active_url = url
        supplier_host = _canonical_host(url, supplier_hosts)
        if supplier_host in successful_hosts or supplier_host in blocked_hosts:
            continue
        if replay_value is None and _canonical_host(url) == "elektromehanika.ru":
            try:
                with supplier_transport(url):
                    resolved = _resolve_elektromehanika_listing(url, expected_type_mark)
            except SearchBudgetExceeded:
                _record_search_budget(budget_phase or "поиск", budget_seconds)
                break
            except SupplierDeferred as exc:
                diagnostics = _CARD_DIAGNOSTICS.get()
                if diagnostics is not None:
                    diagnostics.append({**exc.check, 'url':url, 'code':exc.check['reason_code']})
                continue
            if not resolved and urlparse(url).path.casefold() == "/items.php":
                _reject_card(url, "card_not_found", "точная карточка в разделе не найдена", skipped=True)
                continue
            if resolved and resolved != url:
                _log(f"  ↪ {url[:60]} → карточка {resolved[:75]}")
                url = resolved
        if source_type(_canonical_host(url)) != "direct_supplier":
            _reject_card(url, "not_direct_supplier", "не прямой поставщик, пропуск", skipped=True)
            continue
        if _is_listing_url(url):
            _reject_card(url, "listing", "каталог с разными товарами, пропуск", skipped=True)
            continue
        # A contradictory model code in the URL cannot be repaired by the
        # page's price parser; avoid opening and charging for that card.
        different_skus = _different_full_skus_in_url(expected_type_mark, url)
        if (_type_mark_match_status(expected_type_mark, url) is False
                or different_skus):
            reason = (f"в адресе указан {', '.join(different_skus[:3])} вместо "
                      f"{expected_type_mark.strip()}; сверьте артикул по оригиналу"
                      if different_skus else "артикул в адресе противоречит спецификации")
            _reject_card(url, "article_mismatch", reason,
                         observed_article=different_skus[0] if different_skus else "")
            continue
        from .card_batch import replay_for
        replaying=replay_for(requested_url) is not None
        if not replaying and supplier_paused(url):
            _reject_card(url, "supplier_paused",
                         "сайт временно пропущен после повторных сбоев в этом прогоне; "
                         "повторите поиск позже", skipped=True)
            continue
        checked += 1
        _log(f"  → проверка {url[:120]}")
        try:
            from .card_batch import replay_for,pending_enabled,CardCapturePending,extract_batch
            replay=replay_for(requested_url)
            if replay is not None:
                snapshot,data,error=replay
                capture_ref=snapshot.retain(CACHE_DIR)
                if error is not None:raise error
            else:
                snapshot=_capture_card(browser,url,requested_url,supplier_host,expected_type_mark,expected_name,expected_unit,expected_qty,required_brand,blocked_hosts,deadline)
                if snapshot is None:continue
                capture_ref=snapshot.retain(CACHE_DIR)
                diagnostics=_CARD_DIAGNOSTICS.get()
                if diagnostics is not None:diagnostics.append({'url':url,'code':'capture_snapshot_observation','reason':'Снимок текущих исходных данных сохранён до извлечения','capture_snapshot':capture_ref})
                data=_deterministic_card_data(snapshot)
                if data is None:
                    if pending_enabled():raise CardCapturePending(snapshot)
                    with deadline_scope(snapshot.deadline):
                        data,error=extract_batch([snapshot])[snapshot.card_id]
                    if error is not None:raise error
            with deadline_scope(snapshot.deadline):
                from .session import bounded_timeout
                bounded_timeout(1)
                candidate=_finish_captured_card(browser,snapshot,data)
                bounded_timeout(1)
            if candidate is None:continue
            url=snapshot.facts()['url']
            regional={'region_evidence':candidate.region_evidence}
            offers.append(candidate)
            comparable = offer_is_comparable(offers[-1], {'unit': expected_unit,
                'type_mark': expected_type_mark, 'name': expected_name})
            if region_status(offers[-1]) == "incompatible":
                _reject_card(url, "region_incompatible", regional["region_evidence"])
            if comparable:
                successful_hosts.add(supplier_host)
            if queue is not None:
                queue.mark(queue.active_url, 'accepted' if comparable else 'pending')
            from .session import observed_quote
            if not comparable:
                observed_quote(offers[-1])
            if comparable:
                accepted(offers[-1])
        except Exception as e:
            if isinstance(e, ResearchProgressError):
                raise
            if isinstance(e, (ModelOutputInvalid,LLMOperationFailed,SearchQueryFailed)):
                diagnostics = _CARD_DIAGNOSTICS.get()
                if diagnostics is not None:
                    diagnostics.append({**e.diagnostic(), 'url':url,**({'capture_snapshot':capture_ref} if capture_ref else {})})
                if queue is not None:
                    queue.mark(queue.active_url, 'pending')
                continue
            if isinstance(e, SupplierDeferred):
                checked -= 1
                diagnostics = _CARD_DIAGNOSTICS.get()
                if diagnostics is not None:
                    diagnostics.append({**e.check, 'url': url, 'code': e.check['reason_code']})
                continue
            if isinstance(e, SupplierProfileBusy):
                checked -= 1
                _reject_card(url, 'profile_busy', 'Проверка отложена: профиль браузера занят другим процессом')
                continue
            if isinstance(e, (LLMUnavailable, SearchServiceUnavailable)):
                diagnostic = e.diagnostic()
                diagnostics = _CARD_DIAGNOSTICS.get()
                if diagnostics is not None:
                    diagnostics.append(diagnostic)
                break
            if isinstance(e, SearchBudgetExceeded) or (deadline is not None and time.monotonic() >= deadline):
                _record_search_budget(budget_phase or "первый проход", budget_seconds)
                break
            if isinstance(e, ResearchControlError):
                raise
            context = getattr(e, '_kac_supplier_failure', None)
            if not isinstance(context,FailureContext) or context.source not in {'supplier_http','supplier_navigation'}:
                from .session import internal_failure
                internal_failure('collector',e,url=url)
            _reject_card(url, "site_error", f"ошибка: {type(e).__name__}")
            diagnostics = _CARD_DIAGNOSTICS.get()
            if context is not None and diagnostics:
                diagnostics[-1].update(context.diagnostic(diagnostics[-1]['reason'], code='supplier_local_error'))
                diagnostics[-1]['url'] = url
            continue
    if queue is not None:
        queue.active_url = None
    return offers, checked


def _validated_retained_offers(pos: SpecPosition, existing_offers: list[PriceOffer]) -> list[PriceOffer]:
    offers = []
    for offer in existing_offers:
        offer.match_review_required = offer.match_review_required or bool(offer.product_title and _needs_model_review(
            pos.type_mark, offer.product_title, offer.url, offer.page_title, offer.page_article))
        from ..price_capture import unit_capture_review_reason
        if not unit_capture_review_reason(offer, pos):
            offers.append(offer)
        else:
            offer.unit_review_required = True
            offer.unit_review_reason = "Сохранённая цена требует подтверждения единицы продажи"
            offer.normalized_price_unit = ""
            offers.append(offer)
            _reject_card(offer.url, "price_or_unit_unconfirmed",
                         "сохранённая цена требует подтверждения единицы продажи")
    return offers


def find_offers_for_position(browser, pos: SpecPosition,
                             existing_offers: list[PriceOffer] | None = None
                             ) -> tuple[list[PriceOffer], int]:
    """One agreed supplier list, literal search then semantic expansion within one budget."""
    want = settings.min_sources
    offers = _validated_retained_offers(pos, existing_offers or [])
    checked = 0
    from .session import batch_card_urls, exhaustive_cards
    supplied = batch_card_urls()
    if supplied is not None:
        # The job coordinator owns discovery and the common deadline. Do not
        # pull other saved URLs here: each slice is assigned exactly once.
        if supplied and (exhaustive_cards() or source_count(offers) < want):
            fresh, checked = _collect_offers(
                browser, supplied, len(supplied)+source_count(offers) if exhaustive_cards() else want-source_count(offers), pos.type_mark,
                pos.name, pos.unit, deadline=research_deadline(),
                budget_phase="общее задание", budget_seconds=settings.research_job_seconds,
                expected_qty=pos.qty, existing_offers=offers)
            # A lower fresh price must replace the old quote before price sorting.
            by_url = {offer.url: offer for offer in offers}
            by_url.update({offer.url: offer for offer in fresh})
            offers = list(by_url.values())
        return offers, checked
    if source_count(offers) >= want:
        return offers, checked
    started = time.monotonic()
    total_budget = max(1, settings.position_search_seconds)
    total_deadline = started + total_budget
    allocation = research_deadline()
    if allocation is not None:
        total_deadline = min(total_deadline, allocation)
        total_budget = max(1, int(total_deadline - started))
    queue = current_queue()
    if queue is not None:
        used_hosts = {_canonical_host(o.url) for o in offers}
        saved = [u for u in queue.urls() if _canonical_host(u) not in used_hosts]
        saved = _candidate_urls_by_host(saved, expected_mark=pos.type_mark)
        if saved:
            _log(f"продолжение очереди: {len(saved)} ранее найденных карточек")
            fresh, visited = _collect_offers(browser, saved, want-source_count(offers),
                pos.type_mark, pos.name, pos.unit, deadline=total_deadline,
                budget_phase="сохранённые карточки", budget_seconds=total_budget, expected_qty=pos.qty,
                existing_offers=offers)
            offers.extend(fresh)
            checked += visited
            if source_count(offers) >= want or time.monotonic() >= total_deadline:
                return offers, checked
    queries = build_queries(pos, semantic=False)
    _log(f"позиция «{pos.name[:50]}» → запросы: {queries}")
    # выдачу берём с запасом: после фильтров (маркетплейсы, дубли, нет цены, капча) отсев большой
    per_query = max(20, settings.min_sources * 4)

    # Every approved domain participates in the same literal-first phase.
    hosts = whitelist_hosts()
    fresh, visited, discovered = _search_phase(
        browser, pos, queries, hosts, offers, total_deadline,
        "единый список", total_budget, per_query,
    )
    offers += fresh
    checked += visited
    # Literal recovery belongs to the same full list and common deadline.
    host_set = set(hosts)
    used_hosts = {_canonical_host(o.url, host_set) for o in offers}
    brand_hosts = [h for h in _brand_direct_hosts(pos.type_mark, hosts)
                   if h not in used_hosts]
    if source_count(offers) < want and brand_hosts and time.monotonic() < total_deadline:
        brand_urls = _search_queries_on_sites(
            [f"{pos.name} {pos.type_mark}"], brand_hosts, 10,
            "сайт производителя", deadline=total_deadline)
        brand_urls = _candidate_urls_by_host(brand_urls, skip_hosts=used_hosts,
            supplier_hosts=host_set, expected_mark=pos.type_mark)
        fresh, visited = _collect_offers(browser, brand_urls, want-source_count(offers),
            pos.type_mark, pos.name, pos.unit, supplier_hosts=host_set,
            deadline=total_deadline, budget_phase="единый список", budget_seconds=total_budget,
            expected_qty=pos.qty, existing_offers=offers)
        offers += fresh
        checked += visited
        discovered += brand_urls
    if source_count(offers) < want and queries and time.monotonic() < total_deadline:
        targeted = _search_unrepresented_hosts(queries[0], hosts,
            discovered + [o.url for o in offers], deadline=total_deadline)
        targeted = _candidate_urls_by_host(targeted,
            skip_hosts={_canonical_host(o.url, host_set) for o in offers},
            supplier_hosts=host_set, expected_mark=pos.type_mark)
        fresh, visited = _collect_offers(browser, targeted, want-source_count(offers),
            pos.type_mark, pos.name, pos.unit, supplier_hosts=host_set,
            deadline=total_deadline, budget_phase="единый список", budget_seconds=total_budget,
            expected_qty=pos.qty, existing_offers=offers)
        offers += fresh
        checked += visited
    _record_phase_timing("primary", time.monotonic() - started)
    if (source_count(offers) < want and hosts
            and total_deadline - time.monotonic() >= 10):
        literal_keys = {q.casefold() for q in queries}
        expanded = [q for q in build_queries(pos) if q.casefold() not in literal_keys]
        if expanded:
            fresh, visited, _ = _search_phase(
                browser, pos, expanded, hosts, offers, total_deadline,
                "единый список: расширение", total_budget, per_query)
            offers += fresh
            checked += visited

    if source_count(offers) < want and time.monotonic() >= total_deadline:
        _record_search_budget("весь поиск", total_budget)

    _log(f"итог по позиции: офферов {source_count(offers)} из {checked} обойдённых")
    return offers, checked


def _factory_request_entry(function):
    from functools import wraps
    @wraps(function)
    def scoped(browser,pos,*args,**kwargs):
        from .factory_reference import request_scope
        with request_scope(pos):return function(browser,pos,*args,**kwargs)
    return scoped

@_factory_request_entry
def research_position(browser, pos: SpecPosition, year: str, quarter: str,
                      existing_offers: list[PriceOffer] | None = None) -> PositionResult:
    """Полный цикл по одной позиции: поиск → отбор TOP максимальных по возрастанию.

    В all_offers сохраняются ВСЕ найденные кандидаты (для замены позиции в топ-3 в UI).
    """
    offers, checked = find_offers_for_position(browser, pos, existing_offers)
    offers.sort(key=lambda o: o.price_with_vat)  # по возрастанию
    top = select_top_offers(offers, settings.top_prices, pos)
    return PositionResult(position=pos, offers=top, all_offers=offers, year=year, quarter=quarter,
                          sources_checked=checked, sources_found=source_count(offers, pos),
                          sources_target=settings.min_sources)
