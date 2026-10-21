"""Local projection of server-owned catalogue observations; no acquisition."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import math
import re

_STORE = None
_SCOPED_STORE = ContextVar('catalogue_projection_store', default=None)
_SCOPED_SNAPSHOT = ContextVar('catalogue_projection_snapshot', default=None)

def configure_store(store):
    """Server composition only. Request data must never supply a store."""
    global _STORE
    _STORE = store

@contextmanager
def catalogue_scope(store, *, snapshot=None):
    token = _SCOPED_STORE.set(store)
    snapshot_token = _SCOPED_SNAPSHOT.set(snapshot)
    try:
        yield
    finally:
        _SCOPED_SNAPSHOT.reset(snapshot_token)
        _SCOPED_STORE.reset(token)

def _field(value, name, default=''):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)

def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0

def vat_notice(status):
    if status=='unknown':return 'НДС не подтверждён источником; сохранена исходная цена без налогового пересчёта'
    if status=='excluded':return 'Источник указал цену без НДС; налоговый пересчёт не подтверждён'
    return ''

def _image(observation):
    """Owned local bytes only; S3 materialization belongs to the server adapter."""
    from pathlib import Path
    from ..config import CACHE_DIR
    from ..review_context import context_valid
    image = observation['image']
    evidence = image.get('context_evidence') or {}
    if image.get('status') != 'captured' or not image.get('local_path'):
        return '', {}, image.get('error') or 'Снимок наблюдения не сохранён'
    try:
        root = CACHE_DIR.resolve()
        path = root/image['local_path']
        if path.is_symlink() or path.resolve().parent != root or path.stat().st_size > 20*1024*1024:
            return '', {}, 'Снимок каталога вне разрешённого кэша'
        data = path.read_bytes()
        if (len(data) != image['size'] or hashlib.sha256(data).hexdigest() != image['sha256']
            or image['observation_id'] != observation['observation_id']
            or image['source_url'] != observation['url'] or image['observed_at'] != observation['observed_at']
            or evidence.get('captured_at') != image.get('captured_at')
            or not context_valid(evidence, str(path), observation['url'], data)):
            return '', {}, 'Снимок не соответствует наблюдению каталога'
        return str(path), evidence, ''
    except (OSError, KeyError, TypeError, ValueError):
        return '', {}, 'Сохранённый снимок каталога недоступен'

def _identity_reason(observation, position):
    from .agent import (_type_mark_match_status, _conflicting_page_article,
                        _conflicting_model_code_in_title, _conflicting_numeric_article,
                        _conflicting_full_model_code, _conflicting_network_cable_spec,
                        _different_explicit_article, _different_full_skus_in_url,
                        _is_listing_url)
    mark = _field(position, 'type_mark')
    title, article, url = (observation[k] for k in ('title', 'manufacturer_code', 'url'))
    if not mark or _is_listing_url(url):
        return 'Товар каталога не подтверждён'
    from ..source_quote import primary_text
    # DOM layout whitespace may divide the exact printed title; no case,
    # punctuation, article or numeric normalization is permitted here.
    bound_title = ' '.join(title.split())
    if not bound_title or bound_title not in ' '.join(primary_text(observation['source_text']).split()):
        return 'Основной товар не подтверждён сохранённым текстом'
    maker=observation['identity'].get('manufacturer','').strip()
    if maker:
        from .manufacturer_aliases import manufacturer_equal, manufacturer_in_title
        labels=re.findall(r'(?im)^\s*(?:Производитель|Бренд|Торговая\s+марка|Manufacturer|Brand)(?!\s+(?:code|article|number|код|артикул|номер)\b)(?=\s|[:：]|$)\s*[:：]?\s*\n?([^\n]{1,512})',primary_text(observation['source_text']))
        if labels and any(not manufacturer_equal(maker,value) for value in labels):
            return 'Производитель основного товара противоречит каталогу'
        if not manufacturer_in_title(maker,title) and not any(manufacturer_equal(maker,value) for value in labels):
            return 'Производитель основного товара не подтверждён'
    from .product_identity import native_request_role, native_model_spec_reason, native_source_spec_reason, native_comparison_mark
    specification_reason=(native_source_spec_reason(position,title,primary_text(observation['source_text']),article)
                          or native_model_spec_reason(position,title,primary_text(observation['source_text'])))
    if specification_reason:return specification_reason
    comparison_mark=native_comparison_mark(position)
    if (_type_mark_match_status(comparison_mark, title if native_request_role(position)=='model' else title+' '+article) is not True
        or (native_request_role(position)!='model' and _conflicting_page_article(comparison_mark, article,request_role='unknown'))
        or _conflicting_model_code_in_title(comparison_mark, title)
        or _conflicting_numeric_article(comparison_mark, title)
        or _conflicting_full_model_code(comparison_mark, title)
        or _conflicting_network_cable_spec(_field(position, 'name'), comparison_mark, title)
        or _different_explicit_article(comparison_mark, title, url)
        or _different_full_skus_in_url(comparison_mark, url)):
        return 'Товар/артикул каталога не соответствует спецификации'
    return ''

def _unit_rationale(basis, *, title, sale_unit, requested_unit, quantity, witness=None, derived=None):
    """Display the existing validated decision, never nearby commerce context."""
    if basis == 'complete_set':
        return 'Одна продаваемая штука — один комплект: ' + title
    if basis == 'derived_factory_singleton_kit':
        excerpt = (derived or {}).get('assembly_excerpt')
        if isinstance(excerpt, str) and excerpt:
            return 'Заводская упаковка — один комплект: ' + excerpt
    elif basis == 'explicit_complete_kit_pack':
        excerpt = (witness or {}).get('excerpt')
        if isinstance(excerpt, str) and excerpt:
            return excerpt
    elif basis in {'same_unit', 'same_unit_preview'}:
        return f'Единица продажи совпадает с единицей спецификации: {sale_unit} → {requested_unit}'
    elif basis == 'price_lot':
        return f'Цена указана за {quantity:g} {requested_unit}'
    elif basis == 'pack_contents':
        return f'Состав продаваемой упаковки: {quantity:g} {requested_unit}'
    elif basis == 'product_length':
        return f'Длина изделия: {title}; единиц спецификации в цене: {quantity:g}'
    return 'Основание пересчёта не сохранено'


def _facts(observation, position):
    """Return derived price and quantity; never rewrite stored source evidence."""
    from ..source_quote import selected_price_context, firm_price_role_reason, _pricing_terms, primary_text, literal_same_unit_confirmed, complete_kit_pack_context, complete_kit_identity_metadata
    from ..price_capture import _unit, _sale_lot, _sale_unit_token, visible_prices
    from .agent import (_price_per_requested_unit, _product_length_metres,
                        _piece_is_complete_set)
    raw = observation['raw_amount']
    if not _positive(raw) or observation['state'] != 'observed':
        from .purchase_preview import preview_reason_text
        return None, preview_reason_text(observation.get('unit_basis_reason')) or 'Текущее наблюдение каталога требует проверки'
    if observation['currency'] != 'RUB':
        return None, 'Валюта цены каталога не подтверждена'
    if observation['vat'] == 'excluded':
        return None, 'Цена указана без НДС; налоговый пересчёт не подтверждён'
    text = observation['source_text']
    if hashlib.sha256(text.encode()).hexdigest() != observation['source_sha256']:
        return None, 'Источник каталога повреждён'
    proof=observation.get('unit_equivalence')
    if proof is not None:
        from ..source_quote import verified_owned_text
        from .factory_reference import verify_proof,FactoryEvidenceError
        try:
            verified=verify_proof(proof,position)
            text=verified_owned_text(text,verified['supplier_source'],url=observation['url'],observed_at=observation['observed_at'])
        except (FactoryEvidenceError,ValueError,TypeError,KeyError,OSError):return None,'Источник каталога повреждён'
    else:text = primary_text(text)
    price_text, unit_text = selected_price_context(text, raw)
    if observation.get('purchase_row') is not None:
        from .owned_purchase_row import verify
        row=verify(observation['purchase_row'],observation)
        if not row:return None,'Текущая строка покупки не подтверждена'
        price_text,unit_text=row['price_text'],row['unit_text']
    if not any(math.isclose(v, raw, rel_tol=1e-8, abs_tol=.005) for v in visible_prices(price_text)):
        return None, 'Исходная цена не подтверждена текстом карточки'
    terms = observation['purchase_terms']+' '+_pricing_terms(text, raw)
    reason = firm_price_role_reason(price_text, unit_text, terms)
    if reason:
        return None, reason
    if re.search(r'старая\s+цена|прошлая\s+цена|до\s+скидк', price_text, re.I):
        return None, 'Выбрана прежняя цена вместо текущей'
    if re.search(r'клуб|по\s+карт[еы]|при\s+(?:покупк|заказ)|оптов|от\s+\d+\s*(?:шт|м)', unit_text+' '+price_text+' '+terms, re.I):
        return None, 'Цена зависит от условий покупки'
    sale_text = observation['sale_unit']
    sale, wanted = _unit(sale_text), _unit(_field(position, 'unit'))
    lot = _sale_lot(sale_text)
    metadata=complete_kit_identity_metadata(observation['title'],observation.get('manufacturer_code',''),_field(position,'name'),_field(position,'type_mark'))
    if sale == 'уп' and wanted == 'компл':unit_text = complete_kit_pack_context(text, raw)
    quantity = 1
    basis, witness = '', None
    token = _sale_unit_token(wanted)
    from .purchase_preview import valid_unit_basis
    preview=observation.get('unit_basis')
    derived=observation.get('unit_equivalence')
    if derived is not None:
        from .factory_reference import verify_proof,validate_observation_proof,FactoryEvidenceError
        try:verify_proof(derived,position);validate_observation_proof(derived,observation)
        except (FactoryEvidenceError,OSError,ValueError,KeyError):return None,'Заводское доказательство комплектации не подтверждено'
        if (sale!='уп' or wanted!='компл' or preview is not None
            or any(value is not None and (type(value) not in (int,float) or not math.isfinite(value) or value!=1)
                   for value in (observation['lot_quantity'],observation['package_quantity']))):return None,'Единица заводской упаковки противоречива'
        quantity=1
        basis='derived_factory_singleton_kit'
    elif preview is not None:
        if (not valid_unit_basis(observation,preview) or sale != preview['sale_unit']
            or sale != wanted or observation['lot_quantity'] not in (None,1)
            or observation['package_quantity'] is not None):
            return None, 'Единица предпросмотра товара не подтверждена'
        quantity=1
        basis='same_unit_preview'
    else:
        # Catalogue lot quantity must be explicitly stored, not inferred.
        if lot and observation['lot_quantity'] != lot[0]:
            return None, 'Количество единиц в цене не подтверждено'
        from ..source_quote import strict_unit_decision
        decision, reason = strict_unit_decision(listed=raw, sale_unit=sale_text,
            requested_unit=_field(position, 'unit'), title=observation['title'], unit_text=unit_text,
            package_quantity=observation['package_quantity'], lot_quantity=observation['lot_quantity'],identity_metadata=metadata)
        if reason:return None, reason
        quantity = decision['quantity']
        basis, witness = decision.get('basis', ''), decision.get('witness')
    price = raw/quantity
    if not lot:
        data = dict(price_unit=sale_text, price_with_vat=price, listed_price_with_vat=raw)
        if sale == 'уп' and wanted != sale:
            data.update(package_quantity=quantity, listed_price_covers_multiple_requested_units=True)
        checked = _price_per_requested_unit(data, _field(position, 'unit'), observation['title'], owned_unit_context=unit_text,identity_metadata=metadata,factory_proof=derived,request_position=position)
        if checked is None or not math.isclose(checked, price, rel_tol=1e-8, abs_tol=.005):
            return None, 'Пересчёт единицы/комплектации не подтверждён'
    return dict(price=price, quantity=quantity, unit_text=unit_text, price_text=price_text,
                unit_basis=basis, unit_rationale=_unit_rationale(basis,
                    title=observation['title'], sale_unit=sale_text,
                    requested_unit=_field(position, 'unit'), quantity=quantity,
                    witness=witness, derived=derived)), ''

def catalogue_raw_facts(offer, position, *, store=None):
    """Bound raw source display only; this is never financial admission."""
    store=store or _SCOPED_STORE.get() or _STORE
    reference=_field(offer,'catalogue_ref',{})
    if not store or position is None or type(reference) is not dict or set(reference)!={'product_id','observation_id'}:return None
    try:
        record=store.resolve_observation(reference['product_id'],reference['observation_id'])
        if not record:return None
        observation=record['observation']
        if observation['currency']!='RUB':return None # Existing report columns are RUB-only.
        from .catalogue import identity_for_position
        requested=identity_for_position(position)
        expected_identity=requested.payload();actual_identity=record['identity']
        from .manufacturer_aliases import manufacturer_equal
        if any(expected_identity[k]!=actual_identity[k] for k in ('kind','code','descriptor','features')):return None
        if expected_identity['manufacturer']!=actual_identity['manufacturer'] and not manufacturer_equal(expected_identity['manufacturer'],actual_identity['manufacturer']):return None
        frozen=_SCOPED_SNAPSHOT.get()
        if frozen is not None:
            if 'products' in frozen:
                product=frozen.get('products',{}).get(requested.key)
            else:
                # The original neutral identity is independently bound above;
                # the trusted frozen issuer need not follow a newer head.
                product=frozen if frozen.get('product_id')==reference['product_id'] else None
            if not product or product.get('identity_ambiguous') or product.get('product_id')!=reference['product_id']:return None
            selected=next((h['current'] for h in product.get('heads',[]) if h['current']['observation_id']==reference['observation_id']),None)
            if selected!=observation:return None
        else:
            expected=store.lookup(requested)
            if not record['current'] or not expected or expected['product_id']!=reference['product_id']:return None
        if (not _positive(observation['raw_amount']) or not _positive(_field(offer,'listed_price_with_vat',None))
            or _field(offer,'listed_price_with_vat',None)!=observation['raw_amount']
            or not _positive(_field(offer,'catalogue_observed_at',None))
            or _field(offer,'catalogue_observed_at',None)!=observation['observed_at']):return None
        for field,source in (('url','url'),('product_title','title'),('page_article','manufacturer_code'),
                             ('price_unit','sale_unit'),('catalogue_source_sha256','source_sha256')):
            if _field(offer,field,None)!=observation[source]:return None
        return dict(raw_amount=observation['raw_amount'],sale_unit=observation['sale_unit'],observed_at=observation['observed_at'])
    except (KeyError,TypeError,ValueError,AttributeError):return None


def catalogue_review_reason(offer, position=None, *, store=None):
    store = store or _SCOPED_STORE.get() or _STORE
    reference = _field(offer, 'catalogue_ref', {})
    if not store or type(reference) is not dict or set(reference) != {'product_id', 'observation_id'}:
        return 'Ссылка на источник каталога не подтверждена'
    try:
        record = store.resolve_observation(reference['product_id'], reference['observation_id'])
        if not record:
            return 'Наблюдение каталога не является текущим'
        frozen = _SCOPED_SNAPSHOT.get()
        if frozen is not None:
            products = frozen.get('products', {}).values() if 'products' in frozen else [frozen]
            selected = next((p for p in products if p and p['product_id'] == reference['product_id']), None)
            selected_observation = next((h['current'] for h in selected['heads'] if h['current']['observation_id'] == reference['observation_id']), None) if selected else None
            if selected_observation != record['observation']:
                return 'Наблюдение не относится к серверному снимку каталога'
        elif not record['current']:
            return 'Наблюдение каталога не является текущим'
        observation = record['observation']
        row = position if position is not None else dict(type_mark=record['identity']['code'], name='', unit=_field(offer, 'normalized_price_unit'))
        if position is not None:
            from .catalogue import identity_for_position
            expected = store.lookup(identity_for_position(position))
            if not expected or expected['product_id'] != reference['product_id']:
                return 'Ссылка каталога относится к другому товару'
        from .product_identity import _resolved_model_role
        with _resolved_model_role(observation,record['identity']):
            reason = _identity_reason(observation, row)
        if reason:
            return reason
        facts, reason = _facts(observation, row)
        if reason:
            return reason
        from ..requisites.supplier import seller_evidence_reason
        seller_reason = seller_evidence_reason(observation.get('seller_evidence'), product_url=observation['url'], captured_at=observation['observed_at'])
        if seller_reason:return seller_reason
        if (_field(offer, 'url') != observation['url']
            or _field(offer, 'product_title') != observation['title']
            or _field(offer, 'page_article') != observation['manufacturer_code']
            or _field(offer, 'price_unit') != observation['sale_unit']
            or _field(offer, 'catalogue_observed_at', 0) != observation['observed_at']
            or _field(offer, 'catalogue_source_sha256') != observation['source_sha256']
            or _field(offer, 'catalogue_vat_status') != observation['vat']
            or _field(offer, 'catalogue_vat_notice') != vat_notice(observation['vat'])
            or _field(offer, 'normalized_price_unit') != _field(row, 'unit')
            or (_field(offer, 'region_status') or 'unknown') != observation.get('region_status','unknown')
            or _field(offer, 'region_evidence') != observation.get('region_evidence','')
            or _field(offer, 'in_moscow_region',None) != observation.get('in_moscow_region')
            or (_field(offer, 'inn') or _field(_field(offer, 'requisites', {}), 'inn')) != observation['seller_inn']
            or (_field(offer, 'org_name') or _field(_field(offer, 'requisites', {}), 'name')) != observation['seller_name']
            or _field(offer, 'seller_evidence', None) != observation.get('seller_evidence')
            or _field(offer, 'catalogue_image', {}) != observation['image']):
            return 'Проекция изменена относительно источника каталога'
        for actual, expected in ((_field(offer, 'listed_price_with_vat', None), observation['raw_amount']), (_field(offer, 'price_with_vat', None), facts['price'])):
            if not _positive(actual) or not math.isclose(actual, expected, rel_tol=1e-8, abs_tol=.005):
                return 'Сохранённая цена изменена относительно источника каталога'
        screenshots = [_field(offer, 'screenshot_context'),_field(offer, 'screenshot_product')]
        if any(screenshots):
            from pathlib import Path
            from ..config import CACHE_DIR
            image=observation['image'];local=image.get('local_path')
            path=str(CACHE_DIR.resolve()/local) if local else ''
            # Immutable locator/context binding still rejects caller forgery.
            # Availability/hash of optional bytes belongs to attachment output.
            if not path or any(s and s not in {path,Path(path).name} for s in screenshots) or _field(offer, 'context_capture_evidence', {}) != (image.get('context_evidence') or {}):
                return 'Снимок не связан с наблюдением каталога'
        return ''
    except (KeyError, TypeError, ValueError, AttributeError):
        return 'Сведения каталога не подтверждены'

def project_product(store, identity, position, *, snapshot=None):
    """Known-empty is a reportable result, not permission to acquire."""
    from ..models import PriceOffer, Requisites, PositionResult
    from ..offer_selection import independent_offers
    product = snapshot if snapshot is not None else store.lookup(identity)
    if product is None:
        return None
    offers = []
    with catalogue_scope(store, snapshot=product):
        for head in product['heads']:
            observed = head['current']
            facts, reason = _facts(observed, position)
            from .product_identity import _resolved_model_role
            resolved=store.resolve_observation(product['product_id'],observed['observation_id'])
            with _resolved_model_role(observed if resolved and resolved['observation']==observed else {},product['identity']):
                reason = _identity_reason(observed, position) or reason
            screenshot, context, image_reason = _image(observed)
            offer = PriceOffer(price_with_vat=facts['price'] if facts else observed['raw_amount'] or 0,
                url=observed['url'], product_title=observed['title'], page_title=observed['title'],
                page_article=observed['manufacturer_code'], price_unit=observed['sale_unit'],
                normalized_price_unit=position.unit, listed_price_with_vat=observed['raw_amount'],
                price_unit_evidence=facts['unit_rationale'] if facts else '',
                catalogue_ref=dict(product_id=product['product_id'], observation_id=observed['observation_id']),
                source_quote_evidence={'factory_unit_proof':observed['unit_equivalence']} if facts and observed.get('unit_equivalence') is not None else {},
                catalogue_observed_at=observed['observed_at'], catalogue_source_sha256=observed['source_sha256'],
                catalogue_image=observed['image'], unit_review_required=bool(reason), unit_review_reason=reason,
                catalogue_vat_status=observed['vat'],catalogue_vat_notice=vat_notice(observed['vat']),
                screenshot_context=screenshot, screenshot_product=screenshot or None,context_capture_evidence=context,
                region_status=observed.get('region_status','unknown'),region_evidence=observed.get('region_evidence',''),
                in_moscow_region=observed.get('in_moscow_region'),
                requisites=Requisites(name=observed['seller_name'], inn=observed['seller_inn']),
                seller_evidence=observed.get('seller_evidence'),
                context_capture_review_reason=image_reason)
            offers.append(offer)
        independent = independent_offers(offers, position)
        result = PositionResult(position=position, all_offers=offers, offers=independent[-3:],
            sources_found=len(independent), sources_target=5,
            catalogue_revision=product['revision'], catalogue_product_id=product['product_id'],
            catalogue_coverage=product['coverage'], catalogue_refresh_attempts=product['refresh_attempts'])
    return result
