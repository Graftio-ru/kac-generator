"""Реквизиты продавца с его собственного сайта (раздел «Контакты»/«Реквизиты»/подвал).

Альтернатива checko.ru/ФНС, которые блокируют зарубежный IP (VPN). Сайты самих магазинов
под VPN открываются, и продавцы публикуют там свои ИНН/ОГРН/наименование. ИНН со страницы
товара обычно отсутствует — поэтому добираем с типовых страниц реквизитов.

Результат кэшируется по хосту: у одного магазина реквизиты общие для всех позиций.
"""
from __future__ import annotations

from ..search.failure_policy import ResearchControlError
import re
import sys
from urllib.parse import urlparse

from ..llm import complete_json, ModelOutputInvalid
from ..models import OrgStatus, Requisites
from ..search.session import SearchBudgetExceeded, bounded_timeout


def _log(msg: str) -> None:
    """Диагностика добора реквизитов в окно движка (start.command)."""
    print(f"[реквизиты] {msg}", file=sys.stderr, flush=True)


# Frozen Unicode 15.0.0 decimal alphabet, shared exactly with web/common.js.
# Runtime Unicode upgrades must not silently change seller identity admission.
SELLER_DECIMAL_ZEROS = (48, 1632, 1776, 1984, 2406, 2534, 2662, 2790, 2918, 3046, 3174, 3302, 3430, 3558, 3664, 3792, 3872, 4160, 4240, 6112, 6160, 6470, 6608, 6784, 6800, 6992, 7088, 7232, 7248, 42528, 43216, 43264, 43472, 43504, 43600, 44016, 65296, 66720, 68912, 69734, 69872, 69942, 70096, 70384, 70736, 70864, 71248, 71360, 71472, 71904, 72016, 72784, 73040, 73120, 73552, 92768, 92864, 93008, 120782, 120792, 120802, 120812, 120822, 123200, 123632, 124144, 125264, 130032)
_SELLER_DECIMAL_DIGITS = {chr(z + d): str(d) for z in SELLER_DECIMAL_ZEROS for d in range(10)}


def canonical_inn(inn: str) -> str:
    """Identity-only frozen decimal normalization; retain original source fields."""
    return ''.join(_SELLER_DECIMAL_DIGITS.get(c, '') for c in str(inn or ''))


def valid_inn(inn: str) -> bool:
    """Проверяет контрольные цифры ИНН (10 или 12 знаков). Отсекает выдуманные/ошибочные."""
    digits = canonical_inn(inn)
    if not digits or not any(c != '0' for c in digits):
        return False
    d = [int(c) for c in digits]
    if len(d) == 10:
        w = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        return sum(w[i] * d[i] for i in range(9)) % 11 % 10 == d[9]
    if len(d) == 12:
        w1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        w2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        return (sum(w1[i] * d[i] for i in range(10)) % 11 % 10 == d[10]
                and sum(w2[i] * d[i] for i in range(11)) % 11 % 10 == d[11])
    return False

_REQ_SYSTEM = (
    "Из текста страниц интернет-магазина извлеки реквизиты юрлица-продавца (не производителя "
    "товара, а самого магазина). Верни JSON: {name: полное наименование (ООО/АО/ИП…), "
    "inn: ИНН (10 или 12 цифр), kpp: КПП, ogrn: ОГРН или ОГРНИП, address: юридический адрес, "
    "city: явно указанный населённый пункт склада}. Юридический адрес не подтверждает расположение склада. Поле, которого нет в тексте, — пустая строка. Только JSON."
)

# Типовые пути к странице с реквизитами (по убыванию вероятности). Пустой путь — главная (подвал).
_REQ_PATHS = ["", "/contacts", "/kontakty", "/contact", "/about", "/o-kompanii", "/info",
              "/oplata-i-dostavka", "/rekvizity", "/requisites", "/payment", "/dostavka-i-oplata"]
_SITE_REQ_PATHS = {
    "elektromehanika.ru": ["/index.php?sid=60"],
    "electro-mpo.ru": ["/about/terms/"],
    "etm.ru": ["/company/contacts/rekvizity/"],
}


def _requisites_paths(host: str) -> list[str]:
    """Visit a site's published requisites page before generic guesses."""
    return _SITE_REQ_PATHS.get(host, []) + _REQ_PATHS

# Похоже на «ИНН 7701234567» — признак, что страница содержит реквизиты. Допускаем пробелы
# внутри числа (часто верстают «ИНН 7733 123456»).
_INN_RE = re.compile(r"ИНН[\s:№]*(\d[\d\s]{8,13}\d)", re.IGNORECASE)
_SELLER_TERMS_RE = re.compile(
    r"(?im)^\s*Продавец:\s*(?P<name>(?:ЗАО|АО|ООО|ПАО)\s+[^\n,]{2,150}),"
    r"\s*ИНН\s*(?P<inn>\d{10}|\d{12})(?!\d)"
)


def _electro_mpo_terms_seller(body: str) -> Requisites | None:
    """The site's purchase terms explicitly identify the seller of its goods."""
    match = _SELLER_TERMS_RE.search(body)
    if not match or not valid_inn(match.group("inn")):
        return None
    return Requisites(name=" ".join(match.group("name").split()),
                      inn=match.group("inn"),
                      status=OrgStatus.SUPPLIER)


def _etm_published_requisites(body: str) -> Requisites | None:
    """Read labeled corporate requisites linked directly from ETM product cards."""
    match = re.search(
        r"Полное наименование\s*\n\s*(?P<name>[^\n]+)\s*\n"
        r"ИНН\s*\n\s*(?P<inn>\d{10}|\d{12})\s*\n"
        r"КПП\s*\n\s*(?P<kpp>\d{9})(?!\d)",
        body, re.IGNORECASE,
    )
    if not match or not valid_inn(match.group("inn")):
        return None
    address = re.search(r"Адрес юридического лица\s*\n\s*([^\n]+)",
                        body, re.IGNORECASE)
    return Requisites(name=" ".join(match.group("name").split()),
                      inn=match.group("inn"), kpp=match.group("kpp"),
                      address=address.group(1).strip() if address else "",
                      status=OrgStatus.SUPPLIER)


def _relevant_chunk(body: str) -> str:
    """Возвращает фрагмент с реквизитами: окно вокруг ИНН (если найден), иначе хвост страницы."""
    m = _INN_RE.search(body)
    if m:
        start = max(0, m.start() - 1500)
        return body[start:m.end() + 1500]
    return body[-3500:]


_cache: dict[str, Requisites | None] = {}


def fetch_requisites_from_site(browser, product_url: str, *, capture_context=None) -> Requisites | None:
    """Ищет реквизиты продавца на его сайте. None, если ИНН найти не удалось.

    Обходит несколько типовых страниц, собирая «подвальный» текст; как только встречает
    шаблон ИНН — отдаёт собранное в LLM. Не делает больше 4 успешных загрузок на хост.
    """
    p = urlparse(product_url)
    if not p.netloc:
        return None
    host = p.netloc.replace("www.", "")
    cached = _cache.get(host)
    if cached is not None and cached.name.strip() and valid_inn(cached.inn):
        return cached

    base = f"{p.scheme}://{p.netloc}"
    texts: list[str] = []
    inn_seen = False
    loaded = 0
    for path in _requisites_paths(host):
        if loaded >= 5 or (inn_seen and texts):
            break
        try:
            with browser.page(base + path) as page:
                page.wait_for_timeout(max(1, int(bounded_timeout(0.9) * 1000)))
                body = page.inner_text("body")
                terms_ref = retain_contextual_seller_terms(body, page.url, product_url, capture_context=capture_context)
                if terms_ref:
                    source = read_contextual_seller_terms(terms_ref, product_url, capture_context["captured_at"])
                    seller = Requisites(name=source["raw_name"], inn=source["raw_inn"], seller_source=terms_ref, status=OrgStatus.SUPPLIER)
                    _cache[host] = seller
                    return seller
        except SearchBudgetExceeded:
            raise
        except ResearchControlError:
            raise
        except Exception as e:
            _log(f"  {host}{path} → не загрузилась: {type(e).__name__}")
            continue
        loaded += 1
        if host == "electro-mpo.ru" and path == "/about/terms/":
            seller = _electro_mpo_terms_seller(body)
            if seller:
                _log(f"{host}: ✓ продавец подтверждён условиями покупки, ИНН {seller.inn}")
                _cache[host] = seller
                return seller
        if host == "etm.ru" and path == "/company/contacts/rekvizity/":
            seller = _etm_published_requisites(body)
            if seller:
                _log(f"{host}: ✓ реквизиты подтверждены страницей сайта, ИНН {seller.inn}")
                _cache[host] = seller
                return seller
        has_inn = bool(_INN_RE.search(body))
        _log(f"  {host}{path or '/'} → {len(body)} симв., ИНН на странице: {'да' if has_inn else 'нет'}")
        texts.append(_relevant_chunk(body))
        if has_inn:
            inn_seen = True

    if not texts:
        _log(f"{host}: ни одна страница не загрузилась")
        return None

    combined = "\n---СТРАНИЦА---\n".join(texts)[:13000]
    try:
        data = complete_json(f"Магазин: {host}\n\n{combined}", system=_REQ_SYSTEM, smart=True)
    except SearchBudgetExceeded:
        raise
    except ResearchControlError:
        raise
    except Exception as e:
        _log(f"{host}: LLM-извлечение упало: {type(e).__name__}: {e}")
        return None

    if not isinstance(data, dict):
        raise ModelOutputInvalid('invalid_schema')
    inn = re.sub(r"\D", "", str(data.get("inn") or ""))
    if not valid_inn(inn):
        _log(f"{host}: ИНН не извлечён/невалиден (LLM вернул {data.get('inn')!r}); "
             f"страниц с ИНН: {inn_seen}")
        return None
    name = str(data.get("name") or "").strip()
    if not name:
        _log(f"{host}: ИНН найден, но имя продавца не подтверждено")
        return None
    _log(f"{host}: ✓ ИНН {inn}, {name!r}")

    req = Requisites(
        name=name,
        inn=inn,
        kpp=re.sub(r"\D", "", str(data.get("kpp") or "")),
        ogrn=re.sub(r"\D", "", str(data.get("ogrn") or "")),
        address=str(data.get("address") or "").strip(),
        city=str(data.get("city") or "").strip(),
        status=OrgStatus.SUPPLIER,
    )
    _cache[host] = req
    return req

# Immutable seller-role facts. These functions are pure; trusted producers supply
# the sources, and catalogue projection binds their exact stored envelope.
SELLER_EVIDENCE_VERSION = 'seller-evidence-v1'
_SELLER_ORIGINS = {'scoped_offer', 'published_seller_terms', 'approved_supplier_reference', 'identity_enrichment'}
_SELLER_ROLES = {'seller', 'manufacturer', 'marketplace_operator', 'unknown'}

def _seller_json(value):
    import json
    try:return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    except (RecursionError, TypeError) as error:raise ValueError('Invalid seller evidence encoding') from error

def _seller_digest(value):
    import hashlib
    return hashlib.sha256(_seller_json(value).encode()).hexdigest()

def _seller_url(value):
    if type(value) is not str or len(value) > 2048:
        raise ValueError('Invalid seller source URL')
    parsed = urlparse(value)
    parsed.port  # Reject malformed authority/port before using source ownership.
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Invalid seller source URL')
    return value

def _seller_stamp(value):
    import math, time
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and 0 < value <= time.time() + 300
    except OverflowError:
        valid = False
    if not valid:raise ValueError('Invalid seller source date')

def _seller_text(value, maximum=2048, empty=False):
    if type(value) is not str or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError('Invalid seller source text')

def _seller_hex(value):
    if type(value) is not str or len(value) != 64 or not re.fullmatch('[a-f0-9]{64}', value):
        raise ValueError('Invalid seller source hash')

def seller_source(origin, role, name, inn, *, source_url, source_sha256,
                  source_observed_at, role_text, binding):
    """Build a source assertion, not an authorization for client-supplied facts."""
    for text in (name, inn, role_text):_seller_text(text, empty=True)
    _seller_text(origin, 32);_seller_text(role, 32)
    _seller_url(source_url);_seller_hex(source_sha256);_seller_stamp(source_observed_at)
    item = dict(origin=origin, role=role, raw_name=name, raw_inn=inn,
                source_url=source_url, source_sha256=source_sha256,
                source_observed_at=source_observed_at, role_text=role_text,
                binding=binding)
    if type(binding) is not dict:raise ValueError('Invalid seller source binding')
    probe={**item,'id':'0'*64}
    _seller_evidence_shape(dict(version=SELLER_EVIDENCE_VERSION, product_url=binding.get('product_url'),
        captured_at=binding.get('captured_at'), sources=[probe],
        decision=dict(status='unknown',canonical_inn=None,reason='seller_role_unconfirmed',resolved_source_ids=[])))
    item['id'] = _seller_digest(item)
    return item

def _seller_decision(sources):
    qualified = [s for s in sources if s['role'] == 'seller'
                 and s['origin'] != 'identity_enrichment'
                 and s['raw_name'].strip() and valid_inn(s['raw_inn'])]
    identities = {canonical_inn(s['raw_inn']) for s in qualified}
    if len(identities) > 1:
        return dict(status='conflict', canonical_inn=None, reason='seller_identity_conflict', resolved_source_ids=[])
    if not qualified:
        return dict(status='unknown', canonical_inn=None, reason='seller_role_unconfirmed', resolved_source_ids=[])
    current = [s for s in qualified if s['origin'] != 'approved_supplier_reference']
    return dict(status='role_confirmed' if current else 'reference_accepted',
                canonical_inn=next(iter(identities)), reason='',
                resolved_source_ids=[s['id'] for s in qualified])

def make_seller_evidence(product_url, captured_at, sources):
    import json
    if type(sources) is not list or len(sources) > 8:raise ValueError('Seller source count exceeded')
    value = dict(version=SELLER_EVIDENCE_VERSION, product_url=product_url,
                 captured_at=captured_at, sources=sources,
                 decision=dict(status='unknown',canonical_inn=None,reason='seller_role_unconfirmed',resolved_source_ids=[]))
    _seller_evidence_shape(value, product_url=product_url, captured_at=captured_at)
    value['decision'] = _seller_decision(sources)
    validate_seller_evidence(value, product_url=product_url, captured_at=captured_at)
    return json.loads(_seller_json(value))

def _seller_evidence_shape(value, *, product_url=None, captured_at=None):
    """Shared bounded preflight, before decision/normalization/digest work."""
    fields = {'version', 'product_url', 'captured_at', 'sources', 'decision'}
    if type(value) is not dict or len(value) != len(fields) or set(value) != fields or value['version'] != SELLER_EVIDENCE_VERSION:
        raise ValueError('Invalid seller evidence version/shape')
    _seller_url(value['product_url']); _seller_stamp(value['captured_at'])
    if product_url is not None and value['product_url'] != product_url or captured_at is not None and value['captured_at'] != captured_at:
        raise ValueError('Seller evidence belongs to another observation')
    sources = value['sources']
    if type(sources) is not list or len(sources) > 8:
        raise ValueError('Seller source count exceeded')
    decision = value['decision']
    dfields = {'status', 'canonical_inn', 'reason', 'resolved_source_ids'}
    if type(decision) is not dict or len(decision) != len(dfields) or set(decision) != dfields:
        raise ValueError('Invalid seller decision shape')
    _seller_text(decision['status'], 32); _seller_text(decision['reason'], 64, empty=True)
    if decision['status'] not in {'role_confirmed', 'reference_accepted', 'unknown', 'conflict'} or decision['reason'] not in {'', 'seller_identity_conflict', 'seller_role_unconfirmed'}:
        raise ValueError('Invalid seller decision state')
    if decision['canonical_inn'] is not None:
        _seller_text(decision['canonical_inn'], 12)
        if not re.fullmatch(r'[0-9]{10}|[0-9]{12}', decision['canonical_inn']):
            raise ValueError('Invalid canonical seller identity')
    ids = decision['resolved_source_ids']
    if type(ids) is not list or len(ids) > 8:
        raise ValueError('Seller resolved source count exceeded')
    for ident in ids:_seller_hex(ident)
    seen = set()
    for item in sources:
        fields = {'id', 'origin', 'role', 'raw_name', 'raw_inn', 'source_url', 'source_sha256', 'source_observed_at', 'role_text', 'binding'}
        if type(item) is not dict or len(item) != len(fields) or set(item) != fields:
            raise ValueError('Invalid seller assertion shape')
        _seller_text(item['origin'],32);_seller_text(item['role'],32)
        if item['origin'] not in _SELLER_ORIGINS or item['role'] not in _SELLER_ROLES:
            raise ValueError('Invalid seller assertion role')
        _seller_hex(item['id']); _seller_hex(item['source_sha256'])
        for name in ('raw_name', 'raw_inn', 'role_text'):_seller_text(item[name], empty=True)
        _seller_url(item['source_url']); _seller_stamp(item['source_observed_at'])
        if item['source_observed_at'] > value['captured_at'] + 300:
            raise ValueError('Seller source follows observation')
        binding = item['binding']; common = {'product_url', 'captured_at'}
        extra = {'scoped_offer': {'record_sha256', 'body_sha256', 'scope_sha256'},
                 'approved_supplier_reference': {'source_ref', 'row_sha256'},
                 'published_seller_terms': {'retained_ref', 'context_sha256'},
                 'identity_enrichment': set()}[item['origin']]
        expected = common | extra
        if type(binding) is not dict or len(binding) != len(expected) or set(binding) != expected:
            raise ValueError('Invalid seller assertion binding shape')
        _seller_url(binding['product_url']);_seller_stamp(binding['captured_at'])
        if binding['product_url'] != value['product_url'] or binding['captured_at'] != value['captured_at']:
            raise ValueError('Foreign seller assertion binding')
        for name in extra:
            if name.endswith('sha256'):_seller_hex(binding[name])
            else:
                _seller_text(binding[name], 2048)
                if binding[name].startswith('/') or '..' in binding[name].split('/'):
                    raise ValueError('Unowned seller source reference')
        if item['origin'] == 'scoped_offer' and (item['source_sha256'] != binding['body_sha256'] or item['source_url'] != value['product_url'] or item['source_observed_at'] != value['captured_at']):
            raise ValueError('Seller Offer generation differs')
    # Every possible container/string is now bounded. Only then encode/hash.
    if len(_seller_json(value).encode()) > 16384:
        raise ValueError('Seller evidence limit exceeded')
    return value


def validate_seller_evidence(value, *, product_url=None, captured_at=None):
    _seller_evidence_shape(value,product_url=product_url,captured_at=captured_at)
    sources=value['sources'];decision=value['decision'];seen=set()
    for item in sources:
        binding=item['binding']
        if item['origin'] == 'published_seller_terms':
            import hashlib
            if binding['context_sha256'] != hashlib.sha256(item['role_text'].encode()).hexdigest():
                raise ValueError('Seller terms context changed')
        if item['id'] != _seller_digest({k:v for k,v in item.items() if k != 'id'}) or item['id'] in seen:
            raise ValueError('Seller assertion changed/duplicated')
        seen.add(item['id'])
    if decision != _seller_decision(sources):
        raise ValueError('Seller decision differs from source assertions')
    return value

def seller_evidence_reason(value, *, product_url=None, captured_at=None):
    if value is None:
        return ''  # Historical/manual compatibility; never current-role proof.
    try:
        evidence = validate_seller_evidence(value, product_url=product_url, captured_at=captured_at)
    except (ValueError, TypeError, OverflowError, KeyError):
        return 'Сведения о происхождении реквизитов продавца повреждены'
    if evidence['decision']['status'] == 'conflict':
        return 'Реквизиты продавца противоречат текущему источнику или справочнику'
    if evidence['decision']['status'] == 'unknown':
        return 'Роль продавца не подтверждена источником'
    return ''

def seller_origin_notice(value):
    if value is None:
        return 'Происхождение реквизитов не сохранено: прежние данные'
    reason = seller_evidence_reason(value)
    if reason:
        return reason
    return ('Продавец указан в текущем источнике' if value['decision']['status'] == 'role_confirmed'
            else 'Реквизиты из утверждённого справочника поставщиков')


def captured_seller_evidence(facts, terms_ref=None):
    """Rederive from retained Offer and trusted approved-loader snapshot. No I/O wires."""
    import hashlib
    from ..search.whitelist import read_seller_reference, frozen_seller_reference
    url = facts['url']; stamp = facts['captured_at']; sources = []
    frozen = frozen_seller_reference(url,stamp)
    claim = facts.get('seller_reference_snapshot')
    if claim is not None and claim != frozen:
        raise ValueError('Seller reference claim differs from server capture context')
    reference = read_seller_reference(frozen,url,stamp) if frozen is not None else None
    if reference:
        ref = reference['seller_reference']
        sources.append(seller_source('approved_supplier_reference', 'seller',
            reference['name'], reference['inn'], source_url=reference['url'],
            source_sha256=ref['source_sha256'], source_observed_at=ref['source_observed_at'],
            role_text=ref['row_text'], binding=dict(product_url=url, captured_at=stamp,
                source_ref=ref['source_ref'], row_sha256=ref['row_sha256'])))
    if facts.get('http_source'):
        from ..search.http_card import read_source
        retained = read_source(facts['http_source'])
        if not retained:
            raise ValueError('Seller HTTP source missing')
        record, owned = retained
        if record['url'] != url or record['observed_at'] != stamp:
            raise ValueError('Seller HTTP source belongs to another observation')
        merchants = owned.get('merchant_assertions')
        if merchants is None:
            merchant = owned.get('merchant_requisites') or {}
            merchants = [merchant] if merchant else []
        if owned.get('merchant_assertions_overflow') or len(merchants) > 6:
            return make_seller_evidence(url, stamp, [])  # Unknown, never reference/terms borrowing.
        for merchant in merchants:
            source = facts['http_source']
            sources.append(seller_source('scoped_offer', 'seller',
                merchant.get('name', ''), merchant.get('inn', ''), source_url=url,
                source_sha256=record['body_sha256'], source_observed_at=stamp,
                role_text=_seller_json(merchant), binding=dict(product_url=url, captured_at=stamp,
                    record_sha256=source['record_sha256'], body_sha256=record['body_sha256'],
                    scope_sha256=owned['scope_sha256'])))
    if frozen is None:
        return make_seller_evidence(url, stamp, [])  # Unbound capture cannot suppress a reference conflict.
    if terms_ref is not None:
        try:sources.append(read_contextual_seller_terms(terms_ref, url, stamp))
        except (ValueError, TypeError, OSError, KeyError):pass  # unbound cache remains a hint, not seller proof
    return make_seller_evidence(url, stamp, sources)

def seller_evidence_requisites(value, fallback=None):
    """Keep source strings, with no arbitrary party choice on conflict/unknown."""
    validate_seller_evidence(value)
    if seller_evidence_reason(value):
        return fallback.model_copy(deep=True) if fallback is not None else Requisites()
    ids = value['decision']['resolved_source_ids']
    sources = [s for s in value['sources'] if s['id'] in ids]
    chosen = next((s for s in sources if s['origin'] != 'approved_supplier_reference'), sources[0])
    original = fallback.model_copy(deep=True) if fallback is not None else Requisites()
    original.name = chosen['raw_name']; original.inn = chosen['raw_inn']
    return original


def _contextual_terms_seller(body, product_url):
    """One explicit seller sentence bound to this product, never domain inference."""
    if type(body) is not str or len(body.encode()) > 131072:
        return None
    # A legal-name field is one literal quotation, not arbitrary role prose.
    name_token = r'\w(?:[\w .&/-]{0,148}\w)?'
    quoted = r'(?:«' + name_token + r'»|"' + name_token + r'")'
    pattern = (r'(?i)^\s*Продавец\s+товара\s+' + re.escape(product_url)
               + r'\s*:\s*(?P<name>(?:ЗАО|АО|ООО|ПАО)\s+' + quoted + r'),'
                 r'\s*ИНН\s*(?P<inn>\d{10}|\d{12})(?!\d)[ \t]*\.?[ \t]*$')
    # Plain text has no independent field boundary. Consume the entire owned
    # clause; do not ignore a contradictory next line or neighbouring INN.
    match = re.fullmatch(pattern, body.strip())
    if match is None or not valid_inn(match['inn']):
        return None
    return dict(name=match['name'].strip(), inn=match['inn'], role_text=match[0].strip())


def retain_contextual_seller_terms(body, source_url, product_url, *, capture_context=None):
    """Retain only already loaded explicitly product-bound terms; no extra fetch."""
    import hashlib, time
    from ..config import CACHE_DIR
    import json
    if not _contextual_terms_seller(body, product_url):
        return None
    if type(capture_context) is not dict or set(capture_context) != {'product_url','captured_at'} or capture_context['product_url'] != product_url:
        return None  # Legacy API calls cannot mint current capture-role proof.
    _seller_stamp(capture_context['captured_at'])
    _seller_url(source_url); _seller_url(product_url)
    if urlparse(source_url).hostname.removeprefix('www.') != urlparse(product_url).hostname.removeprefix('www.'):
        return None  # Necessary owner boundary plus explicit product-role sentence.
    record = dict(version='seller-terms-record-v1', source_url=source_url,
                  product_url=product_url, capture_context=dict(capture_context), observed_at=time.time(), body=body,
                  body_sha256=hashlib.sha256(body.encode()).hexdigest())
    encoded = _seller_json(record).encode(); digest = hashlib.sha256(encoded).hexdigest()
    root = CACHE_DIR.resolve(); folder = root/'seller_terms'
    folder.mkdir(parents=True, exist_ok=True)
    if folder.is_symlink() or folder.resolve().parent != root:
        raise ValueError('Unowned seller terms directory')
    path = folder/(digest+'.json')
    if path.is_symlink():raise ValueError('Unowned seller terms record')
    if not path.exists():
        with path.open('xb') as output:output.write(encoded)
    elif path.read_bytes() != encoded:raise ValueError('Seller terms record changed')
    return dict(ref='seller_terms/'+path.name, record_sha256=digest)


def read_contextual_seller_terms(reference, product_url, captured_at):
    import hashlib, json
    from pathlib import Path
    from ..config import CACHE_DIR
    if type(reference) is not dict or set(reference) != {'ref', 'record_sha256'}:
        raise ValueError('Invalid seller terms reference')
    _seller_hex(reference['record_sha256'])
    name = reference['ref']
    if type(name) is not str or not re.fullmatch(r'seller_terms/[a-f0-9]{64}\.json', name):
        raise ValueError('Unowned seller terms reference')
    root=CACHE_DIR.resolve(); path=root/name
    if path.is_symlink() or path.parent.is_symlink() or path.resolve().parent.parent != root or path.stat().st_size > 262144:
        raise ValueError('Unowned seller terms record')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=reference['record_sha256']:
        raise ValueError('Seller terms record changed')
    record=json.loads(raw)
    if type(record) is not dict or set(record) != {'version','source_url','product_url','capture_context','observed_at','body','body_sha256'} or record['version']!='seller-terms-record-v1' or record['product_url']!=product_url:
        raise ValueError('Foreign seller terms record')
    if record['capture_context'] != dict(product_url=product_url,captured_at=captured_at):
        raise ValueError('Seller terms belong to another capture generation')
    _seller_url(record['source_url']);_seller_stamp(record['observed_at'])
    if urlparse(record['source_url']).hostname.removeprefix('www.') != urlparse(product_url).hostname.removeprefix('www.'):
        raise ValueError('Foreign seller terms owner')
    if record['observed_at'] > captured_at+300 or hashlib.sha256(record['body'].encode()).hexdigest()!=record['body_sha256']:
        raise ValueError('Seller terms generation changed')
    merchant=_contextual_terms_seller(record['body'],product_url)
    if not merchant:raise ValueError('Seller role absent from product terms')
    return seller_source('published_seller_terms','seller',merchant['name'],merchant['inn'],
        source_url=record['source_url'],source_sha256=record['body_sha256'],
        source_observed_at=record['observed_at'],role_text=merchant['role_text'],
        binding=dict(product_url=product_url,captured_at=captured_at,retained_ref=name,
                     context_sha256=hashlib.sha256(merchant['role_text'].encode()).hexdigest()))
