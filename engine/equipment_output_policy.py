"""Own-equipment missing prices are explicit omissions, never price offers."""

MISSING_PRICE = 'цена Графтио не предоставлена'
INCOMPLETE_TOTAL = 'Полная стоимость не определена: цена Графтио не предоставлена'


def is_graftio_equipment(position):
    evidence = getattr(position, 'source_evidence', None)
    if not isinstance(evidence, dict) or evidence.get('reader') != 'native_pdf':
        return False
    cells = evidence.get('cells')
    if not isinstance(cells, dict):
        return False
    maker = cells.get('maker')
    if not isinstance(maker, str) or maker.strip().casefold() not in {'графтио', 'graftio'}:
        return False
    marks = [cells.get(key, '') for key in ('model', 'article')]
    if any(not isinstance(value, str) for value in marks):
        return False
    source_mark = ' / '.join(dict.fromkeys(value.strip() for value in marks if value.strip()))
    if source_mark != position.type_mark.strip():
        return False
    # A manufacturer's cell belongs to this source position, not another row.
    return (str(cells.get('number', '')).strip() == str(position.number)
            and str(cells.get('name', '')).strip() == position.name.strip())


def letter_offers(position, offers):
    from .offer_selection import offer_is_comparable
    return [offer for offer in offers if getattr(offer, 'from_letter', False)
            and offer_is_comparable(offer, position, require_seller=False)]


def missing_own_price(position, offers):
    return is_graftio_equipment(position) and not letter_offers(position, offers)


def financial_summary(results):
    results = list(results)
    missing = [r for r in results if missing_own_price(r.position, r.offers)]
    subtotal = sum(r.max_offer.price_with_vat * r.position.qty
                   for r in results if r.max_offer is not None)
    return {'known_subtotal': subtotal, 'full_total': None if missing else subtotal,
            'missing_positions': [r.position.number for r in missing]}
