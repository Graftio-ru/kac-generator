"""Operator-list applicability plus honest raw regional observations."""


def _field(offer, name, default=None):
    return offer.get(name, default) if isinstance(offer, dict) else getattr(offer, name, default)


def _observed_region_status(offer):
    state = _field(offer, 'region_status', '')
    evidence = str(_field(offer, 'region_evidence', '') or '').strip()
    if state == 'unknown':
        return 'unknown'
    if state == 'incompatible':
        return 'incompatible' if evidence else 'unknown'
    if state == 'confirmed':
        return 'confirmed' if evidence or _field(offer, 'in_moscow_region') is True else 'unknown'
    if _field(offer, 'in_moscow_region') is True:
        return 'confirmed'
    # Legacy false alone meant both absent evidence and actual negative evidence.
    # A city (often the seller's legal address) cannot prove delivery incompatibility.
    if _field(offer, 'in_moscow_region') is False and evidence:
        return 'incompatible'
    return 'unknown'


def region_approval(offer):
    """Derive from the server-owned list, ignoring any client approval field."""
    if _field(offer, 'from_letter', False):
        return None
    from .search.whitelist import region_approval_for
    return region_approval_for(_field(offer, 'url', '') or '')


def region_status(offer):
    return 'confirmed' if region_approval(offer) else _observed_region_status(offer)


def region_admission_label(offer):
    if region_approval(offer):
        return 'Регион поставки допущен по согласованному перечню сайтов; не подтверждение наличия/самовывоза'
    return region_notice(offer) or ('Не применяется (письмо)' if _field(offer, 'from_letter', False) else 'Регион подтверждён карточкой')


def region_notice(offer):
    if _field(offer, 'from_letter', False):
        return ''
    state = region_status(offer)
    evidence = str(_field(offer, 'region_evidence', '') or '').strip()
    if state == 'confirmed':
        return ''
    if state == 'incompatible':
        return 'Регион поставки не соответствует условиям: ' + evidence
    return 'Регион поставки не подтверждён: ' + (evidence or
        'нет подтверждения склада/самовывоза или доставки в Москву/МО')


def extracted_region(data):
    """Keep raw pickup observation separate from the delivery eligibility decision."""
    pickup = data.get('moscow_pickup')
    fields = dict(in_moscow_region=pickup if isinstance(pickup, bool) else None,
                  region_status=data.get('region_status') if data.get('region_status') in
                  {'unknown', 'confirmed', 'incompatible'} else '',
                  region_evidence=str(data.get('region_evidence') or '').strip())
    fields['region_status'] = _observed_region_status(fields)
    return fields
