"""Separate estimator sufficiency from search breadth; never approve OCR data."""
from .offer_selection import source_count


def annotate_coverage(row: dict) -> dict:
    candidates = list(row.get('candidates') or [])
    for offer in row.get('offers') or []:
        index = next((i for i, c in enumerate(candidates)
                      if offer.get('url') and c.get('url') == offer['url']), None)
        if index is None:
            candidates.append(offer)
        else:
            candidates[index] = offer
    count = source_count([offer for offer in candidates if not offer.get('from_letter')], row)
    row.update(required_sources=3, coverage_target=5, comparable_sources=count,
               coverage='wide' if count >= 5 else 'limited' if count >= 3 else 'insufficient',
               quote_status='sufficient' if count >= 3 else 'insufficient',
               source_review_required=bool(row.get('provisional')))
    if row.get('status') in {'pending', 'running', 'skipped', 'stale', 'error'}:
        row['quote_status'] = 'not_evaluated'
    return row
