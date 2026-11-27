"""Постоянная база связок «позиция → цены/скриншоты/реквизиты».

Большинство позиций повторяется от спецификации к спецификации, а поиск цен (выдача +
скриншоты + реквизиты) — самый долгий этап. Поэтому результаты `research_position`
складываются в один файл (`engine/data/base/index.json`) и переиспользуются: при обработке
спецификации сперва берём готовое из базы, вживую ищем только промахи.

Матчинг позиции с базой: сперва точное совпадение нормализованного ключа, затем LLM-фолбэк
(DeepSeek выбирает ту же позицию среди похожих по словам кандидатов). Дата в документах всегда
текущая — кэш по возрасту не ограничивается (обновляется вручную кнопкой «Обновить базу»).
"""
from __future__ import annotations

from ..offer_selection import select_top_offers, source_count

import json
import re
import threading
import time
import math
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from ..config import BASE_FILE, CACHE_DIR, settings
from ..llm import complete_json, ModelOutputInvalid
from .failure_policy import ResearchControlError
from ..models import PositionResult, PriceOffer, SpecPosition

_LOCK = threading.Lock()
_VALIDATION_VERSION = 8

_MATCH_SYSTEM = (
    "Тебе дан искомый товар и пронумерованный список кандидатов. Верни JSON "
    '{"match": N}, где N — номер кандидата, обозначающего ТОТ ЖЕ товар (та же марка/тип/'
    "типоразмер/исполнение), или 0, если ни один не подходит. Только JSON."
)


def _position_metadata(pos):
    return dict(alternative_articles=list(pos.alternative_articles),alternative_names=list(pos.alternative_names),full_name=pos.full_name,source_evidence=pos.source_evidence,review_fields=pos.review_fields)


def _norm_key(pos: SpecPosition) -> str:
    """Нормализованный ключ позиции: имя|марка|дисциплина (нижний регистр, схлопнутые пробелы)."""
    def n(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").strip().lower())
    return f"{n(pos.name)}|{n(pos.type_mark)}|{n(pos.discipline)}"


def _tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^\wа-яё]+", (s or "").lower()) if len(t) >= 3}


def _refresh_context(pos):
    from ..price_capture import _unit
    unit = _unit(pos.unit) or pos.unit.strip().casefold()
    from .product_identity import alternative_policy
    value=[unit,float(pos.qty)];policy=alternative_policy(pos)['sha256']
    if policy:value.append(policy)
    return json.dumps(value,ensure_ascii=False)


def _capture_time(offer):
    evidence = offer.source_quote_evidence or offer.price_capture_evidence or {}
    value = evidence.get('observed_at',evidence.get('captured_at', 0))
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else 0.


def _observation_fingerprint(offer, pos):
    """Exact producer observation; absent seller provenance keeps legacy bytes."""
    from .product_identity import canonical_card_url
    payload = dict(context=_refresh_context(pos), url=canonical_card_url(offer.url),
        price=offer.price_with_vat, listed=offer.listed_price_with_vat, unit=offer.price_unit,
        normalized_unit=offer.normalized_price_unit, capture=offer.source_quote_evidence or offer.price_capture_evidence,
        identity=offer.identity_evidence)
    if offer.seller_evidence:
        payload['seller_evidence'] = offer.seller_evidence
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _refresh_view(offers, entry, pos):
    """Contextual refusal overlays preserve the original dated quote as evidence."""
    from .product_identity import canonical_card_url
    receipts = entry.get('refresh_receipts', {}).get(_refresh_context(pos), {})
    viewed = []
    for original in offers:
        offer = original.model_copy(deep=True)
        if pos.is_unique and offer.from_letter:
            viewed.append(offer)
            continue
        from ..source_quote import retired_attachment_reason
        if offer.refresh_failed and retired_attachment_reason(offer.refresh_failure_reason):
            offer.refresh_failed=False;offer.refresh_failure_reason=''
        receipt = receipts.get(canonical_card_url(offer.url), {})
        if receipt.get('status')=='refused' and retired_attachment_reason(receipt.get('reason')):receipt={}
        stale_confirmed = receipt.get('status') == 'confirmed' and receipt.get('proof') != _observation_fingerprint(offer, pos)
        if receipt.get('status') == 'refused' or stale_confirmed:
            offer.refresh_failed = True
            offer.refresh_failure_reason = ('Сохранённая цена не совпадает с последним подтверждённым наблюдением' if stale_confirmed else receipt['reason'])
            offer.refresh_checked_at = receipt['observed_at']
        viewed.append(offer)
    return viewed


@contextmanager
def capture_current_observations():
    """Attest only actual card producer callbacks, preserving enclosing publication."""
    from .session import _ACCEPTED, _OBSERVED
    offers = []
    def wrap(parent):
        def observed(offer):
            if parent:
                parent(offer)
            offers.append(offer.model_copy(deep=True))
        return observed
    a = _ACCEPTED.set(wrap(_ACCEPTED.get()))
    b = _OBSERVED.set(wrap(_OBSERVED.get()))
    try:
        yield offers
    finally:
        _OBSERVED.reset(b)
        _ACCEPTED.reset(a)


def _confirm_new_receipts(receipts, pos, offers):
    from .product_identity import canonical_card_url
    from ..offer_selection import offer_is_comparable
    context = receipts.setdefault(_refresh_context(pos), {})
    for offer in offers:
        receipt = context.get(canonical_card_url(offer.url))
        if _capture_time(offer) > (receipt or {}).get('ordering_at', 0) and offer_is_comparable(offer, pos):
            context[canonical_card_url(offer.url)] = dict(status='confirmed', ordering_at=_capture_time(offer),
                observed_at=time.time(), reason='Текущая цена подтверждена', proof=_observation_fingerprint(offer, pos))


def reconcile_refresh(pos, urls, observations=(), *, started_at, reason='', diagnostics=()):
    """Atomically reconcile assigned card outcomes; never touch other request contexts.

    Attempt start orders competing failures against current validated captures.
    No URL means no card was assigned; deferred admission is a dated unknown
    confirmation outcome, not a transport failure or evidence of stock absence.
    """
    from .product_identity import canonical_card_url
    from ..offer_selection import offer_is_comparable
    urls = {canonical_card_url(url) for url in urls if url}
    if not urls:
        return None
    with _LOCK:
        data = _load()
        entry = data.get('entries', {}).get(_norm_key(pos))
        if not entry:
            entry = data.setdefault('entries', {}).setdefault(_norm_key(pos),dict(key=_norm_key(pos),
                name=pos.name,type_mark=pos.type_mark,unit=pos.unit,qty=pos.qty,discipline=pos.discipline,**_position_metadata(pos),
                offers=[],candidates=[],sources_found=0,sources_checked=0,validation_version=_VALIDATION_VERSION))
        receipts = entry.setdefault('refresh_receipts', {}).setdefault(_refresh_context(pos), {})
        raw = [PriceOffer(**value) for value in (entry.get('candidates') or entry.get('offers') or [])]
        fresh = {canonical_card_url(o.url):o for o in observations if _capture_time(o) >= started_at and offer_is_comparable(o, pos)}
        for url in urls | set(fresh):
            prior = receipts.get(url, {})
            newer = 0.  # Only producer-attested receipts establish supersession.
            if max(prior.get('ordering_at', 0), newer) > started_at and url not in fresh:
                continue  # A late failed operation cannot downgrade a newer quote.
            if url in fresh:
                stamp = _capture_time(fresh[url])
                if stamp >= prior.get('ordering_at', 0):
                    receipts[url] = dict(status='confirmed', ordering_at=stamp, observed_at=time.time(),
                        reason='Текущая цена подтверждена',proof=_observation_fingerprint(fresh[url],pos))
            else:
                reasons = [d.get('reason', '') for d in diagnostics if canonical_card_url(d.get('url', '')) == url and d.get('reason')]
                receipts[url] = dict(status='refused', ordering_at=started_at, observed_at=time.time(),
                    reason='; '.join(dict.fromkeys(reasons)) or reason or 'Текущая проверка не подтвердила прежнюю цену; требуется проверка',
                    requested_unit=pos.unit, requested_qty=pos.qty, product_presence='unknown')
        validated = _result_from_entry(entry, pos, '', '')
        stored_view = _result_from_entry(entry, _position_from_entry(entry), '', '')
        entry['offers'] = [o.model_dump() for o in stored_view.offers]
        entry['sources_found'] = stored_view.sources_found
        _save(data)
        return validated


def refresh_receipt_snapshot():
    """One consistent trusted snapshot for multi-position final/report consumers."""
    with _LOCK:
        return _load().get('entries', {})


def project_refresh_offers(pos, offers, *, snapshot=None):
    if snapshot is None:
        snapshot = refresh_receipt_snapshot()
    return _refresh_view(offers, snapshot.get(_norm_key(pos), {}), pos)


def apply_refresh(pos, result):
    """Apply persisted request-context receipts to a live or retained candidate pool."""
    entry = refresh_receipt_snapshot().get(_norm_key(pos), {})
    if not entry.get('refresh_receipts', {}).get(_refresh_context(pos)):
        return result
    result.all_offers = _refresh_view(result.all_offers or result.offers, entry, pos)
    result.offers = select_top_offers(result.all_offers, settings.top_prices, pos)
    result.sources_found = source_count(result.all_offers, pos)
    return result


def _reconcile_collected(pos, result, diagnostics, started_at, produced):
    """Legacy cache import/refresh shares the same dated card-outcome boundary."""
    observations = [o for o in produced if _capture_time(o) >= started_at]
    urls = [d['url'] for d in diagnostics if d.get('url') and d.get('code') != 'phase_timing']
    urls.extend(o.identity_evidence.get('source_url') or o.url for o in observations)
    reconcile_refresh(pos, urls, observations, started_at=started_at, diagnostics=diagnostics)
    return apply_refresh(pos, result)


def _load() -> dict:
    if not BASE_FILE.exists():
        return {"last_refresh": None, "entries": {}}
    try:
        return json.loads(BASE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"last_refresh": None, "entries": {}}


def _save(data: dict) -> None:
    tmp = BASE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(BASE_FILE)


def record_page_title(url: str, title: str) -> int:
    """Persist verified card-title evidence so stale cache quotes cannot return."""
    if not url or not title.strip():
        return 0
    with _LOCK:
        data = _load()
        changed = 0
        for entry in data.get("entries", {}).values():
            for collection in ("candidates", "offers"):
                for offer in entry.get(collection) or []:
                    if offer.get("url") == url and offer.get("page_title") != title:
                        offer["page_title"] = title
                        offer["model_review_accepted"] = False
                        changed += 1
        if changed:
            _save(data)
        return changed


def record_page_article(url: str, article: str) -> int:
    """Persist a verified main-card article across cached offer copies."""
    if not url or not article.strip():
        return 0
    with _LOCK:
        data = _load()
        changed = 0
        for entry in data.get("entries", {}).values():
            for collection in ("candidates", "offers"):
                for offer in entry.get(collection) or []:
                    if offer.get("url") == url and offer.get("page_article") != article:
                        offer["page_article"] = article
                        offer["model_review_accepted"] = False
                        changed += 1
        if changed:
            _save(data)
        return changed


def _result_from_entry(entry: dict, pos: SpecPosition, year: str, quarter: str) -> PositionResult:
    """Восстанавливает позицию и использует каталог, только когда это разрешено текущим контекстом."""
    from .agent import (_conflicting_explicit_article, _conflicting_full_model_code,
                        _conflicting_model_code_in_title, _conflicting_numeric_article,
                        _conflicting_network_cable_spec, _conflicting_page_article,
                        _is_listing_url, _saved_price_unit_is_verified,
                        _conflicting_full_sku_in_url, _satro_minimum_order_conflict,
                        _needs_model_review)
    def valid(offer):
        if offer.catalogue_ref:return True  # Native current-source checks remain final authority.
        from .product_identity import native_comparison_mark
        mark=native_comparison_mark(pos,article=offer.page_article)
        return not (
        _is_listing_url(offer.url)
        or _satro_minimum_order_conflict(offer.url, offer.product_title,
                                         pos.unit, pos.qty)
        or _conflicting_explicit_article(mark, offer.product_title, offer.url)
        or _conflicting_model_code_in_title(mark, offer.product_title)
        or _conflicting_network_cable_spec(pos.name, mark,
                                           offer.product_title + " " + offer.page_title)
        or _conflicting_page_article(mark, offer.page_article)
        or _conflicting_numeric_article(mark, offer.product_title)
        or _conflicting_numeric_article(mark, offer.page_title)
        or _conflicting_full_model_code(mark, offer.product_title)
        or _conflicting_full_model_code(mark, offer.page_title)
        or _conflicting_full_sku_in_url(mark, offer.url)
    )
    cands = [PriceOffer(**o) for o in
             (entry.get("candidates") or entry.get("offers") or [])]
    from . import catalogue_projection as projection
    store=projection.current_catalogue_store()
    if store is not None and any(o.catalogue_ref for o in cands):
        from .catalogue import identity_for_position
        projected=projection.project_product(store,identity_for_position(pos),pos)
        current={tuple(sorted(o.catalogue_ref.items())):o for o in projected.all_offers}if projected else {}
        cands=[current.get(tuple(sorted(o.catalogue_ref.items())),o)if o.catalogue_ref else o for o in cands]
    for offer in cands:
        offer.match_review_required = offer.match_review_required or bool(not offer.catalogue_ref and offer.product_title and _needs_model_review(
            pos.type_mark, offer.product_title, offer.url, offer.page_title, offer.page_article))
        from ..price_capture import unit_capture_review_reason
        if unit_capture_review_reason(offer, pos):
            offer.unit_review_required = True
            offer.unit_review_reason = offer.unit_review_reason or "Сохранённая цена: единица продажи/пересчёт не подтверждены"
            offer.normalized_price_unit = ""
    cands = sorted((offer for offer in cands if valid(offer)),
                   key=lambda offer: offer.price_with_vat)
    cands = _refresh_view(cands, entry, pos)
    offers = select_top_offers(cands, settings.top_prices, pos)
    found = source_count(cands, pos)
    return PositionResult(
        position=pos, offers=offers, all_offers=cands, year=year, quarter=quarter,
        sources_found=found, sources_checked=max(found, int(entry.get("sources_checked") or 0)),
        sources_target=settings.min_sources)


def has_enough_sources(result: PositionResult) -> bool:
    """A partial cached search must not prevent a new live search."""
    return result.sources_found >= settings.min_sources


def _llm_match_key(pos: SpecPosition, entries: dict) -> str | None:
    """LLM-фолбэк: среди похожих по словам кандидатов выбрать ту же позицию. Ключ или None."""
    target_tokens = _tokens(pos.name)
    if not target_tokens:
        return None
    # префильтр: кандидаты с пересечением слов или вхождением подстроки (дёшево, без LLM)
    scored = []
    for key, e in entries.items():
        ov = len(target_tokens & _tokens(e.get("name", "")))
        nm = (e.get("name") or "").lower()
        if ov >= 2 or (nm and (nm in pos.name.lower() or pos.name.lower() in nm)):
            scored.append((ov, key, e))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    cands = scored[:20]
    listing = "\n".join(
        f"{i+1}. {e.get('name','')} {e.get('type_mark','')}".strip()
        for i, (_, _, e) in enumerate(cands))
    prompt = (f"Искомый товар: {pos.name} {pos.type_mark}".strip()
              + f"\n\nКандидаты:\n{listing}")
    try:
        data = complete_json(prompt, system=_MATCH_SYSTEM)
        if not isinstance(data,dict):
            raise ModelOutputInvalid('invalid_schema')
        value=data.get('match',0)
        if isinstance(value,bool)or not isinstance(value,int):
            raise ModelOutputInvalid('invalid_schema')
        n=value
    except ResearchControlError:
        raise
    except Exception:
        return None
    if 1 <= n <= len(cands):
        return cands[n - 1][1]
    return None


def lookup(pos: SpecPosition, year: str, quarter: str, *, exact_only: bool = False) -> PositionResult | None:
    """Ищет позицию в базе: точное совпадение → LLM-фолбэк. None, если не найдено."""
    data = _load()
    entries = data.get("entries", {})
    if not entries:
        return None
    entry = entries.get(_norm_key(pos))
    if entry is None and not exact_only:
        key = _llm_match_key(pos, entries)
        entry = entries.get(key) if key else None
    if entry is None or not (entry.get("candidates") or entry.get("offers")):
        return None
    # Old records may contain wrong price units, a different article variant,
    # an offer without verified seller requisites, or a category listing quote.
    # Preserve them on disk for review, but do not use them automatically.
    if entry.get("validation_version", 0) < _VALIDATION_VERSION:
        return None
    result = _result_from_entry(entry, pos, year, quarter)
    return result if result.all_offers else None


def _offer_source(offer: PriceOffer) -> str:
    """Count one supplier once even if search returns several card URLs."""
    inn = offer.requisites.inn.strip()
    if inn:
        return f"inn:{inn}"
    host = (urlparse(offer.url).hostname or "").lower().removeprefix("www.")
    return f"host:{host}" if host else f"url:{offer.url}"


def upsert(pos: SpecPosition, result: PositionResult, *, confirmed_observations=()) -> PositionResult:
    """Save verified offers, accumulating partial searches made on the same day."""
    if not (result.all_offers or result.offers):
        return result
    with _LOCK:
        data = _load()
        key = _norm_key(pos)
        existing = data.setdefault("entries", {}).get(key)
        from ..price_capture import unit_capture_review_reason
        from .agent import (_conflicting_explicit_article, _conflicting_full_model_code,
                            _conflicting_model_code_in_title, _conflicting_numeric_article,
                            _conflicting_network_cable_spec, _conflicting_page_article,
                            _is_listing_url, _saved_price_unit_is_verified,
                            _conflicting_full_sku_in_url, _satro_minimum_order_conflict)

        def valid(offer: PriceOffer) -> bool:
            if offer.catalogue_ref:return True
            from .product_identity import native_comparison_mark
            mark=native_comparison_mark(pos,article=offer.page_article)
            return not (
                _is_listing_url(offer.url)
                or _satro_minimum_order_conflict(offer.url, offer.product_title,
                                                 pos.unit, pos.qty)
                or (not offer.unit_review_required and unit_capture_review_reason(offer, pos))
                or _conflicting_explicit_article(mark, offer.product_title, offer.url)
                or _conflicting_model_code_in_title(mark, offer.product_title)
                or _conflicting_network_cable_spec(pos.name, mark,
                                                   offer.product_title + " " + offer.page_title)
                or _conflicting_page_article(mark, offer.page_article)
                or _conflicting_numeric_article(mark, offer.product_title)
                or _conflicting_numeric_article(mark, offer.page_title)
                or _conflicting_full_model_code(mark, offer.product_title)
                or _conflicting_full_model_code(mark, offer.page_title)
                or _conflicting_full_sku_in_url(mark, offer.url)
            )

        incoming = [offer.model_copy(deep=True) for offer in (result.all_offers or result.offers)]
        # A cache view carries contextual refusal flags; do not bake them into
        # the underlying historical quote and thereby poison other quantities.
        baseline = {o['url']:o for o in ((existing or {}).get('candidates') or [])}
        for offer in incoming:
            old = baseline.get(offer.url)
            if old and offer.refresh_checked_at and (offer.source_quote_evidence or offer.price_capture_evidence) == (old.get('source_quote_evidence') or old.get('price_capture_evidence', {})):
                offer.refresh_failed = old.get('refresh_failed', False)
                offer.refresh_failure_reason = old.get('refresh_failure_reason', '')
                offer.refresh_checked_at = old.get('refresh_checked_at', 0.)
        candidates = [offer for offer in incoming
                      if valid(offer)]
        if (existing and existing.get("captured_at") == date.today().isoformat()
                and existing.get("validation_version") == _VALIDATION_VERSION):
            previous = [PriceOffer(**offer) for offer in
                        (existing.get("candidates") or existing.get("offers") or [])]
            previous = [offer for offer in previous if valid(offer)]
            # New observations replace older quotes from the same seller.
            by_source = {offer.url or _offer_source(offer): offer for offer in previous}
            by_source.update({offer.url or _offer_source(offer): offer for offer in candidates})
            candidates = list(by_source.values())
        if not candidates:
            result.offers = []
            result.all_offers = []
            result.sources_found = 0
            return result
        candidates = sorted(candidates, key=lambda offer: offer.price_with_vat)
        receipts = (existing or {}).get('refresh_receipts', {})
        _confirm_new_receipts(receipts, pos, confirmed_observations)
        viewed = _refresh_view(candidates, {'refresh_receipts':receipts}, pos)
        result.all_offers = viewed
        result.offers = select_top_offers(viewed, settings.top_prices, pos)
        result.sources_found = source_count(viewed, pos)
        result.sources_checked = max(result.sources_checked,
                                     int(existing.get("sources_checked") or 0) if existing else 0)
        result.sources_target = settings.min_sources
        data["entries"][key] = {
            "key": key,
            "name": pos.name, "type_mark": pos.type_mark,**_position_metadata(pos),
            "unit": pos.unit, "discipline": pos.discipline,
            "qty": pos.qty,
            "captured_at": date.today().isoformat(),
            "offers": [o.model_dump() for o in result.offers],
            "candidates": [o.model_dump() for o in candidates],
            "refresh_receipts": receipts,
            "sources_found": result.sources_found,
            "sources_checked": result.sources_checked,
            "validation_version": _VALIDATION_VERSION,
        }
        _save(data)
    return result


def replace_exact(pos: SpecPosition, result: PositionResult) -> int:
    """Replace one exact cache entry with a fully reviewed set of card quotes.

    Unlike upsert, a rejected card must not survive from an earlier search on
    the same day. This is used only after revalidating a complete report row.
    """
    with _LOCK:
        data = _load()
        key = _norm_key(pos)
        candidates = result.all_offers or result.offers
        entry = {"candidates": [offer.model_dump() for offer in candidates],
                 "offers": [offer.model_dump() for offer in candidates],
                 "refresh_receipts": data.get('entries', {}).get(key, {}).get('refresh_receipts', {})}
        validated = _result_from_entry(entry, pos, "", "")
        if len(validated.all_offers) != len(candidates):
            raise ValueError("Reviewed quotes failed cache validation")
        if validated.all_offers:
            data.setdefault("entries", {})[key] = {
                "key": key, "name": pos.name, "type_mark": pos.type_mark,**_position_metadata(pos),
                "unit": pos.unit, "discipline": pos.discipline,
                "qty": pos.qty,
                "captured_at": date.today().isoformat(),
                "offers": [offer.model_dump() for offer in validated.offers],
                "candidates": [offer.model_dump() for offer in validated.all_offers],
                "sources_found": validated.sources_found,
                "sources_checked": result.sources_checked,
                "refresh_receipts": entry['refresh_receipts'],
                "validation_version": _VALIDATION_VERSION,
            }
        else:
            data.get("entries", {}).pop(key, None)
        _save(data)
        return validated.sources_found


def entries() -> list[dict]:
    """Сводка записей базы для UI (без полных офферов)."""
    data = _load()
    out = []
    for e in data.get("entries", {}).values():
        offers = e.get("offers", [])
        prices = [o.get("price_with_vat", 0) for o in offers]
        out.append({
            "key": e.get("key", ""), "name": e.get("name", ""),
            "type_mark": e.get("type_mark", ""), "unit": e.get("unit", ""),
            "alternative_articles":e.get("alternative_articles",[]),"alternative_names":e.get("alternative_names",[]),
            "discipline": e.get("discipline", ""),
            "offers_count": len(offers),
            "max_price": max(prices) if prices else 0,
            "captured_at": e.get("captured_at", ""),
        })
    out.sort(key=lambda x: x["name"].lower())
    return out


def status() -> dict:
    data = _load()
    entries = data.get("entries", {})
    # занятое место: индекс + реально используемые базой скриншоты карточек (в CACHE_DIR)
    total = BASE_FILE.stat().st_size if BASE_FILE.exists() else 0
    seen: set[str] = set()
    for e in entries.values():
        for o in e.get("offers", []):
            sp = o.get("screenshot_product")
            if not sp:
                continue
            name = Path(sp).name
            if name in seen:
                continue
            seen.add(name)
            f = CACHE_DIR / name
            if f.exists():
                total += f.stat().st_size
    return {"count": len(entries), "last_refresh": data.get("last_refresh"),
            "disk_bytes": total}


def remove(key: str) -> bool:
    with _LOCK:
        data = _load()
        if key in data.get("entries", {}):
            del data["entries"][key]
            _save(data)
            return True
    return False


def _position_from_entry(e: dict) -> SpecPosition:
    return SpecPosition(number=1, name=e.get("name", ""), type_mark=e.get("type_mark", ""),
                        unit=e.get("unit", "шт") or "шт",qty=e.get('qty',0), discipline=e.get("discipline", "ЭМ"),
                        alternative_articles=e.get("alternative_articles",[]),alternative_names=e.get("alternative_names",[]),full_name=e.get("full_name",e.get("name","")),source_evidence=e.get("source_evidence",{}),review_fields=e.get("review_fields",{}))


def positions_from_xlsx(path: str | Path, default_discipline: str = "ЭМ") -> list[SpecPosition]:
    """Прямой разбор Excel со списком позиций (БЕЗ LLM) для наполнения базы.

    Ищет строку-заголовок и сопоставляет колонки по подстрокам:
    наимен./название→name, тип/марка/обознач→type_mark, ед→unit, дисципл→discipline.
    Если заголовок не найден — берёт первый непустой текстовый столбец как наименование.
    Строки без наименования пропускаются.
    """
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = [[("" if c is None else str(c).strip()) for c in r]
            for r in ws.iter_rows(values_only=True)]
    wb.close()
    if not rows:
        return []

    def find_col(row: list[str], *subs: str) -> int:
        for j, c in enumerate(row):
            cl = c.lower()
            if any(s in cl for s in subs):
                return j
        return -1

    # ищем заголовок в первых 10 строках (где есть «наимен»/«название»)
    hdr_idx, col = -1, {}
    for i, row in enumerate(rows[:10]):
        jn = find_col(row, "наимен", "название")
        if jn >= 0:
            hdr_idx = i
            col = {"name": jn,
                   "type_mark": find_col(row, "тип", "марка", "обознач", "артикул"),
                   "unit": find_col(row, "ед"),
                   "discipline": find_col(row, "дисципл", "лист")}
            break

    out: list[SpecPosition] = []
    if hdr_idx >= 0:
        for row in rows[hdr_idx + 1:]:
            name = row[col["name"]] if col["name"] < len(row) else ""
            if not name:
                continue
            def cell(key: str) -> str:
                j = col.get(key, -1)
                return row[j] if 0 <= j < len(row) else ""
            out.append(SpecPosition(
                number=len(out) + 1, name=name,
                type_mark=cell("type_mark"), unit=cell("unit") or "шт",
                discipline=cell("discipline") or default_discipline))
    else:
        # без заголовка: первый столбец с текстом (не числом) считаем наименованием
        for row in rows:
            name = next((c for c in row if c and not c.replace(",", ".").replace(".", "").isdigit()), "")
            if name:
                out.append(SpecPosition(number=len(out) + 1, name=name,
                                        unit="шт", discipline=default_discipline))
    return out


def import_positions(positions: list[SpecPosition], browser_factory, job=None) -> int:
    """Ищет цены по списку позиций и сохраняет блоки в базу. Возвращает число добавленных."""
    from .agent import research_position, capture_card_diagnostics

    year = str(date.today().year)
    quarter = str((date.today().month - 1) // 3 + 1)
    if job:
        job.say(f"Импорт в базу: {len(positions)} позиций…", 0.05)
    added = 0
    if positions:
        with browser_factory() as browser:
            for i, pos in enumerate(positions, 1):
                if job:
                    job.step = f"Импорт: {pos.name[:40]}"
                n_offers = 0
                started_at = time.time()
                diagnostics = []
                try:
                    from .candidate_queue import candidate_scope
                    with candidate_scope(pos), capture_card_diagnostics() as diagnostics, capture_current_observations() as produced:
                        res = research_position(browser, pos, year, quarter)
                    res = _reconcile_collected(pos, res, diagnostics, started_at, produced)
                    n_offers = len(res.offers)
                    if res.offers:
                        upsert(pos, res, confirmed_observations=produced)
                        added += 1
                except Exception as ex:
                    reconcile_refresh(pos, [d['url'] for d in diagnostics if d.get('url')],
                        started_at=started_at, diagnostics=diagnostics)
                    if job:
                        job.say(f"  ✗ {pos.name[:40]}: {type(ex).__name__}")
                if job:
                    job.say(f"  [{i}/{len(positions)}] {pos.name[:40]} → {n_offers} цен",
                            0.05 + 0.9 * i / len(positions))
    if job:
        job.say(f"Импортировано: {added}/{len(positions)}.", 1.0)
    return added


def refresh_all(browser_factory, job=None) -> int:
    """Заново ищет цены по всем записям базы и обновляет их. Возвращает число обновлённых."""
    from .agent import research_position, capture_card_diagnostics

    data = _load()
    items = list(data.get("entries", {}).values())
    year = str(date.today().year)
    quarter = str((date.today().month - 1) // 3 + 1)
    if job:
        job.say(f"Обновление базы: {len(items)} позиций…", 0.05)
    updated = 0
    if items:
        with browser_factory() as browser:
            for i, e in enumerate(items, 1):
                pos = _position_from_entry(e)
                if job:
                    job.step = f"База: {pos.name[:40]}"
                n_offers = 0
                started_at = time.time()
                diagnostics = []
                try:
                    from .candidate_queue import candidate_scope
                    with candidate_scope(pos), capture_card_diagnostics() as diagnostics, capture_current_observations() as produced:
                        res = research_position(browser, pos, year, quarter)
                    res = _reconcile_collected(pos, res, diagnostics, started_at, produced)
                    n_offers = len(res.offers)
                    if res.offers:
                        upsert(pos, res, confirmed_observations=produced)
                        updated += 1
                except Exception as ex:  # одна позиция не валит весь прогон
                    reconcile_refresh(pos, [d['url'] for d in diagnostics if d.get('url')],
                        started_at=started_at, diagnostics=diagnostics)
                    if job:
                        job.say(f"  ✗ {pos.name[:40]}: {type(ex).__name__}")
                if job:
                    job.say(f"  [{i}/{len(items)}] {pos.name[:40]} → {n_offers} цен",
                            0.05 + 0.9 * i / len(items))
    with _LOCK:
        data = _load()
        data["last_refresh"] = date.today().isoformat()
        _save(data)
    if job:
        job.say(f"База обновлена: {updated}/{len(items)}.", 1.0)
    return updated
