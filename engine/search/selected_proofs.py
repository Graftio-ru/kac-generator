"""Separate admissible price ranking from current selected photographic proof."""
from ..offer_selection import select_top_offers,offer_is_comparable

VERSION='selected-catalogue-proof-v1'
NOT_SELECTED='not_selected_price_observation'
PROOF_FAILURE='selected_proof_failed: '

def _field(value,key,default=None):return value.get(key,default) if isinstance(value,dict) else getattr(value,key,default)

def proof_ready(offer,position):
    """No caller flags/paths: financial guard then immutable server image bytes."""
    if not offer_is_comparable(offer,position):return False
    reference=_field(offer,'catalogue_ref')
    if not reference:return True # Existing manual/letter/noncatalogue rules.
    from . import catalogue_projection as projection
    store=projection._SCOPED_STORE.get() or projection._STORE
    if store is None:return False
    try:
        record=store.resolve_observation(reference['product_id'],reference['observation_id'])
        if not record:return False
        # Comparable already verifies current/frozen reference, expected native
        # position and every source amount/unit/date/seller/image projection.
        path,context,reason=projection._image(record['observation'])
        return bool(path and context and not reason)
    except (KeyError,TypeError,ValueError):return False

def _server_attempts(position):
    from . import catalogue_projection as projection
    from .catalogue import identity_for_position
    store=projection._SCOPED_STORE.get() or projection._STORE
    if store is None:return []
    identity=identity_for_position(position);frozen=projection._SCOPED_SNAPSHOT.get()
    product=(frozen.get('products',{}).get(identity.key) if 'products' in frozen else frozen) if frozen is not None else store.lookup(identity)
    return product.get('refresh_attempts',[]) if product else []

def price_candidates(result):
    # Attachment attempts cannot change financially qualified seller ranking.
    return select_top_offers(result.all_offers or result.offers,3,result.position)

def final_offers(result):
    selected=price_candidates(result);output=[]
    from . import catalogue_projection as projection
    store=projection._SCOPED_STORE.get() or projection._STORE
    for offer in selected:
        reference=_field(offer,'catalogue_ref')
        if not reference:
            output.append(offer);continue
        record=store.resolve_observation(reference['product_id'],reference['observation_id'])
        path,context,reason=projection._image(record['observation'])
        # File integrity governs attachment bytes only. Preserve immutable facts
        # and raw result; unavailable attachments are stripped on an output copy.
        output.append(offer.model_copy(update={'screenshot_context':path,
            'screenshot_product':path or None,'context_capture_evidence':context,
            'context_capture_review_reason':reason}))
    return output

def final_result(result):
    """Copy for final documents; raw matrix/result is never discarded."""
    pool=result.all_offers or result.offers
    if not any(_field(o,'catalogue_ref') for o in pool):return result
    selected=[o for o in final_offers(result) if _field(o,'catalogue_ref')]
    # The original validator retains authority over legacy/manual/letter pools.
    legacy=[o for o in pool if not _field(o,'catalogue_ref')]
    return result.model_copy(update={'all_offers':legacy+selected,'offers':[o for o in result.offers if not _field(o,'catalogue_ref')]+selected})

def selected_for_report(offers,position):
    from types import SimpleNamespace
    return final_offers(SimpleNamespace(all_offers=offers,offers=offers,position=position))

def capture_selected_proofs(store,positions,*,browser,image_store,acquisition=None,control=lambda:None,paused=lambda:False,max_attempts=6):
    """At most3 final sources, finite replacement pool; no discovery or old image."""
    import time
    from .catalogue import identity_for_position,ProductIdentity
    from .catalogue_projection import project_product,catalogue_scope
    from .catalogue_producer import capture_and_publish,CataloguePublicationError
    from .failure_policy import supplier_card_transport_diagnostic
    from .session import SupplierDeferred
    if type(max_attempts) is not int or not 3<=max_attempts<=6:raise ValueError('Invalid selected proof budget')
    report=[]
    for position in positions:
        identity=identity_for_position(position);attempted=set();failures=[]
        while True:
            control();product=store.lookup(identity)
            if not product or product.get('identity_ambiguous'):break
            with catalogue_scope(store,snapshot=product):
                result=project_product(store,identity,position,snapshot=product)
                chosen=price_candidates(result);pending=[o for o in chosen if not proof_ready(o,position)]
            if not pending or len(attempted)>=max_attempts:break
            if paused():return dict(paused=True,positions=report,reason='Выбранные снимки ожидают явного продолжения')
            offer=next((o for o in reversed(pending) if o.url not in attempted),None)
            if offer is None:break
            attempted.add(offer.url);control();reference=offer.catalogue_ref['observation_id'];host=next(h['host'] for h in product['heads'] if h['current']['observation_id']==reference)
            payload=product['identity'];canonical=ProductIdentity(**{**payload,'features':tuple(tuple(v) for v in payload['features'])})
            started=time.time();reason='';observed=None
            try:
                # Selected photographs reuse the existing model-free capture mode.
                # The native producer still owns current-frame financial publication.
                observed=capture_and_publish(store,canonical,position,offer.url,browser=browser,image_store=image_store,photo_only=True,acquisition=acquisition,supplier_host=host)
            except SupplierDeferred:
                reason='Получение дополнительного снимка отложено поставщиком'
            except CataloguePublicationError:
                raise # Source/price/unit/identity publication failure is not a photo warning.
            except Exception as failure:
                diagnostic=supplier_card_transport_diagnostic(failure,host)
                if diagnostic is None:raise
                reason=diagnostic['reason']
            if observed is not None:attempted.add(observed.url)
            control();current=store.lookup(identity)
            with catalogue_scope(store,snapshot=current):
                result=project_product(store,identity,position,snapshot=current)
                ready=any(o.url==(observed.url if observed else offer.url) and proof_ready(o,position) for o in result.all_offers)
            if not ready:
                text=reason or 'Текущий снимок и допустимая цена выбранного источника не подтверждены'
                store.record_refresh_attempt(canonical.key,host,offer.url,attempted_at=time.time(),outcome='unknown',reason=PROOF_FAILURE+text,acquisition=acquisition)
                failures.append(dict(url=offer.url,reason=text))
        control();product=store.lookup(identity)
        if product:
            with catalogue_scope(store,snapshot=product):
                result=project_product(store,identity,position,snapshot=product);selected=final_offers(result);count=len(selected);attached=sum(proof_ready(o,position) for o in selected)
            report.append(dict(product_id=product['product_id'],attempts=len(attempted),selected=count,shortfall=max(0,3-count),attachments=attached,attachment_shortfall=count-attached,failures=failures))
    return dict(paused=False,positions=report)

def refresh_prices_and_selected_proofs(store,positions,*,browser,image_store):
    """Explicit daily operation; all prices, current photos only chosen sources."""
    from .catalogue_producer import refresh_recorded
    from .card_attachments import collect_current
    prices=refresh_recorded(store,browser=browser,image_store=image_store,capture=collect_current)
    proofs=capture_selected_proofs(store,positions,browser=browser,image_store=image_store)
    return dict(prices=prices,proofs=proofs)
