"""One operator-approved site inventory; legal references remain separate."""
from __future__ import annotations

import re
import hashlib
import time
import copy
from urllib.parse import urlparse

from ..config import SUPPLIERS_FILE
from ..models import OrgStatus, Requisites


def _host(url: str) -> str:
    # WHATWG treats backslashes as authority separators, unlike urllib.
    # Reject ambiguous inputs before either regional or legal-reference lookup.
    if type(url) is not str or re.search(r'[\\\x00-\x20\x7f]', url):
        return ''
    try:
        parsed = urlparse(url if "//" in url else "//" + url)
        if parsed.username is not None or parsed.password is not None:
            return ''
        parsed.port  # Validate malformed/ambiguous port authorities as well.
        if parsed.scheme and parsed.scheme not in {'http', 'https'}:
            return ''
        return (parsed.hostname or '').lower().removeprefix('www.').rstrip('.')
    except (TypeError, ValueError):
        return ''


def _load_primary() -> dict[str, dict]:
    """Парсит таблицу Поставщики.md → {host: {name, kpp, inn, url}}."""
    out: dict[str, dict] = {}
    if not SUPPLIERS_FILE.exists():
        return out
    raw = SUPPLIERS_FILE.read_bytes()
    source_hash = hashlib.sha256(raw).hexdigest()
    loaded_at = time.time()
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        name, kpp, inn, url = cells[0], cells[1], cells[2], cells[3]
        # пропускаем шапку и разделитель
        if not re.search(r"https?://", url) or not inn.isdigit():
            continue
        host = _host(url)
        if host:
            out[host] = {"name": name, "kpp": kpp, "inn": inn, "url": url,
                         "seller_reference": {"source_ref": "approved-supplier-list",
                             "source_sha256": source_hash, "source_observed_at": loaded_at,
                             "row_text": line, "row_sha256": hashlib.sha256(line.encode()).hexdigest()}}
    return out


def _load_hosts(path) -> list[str]:
    """Читает URL из Markdown-таблицы, сохраняя порядок и удаляя дубли."""
    if not path.exists():
        return []
    seen: set[str] = set()
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip().startswith("|"):
            continue
        for cell in (c.strip() for c in line.strip().strip("|").split("|")):
            match = re.search(r"https?://[^\s)]+", cell)
            if not match:
                continue
            host = _host(match.group(0))
            if host and host not in seen:
                seen.add(host)
                out.append(host)
            break
    return out


_BY_HOST = _load_primary()
_ACTIVE_HOSTS = _load_hosts(SUPPLIERS_FILE)
_ACTIVE_SOURCE_SHA = hashlib.sha256(SUPPLIERS_FILE.read_bytes()).hexdigest() if SUPPLIERS_FILE.exists() else ''
_NON_DIRECT_SOURCES = {
    "poisk.im": "aggregator",
    "russvet.ru": "corporate_site",
}


def source_type(host: str) -> str:
    """Classify list entries without changing the operator's supplier files."""
    host = host.lower().removeprefix("www.")
    return next((kind for domain, kind in _NON_DIRECT_SOURCES.items()
                 if host == domain or host.endswith(f".{domain}")), "direct_supplier")


def whitelist_hosts() -> list[str]:
    _refresh_seller_reference()
    return list(_ACTIVE_HOSTS)


def fallback_hosts() -> list[str]:
    """Inert legacy alias: every active site belongs to whitelist_hosts()."""
    return []


def region_approval_for(url: str) -> dict | None:
    """Current server list authority, never card stock/pickup or seller evidence."""
    _refresh_seller_reference()
    host = _host(url)
    domain = next((item for item in _ACTIVE_HOSTS
                   if host == item or host.endswith('.' + item)), None)
    if not domain:
        return None
    return {'policy': 'operator-approved-sites-moscow-v1',
            'source_ref': 'approved-supplier-list', 'source_sha256': _ACTIVE_SOURCE_SHA,
            'host': domain}


def _primary_record(url: str) -> dict | None:
    """Находит основной домен и для его региональных поддоменов."""
    _refresh_seller_reference()
    host = _host(url)
    if host in _BY_HOST:
        return _BY_HOST[host]
    return next((record for primary_host, record in _BY_HOST.items()
                 if host.endswith(f".{primary_host}")), None)


def is_whitelisted(url: str) -> bool:
    return region_approval_for(url) is not None


def requisites_for(url: str) -> Requisites | None:
    """Готовые реквизиты поставщика из файла по хосту URL (или None, если не наш)."""
    rec = _primary_record(url)
    if not rec:
        return None
    return Requisites(name=rec["name"], inn=rec["inn"], kpp=rec["kpp"],
                      city="", status=OrgStatus.SUPPLIER)


def seller_reference_for(url: str) -> dict | None:
    """Refresh only changed file revision; never hash the list per quotation."""
    _refresh_seller_reference()
    record = _primary_record(url)
    if not record or not record.get('seller_reference'):
        return None
    return copy.deepcopy({k: record[k] for k in ('name', 'inn', 'url', 'seller_reference')})


_PRIMARY_REFERENCE_REVISION = None

def _refresh_seller_reference():
    global _BY_HOST, _ACTIVE_HOSTS, _ACTIVE_SOURCE_SHA, _PRIMARY_REFERENCE_REVISION
    try:
        st=SUPPLIERS_FILE.stat()
        revision=(str(SUPPLIERS_FILE),st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns)
    except FileNotFoundError:
        revision=(str(SUPPLIERS_FILE),None)
    if revision != _PRIMARY_REFERENCE_REVISION:
        _BY_HOST = _load_primary()
        _ACTIVE_HOSTS = _load_hosts(SUPPLIERS_FILE)
        _ACTIVE_SOURCE_SHA = hashlib.sha256(SUPPLIERS_FILE.read_bytes()).hexdigest() if SUPPLIERS_FILE.exists() else ''
        _PRIMARY_REFERENCE_REVISION = revision


def capture_seller_reference(product_url, captured_at):
    """Freeze presence/absence once per exact capture, including repeated sinks."""
    import json
    from ..config import CACHE_DIR
    from ..requisites.supplier import _seller_url,_seller_stamp,_seller_json
    _seller_url(product_url);_seller_stamp(captured_at)
    root=CACHE_DIR.resolve();folder=root/'seller_references';folder.mkdir(parents=True,exist_ok=True)
    if folder.is_symlink() or folder.resolve().parent != root:raise ValueError('Unowned seller reference directory')
    context=dict(product_url=product_url,captured_at=captured_at)
    key=hashlib.sha256(_seller_json(context).encode()).hexdigest()
    index=folder/('context-'+key+'.json')
    def existing():
        if index.is_symlink() or index.stat().st_size>256:raise ValueError('Unowned seller reference context')
        reference=json.loads(index.read_bytes())
        read_seller_reference(reference,product_url,captured_at)
        return reference
    if index.exists():return existing()
    source=seller_reference_for(product_url)
    record=dict(version='seller-reference-record-v1',**context,source=source)
    raw=_seller_json(record).encode()
    if len(raw)>16384:raise ValueError('Seller reference record limit exceeded')
    digest=hashlib.sha256(raw).hexdigest();path=folder/(digest+'.json')
    if path.is_symlink():raise ValueError('Unowned seller reference record')
    if not path.exists():
        with path.open('xb') as out:out.write(raw)
    elif path.read_bytes()!=raw:raise ValueError('Seller reference snapshot changed')
    reference=dict(ref='seller_references/'+path.name,record_sha256=digest)
    try:
        with index.open('x') as out:out.write(_seller_json(reference))
    except FileExistsError:return existing()
    return reference


def read_seller_reference(reference, product_url, captured_at):
    import json
    from ..config import CACHE_DIR
    from ..requisites.supplier import _seller_hex,_seller_text,_seller_stamp,_seller_url
    if type(reference) is not dict or len(reference)!=2 or set(reference)!={'ref','record_sha256'}:raise ValueError('Invalid reference snapshot')
    _seller_hex(reference['record_sha256'])
    ref=reference['ref']
    if type(ref) is not str or not re.fullmatch(r'seller_references/[a-f0-9]{64}\.json',ref):raise ValueError('Unowned reference snapshot')
    root=CACHE_DIR.resolve();path=root/ref
    if path.is_symlink() or path.parent.is_symlink() or path.resolve().parent.parent!=root or path.stat().st_size>16384:raise ValueError('Unowned reference snapshot')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=reference['record_sha256']:raise ValueError('Reference snapshot changed')
    record=json.loads(raw)
    if type(record) is not dict or len(record)!=4 or set(record)!={'version','product_url','captured_at','source'} or record['version']!='seller-reference-record-v1' or record['product_url']!=product_url or record['captured_at']!=captured_at:raise ValueError('Reference belongs to another capture')
    source=record['source']
    if source is None:return None
    if type(source) is not dict or len(source)!=4 or set(source)!={'name','inn','url','seller_reference'}:raise ValueError('Invalid approved reference source')
    _seller_text(source['name']);_seller_text(source['inn']);_seller_url(source['url'])
    meta=source['seller_reference']
    fields={'source_ref','source_sha256','source_observed_at','row_text','row_sha256'}
    if type(meta) is not dict or len(meta)!=len(fields) or set(meta)!=fields:raise ValueError('Invalid approved reference source')
    _seller_text(meta['source_ref']);_seller_text(meta['row_text']);_seller_stamp(meta['source_observed_at'])
    _seller_hex(meta['source_sha256']);_seller_hex(meta['row_sha256'])
    if hashlib.sha256(meta['row_text'].encode()).hexdigest()!=meta['row_sha256']:raise ValueError('Approved reference row changed')
    return source


def frozen_seller_reference(product_url, captured_at):
    """Existing server context only; never consult/mint from today's list."""
    import json
    from ..config import CACHE_DIR
    from ..requisites.supplier import _seller_json,_seller_url,_seller_stamp
    _seller_url(product_url);_seller_stamp(captured_at)
    root=CACHE_DIR.resolve();folder=root/'seller_references'
    key=hashlib.sha256(_seller_json(dict(product_url=product_url,captured_at=captured_at)).encode()).hexdigest()
    path=folder/('context-'+key+'.json')
    if not path.exists():return None
    if folder.is_symlink() or folder.resolve().parent!=root or path.is_symlink() or path.stat().st_size>256:raise ValueError('Unowned seller capture context')
    reference=json.loads(path.read_bytes())
    read_seller_reference(reference,product_url,captured_at)
    return reference
