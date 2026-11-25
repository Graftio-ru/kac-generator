"""Automatic price selection: highest quotes from independent sellers."""
import math
from .region_policy import region_status
from urllib.parse import urlparse


def _field(offer, name, default=None):
    return offer.get(name, default) if isinstance(offer, dict) else getattr(offer, name, default)


def seller_key(offer):
    requisites = _field(offer, 'requisites')
    inn = str(_field(offer, 'inn') or
              (_field(requisites, 'inn') if requisites else '') or '').strip()
    if inn:
        from .requisites.supplier import canonical_inn
        # Keep legacy manual identifiers, but never split a validated INN by
        # spelling/spacing. Evidence itself is not rewritten.
        return ('inn', canonical_inn(inn) or inn)
    try:
        host = (urlparse(_field(offer, 'url', '') or '').hostname or '').lower().removeprefix('www.')
    except ValueError:
        return None
    return ('host', host) if host else None


def _host(offer):
    try:
        return (urlparse(_field(offer, 'url', '') or '').hostname or '').lower().removeprefix('www.')
    except ValueError:
        return ''


def _known_host_sellers(offers):
    known = {}
    for offer in offers:
        key = seller_key(offer)
        if key and key[0] == 'inn' and _host(offer):
            known.setdefault(_host(offer), set()).add(key)
    return known


def seller_review_reason(offer, offers):
    if not _field(offer, 'from_letter'):
        from .requisites.supplier import seller_evidence_reason
        reason = seller_evidence_reason(_field(offer, 'seller_evidence'), product_url=_field(offer, 'url'))
        if reason:return reason
    requisites=_field(offer,'requisites')
    if not _field(offer,'from_letter') and (_field(offer,'source_quote_evidence') or _field(offer,'listed_price_with_vat') is not None):
        from .requisites.supplier import valid_inn
        inn=str(_field(offer,'inn') or (_field(requisites,'inn') if requisites else '') or '')
        name=str(_field(offer,'org_name') or (_field(requisites,'name') if requisites else '') or '')
        if not name.strip() or not valid_inn(inn):return 'Реквизиты продавца не подтверждены'
    key = seller_key(offer)
    if key and key[0] == 'host' and len(_known_host_sellers(offers).get(key[1], ())) > 1:
        return 'Не подтверждён самостоятельный поставщик: на сайте несколько продавцов, ИНН предложения не указан'
    return ''


def independent_offers(offers, position=None):
    """Highest eligible quote per seller, conservatively linking anonymous host quotes."""
    eligible = [o for o in offers if offer_is_comparable(o, position)]
    known = _known_host_sellers(eligible)
    sellers = {}
    for offer in eligible:
        price = float(_field(offer, 'price_with_vat', 0) or 0)
        key = seller_key(offer)
        if key and key[0] == 'host':
            identified = known.get(key[1], set())
            if len(identified) > 1:
                # No evidence which marketplace seller owns this quote.
                continue
            if identified:
                key = next(iter(identified))
        if key is None or not math.isfinite(price) or price <= 0:
            continue
        if key not in sellers or price > float(_field(sellers[key], 'price_with_vat')):
            sellers[key] = offer
        elif (position is not None and price == float(_field(sellers[key], 'price_with_vat'))
              and _field(offer,'catalogue_ref') and _field(sellers[key],'catalogue_ref')
              and not _field(offer,'from_letter') and not _field(sellers[key],'from_letter')):
            # Only a true ranking tie may reuse an independently validated image.
            # Missing proof never authorizes a cheaper quote or another seller.
            from .search.selected_proofs import proof_ready
            if proof_ready(offer,position) and not proof_ready(sellers[key],position):
                sellers[key] = offer
    return sorted(sellers.values(), key=lambda offer: float(_field(offer, 'price_with_vat')))


def unit_review_pending(offer):
    """Raw card evidence needs a selling unit; legacy manual prices have no raw price."""
    from .source_quote import retired_attachment_reason
    if _field(offer,'unit_review_required') and retired_attachment_reason(_field(offer,'unit_review_reason')):
        from .source_quote import quote_review_reason
        return bool(quote_review_reason(offer))
    return bool(_field(offer, 'unit_review_required') or
                (not _field(offer, 'price_unit') and _field(offer, 'listed_price_with_vat') is not None))


def offer_is_comparable(offer, position=None, *, require_seller=True):
    """One price eligibility rule for models, API dictionaries, reports and caches."""
    from .source_quote import retired_attachment_reason
    if _field(offer,'estimator_approval_ref'):
        from .search.catalogue_projection import catalogue_review_reason
        if catalogue_review_reason(offer,position):return False
    if _field(offer, 'refresh_failed') and not retired_attachment_reason(_field(offer,'refresh_failure_reason')):
        return False
    if region_status(offer) == 'incompatible':
        return False
    from .price_capture import unit_capture_review_reason
    if unit_capture_review_reason(offer, position):
        return False
    if unit_review_pending(offer):
        return False
    if _field(offer, 'match_review_required') and not _field(offer, 'model_review_accepted'):
        return False
    try:
        price = float(_field(offer, 'price_with_vat', 0) or 0)
    except (TypeError, ValueError):
        return False
    if require_seller and not _field(offer, 'from_letter'):
        from .requisites.supplier import seller_evidence_reason
        if seller_evidence_reason(_field(offer, 'seller_evidence'), product_url=_field(offer, 'url')):return False
    if require_seller and not _field(offer,'from_letter') and (_field(offer,'source_quote_evidence') or _field(offer,'listed_price_with_vat') is not None):
        from .requisites.supplier import valid_inn
        requisites=_field(offer,'requisites')
        inn=str(_field(offer,'inn') or (_field(requisites,'inn') if requisites else '') or '')
        name=str(_field(offer,'org_name') or (_field(requisites,'name') if requisites else '') or '')
        if not name.strip() or not valid_inn(inn):return False
    return math.isfinite(price) and price > 0 and (not require_seller or seller_key(offer) is not None)


def select_top_offers(offers, count=3, position=None):
    return independent_offers(offers, position)[-count:] if count > 0 else []


def source_count(offers, position=None):
    return len(independent_offers(offers, position))
