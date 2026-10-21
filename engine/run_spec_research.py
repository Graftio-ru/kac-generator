"""Resume a real supplier search for an exported estimator draft.

Run from the repository root: python -m engine.run_spec_research --draft FILE --report FILE
The report is local and contains no source images or credentials.
"""
from __future__ import annotations

from .offer_selection import select_top_offers, source_count

import argparse
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .app import _offer_to_dict, _research_row
from .models import PositionResult, PriceOffer, Requisites, SpecPosition
from .schemas import PositionIn
from .search import base_db
from .config import CACHE_DIR, settings


def _save(path: Path, report: dict) -> None:
    from .coverage import annotate_coverage
    for row in report.get('rows', []):
        if row is not None:
            annotate_coverage(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def reconcile_cache(report: dict, positions: list[PositionIn]) -> int:
    """Restore only exact, validated cached quotes lost in a weaker live retry."""
    today = date.today()
    year, quarter = str(today.year), str((today.month - 1) // 3 + 1)
    restored = 0
    for index, p in enumerate(positions):
        row = report["rows"][index]
        if not row or p.is_unique or p.needs_review or row["found"] >= settings.min_sources:
            continue
        pos = SpecPosition(number=p.number, name=p.name, type_mark=p.type_mark,
                           unit=p.unit, qty=p.qty, discipline=p.discipline)
        cached = base_db.lookup(pos, year, quarter, exact_only=True)
        if cached is None or cached.sources_found <= row["found"]:
            continue
        row["offers"] = [_offer_to_dict(o, row.get("type_mark", "")) for o in cached.offers]
        row["candidates"] = [_offer_to_dict(o, row.get("type_mark", "")) for o in (cached.all_offers or cached.offers)]
        row["found"] = cached.sources_found
        row["target"] = cached.sources_target or settings.min_sources
        row["from_base"] = True
        row["status"] = "complete" if row["found"] >= row["target"] else "partial"
        row["reason"] = ("Свежий поиск не улучшил результат; сохранено "
                         f"{row['found']} из {row['target']} ранее проверенных источников")
        restored += 1
    return restored


def repair_missing_candidates(report: dict, positions: list[PositionIn]) -> list[int]:
    """Restore rows whose quote list was lost while a saved count remained."""
    today = date.today()
    year, quarter = str(today.year), str((today.month - 1) // 3 + 1)
    repaired = []
    for index, position in enumerate(positions):
        row = report["rows"][index]
        if not row or row.get("status") == "skipped" or not row.get("found"):
            continue
        if row.get("candidates") or row.get("offers"):
            continue
        pos = SpecPosition(number=position.number, name=position.name,
                           type_mark=position.type_mark, unit=position.unit,
                           qty=position.qty, discipline=position.discipline)
        cached = base_db.lookup(pos, year, quarter, exact_only=True)
        if not cached or not (cached.all_offers or cached.offers):
            raise ValueError(f"No exact cached quotes for position {position.number}")
        candidates = [_offer_to_dict(offer, position.type_mark)
                      for offer in (cached.all_offers or cached.offers)]
        row["candidates"] = candidates
        row["offers"] = select_top_offers(candidates, settings.top_prices)
        row["found"] = source_count(candidates)
        row["status"] = ("complete" if row["found"] >= row["target"]
                         else "partial")
        row["reason"] = (f"Восстановлено {row['found']} подтверждённых источников "
                         "из точного локального кэша")
        repaired.append(position.number)
    return repaired


def _offer_source(offer: dict) -> str:
    inn = str(offer.get("inn") or "").strip()
    if inn:
        return f"inn:{inn}"
    host = (urlparse(str(offer.get("url") or "")).hostname or "").lower().removeprefix("www.")
    return f"host:{host}" if host else f"url:{offer.get('url', '')}"


def _position_model(position: PositionIn) -> SpecPosition:
    return SpecPosition(number=position.number, name=position.name,
                        type_mark=position.type_mark, unit=position.unit,
                        qty=position.qty, discipline=position.discipline)


def sync_cache_row(row: dict, position: PositionIn) -> bool:
    """Copy reviewed report quotes into the exact cache without OCR assumptions."""
    if row.get("status") == "skipped" or row.get("provisional") or position.needs_review:
        return False
    pos = _position_model(position)
    today = date.today()
    cached = base_db.lookup(pos, str(today.year),
                            str((today.month - 1) // 3 + 1), exact_only=True)
    old = {offer.url: offer for offer in (cached.all_offers if cached else [])}
    candidates = row.get("candidates") or row.get("offers") or []
    fields = ("product_title", "page_title", "page_article", "price_unit", "price_unit_evidence",
              "normalized_price_unit", "unit_review_reason", "source_quote_evidence", "identity_evidence", "price_capture_review_reason", "price_capture_evidence")
    def evidence(offer) -> tuple:
        value = offer.get if isinstance(offer, dict) else lambda key: getattr(offer, key, None)
        listed = value("listed_price_with_vat")
        return (value("url"), round(float(value("price_with_vat")), 2),
                *(str(value(field) or "") for field in fields),
                *(bool(value(field)) for field in ("match_review_required", "model_review_accepted", "unit_review_required")),
                value("in_moscow_region"), str(value("region_status") or ""),
                str(value("region_evidence") or ""),
                round(float(listed), 2) if listed is not None else None)
    before = {evidence(offer) for offer in old.values()}
    after = {evidence(offer) for offer in candidates}
    if before == after:
        return False
    offers = []
    for offer in candidates:
        current = old.get(offer["url"])
        if current and evidence(current) == evidence(offer):
            offers.append(current)
            continue
        requisites = (current.requisites.model_copy() if current else Requisites())
        requisites.name = str(offer.get("org_name") or requisites.name)
        requisites.inn = str(offer.get("inn") or requisites.inn)
        requisites.kpp = str(offer.get("kpp") or requisites.kpp)
        requisites.city = str(offer.get("city") or requisites.city)
        screenshot = offer.get("screenshot_product")
        offers.append(PriceOffer(
            price_with_vat=offer["price_with_vat"], url=offer["url"],
            product_title=offer.get("product_title") or "",
            page_title=offer.get("page_title") or "",
            page_article=offer.get("page_article") or "",
            price_unit=offer.get("price_unit") or "",
            listed_price_with_vat=offer.get("listed_price_with_vat"),
            price_unit_evidence=offer.get("price_unit_evidence", ""),
            source_quote_evidence=offer.get("source_quote_evidence") or {},
            identity_evidence=offer.get("identity_evidence") or {},
            screenshot_context=offer.get("screenshot_context") or "",
            context_capture_evidence=offer.get("context_capture_evidence") or {},
            context_capture_review_reason=offer.get("context_capture_review_reason") or "",
            price_capture_evidence=offer.get("price_capture_evidence") or {},
            price_capture_review_reason=offer.get("price_capture_review_reason") or "",
            normalized_price_unit=offer.get("normalized_price_unit", ""),
            match_review_required=bool(offer.get("match_review_required")),
            model_review_accepted=bool(offer.get("model_review_accepted")),
            unit_review_required=bool(offer.get("unit_review_required")),
            unit_review_reason=offer.get("unit_review_reason", ""),
            in_moscow_region=offer.get("in_moscow_region"),
            region_status=offer.get("region_status") or "",
            region_evidence=offer.get("region_evidence") or "",
            requisites=requisites,
            screenshot_product=str(CACHE_DIR / Path(screenshot).name) if screenshot else None,
            from_letter=bool(offer.get("from_letter")),
        ))
    result = PositionResult(position=pos, offers=select_top_offers(offers, settings.top_prices),
                            all_offers=offers, sources_checked=row.get("new_cards_checked") or 0)
    stored = base_db.replace_exact(pos, result)
    if stored != source_count(candidates):
        raise ValueError(f"Position {position.number}: cache rejected {len(candidates) - stored} report quotes")
    return True


def sync_report_cache(report: dict, positions: list[PositionIn]) -> list[int]:
    """Sync only rows whose reviewed quotes differ from the reusable cache."""
    changed = []
    for row, position in zip(report["rows"], positions):
        if row and sync_cache_row(row, position):
            changed.append(position.number)
    return changed


def revalidate_offers(report: dict) -> int:
    """Remove mismatched quotes and correct diagnostics derivable from stored URLs."""
    from .search.agent import (_needs_model_review, _conflicting_explicit_article,
                               _conflicting_full_model_code, _conflicting_model_code_in_title,
                               _conflicting_network_cable_spec,
                               _conflicting_page_article,
                               _conflicting_numeric_article, _is_listing_url,
                               _saved_price_unit_is_verified, _conflicting_full_sku_in_url)
    from .search.agent import _satro_minimum_order_conflict

    removed = 0
    for row in report["rows"]:
        if not row or row.get("status") == "skipped":
            continue
        for diagnostic in row.get("diagnostics", []):
            if (diagnostic.get("code") == "price_or_unit_unconfirmed"
                    and _is_listing_url(diagnostic.get("url", ""))):
                diagnostic["code"] = "listing"
                diagnostic["reason"] = "страница каталога с несколькими товарами, не отдельная карточка"
        candidates = row.get("candidates") or row.get("offers") or []
        valid = []
        rejected = []
        for offer in candidates:
            mark = row.get("type_mark", "")
            title = offer.get("product_title", "")
            page_title = offer.get("page_title", "")
            page_article = offer.get("page_article", "")
            url = offer.get("url", "")
            if _is_listing_url(url):
                rejected.append((offer, "listing"))
            elif _satro_minimum_order_conflict(
                    url, title, row.get("unit", ""), float(row.get("qty") or 0)):
                rejected.append((offer, "minimum_order"))
            elif _conflicting_full_sku_in_url(mark, url):
                rejected.append((offer, "article_mismatch"))
            elif not offer.get("unit_review_required") and not _saved_price_unit_is_verified(
                    row.get("unit", ""), offer.get("price_unit", ""),
                    offer.get("listed_price_with_vat"), offer.get("price_with_vat"),
                    title, url, offer.get("price_unit_evidence", "")):
                rejected.append((offer, "unit_unverified"))
            elif _conflicting_page_article(mark, page_article):
                rejected.append((offer, "page_article_mismatch"))
            elif (_conflicting_model_code_in_title(mark, title)
                    or _conflicting_network_cable_spec(row.get("name", ""), mark,
                                                       title + " " + page_title)
                    or _conflicting_explicit_article(mark, title, url)
                    or _conflicting_numeric_article(mark, title)
                    or _conflicting_numeric_article(mark, page_title)
                    or _conflicting_full_model_code(mark, title)
                    or _conflicting_full_model_code(mark, page_title)):
                rejected.append((offer, "article_mismatch"))
            else:
                offer["match_review_required"] = bool(title and _needs_model_review(
                    mark, title, url, page_title, page_article))
                offer["model_review_accepted"] = False
                valid.append(offer)
        if rejected:
            removed += len(rejected)
            valid.sort(key=lambda offer: float(offer["price_with_vat"]))
            row["candidates"] = valid
            row["offers"] = select_top_offers(valid, settings.top_prices)
            row["found"] = source_count(valid)
            row["status"] = ("complete" if row["found"] >= row["target"] else
                             "partial" if row["found"] else "failed")
            row["reason"] = (f"Найдено {row['found']} из {row['target']} подтверждённых источников; "
                             f"исключено неподходящих предложений: {len(rejected)}")
            reasons = {
                "article_mismatch": "сохранённое предложение относится к другой модели",
                "page_article_mismatch": "артикул основного товара на карточке не совпадает со спецификацией",
                "listing": "страница каталога с несколькими товарами, не отдельная карточка",
                "unit_unverified": "единица продажи или пересчёт цены не подтверждены",
                "minimum_order": "минимальная кратность заказа превышает требуемое количество",
            }
            row.setdefault("diagnostics", []).extend({
                "url": offer.get("url", ""), "code": code,
                "reason": (f"артикул карточки {offer.get('page_article')} не совпадает "
                           f"с указанным {row.get('type_mark')}"
                           if code == "page_article_mismatch" else reasons[code]),
            } for offer, code in rejected)
        for offer in row.get("offers", []):
            offer["match_review_required"] = bool(offer.get("product_title") and
                _needs_model_review(row.get("type_mark", ""),
                                    offer.get("product_title", ""), offer.get("url", ""),
                                    offer.get("page_title", ""), offer.get("page_article", "")))
            offer["model_review_accepted"] = False
        row["match_review_count"] = sum(
            _needs_model_review(row.get("type_mark", ""),
                                offer.get("product_title", ""), offer.get("url", ""),
                                offer.get("page_title", ""), offer.get("page_article", ""))
            for offer in valid
        )
        reason_parts = [part for part in row.get("reason", "").split("; ")
                        if not (part.startswith("у ") and
                                "предложений марка не подтверждена" in part)]
        if row["match_review_count"]:
            reason_parts.append(
                f"у {row['match_review_count']} предложений марка не подтверждена "
                "точным совпадением — проверьте карточки"
            )
        if row.get("provisional") and not any(
                "исходный артикул" in part for part in reason_parts):
            reason_parts.append("исходный артикул на изображении требует сверки")
        row["reason"] = "; ".join(part for part in reason_parts if part)
    return removed


def enrich_offer_page_titles(report: dict, browser, *, host: str = "",
                             limit: int = 0, on_progress=None) -> tuple[int, int]:
    """Recheck saved cards' page titles without repeating supplier search or LLM calls."""
    from .search.agent import (_printed_page_article, _looks_like_bot_check,
                               _needs_model_review)

    def note_failed_check(row: dict, url: str, code: str, reason: str) -> None:
        diagnostics = row.setdefault("diagnostics", [])
        diagnostics[:] = [item for item in diagnostics if not (
            item.get("url") == url and item.get("stage") == "article_evidence_check")]
        diagnostics.append({"url": url, "stage": "article_evidence_check",
                            "code": code, "reason": reason})
        if on_progress is not None:
            on_progress(report)

    checked = cleared = 0
    for row in report["rows"]:
        if not row or row.get("status") == "skipped":
            continue
        candidates = row.get("candidates") or row.get("offers") or []
        for offer in list(candidates):
            url = offer.get("url", "")
            if not url or not offer.get("match_review_required"):
                continue
            if host and (urlparse(url).hostname or "").lower().removeprefix("www.") != host:
                continue
            if limit and checked >= limit:
                revalidate_offers(report)
                return checked, cleared
            checked += 1
            try:
                with browser.page(url) as page:
                    page.wait_for_timeout(1200)
                    title = page.title().strip()
                    actual = urlparse(page.url)
                    original = urlparse(url)
                    if _looks_like_bot_check("", title):
                        note_failed_check(row, url, "captcha",
                                          "при повторной проверке открылась антибот-страница")
                        continue
                    if (not title or actual.hostname != original.hostname
                            or actual.path.rstrip("/") != original.path.rstrip("/")):
                        note_failed_check(row, url, "unusable_page",
                                          "карточка перенаправила запрос или не дала заголовок товара")
                        continue
                    visible_text = page.inner_text("body")
                    if _looks_like_bot_check(visible_text[:500], title):
                        note_failed_check(row, url, "captcha",
                                          "при повторной проверке открылась антибот-страница")
                        continue
                    supplier_host = (original.hostname or "").lower().removeprefix("www.")
                    article = (_printed_page_article(
                        visible_text, bracket_heading=supplier_host == "lanmark.ru")
                               if supplier_host in {"lanitnord.ru", "elevel.ru", "lanmark.ru"}
                               or supplier_host.endswith(".lanitnord.ru") else "")
            except Exception as exc:
                note_failed_check(row, url, "site_error",
                                  f"повторная загрузка карточки не удалась: {type(exc).__name__}")
                continue
            row["diagnostics"] = [item for item in row.get("diagnostics", []) if not (
                item.get("url") == url and item.get("stage") == "article_evidence_check")]
            offer["page_title"] = title
            if article:
                offer["page_article"] = article
            for selected in row.get("offers", []):
                if selected.get("url") == url:
                    selected["page_title"] = title
                    if article:
                        selected["page_article"] = article
            base_db.record_page_title(url, title)
            if article:
                base_db.record_page_article(url, article)
            if not _needs_model_review(row.get("type_mark", ""),
                                       offer.get("product_title", ""), url, title,
                                       article):
                cleared += 1
            if on_progress is not None:
                revalidate_offers(report)
                on_progress(report)
    revalidate_offers(report)
    return checked, cleared


def merge_retry_result(previous: dict, fresh: dict) -> dict:
    """Keep validated quotes from the same draft when a live retry is weaker."""
    old_offers = previous.get("candidates") or previous.get("offers") or []
    new_offers = fresh.get("candidates") or fresh.get("offers") or []
    by_source = {_offer_source(offer): offer for offer in old_offers}
    by_source.update({_offer_source(offer): offer for offer in new_offers})
    candidates = sorted(by_source.values(), key=lambda offer: float(offer["price_with_vat"]))
    fresh["candidates"] = candidates
    fresh["offers"] = select_top_offers(candidates, settings.top_prices)
    fresh["found"] = source_count(candidates)
    fresh["target"] = settings.min_sources
    fresh["preserved_prior_offers"] = sum(
        _offer_source(offer) not in {_offer_source(item) for item in new_offers}
        for offer in old_offers
    )
    if fresh["found"] >= fresh["target"]:
        fresh["status"] = "complete"
    elif fresh["found"]:
        fresh["status"] = "partial"
    else:
        fresh["status"] = "failed"
    qualifiers = [part for part in fresh.get("reason", "").split("; ")
                  if part.startswith(("причины отклонения", "сверьте распознавание",
                                      "не указано количество", "этап «"))]
    if fresh["status"] == "complete":
        fresh["reason"] = "; ".join(qualifiers)
    else:
        base = (f"Найдено {fresh['found']} из {fresh['target']} подтверждённых источников"
                if fresh["found"] else "Подтверждённых цен не найдено")
        fresh["reason"] = "; ".join([base, *qualifiers])
    revalidate_offers({"rows": [fresh]})
    return fresh


def retry_incomplete(report: dict, positions: list[PositionIn], path: Path,
                     workers: int, numbers: set[int] | None = None) -> None:
    """Refresh only unfinished rows, saving each result without losing old offers."""
    pending = report.get("retry_pending")
    if not pending:
        pending = [index for index, row in enumerate(report["rows"])
                   if row and row["status"] in {"partial", "failed"}
                   and row["found"] < settings.min_sources
                   and (numbers is None or positions[index].number in numbers)]
    pending = list(pending)
    report["retry_pending"] = pending
    report["retry_total"] = len(pending)
    report["retry_completed"] = 0
    report["status"] = "running"
    _save(path, report)
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_research_row, positions[index]): index
                       for index in pending}
            for future in as_completed(futures):
                index = futures[future]
                fresh = future.result()
                fresh["index"] = index
                report["rows"][index] = merge_retry_result(report["rows"][index], fresh)
                report["retry_pending"].remove(index)
                report["retry_completed"] += 1
                report["updated_at"] = datetime.now(timezone.utc).isoformat()
                _save(path, report)
                row = report["rows"][index]
                print(f"[{report['retry_completed']}/{report['retry_total']}] "
                      f"№{row['number']} {row['status']} {row['found']}/{row['target']}", flush=True)
    except BaseException:
        report["status"] = "interrupted"
        _save(path, report)
        raise
    attach_report(report,positions)
    report["status"] = "done"
    _save(path, report)


def recheck_card(report: dict, positions: list[PositionIn], path: Path,
                 number: int, url: str) -> tuple[int, int]:
    """Recheck one known product card without repeating broad supplier search."""
    from .browser import Browser
    from .search.agent import _collect_offers, capture_card_diagnostics

    matches = [index for index, position in enumerate(positions)
               if position.number == number]
    if len(matches) != 1:
        raise ValueError(f"Specification position {number} is not unique or missing")
    index = matches[0]
    row = report["rows"][index]
    if not row or row["status"] == "skipped":
        raise ValueError(f"Specification position {number} cannot be rechecked")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Card URL must be an HTTPS product page")
    previous_found = row["found"]
    retained = [offer for offer in (row.get("candidates") or row.get("offers") or [])
                if offer.get("url") != url]

    with capture_card_diagnostics() as diagnostics:
        with Browser() as browser:
            offers, checked = _collect_offers(
                browser, [url], 1, expected_type_mark=positions[index].type_mark,
                expected_name=positions[index].name, expected_unit=positions[index].unit,
                expected_qty=positions[index].qty,
            )
    fresh = dict(row)
    fresh["candidates"] = retained
    fresh["offers"] = select_top_offers(retained, settings.top_prices)
    fresh["found"] = source_count(retained)
    fresh["diagnostics"] = [item for item in row.get("diagnostics", [])
                            if item.get("url") != url] + diagnostics
    fresh["new_cards_checked"] = row.get("new_cards_checked", 0) + checked
    if offers:
        newly_verified = dict(fresh)
        newly_verified["candidates"] = [_offer_to_dict(offer, row.get("type_mark", ""))
                                        for offer in offers]
        newly_verified["offers"] = newly_verified["candidates"]
        newly_verified["reason"] = ""
        report["rows"][index] = merge_retry_result(fresh, newly_verified)
    else:
        fresh["status"] = ("complete" if fresh["found"] >= fresh["target"] else
                           "partial" if fresh["found"] else "failed")
        fresh["reason"] = (f"Найдено {fresh['found']} из {fresh['target']} подтверждённых источников"
                           if fresh["found"] else "Подтверждённых цен не найдено")
        report["rows"][index] = fresh
    updated = report["rows"][index]
    if updated["found"] != source_count(updated.get("candidates") or []):
        raise ValueError(f"Position {number} has an inconsistent saved source count")
    report["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save(path, report)
    return previous_found, report["rows"][index]["found"]


def attach_report(report,positions):
    """CLI uses the same finite attachment/price-refresh route as normal jobs."""
    from .browser import Browser
    from .search.card_attachments import attach_row
    from .search.session import research_session,PersistentSupplierHealth,research_deadline
    from .llm import LLMRequestGuard,_REQUEST_GUARD
    with Browser() as browser,research_session(PersistentSupplierHealth(),deadline=research_deadline(),llm_guard=_REQUEST_GUARD.get() or LLMRequestGuard()):
        for index,position in enumerate(positions):
            row=report['rows'][index]
            if row and row.get('status')!='skipped' and (row.get('candidates') or row.get('offers')):
                report['rows'][index]=attach_row(position,row,browser)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--retry-errors", action="store_true",
                        help="retry rows whose previous online search was interrupted")
    parser.add_argument("--reconcile-cache", action="store_true",
                        help="only restore exact cached quotes in a completed report")
    parser.add_argument("--revalidate-offers", action="store_true",
                        help="remove previously accepted quotes with conflicting model codes")
    parser.add_argument("--enrich-page-titles", action="store_true",
                        help="recheck saved offer page titles without repeating supplier search")
    parser.add_argument("--title-host", default="",
                        help="optional exact supplier host for --enrich-page-titles")
    parser.add_argument("--title-limit", type=int, default=0,
                        help="maximum cards to open; zero means all")
    parser.add_argument("--retry-incomplete", action="store_true",
                        help="refresh only rows with fewer than MIN_SOURCES, preserving prior offers")
    parser.add_argument("--workers", type=int, default=3,
                        help="concurrent position searches for --retry-incomplete (1-4)")
    parser.add_argument("--retry-numbers", type=str, default="",
                        help="comma-separated specification numbers to refresh with --retry-incomplete")
    parser.add_argument("--recheck-card", type=str, default="",
                        help="recheck one known product card as NUMBER=HTTPS_URL")
    parser.add_argument("--repair-report", action="store_true",
                        help="restore missing saved quote lists from exact local cache")
    parser.add_argument("--sync-cache", action="store_true",
                        help="copy reviewed report quotes into the exact reusable cache")
    args = parser.parse_args()
    if not 1 <= args.workers <= 4:
        parser.error("--workers must be between 1 and 4")
    if sum((args.retry_errors, args.reconcile_cache, args.revalidate_offers,
            args.enrich_page_titles,
            args.retry_incomplete, bool(args.recheck_card), args.repair_report,
            args.sync_cache)) > 1:
        parser.error("choose only one retry/reconcile mode")
    if (args.title_host or args.title_limit) and not args.enrich_page_titles:
        parser.error("--title-host and --title-limit require --enrich-page-titles")
    if args.title_limit < 0:
        parser.error("--title-limit must be nonnegative")
    if args.retry_numbers and not args.retry_incomplete:
        parser.error("--retry-numbers requires --retry-incomplete")
    try:
        numbers = {int(value.strip()) for value in args.retry_numbers.split(",") if value.strip()}
    except ValueError:
        parser.error("--retry-numbers must contain comma-separated integers")

    draft = json.loads(args.draft.read_text(encoding="utf-8"))
    if draft.get("version") != 1 or not isinstance(draft.get("positions"), list):
        raise ValueError("Unsupported estimator draft")
    positions = [PositionIn.model_validate(p) for p in draft["positions"]]
    if not positions:
        raise ValueError("Draft has no positions")
    identity = [{key: getattr(p, key) for key in
                 ("number", "name", "type_mark", "unit", "qty", "discipline", "is_unique")}
                for p in positions]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if args.report.exists():
        report = json.loads(args.report.read_text(encoding="utf-8"))
        if report.get("draft_sha256") != digest or len(report.get("rows", [])) != len(positions):
            raise ValueError("Report belongs to a different draft; choose another report path")
    else:
        report = {"draft_sha256": digest, "status": "running", "total": len(positions),
                  "completed": 0, "rows": [None] * len(positions)}
    for row, position in zip(report["rows"], positions):
        if row is not None:
            row.setdefault("qty", position.qty)
    if args.reconcile_cache:
        if report["status"] != "done":
            raise ValueError("Wait for the live run to finish before reconciling its report")
        restored = reconcile_cache(report, positions)
        report["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save(args.report, report)
        print(f"Restored {restored} rows from exact validated cache", flush=True)
        return
    if args.sync_cache:
        if report["status"] != "done":
            raise ValueError("Wait for the live run to finish before syncing its cache")
        revalidate_offers(report)
        changed = sync_report_cache(report, positions)
        report["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save(args.report, report)
        print(f"Synced reviewed cache for positions: {changed}", flush=True)
        return
    if args.repair_report:
        if report["status"] != "done":
            raise ValueError("Wait for the live run to finish before repairing its report")
        repaired = repair_missing_candidates(report, positions)
        report["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save(args.report, report)
        print(f"Restored exact cached quotes for positions: {repaired}", flush=True)
        return
    if args.revalidate_offers:
        if report["status"] != "done":
            raise ValueError("Wait for the live run to finish before revalidating its report")
        removed = revalidate_offers(report)
        report["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save(args.report, report)
        print(f"Removed {removed} mismatched offers", flush=True)
        return
    if args.enrich_page_titles:
        if report["status"] != "done":
            raise ValueError("Wait for the live run to finish before enriching its report")
        from .browser import Browser
        def save_progress(current: dict) -> None:
            current["updated_at"] = datetime.now(timezone.utc).isoformat()
            _save(args.report, current)
        with Browser() as browser:
            checked, cleared = enrich_offer_page_titles(
                report, browser, host=args.title_host.lower().removeprefix("www."),
                limit=args.title_limit, on_progress=save_progress,
            )
        report["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save(args.report, report)
        print(f"Checked {checked} saved cards; cleared {cleared} model-review flags", flush=True)
        return
    if args.retry_incomplete:
        if any(row is None for row in report["rows"]):
            raise ValueError("Complete the first pass before retrying incomplete rows")
        retry_incomplete(report, positions, args.report, args.workers,
                         numbers=numbers or None)
        return
    if args.recheck_card:
        if report["status"] != "done":
            raise ValueError("Wait for the live run to finish before rechecking a card")
        try:
            raw_number, url = args.recheck_card.split("=", 1)
            number = int(raw_number)
        except (ValueError, TypeError):
            parser.error("--recheck-card must be NUMBER=HTTPS_URL")
        before, after = recheck_card(report, positions, args.report, number, url)
        synced = sync_cache_row(report["rows"][next(i for i, p in enumerate(positions)
                                                   if p.number == number)],
                                next(p for p in positions if p.number == number))
        print(f"Position {number}: {before} -> {after} confirmed sources", flush=True)
        if synced:
            print(f"Position {number}: exact cache updated", flush=True)
        return
    if args.retry_errors:
        for index, row in enumerate(report["rows"]):
            if row and (row["status"] == "failed" and "Поиск прерван" in row["reason"]
                        or "Онлайн-поиск прерван" in row.get("reason", "")):
                report["rows"][index] = None
        report["completed"] = sum(item is not None for item in report["rows"])
    report["status"] = "running"
    _save(args.report, report)
    try:
        for index, position in enumerate(positions):
            if report["rows"][index] is not None:
                continue
            print(f"[{index + 1}/{len(positions)}] №{position.number} {position.name[:65]}", flush=True)
            row = _research_row(position)
            row["index"] = index
            report["rows"][index] = row
            report["completed"] = sum(item is not None for item in report["rows"])
            report["updated_at"] = datetime.now(timezone.utc).isoformat()
            _save(args.report, report)
            print(f"  {row['status']}: {row['found']}/{row['target']} {row['reason']}", flush=True)
    except BaseException:
        report["status"] = "interrupted"
        _save(args.report, report)
        raise
    attach_report(report,positions)
    report["status"] = "done"
    _save(args.report, report)
    print(f"Finished {report['completed']}/{report['total']}: {args.report}", flush=True)


if __name__ == "__main__":
    main()
