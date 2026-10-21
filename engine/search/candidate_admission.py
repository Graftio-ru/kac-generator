"""Conservative pre-capture exclusions; never product-presence authority."""
from dataclasses import dataclass
from decimal import Decimal
import math
import re
import time
from urllib.parse import urlsplit
from .product_identity import canonical_card_url, card_host, native_request_role


@dataclass(frozen=True)
class CandidateAdmission:
    reason: str = ''
    evidence: tuple[tuple[str, str], ...] = ()
    observed_at: float | None = None

    @property
    def rejected(self):
        return bool(self.reason)

    def diagnostic(self, url):
        listing = self.reason == 'candidate_known_listing'
        # Evidence is also retained in the existing bounded diagnostic reason;
        # no acquisition schema change or raw HTML is needed.
        details = '; '.join(f'{k}={v}' for k, v in self.evidence)
        return dict(code=self.reason, url=url, observed_at=self.observed_at,
                    reason=('Известная страница списка товаров' if listing else
                            'Явное противоречие идентичности результата') + ': ' + details,
                    failure_source='candidate_admission', failure_scope='card',
                    failure_cause='listing' if listing else 'identity_conflict')


def _listing_route(url, host):
    from .supplier_discovery import PROVIDERS
    path = urlsplit(url).path
    # Exact observed search endpoints, including minimaks /catalog/, do not
    # imply that a child /catalog/... path is a listing.
    provider = PROVIDERS.get(host)
    if provider and path.rstrip('/') == urlsplit(provider.endpoint).path.rstrip('/'):
        return 'supplier_search_endpoint'
    # Frozen TASK-0071 saved queue labels positively identify these routes.
    if host == 'rs24.ru' and path.startswith('/series/'):
        return 'rs24_series'
    if host == 'vseinstrumenti.ru' and path.startswith('/tag-page/'):
        return 'vseinstrumenti_tag_page'
    if host == 'chipdip.ru' and path.startswith('/catalog-show/'):
        return 'chipdip_catalog_show'
    if host == 'thermopat.com' and path.rstrip('/') == '/products':
        return 'thermopat_products_index'
    # TASK162 retained this exact self-canonical filter page with multiple
    # product cards. Other catalog/filter routes remain ordinary candidates.
    if host == 'ledpremium.ru' and path.rstrip('/') == '/catalog/silovoy_kabel/filter/zhily-4':
        return 'ledpremium_four_core_cable_filter'
    # Exact saved TASK174 category; ETM /cat/... cards remain candidates.
    if host == 'etm.ru' and path.rstrip('/') == '/catalog/1635_aksessuary_dlja_kabelnyh_lotkov-138_dkc':
        return 'etm_dkc_cable_tray_accessories_category'
    return ''


def _trusted_hint(hint, url, host):
    from .supplier_discovery import CandidateHint, PROVIDERS
    if type(hint) is not dict or set(hint) != set(CandidateHint.__dataclass_fields__):
        return False
    strings = ('url', 'title', 'manufacturer_article', 'brand', 'source_role',
               'source_url', 'source_sha256')
    if any(type(hint[k]) is not str for k in strings):
        return False
    if hint['source_role'] != 'search_result_owner' or hint['url'] != url:
        return False
    provider = PROVIDERS.get(host)
    if not provider or card_host(hint['source_url']) != host:
        return False
    if urlsplit(hint['source_url']).path != urlsplit(provider.endpoint).path:
        return False
    stamp = hint['observed_at']
    return (bool(re.fullmatch('[a-f0-9]{64}', hint['source_sha256']))
            and type(stamp) in (int, float) and math.isfinite(stamp)
            and 0 < stamp <= time.time()
            and all(len(hint[k]) <= 4096 for k in ('title', 'manufacturer_article', 'brand')))


def admit_candidate(url, host, position):
    """Reject only positive route/source evidence; unknown means normal capture."""
    if not canonical_card_url(url) or card_host(url) != host:
        return CandidateAdmission()
    route = _listing_route(url, host)
    if route:
        return CandidateAdmission('candidate_known_listing', (('route', route),), time.time())
    return CandidateAdmission()


def _power_values(text):
    # Unit-bearing standalone values only. Ranges, alternatives and bounds
    # cannot establish a single incompatible rating, including Unicode dashes.
    number = r'\d+(?:[.,]\d+)?'
    unit = r'(?:ВА|VA)(?!\w)'
    range_pattern = number + r'\s*(?:' + unit + r')?\s*(?:[-‐‑‒–—−/…~]|\bto\b|\bдо\b)\s*' + number + r'\s*' + unit
    qualifiers = r'[<>≤≥≈±~]|\b(?:до|от|более|менее|макс\w*|миним\w*|максим\w*|min|max|over|under|above|below|from|upto)\b|\b(?:up\s+to|at\s+(?:least|most))\b'
    if re.search(range_pattern, text, re.I) or re.search(qualifiers, text, re.I):
        return set()
    return {Decimal(value.replace(',', '.')) for value in re.findall(
        r'(?<![\w.,/\-‐‑‒–—−])(' + number + r')\s*' + unit, text, re.I)}


def admit_with_hint(url, host, position, hint=None):
    decision = admit_candidate(url, host, position)
    if decision.rejected or not _trusted_hint(hint, url, host):
        return decision
    role = native_request_role(position)
    if role == 'unknown':
        # Preserve the pre-existing non-native hint check. Native rows whose
        # cell binding is ambiguous must never fall back to that legacy SKU
        # heuristic (a native model may legitimately have a different MPN).
        if getattr(position, 'source_evidence', None):
            return decision
        from .supplier_discovery import candidate_hint_reason
        reason = candidate_hint_reason(hint, url, host, position)
        if not reason:
            return decision
        return CandidateAdmission(reason, (('request_role', 'legacy_existing_check'),
            ('source_url', hint['source_url'][:512]),
            ('source_sha256', hint['source_sha256'])), hint['observed_at'])
    cells = position.source_evidence['cells']
    from .agent import _conflicting_page_article, _conflicting_full_model_code
    article = str(cells.get('article') or '').strip()
    model = str(cells.get('model') or '').strip()
    reason = ''
    if role == 'article' and _conflicting_page_article(article, hint['manufacturer_article'], request_role='article'):
        reason = 'discovery_article_conflict'
    elif (model and _conflicting_full_model_code(model, hint['title'])
          and _conflicting_full_model_code(hint['title'], model)):
        reason = 'discovery_identity_conflict'
    wanted_power = _power_values(position.name)
    observed_power = _power_values(hint['title'])
    if (not reason and len(wanted_power) == len(observed_power) == 1
        and wanted_power.isdisjoint(observed_power)):
        reason = 'discovery_power_conflict'
    maker = str(cells.get('maker') or '').strip()
    if not reason and maker and hint['brand']:
        from .manufacturer_aliases import manufacturer_equal
        if not manufacturer_equal(maker, hint['brand']):
            reason = 'discovery_manufacturer_conflict'
    if not reason:
        return decision
    return CandidateAdmission(reason, (('request_role', role),
        ('source_sha256', hint['source_sha256']), ('source_url', hint['source_url'][:512]),
        ('article', article), ('model', model), ('maker', maker),
        ('requested_name', position.name[:512]), ('title', hint['title'][:512]),
        ('manufacturer_article', hint['manufacturer_article'][:256]),
        ('brand', hint['brand'][:128])), hint['observed_at'])
