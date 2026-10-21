"""Publish owned source captures; refresh never discovers a new supplier URL."""
import hashlib
import json
import math
import re
import time
import uuid
from pathlib import Path
from concurrent.futures import CancelledError

from .catalogue import ProductIdentity, SourceObservation, ImageEvidence
from .failure_policy import ResearchControlError

_PUBLICATION_REASONS = {
    'catalogue_seller_binding_invalid': 'Происхождение реквизитов продавца не связано с текущим источником',
    'catalogue_operation_id_invalid': 'Идентификатор серверной операции публикации некорректен',
    'catalogue_snapshot_changed': 'Сохранённый снимок исходных данных изменён',
    'catalogue_owner_unconfirmed': 'Независимый основной товар не подтверждён',
    'catalogue_article_conflict': 'Артикулы одного поколения источника противоречат друг другу',
    'catalogue_generation_mismatch': 'Предложение относится к другому поколению источника',
    'catalogue_source_binding_invalid': 'Привязка исходных данных основного товара не подтверждена',
    'catalogue_source_amount_unobserved': 'Цена не наблюдается в сохранённом источнике',
    'catalogue_purchase_price_conflict': 'Выбрана другая ценовая ветка вместо текущего основного предложения',
    'catalogue_purchase_unit_conflict': 'Единица предложения противоречит напечатанной единице продажи',
}

class CataloguePublicationError(ValueError):
    """Safe producer cause, never raw supplier/SDK exception text."""
    def __init__(self, code):
        if code not in _PUBLICATION_REASONS:
            raise ValueError('Unknown publication code')
        self.code = code
        super().__init__(_PUBLICATION_REASONS[code])
    def diagnostic(self):
        return dict(code=self.code,reason=_PUBLICATION_REASONS[self.code],
                    failure_source='catalogue_producer',failure_scope='card',failure_cause='source_binding')


def _image(facts, observation_id, url, stamp, image_store):
    from ..review_context import context_valid
    context = facts.get('context_capture_evidence') or {}
    reference = facts.get('screenshot_context') or ''
    error = facts.get('context_capture_review_reason') or 'native_capture_missing'
    if reference and context:
        try:
            root = image_store.root.resolve()
            path = Path(reference)
            path = path if path.is_absolute() else root/path
            if path.is_symlink() or path.resolve().parent != root or path.stat().st_size > image_store.max_bytes:
                raise ValueError('unowned_capture')
            data = path.read_bytes()
            if not context_valid(context, str(path), url, data):
                raise ValueError('capture_binding_invalid')
            retained = image_store.retain(data, observation_id=observation_id, url=url,
                observed_at=stamp, captured_at=context['captured_at'])
            # Filename is a storage locator; source pixels/hash/date stay intact.
            filename = Path(retained['local_path']).name
            context = dict(context, filename=filename)
            return ImageEvidence(observation_id, url, stamp, 'captured',
                sha256=retained['sha256'], local_path=filename, size=retained['size'],
                bucket=retained['bucket'], key=retained['key'], captured_at=context['captured_at'],
                error=retained['error'], context_evidence=context)
        except (ResearchControlError, CancelledError):
            raise
        except (OSError, KeyError, TypeError, ValueError):
            error = 'capture_binding_invalid'
    return ImageEvidence(observation_id, url, stamp, 'missing', error=error[:2048])

def _interpret(text, raw):
    from ..source_quote import _pricing_terms, selected_price_context, firm_price_role_reason
    from ..price_capture import visible_prices
    currency = 'RUB' if re.search(r'₽|\bруб(?:лей|ля|ль|\.)?\b|\bRUB\b', text, re.I) else 'unknown'
    vat = ('excluded' if re.search(r'без\s+НДС|НДС\s+не\s+включ', text, re.I) else
           'included' if re.search(r'с\s+НДС|НДС\s+включ', text, re.I) else 'unknown')
    availability = 'unknown' # Source purchase scope is selected after interpretation.
    values = list(dict.fromkeys(visible_prices(text)))
    if raw is None:
        raw = values[0] if len(values) == 1 else None
    if raw is not None and (type(raw) not in (int,float) or not math.isfinite(raw) or raw <= 0
        or not any(math.isclose(raw,v,rel_tol=1e-8,abs_tol=.005) for v in values)):
        raise CataloguePublicationError('catalogue_source_amount_unobserved')
    price_text, unit_text = selected_price_context(text, raw) if raw is not None else ('','')
    terms = _pricing_terms(text, raw) if raw is not None else text
    review = firm_price_role_reason(price_text, unit_text, terms)
    if re.search(r'клуб|по\s+карт[еы]|при\s+(?:покупк|заказ)|оптов|от\s+\d+\s*(?:шт|м)',unit_text+' '+price_text+' '+terms,re.I):
        review = 'conditional_purchase_price'
    return raw, currency, vat, availability, terms, bool(review)

def _bound_purchase(facts, image_store):
    """Reparse the immutable producer frame, never caller/model price fields."""
    text=facts.get('purchase_text','')
    if not text:return None
    reference=(facts.get('main_facts') or {}).get('ref','')
    if not re.fullmatch(r'main_facts/[a-f0-9]{32}\.json',reference):raise CataloguePublicationError('catalogue_source_binding_invalid')
    root=image_store.root.resolve();folder=image_store.root/'main_facts';path=folder/Path(reference).name
    try:
        if folder.is_symlink() or folder.resolve()!=root/'main_facts' or path.is_symlink() or path.resolve().parent!=root/'main_facts' or path.stat().st_size>1024*1024:raise ValueError('unowned_frame')
        raw=path.read_bytes();owned=json.loads(raw)
        if (hashlib.sha256(raw).hexdigest()!=facts['main_facts']['sha256']
            or owned.get('version')!=1 or owned.get('role')!='main_card_facts'
            or not owned.get('owner_bound') or owned.get('owner_truncated')
            or any(owned.get(k)!=facts.get(v) for k,v in [('purchase_text','purchase_text'),('owner_text','text'),('main_title','main_title'),('article','page_article'),('observed_at','captured_at')])
            or facts['main_facts'].get('observed_at')!=facts['captured_at']):raise ValueError('frame_changed')
        from .product_identity import canonical_card_url
        if canonical_card_url(owned.get('document_url'))!=canonical_card_url(facts['url']).split('?')[0] or canonical_card_url(owned.get('requested_url'))!=canonical_card_url(facts.get('requested_url',facts['url'])).split('?')[0]:raise ValueError('frame_url_changed')
        from ..source_quote import owned_purchase_facts
        return owned_purchase_facts(text)
    except (OSError,KeyError,TypeError,ValueError):raise CataloguePublicationError('catalogue_source_binding_invalid') from None

def publish_facts(store, identity, position, facts, *, image_store, offer=None, acquisition=None, verified_owner=False, observation_id=None):
    """Internal server capture interface, never an endpoint for arbitrary dicts."""
    from ..source_quote import primary_text,verified_owned_text
    from .catalogue_projection import _identity_reason
    from .product_identity import canonical_card_url, card_host
    from ..price_capture import _sale_lot
    from .session import bounded_timeout
    bounded_timeout(1)
    url = canonical_card_url(facts['url'])
    stamp = facts['captured_at']
    source=facts.get('http_source') or facts.get('rendered_source')
    try:text=verified_owned_text(facts['text'],source,url=url,observed_at=stamp) if source else primary_text(facts['text'])
    except (ValueError,TypeError,KeyError,OSError):raise CataloguePublicationError('catalogue_source_binding_invalid') from None
    purchase=_bound_purchase(facts,image_store)
    from .owned_purchase_row import from_facts
    row=from_facts(facts)
    raw = offer.listed_price_with_vat if offer is not None else None
    if row:
        from ..price_capture import _unit
        if offer is not None and (raw is not None and raw!=row['raw_amount'] or _unit(offer.price_unit)!=_unit(row['sale_unit'])):
            raise CataloguePublicationError('catalogue_purchase_row_conflict')
        raw=row['raw_amount']
    if purchase is not None:
        from ..price_capture import _unit
        if offer is not None:
            if raw is not None and (purchase['raw_amount'] is None or not math.isclose(raw,purchase['raw_amount'],rel_tol=1e-8,abs_tol=.005)):raise CataloguePublicationError('catalogue_purchase_price_conflict')
            placeholder=not purchase['sale_unit'] and offer.price_unit in ('','не указана') and offer.unit_review_required
            if offer.price_unit and not placeholder and _unit(offer.price_unit)!=_unit(purchase['sale_unit']):raise CataloguePublicationError('catalogue_purchase_unit_conflict')
        raw, currency, vat, availability, terms, conditional = _interpret(facts['purchase_text'],purchase['raw_amount'])
        if purchase['raw_amount'] is None:raw=None
        conditional=conditional or bool(purchase['reason'])
        sale=purchase['sale_unit']
    else:
        raw, currency, vat, availability, terms, conditional = _interpret(text, raw)
        sale = offer.price_unit if offer is not None else facts.get('sale_unit','')
    if facts.get('rendered_source'):
        from .http_card import read_source
        source=facts['rendered_source'];retained=read_source(source,allow_rendered=True)
        if (not retained or retained[0].get('role')!='rendered_primary_dom'
            or retained[0]['observed_at']!=stamp or retained[1]['text']!=text
            or source.get('main_facts')!=facts.get('main_facts')):raise CataloguePublicationError('catalogue_source_binding_invalid')
        if facts.get('context_capture_evidence') or facts.get('screenshot_context'):
            from .http_card import rendered_photo_valid
            if not rendered_photo_valid(source,facts.get('context_capture_evidence') or {},facts.get('screenshot_context') or ''):
                facts=dict(facts,screenshot_context='',context_capture_evidence={},context_capture_review_reason='Текущий снимок не связан с сохранёнными сведениями карточки')
    if not sale and raw is not None and purchase is None:
        from ..source_quote import selected_price_context
        unit_text = selected_price_context(text,raw)[1]
        denominations = set(re.findall(r'(?:/|\bза(?=\s|\d|$)\s*)\s*((?:\d+(?:[.,]\d+)?\s*)?(?:шт|м|упаковк[ау]|компл))\b', unit_text,re.I))
        if len(denominations) == 1:
            sale = denominations.pop().strip()
            sale = re.sub(r'^1\s+','',sale)
    if row:
        sale=row['sale_unit']
    sale = sale or 'unknown'
    lot = _sale_lot(sale)
    package = facts.get('package_quantity')
    state = 'review' if conditional or vat=='excluded' or currency=='unknown' or sale=='unknown' else 'observed'
    if raw is None:
        state = 'unpriced'
    if observation_id is not None and (type(observation_id) is not str or not re.fullmatch(r"[0-9a-f]{32}",observation_id)):
        raise CataloguePublicationError("catalogue_operation_id_invalid")
    oid = observation_id or uuid.uuid4().hex
    from ..requisites.supplier import captured_seller_evidence, seller_evidence_requisites, seller_evidence_reason
    seller_evidence = captured_seller_evidence(facts, offer.requisites.seller_source if offer is not None else None)
    if offer is not None and offer.seller_evidence is not None and offer.seller_evidence != seller_evidence:
        raise CataloguePublicationError('catalogue_seller_binding_invalid')
    seller = seller_evidence_requisites(seller_evidence, offer.requisites if offer is not None else None)
    values = dict(observation_id=oid,identity=identity,supplier_host=facts.get('supplier_host') or card_host(url),
        requested_url=facts.get('requested_url',url),url=url,observed_at=stamp,issuer=facts.get('issuer','supplier_rendered'),
        title=facts['main_title'],manufacturer_code=facts.get('page_article',''),source_text=text,
        source_sha256=hashlib.sha256(text.encode()).hexdigest(),raw_amount=raw,currency=currency,vat=vat,
        sale_unit=sale,lot_quantity=lot[0] if lot else None,package_quantity=package,
        purchase_terms=terms,availability=availability,state=state,
        seller_name=seller.name, seller_inn=seller.inn, seller_evidence=seller_evidence,
        image=_image(facts,oid,url,stamp,image_store),validation_version='catalogue-native-v1',purchase_row=row)
    from .product_identity import native_request_role, _NATIVE_MODEL_INTENT
    if verified_owner and native_request_role(position)=='model':
        from .catalogue import identity_for_position
        expected=store.lookup(identity_for_position(position))
        # Background context is independently installed from this same issuer.
        inherited=any(h['current']['validation_version']==_NATIVE_MODEL_INTENT and h['current']['identity']==identity.payload() for h in (store.lookup(identity) or {}).get('heads',[]))
        if ((expected and expected['product_id']==identity.key) or inherited) and identity.kind=='model' and identity.code==position.type_mark and identity.descriptor==(position.full_name or position.name):
            values['validation_version']=_NATIVE_MODEL_INTENT
    from .product_identity import _NATIVE_ARTICLE_INTENT, _NATIVE_REQUEST_CONTEXT
    if verified_owner and identity.kind=='article':
        binding=_NATIVE_REQUEST_CONTEXT.get()
        if binding and binding.get('article')==identity.code:
            from .catalogue import validate_native_request_contract
            original_contract=binding.get('contract')
            contract=original_contract or dict(version='native-source-constraints-v1',identity_sha256=identity.key,
                issuer=values['issuer'],reader='native_pdf',**{k:binding[k] for k in ('model','article','mark','name','requested')})
            contract={**contract,'issuer':values['issuer']}
            values['native_request_contract']=validate_native_request_contract(contract,identity,values['issuer'])
            values['validation_version']=_NATIVE_ARTICLE_INTENT
    from .purchase_preview import derive_unit_basis,has_preview_sequence
    cash=offer.source_quote_evidence or {} if offer is not None else None
    basis=derive_unit_basis(values,cash)
    if basis is not None:
        values.update(unit_basis=basis,sale_unit=basis['sale_unit'],lot_quantity=1,package_quantity=None)
        if not conditional and currency=='RUB' and vat!='excluded':values['state']='observed'
    elif has_preview_sequence(text):
        values['unit_basis_reason']='preview_selected_cash_missing' if not cash else 'preview_cash_or_roles_unconfirmed'
    proof=(offer.source_quote_evidence or {}).get('factory_unit_proof') if offer is not None else None
    if proof is not None:
        from .factory_reference import verify_proof,validate_observation_proof,FactoryEvidenceError
        try:proof=verify_proof(proof,position);validate_observation_proof(proof,{**values,'identity':identity.payload()})
        except (FactoryEvidenceError,OSError,ValueError,KeyError):raise CataloguePublicationError('catalogue_source_binding_invalid') from None
        values['unit_equivalence']=proof
    from .primary_availability import scoped_availability
    values['availability']=scoped_availability(text,raw,owner_bound=verified_owner,unit_basis=basis)
    identity_reason=_identity_reason(dict(values,identity=identity.payload()),position)
    if identity_reason:
        if not verified_owner:
            raise CataloguePublicationError('catalogue_owner_unconfirmed')
        values['state']='review' if identity_reason=='Производитель основного товара не подтверждён' else 'identity_conflict'
    if offer is not None:
        values.update(region_status=offer.region_status or 'unknown',region_evidence=offer.region_evidence,
                      in_moscow_region=offer.in_moscow_region)
    observation = SourceObservation(**values)
    from .catalogue_projection import _facts
    from ..requisites.supplier import valid_inn
    from ..region_policy import region_status
    _, reason = _facts(observation.payload(),position)
    found = not reason and not seller_evidence_reason(seller_evidence) and bool(observation.seller_name) and valid_inn(observation.seller_inn) and region_status(values)!='incompatible'
    bounded_timeout(1) # Cancellation after capture/upload forbids publication.
    store.append_observation(identity.key,observation,acquisition=acquisition,coverage_status='found' if found else 'review')
    if store.resolve_observation(identity.key,observation.observation_id) is None:
        from .catalogue import CatalogueConflict
        raise CatalogueConflict('Capture publication was not admitted')
    return observation

def _validated_snapshot_facts(position,snapshot,image_store):
    from .product_identity import valid_identity
    if hashlib.sha256(snapshot.payload.encode()).hexdigest() != snapshot.sha256:
        raise CataloguePublicationError('catalogue_snapshot_changed')
    facts = snapshot.facts()
    proof = facts.get('identity_evidence') or {}
    if (not valid_identity(proof,expected_mark=position.type_mark,expected_name=position.name,request_position=position,source_text=facts.get('text',''))
        or proof.get('main_title') != facts.get('main_title')):
        raise CataloguePublicationError('catalogue_owner_unconfirmed')
    actual_article = proof.get('main_article') or ''
    legacy_article = facts.get('page_article') or ''
    if legacy_article and legacy_article != actual_article:
        raise CataloguePublicationError('catalogue_article_conflict')
    if not legacy_article and actual_article:
        # Backfill only independently owned SAME-epoch article, never expected SKU.
        source = facts.get('main_facts') or {}
        reference = source.get('ref','')
        if not re.fullmatch(r'main_facts/[a-f0-9]{32}\.json',reference):
            raise CataloguePublicationError('catalogue_source_binding_invalid')
        owned_root = image_store.root.resolve()
        folder = image_store.root/'main_facts'
        path = folder/Path(reference).name
        try:
            if (folder.is_symlink() or folder.resolve() != owned_root/'main_facts'
                or path.is_symlink() or path.resolve().parent != owned_root/'main_facts'
                or path.stat().st_size>1024*1024):
                raise CataloguePublicationError('catalogue_source_binding_invalid')
            raw = path.read_bytes()
            owned = json.loads(raw)
            from .product_identity import canonical_card_url
            from urllib.parse import urlsplit,urlunsplit
            base=lambda u:urlunsplit((*urlsplit(canonical_card_url(u))[:3],'',''))
            if (path.is_symlink() or path.resolve().parent != owned_root/'main_facts'
                or len(raw)>1024*1024 or hashlib.sha256(raw).hexdigest()!=source.get('sha256')
                or owned.get('version')!=1 or owned.get('role')!='main_card_facts'
                or not owned.get('owner_bound') or owned.get('owner_truncated')
                or owned.get('article')!=actual_article or owned.get('main_title')!=facts['main_title']
                or owned.get('owner_text')!=facts['text']
                or owned.get('observed_at')!=facts['captured_at']
                or source.get('observed_at')!=facts['captured_at']
                or proof.get('observed_at')!=facts['captured_at']
                or proof.get('canonical_url')!=canonical_card_url(facts['url'])
                or proof.get('source_url')!=canonical_card_url(facts['requested_url'])
                or base(owned.get('document_url'))!=base(facts['url'])
                or base(owned.get('requested_url'))!=base(facts['requested_url'])):
                raise CataloguePublicationError('catalogue_source_binding_invalid')
        except (OSError,KeyError,TypeError,ValueError) as error:
            if isinstance(error,CataloguePublicationError):raise
            raise CataloguePublicationError('catalogue_source_binding_invalid') from None
        facts = dict(facts,page_article=actual_article)
    return facts

def publish_snapshot(store, identity, position, snapshot, *, image_store, offer=None, acquisition=None, supplier_host=None, observation_id=None):
    facts=_validated_snapshot_facts(position,snapshot,image_store)
    proof=facts.get('identity_evidence') or {}
    facts = dict(facts,issuer='supplier_http' if proof.get('provenance')=='http_main_product' else 'supplier_rendered')
    if supplier_host is not None:
        facts['supplier_host']=supplier_host
    if offer is not None:
        evidence = offer.source_quote_evidence or {}
        if facts.get('purchase_text') and not evidence:
            purchase=_bound_purchase(facts,image_store)
            from ..price_capture import _unit
            if (offer.listed_price_with_vat is not None and (purchase['raw_amount'] is None or not math.isclose(offer.listed_price_with_vat,purchase['raw_amount'],rel_tol=1e-8,abs_tol=.005))):raise CataloguePublicationError('catalogue_purchase_price_conflict')
            placeholder=not purchase['sale_unit'] and offer.price_unit in ('','не указана') and offer.unit_review_required
            if offer.price_unit and not placeholder and _unit(offer.price_unit)!=_unit(purchase['sale_unit']):raise CataloguePublicationError('catalogue_purchase_unit_conflict')
            # A current raw unit mismatch is source evidence, not an offer
            # receipt. Publish independently observed facts for review only.
            offer=None
        if offer is not None:
            if (evidence.get('observed_at') != facts['captured_at'] or evidence.get('url') != facts['url']
                or evidence.get('title') != facts['main_title']):
                raise CataloguePublicationError('catalogue_generation_mismatch')
            from ..price_capture import _unit
            facts = dict(facts, package_quantity=evidence.get('quantity') if _unit(offer.price_unit)=='уп' else None)
    return publish_facts(store,identity,position,facts,image_store=image_store,offer=offer,acquisition=acquisition,verified_owner=True,observation_id=observation_id)

def _neutral_facts(record, requested_url, root):
    """Verify the existing immutable main-facts file, not a diagnostic string."""
    coherent, source = record['coherent'], record['source']
    if not coherent['owner_bound'] or coherent.get('owner_truncated'):
        raise CataloguePublicationError('catalogue_owner_unconfirmed')
    reference = source['ref']
    if not re.fullmatch(r'main_facts/[a-f0-9]{32}\.json',reference):
        raise CataloguePublicationError('catalogue_source_binding_invalid')
    owned_root = root.resolve()
    folder = root/'main_facts'
    path = folder/Path(reference).name
    if (folder.is_symlink() or folder.resolve() != owned_root/'main_facts'
        or path.is_symlink() or path.resolve().parent != owned_root/'main_facts'
        or path.stat().st_size>1024*1024):
        raise CataloguePublicationError('catalogue_source_binding_invalid')
    raw = path.read_bytes()
    stored = json.loads(raw)
    if (stored.get('version')!=1 or stored.get('role')!='main_card_facts'
        or hashlib.sha256(raw).hexdigest()!=source['sha256']
        or source.get('observed_at')!=coherent['observed_at']
        or any(stored.get(k)!=coherent.get(k) for k in ('main_title','article','owner_text','purchase_text','observed_at','owner_bound'))):
        raise CataloguePublicationError('catalogue_source_binding_invalid')
    observer = record['identity_observation']
    from .product_identity import canonical_card_url
    from urllib.parse import urlsplit,urlunsplit
    base=lambda u:urlunsplit((*urlsplit(canonical_card_url(u))[:3],'',''))
    if (not canonical_card_url(coherent['document_url'])
        or observer.get('canonical_url')!=canonical_card_url(coherent['document_url'])
        or observer.get('observed_at')!=coherent['observed_at']
        or canonical_card_url(record.get('requested_url'))!=canonical_card_url(requested_url)
        or base(stored.get('document_url'))!=base(coherent['document_url'])
        or base(stored.get('requested_url'))!=base(requested_url)
        or observer['main_title']!=coherent['main_title'] or observer['main_article']!=coherent['article']):
        raise CataloguePublicationError('catalogue_source_binding_invalid')
    return dict(**({'seller_reference_snapshot':record['seller_reference_snapshot']} if record.get('seller_reference_snapshot') is not None else {}),
        url=coherent['document_url'],requested_url=requested_url,text=coherent['owner_text'],
        main_title=coherent['main_title'],page_article=coherent['article'],captured_at=coherent['observed_at'],purchase_text=coherent.get('purchase_text',''),main_facts=source,
        screenshot_context=record.get('screenshot_context',''),context_capture_evidence=record.get('context_capture_evidence') or {},
        context_capture_review_reason=record.get('context_capture_review_reason') or 'native_pre_admission_capture_missing')

def capture_and_publish(store,identity,position,url,*,browser,image_store,capture=None,acquisition=None,supplier_host=None,observation_id=None,photo_only=False):
    from . import agent
    from .card_attachments import capture_current
    if observation_id is not None and (type(observation_id) is not str or not re.fullmatch(r"[0-9a-f]{32}",observation_id)):
        raise CataloguePublicationError("catalogue_operation_id_invalid")
    from .product_identity import _resolved_model_role, _original_native_model_role, _NATIVE_MODEL_INTENT, _NATIVE_ARTICLE_INTENT, native_request_role
    from .catalogue import identity_for_position
    if native_request_role(position) in {'article','model'} and (position.source_evidence or {}).get('reader')=='native_pdf':
        expected=store.lookup(identity_for_position(position))
        if not expected or expected['product_id']!=identity.key:
            raise CataloguePublicationError('catalogue_source_binding_invalid')
    product=store.lookup(identity)
    current=next((h['current'] for h in (product or {}).get('heads',[]) if h['url']==url and h['current']['identity']==identity.payload() and h['current']['validation_version'] in {_NATIVE_MODEL_INTENT,_NATIVE_ARTICLE_INTENT}),{})
    from .factory_reference import request_scope
    with _resolved_model_role(current,identity), _original_native_model_role(store,identity,position),request_scope(position):
        return _capture_and_publish_owned(store,identity,position,url,browser=browser,image_store=image_store,capture=capture,acquisition=acquisition,supplier_host=supplier_host,observation_id=observation_id,photo_only=photo_only)

def _capture_and_publish_owned(store,identity,position,url,*,browser,image_store,capture=None,acquisition=None,supplier_host=None,observation_id=None,photo_only=False):
    from . import agent
    from .card_attachments import capture_current
    records=[]
    if type(photo_only) is not bool:raise ValueError('Explicit photo publication mode required')
    if photo_only:
        if capture is not None:raise ValueError('Photo publication requires native capture')
        from .card_attachments import CaptureFinancialOutcome,CaptureFinancialResult
        outcomes=[]
        parent=agent._CARD_DIAGNOSTICS.get()
        with agent.capture_card_diagnostics() as diagnostics:
            snapshot,offer=capture_current(browser,position,url,neutral_sink=records.append,
                extract=False,financial_outcome=outcomes.append)
        if parent is not None:parent.extend(diagnostics)
        if snapshot is not None:
            if len(outcomes)!=1 or not isinstance(outcomes[0],CaptureFinancialResult):
                raise CataloguePublicationError('catalogue_source_binding_invalid')
            outcome=outcomes[0]
            if outcome.outcome is CaptureFinancialOutcome.INSUFFICIENT:
                _validated_snapshot_facts(position,snapshot,image_store)
                return None
            if outcome.outcome not in (CaptureFinancialOutcome.ACCEPTED,CaptureFinancialOutcome.REJECTED):
                raise CataloguePublicationError('catalogue_source_binding_invalid')
            if outcome.raw_rejection is not None:
                # Normalization failure is not proof that the supplier changed
                # its source quotation. Compare only independently observed raw
                # facts; never copy saved financial fields onto this frame.
                from .financial_outcome import RawFinancialRejection
                if not isinstance(outcome.raw_rejection,RawFinancialRejection):
                    raise CataloguePublicationError('catalogue_source_binding_invalid')
                raw=outcome.raw_rejection.fields;facts=_validated_snapshot_facts(position,snapshot,image_store)
                product=store.lookup(identity)
                current=next((h['current'] for h in (product or {}).get('heads',[]) if h['url']==url),None)
                if current is None:return None
                from ..source_quote import primary_text,verified_owned_text
                source=facts.get('http_source') or facts.get('rendered_source')
                text=verified_owned_text(facts['text'],source,url=facts['url'],observed_at=facts['captured_at']) if source else primary_text(facts['text'])
                amount,currency,vat,availability,terms,conditional=_interpret(text,raw['listed_price_with_vat'])
                from ..price_capture import _sale_lot,_unit
                same_amount=current['raw_amount'] is not None and current['raw_amount']==amount
                old_lot=_sale_lot(current['sale_unit']);new_lot=_sale_lot(raw['price_unit'])
                old_unit=(old_lot[0],old_lot[1]) if old_lot else (1,_unit(current['sale_unit']))
                new_unit=(new_lot[0],new_lot[1]) if new_lot else (1,_unit(raw['price_unit']))
                same_unit=old_unit==new_unit
                known_currency=currency!='unknown' and current['currency']!='unknown'
                changed_vat=vat!='unknown' and current['vat']!='unknown' and vat!=current['vat']
                if same_amount and same_unit and not changed_vat and not conditional and (not known_currency or currency==current['currency']):return None
        else:
            # Neutral packets precede admission. Only explicit native source
            # contradiction may supersede a saved price; absence is no evidence.
            contradictions={'article_mismatch','product_mismatch','price_on_request',
                            'out_of_stock','minimum_price','minimum_order'}
            if not any(d.get('code') in contradictions for d in diagnostics):return None
    else:
        snapshot,offer=(capture or capture_current)(browser,position,url,neutral_sink=records.append)
    if snapshot is not None:
        return publish_snapshot(store,identity,position,snapshot,image_store=image_store,offer=offer,acquisition=acquisition,supplier_host=supplier_host,observation_id=observation_id)
    if records:
        facts=_neutral_facts(records[-1],url,agent.CACHE_DIR)
        if supplier_host is not None:
            facts['supplier_host']=supplier_host
        return publish_facts(store,identity,position,facts,image_store=image_store,acquisition=acquisition,verified_owner=True,observation_id=observation_id)
    return None

def refresh_recorded(store,*,browser,image_store,capture=None):
    """One stored frontier snapshot; every recorded URL visited once, no search."""
    from ..models import SpecPosition
    report=[]
    snapshot=store.all_snapshot()
    for product in snapshot['products'].values():
        p=product['identity'];identity=ProductIdentity(p['kind'],p['code'],p['manufacturer'],p['descriptor'],tuple(tuple(v) for v in p['features']))
        for head in product['heads']:
            current=head['current'];url=head['url'];started=time.time()
            position=SpecPosition(number=1,name=p['descriptor'] or current['title'],type_mark=p['code'],unit=current['sale_unit'],qty=1)
            try:
                observation=capture_and_publish(store,identity,position,url,browser=browser,image_store=image_store,capture=capture,supplier_host=head['host'])
                outcome='observed' if observation else 'parse_error'
                store.record_refresh_attempt(product['product_id'],head['host'],url,attempted_at=started,
                    outcome=outcome,reason='' if observation else 'no_new_owned_facts',
                    observation_id=observation.observation_id if observation else None)
                report.append(dict(product_id=product['product_id'],url=url,outcome=outcome,
                    image_status=observation.image.status if observation else 'unchanged',
                    image_storage=observation.image.error if observation else '',
                    observation_id=observation.observation_id if observation else None))
            except (ResearchControlError, CancelledError) as error:
                from ..llm import LLMAccountError, LLMUnavailable, ModelOutputInvalid
                from .failure_policy import ResearchInternalFailure
                from .session import SearchBudgetExceeded
                if isinstance(error,LLMAccountError):outcome,reason='account_error','llm_account_error'
                elif isinstance(error,LLMUnavailable):outcome,reason='unknown','llm_provider_unavailable'
                elif isinstance(error,ModelOutputInvalid):outcome,reason='parse_error','model_output_invalid'
                elif isinstance(error,ResearchInternalFailure):outcome,reason='unknown','research_internal_error'
                elif isinstance(error,(SearchBudgetExceeded,CancelledError)):outcome,reason='cancelled','refresh_cancelled_or_budget'
                else:outcome,reason='unknown','research_control_failure'
                store.record_refresh_attempt(product['product_id'],head['host'],url,attempted_at=started,outcome=outcome,reason=reason)
                raise
            except Exception as error:
                from .catalogue import CatalogueStoreError
                if isinstance(error,CatalogueStoreError):raise
                store.record_refresh_attempt(product['product_id'],head['host'],url,attempted_at=started,outcome='unknown',reason='refresh_failed_'+type(error).__name__)
                report.append(dict(product_id=product['product_id'],url=url,outcome='unknown'))
    return report
