"""One finite post-selection pass; photographs never qualify historical prices."""
from contextvars import ContextVar
from contextlib import contextmanager
import time,hashlib,json
from enum import Enum
from dataclasses import dataclass

class CaptureFinancialOutcome(Enum):
    INSUFFICIENT = "no_fields"
    ACCEPTED = "accepted"
    REJECTED = "raw_financial_rejected"

@dataclass(frozen=True)
class CaptureFinancialResult:
    outcome: CaptureFinancialOutcome
    raw_rejection: object = None

from ..offer_selection import select_top_offers, offer_is_comparable, source_count
from .session import bounded_timeout, deadline_scope, supplier_transport
from .failure_policy import ResearchControlError

_CAPTURE = ContextVar('selected_attachment_capture', default=False)

def capturing_attachment():return _CAPTURE.get()

@contextmanager
def attachment_capture():
    token=_CAPTURE.set(True)
    try:yield
    finally:_CAPTURE.reset(token)


def quote_digest(evidence):
    original={k:v for k,v in evidence.items() if k!='attachment_binding'}
    return hashlib.sha256(json.dumps(original,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()

def bind_photo(offer):
    context=offer.context_capture_evidence or {}
    if offer.screenshot_context and offer.source_quote_evidence and context:
        quote=dict(offer.source_quote_evidence)
        quote['attachment_binding']={'quote_sha256':quote_digest(quote),'url':offer.url,'observed_at':quote['observed_at'],'filename':context['filename'],'sha256':context['sha256'],'captured_at':context['captured_at']}
        offer.source_quote_evidence=quote
    return offer


def reusable_photo(offer):
    """File integrity for reuse only, never image-content price validation."""
    from . import agent
    from pathlib import Path
    from ..review_context import context_valid
    bounded_timeout(1)
    context=offer.context_capture_evidence or {};quote=offer.source_quote_evidence or {};binding=quote.get('attachment_binding',{})
    if not (offer.screenshot_context and context and binding.get('quote_sha256')==quote_digest(quote)
            and binding.get('url')==offer.url and binding.get('observed_at')==quote.get('observed_at')
            and binding.get('sha256')==context.get('sha256') and binding.get('filename')==context.get('filename')
            and binding.get('captured_at')==context.get('captured_at')):return False
    try:
        root=agent.CACHE_DIR.resolve();path=Path(offer.screenshot_context)
        path=path if path.is_absolute() else root/path
        if path.is_symlink() or path.resolve().parent!=root or path.stat().st_size>20*1024*1024:return False
        data=path.read_bytes();bounded_timeout(1)
        return context_valid(context,offer.screenshot_context,offer.url,data)
    except (OSError,ValueError,TypeError):return False


def capture_current(browser, position, url, *, extract=True, neutral_sink=None, http_preflight=False, photos=True, financial_outcome=None):
    """Capture with existing transport/authority, then separately apply native quote rules.

    An active fact scope observes retained HTTP input before expected-product
    admission. Those raw observations cannot qualify or replace an offer.
    Native cancellation, seller, ownership and financial failures are unchanged.
    """
    from . import agent
    from .card_batch import extract_batch
    from .product_identity import card_host
    if type(http_preflight) is not bool:raise ValueError('Explicit HTTP preflight boolean required')
    if type(photos) is not bool:raise ValueError('Explicit photograph mode required')
    deadline=time.monotonic()+bounded_timeout(30)
    @contextmanager
    def mode():
        token=_CAPTURE.set(photos)
        try:yield
        finally:_CAPTURE.reset(token)
    with deadline_scope(deadline),mode(),supplier_transport(url):
        if not photos:
            # Reuse the existing bounded HTTP-first producer. The optional
            # contradiction check consumes its saved delivery, never a second GET.
            class PrimaryContradiction(Exception):pass
            class OwnedHttpSnapshot(Exception):
                def __init__(self,snapshot):self.snapshot=snapshot
            def delivered(record,source,*,reused=False):
                """Observe the saved HTTP page before matching; preserve existing refusal and snapshot control flow."""
                bounded_timeout(1)
                from .observed_fact_capture import observe_http
                observe_http(source)
                import base64
                from .http_card import main_product
                from .http_identity_preflight import primary_contradiction,VERSION
                from .failure_policy import FailureContext
                from .session import transport_observed
                from .supplier_health_store import Outcome
                try:
                    text=base64.b64decode(record['body_base64'],validate=True).decode(record['encoding'],errors='replace')
                    owned=main_product(text,record['url'],admission_only=True) if http_preflight else None
                    observed=main_product(text,record['url'])
                except (ValueError,LookupError):owned=observed=None
                refusal=primary_contradiction(position,owned) if owned else None
                if refusal:
                    bounded_timeout(1)
                    diagnostics=agent._CARD_DIAGNOSTICS.get()
                    if diagnostics is not None:diagnostics.append(dict(code='http_primary_owner_observation',reason='Сохранено независимое основное обозначение HTTP-товара',url=url,preflight_version=VERSION,http_wire_attempted=not reused,observed_at=record['observed_at'],http_response_source=source))
                    agent._reject_card(url,refusal['code'],refusal['reason'],failure_context=FailureContext('supplier_http','local','identity_conflict',record['observed_at'],200,card_host(url)))
                    transport_observed(Outcome.SUCCESS) if not reused else None
                    raise PrimaryContradiction()
                if not observed or agent._looks_like_bot_check(observed['text'],observed['title']):return
                brand=agent._verified_manufacturer_brand(position.type_mark)
                if agent._admit_card_text(url,observed['text'],observed['title'],position.type_mark,position.unit,position.qty,brand,set(),card_host(url)) is None:
                    transport_observed(Outcome.SUCCESS) if not reused else None;raise PrimaryContradiction()
                # Preserve the existing producer's mandatory render policy.
                if agent._canonical_host(url)=='e-kc.ru':return
                from .product_identity import http_identity
                identity=http_identity(record,observed,source,position.type_mark,position.name,card_host(url))
                if not identity:return
                from .card_batch import CardSnapshot
                facts=dict(url=record['url'],requested_url=url,supplier_host=card_host(url),expected_type_mark=position.type_mark,expected_name=position.name,expected_unit=position.unit,expected_qty=position.qty,text=observed['text'],title=observed['title'],main_title=observed['title'],page_article=observed['article'],identity_evidence=identity,adapter={},roll_length=None,captured_at=record['observed_at'],screenshot_context='',context_capture_evidence={},context_capture_review_reason='Снимок будет собран после отбора предложения',merchant_requisites=observed['merchant_requisites'],capture_transport='http',http_source=source)
                bounded_timeout(1);transport_observed(Outcome.SUCCESS) if not reused else None
                raise OwnedHttpSnapshot(CardSnapshot.create(facts,deadline))
            try:
                snapshot=agent._capture_card(browser,url,url,card_host(url),position.type_mark,
                    position.name,position.unit,position.qty,agent._verified_manufacturer_brand(position.type_mark),set(),deadline,
                    http_consumer=delivered,render_consumer=neutral_sink)
            except PrimaryContradiction:return None,None
            except OwnedHttpSnapshot as delivery:snapshot=delivery.snapshot
        else:
            from ..supplier_methods import selected
            grant=selected(url)
            owned_http_preflight=bool(http_preflight and grant is not None and grant['method']=='http')
            rejected,failed_http=_http_primary_preflight(position,url) if http_preflight and not owned_http_preflight else (False,False)
            if rejected:return None,None
            from .session import failed_primary_http
            from contextlib import nullcontext
            with failed_primary_http(url) if failed_http else nullcontext():
                snapshot=agent._capture_rendered_card(browser,url,url,card_host(url),position.type_mark,
                    position.name,position.unit,position.qty,agent._verified_manufacturer_brand(position.type_mark),set(),deadline,
                    neutral_sink=neutral_sink,primary_position=position if owned_http_preflight else None)
        bounded_timeout(1)
        if snapshot is None:return None,None
        retained_input=snapshot.retain(agent.CACHE_DIR)
        from .document_context import _capture_input_retained
        _capture_input_retained(snapshot,retained_input)
        from .owned_purchase_row import from_facts,retain_decision
        import uuid
        operation_id=uuid.uuid4().hex
        row=from_facts(snapshot.facts())
        raw_rejections=[]
        data=agent._deterministic_card_data(snapshot) if financial_outcome is None else agent._deterministic_card_data(snapshot,financial_outcome=raw_rejections.append)
        origin='current_owned_row' if row and data is not None else 'existing_deterministic' if data is not None else 'none'
        if data is None and extract:
            origin='model'
            try:
                data,error=extract_batch([snapshot])[snapshot.card_id]
                if error is not None:raise error
            except ResearchControlError:
                raise
            except Exception:
                retain_decision(snapshot,None,operation_id=operation_id,origin=origin,outcome='extraction_failed')
                raise
        retain_decision(snapshot,data,operation_id=operation_id,origin=origin,outcome='fields_selected' if data is not None else 'no_fields')
        if data is None:
            if financial_outcome is not None:
                financial_outcome(CaptureFinancialResult(CaptureFinancialOutcome.REJECTED,raw_rejections[0]) if raw_rejections else CaptureFinancialResult(CaptureFinancialOutcome.INSUFFICIENT))
            return snapshot,None
        offer=agent._finish_captured_card(browser,snapshot,data,enrich_seller=extract)
        retain_decision(snapshot,data,operation_id=operation_id,origin=origin,outcome='offer_created' if offer else 'finish_no_offer')
        if financial_outcome is not None:
            # A native unit-review placeholder is still useful to ordinary
            # callers, but is not a confirmed financial publication. Keep the
            # retained snapshot without replacing an existing catalogue head.
            review_only=(offer is not None and offer.unit_review_required is True
                         and not offer.source_quote_evidence)
            financial_outcome(CaptureFinancialResult(CaptureFinancialOutcome.REJECTED,raw_rejections[0]) if raw_rejections else CaptureFinancialResult(CaptureFinancialOutcome.INSUFFICIENT if review_only else CaptureFinancialOutcome.ACCEPTED if offer else CaptureFinancialOutcome.REJECTED))
        bounded_timeout(1)
        return snapshot,bind_photo(offer) if offer else None


def collect_current(browser, position, url, *, extract=True, neutral_sink=None, http_preflight=False):
    """Collect source facts without photographs, preserving them independently of quote admission.

    The ordinary capture/extraction/catalogue path remains the quote authority.
    Additive fact-journal references are emitted even if matching later refuses
    the item. Facts are neither a normalized price nor a catalogue-head update.
    Context and diagnostics are settled on cancellation/error without retry.
    """
    from . import agent
    from .observed_fact_capture import fact_capture_scope, observe_rendered
    from .selected_proofs import NOT_SELECTED

    def sink(record):
        """Observe the existing owner-verified DOM before forwarding the unchanged native neutral packet."""
        observe_rendered(record)
        if neutral_sink is not None:
            neutral_sink({**record, 'context_capture_review_reason': NOT_SELECTED})

    with fact_capture_scope(agent.CACHE_DIR, position.type_mark) as facts:
        try:
            snapshot, offer = capture_current(
                browser, position, url, extract=extract, neutral_sink=sink,
                http_preflight=http_preflight, photos=False,
            )
        finally:
            diagnostics = agent._CARD_DIAGNOSTICS.get()
            if diagnostics is not None:
                for report in facts.reports:
                    diagnostics.append({
                        'url': url, 'code': 'independent_observed_facts',
                        'reason': 'Сохранённые факты отделены от соответствия и пригодности цены',
                        'observed_facts': report,
                    })
    if snapshot is not None:
        from .card_batch import CardSnapshot
        observed = snapshot.facts()
        observed.update(
            screenshot_context='', context_capture_evidence={},
            context_capture_review_reason=NOT_SELECTED,
        )
        snapshot = CardSnapshot.create(observed, snapshot.deadline)
    return snapshot, offer


def _http_primary_preflight(position,url,*,response=None):
    """One normal bounded HTTP attempt; a rejection is never quote authority."""
    import httpx
    from ..browser import supplier_http_get,supplier_http_configuration_hash,SupplierHttpLimit,SupplierHttpRepresentation,_supplier_failure
    from .session import http_capability_advice,finish_http_capability
    from .product_identity import card_host
    from .http_card import main_product,save_response
    from .http_identity_preflight import primary_contradiction,VERSION
    from .failure_policy import supplier_card_transport_diagnostic
    from . import agent
    from ..supplier_methods import selected
    grant=selected(url)
    if grant is not None and grant['method']=='browser':return False,True
    started=time.monotonic();bounded_timeout(1);route=supplier_http_configuration_hash()
    capability=http_capability_advice(card_host(url),route)if response is None else None
    def record(code,reason,**fields):
        diagnostics=agent._CARD_DIAGNOSTICS.get()
        if diagnostics is not None:diagnostics.append(dict(code=code,reason=reason,url=url,preflight_version=VERSION,elapsed_seconds=time.monotonic()-started,**fields))
    def finish(outcome):
        bounded_timeout(1)
        finish_http_capability(capability,outcome if supplier_http_configuration_hash()==route else 'unknown')
    if capability is not None and not capability[2]['allowed']:
        record('http_primary_browser_first','HTTP пропущен по действующему состоянию транспорта; проверяется браузером',http_wire_attempted=False)
        return False,True
    try:
        if response is None:response=supplier_http_get(url,max_bytes=2*1024*1024,max_redirects=3)
    except (SupplierHttpRepresentation,SupplierHttpLimit):
        finish('unknown');record('http_primary_unavailable','HTTP не предоставил ограниченный документ; проверяется браузером',http_wire_attempted=True)
        return False,True
    except (httpx.TimeoutException,httpx.NetworkError) as error:
        diagnostic=supplier_card_transport_diagnostic(error,card_host(url))
        if diagnostic is None:raise
        finish('read_timeout' if isinstance(error,httpx.ReadTimeout) else 'unknown')
        record('http_primary_transport_fallback',diagnostic['reason']+'; проверяется браузером в оставшееся время',http_wire_attempted=True,
            **{key:value for key,value in diagnostic.items() if key not in {'code','reason','url'}})
        return False,True
    finish('delivered');bounded_timeout(1)
    if response.status_code>=400:
        error=httpx.HTTPStatusError('Supplier HTTP response rejected',request=response.request,response=response)
        _supplier_failure(error,url,'supplier_http');raise error
    if response.status_code!=200 or 'html' not in response.headers.get('content-type','').lower():
        record('http_primary_unsupported','HTTP не предоставил основной HTML-товар; проверяется браузером',http_wire_attempted=True)
        return False,False
    delivery,source=save_response(response,url)
    from .card_link_discovery import delivered
    delivered(delivery,source)
    try:owned=main_product(response.text,str(response.url),admission_only=True)
    except ValueError:owned=None
    bounded_timeout(1)
    refusal=primary_contradiction(position,owned) if owned else None
    bounded_timeout(1)
    if refusal:
        from .failure_policy import FailureContext
        record('http_primary_owner_observation','Сохранено независимое основное обозначение HTTP-товара',http_wire_attempted=True,observed_at=delivery['observed_at'],http_response_source=source)
        agent._reject_card(url,refusal['code'],refusal['reason'],failure_context=FailureContext('supplier_http','local','identity_conflict',delivery['observed_at'],200,card_host(url)))
        from .supplier_health_store import Outcome
        from .session import transport_observed
        transport_observed(Outcome.SUCCESS)
        return True,False
    record('http_primary_continue','Явное противоречие основного товара не установлено; финансовые данные проверяются браузером',http_wire_attempted=True,observed_at=delivery['observed_at'],http_response_source=source)
    return False,False


def attach_result(result,browser):
    """Initial top3 + one real review quote, at most4 URLs; no recursive chase."""
    from . import agent
    pos=result.position
    candidates=[o.model_copy(deep=True) for o in (result.all_offers or result.offers)]
    if pos.is_unique or not candidates:return result,[]
    selected=select_top_offers(candidates,3,pos)
    review=[o for o in candidates if not offer_is_comparable(o,pos) and o.url and o.source_quote_evidence]
    queue=[]
    for offer in list(selected)+review[:1]:
        if offer.url and offer.url not in {o.url for o in queue}:queue.append(offer)
    diagnostics=[]
    # Reuse only photographs already bound to the exact source observation.
    for old in queue:
        bounded_timeout(1)
        if reusable_photo(old):continue
        try:
            diagnostics.append({'url':old.url,'code':'attachment_attempt','reason':'Проверка текущего поколения выбранной карточки','observed_at':time.time()})
            with agent.capture_card_diagnostics() as observed:
                snapshot,fresh=capture_current(browser,pos,old.url,extract=False)
            diagnostics.extend(observed)
            if snapshot is not None:diagnostics.append({'url':old.url,'code':'attachment_source_observation','reason':'Текущие сведения карточки сохранены','capture_snapshot':snapshot.retain(agent.CACHE_DIR),'observed_at':snapshot.facts()['captured_at']})
            if fresh is None:
                reason=next((d.get('reason') for d in reversed(observed) if d.get('code') not in {'render_observation','main_facts_readiness','capture_snapshot_observation'} and d.get('reason')), 'Текущая цена/карточка не подтверждена; снимок не связан с сохранённой ценой')
                old.context_capture_review_reason=reason
                old.screenshot_product=None;old.screenshot_context='';old.context_capture_evidence={}
                quote_codes={'article_mismatch','product_mismatch','product_unconfirmed','price_on_request','out_of_stock','minimum_order','minimum_price','price_or_unit_unconfirmed'}
                if snapshot is not None or any(d.get('code') in quote_codes for d in observed):
                    old.refresh_failed=True;old.refresh_failure_reason=reason;old.refresh_checked_at=time.time()
                continue
            bounded_timeout(1)
            # Every direction of price change is authoritative only through the
            # unchanged fresh quote/unit/product/seller validation.
            if offer_is_comparable(fresh,pos):
                index=next(i for i,o in enumerate(candidates) if o is old)
                candidates[index]=fresh
            else:
                old.context_capture_review_reason='Новая карточка требует проверки; снимок не подтверждает сохранённую цену'
                old.screenshot_product=None;old.screenshot_context='';old.context_capture_evidence={}
                old.refresh_failed=True;old.refresh_failure_reason=old.context_capture_review_reason;old.refresh_checked_at=time.time()
                candidates.append(fresh) # Actual reviewed current facts, not a repair of old price.
                diagnostics.append({'url':old.url,'code':'attachment_quote_review','reason':old.context_capture_review_reason})
        except ResearchControlError as error:
            from .session import SearchBudgetExceeded
            if not isinstance(error,SearchBudgetExceeded):raise
            bounded_timeout(1) # A live parent distinguishes finite child timeout.
            old.screenshot_product=None;old.screenshot_context='';old.context_capture_evidence={}
            old.context_capture_review_reason='Снимок не получен в выделенное время; сохранена датированная цена'
            diagnostics.append({'url':old.url,'code':'attachment_timeout','reason':old.context_capture_review_reason})
        except Exception as error:
            old.screenshot_product=None;old.screenshot_context='';old.context_capture_evidence={}
            old.context_capture_review_reason='Снимок не получен: '+type(error).__name__
            diagnostics.append({'url':old.url,'code':'attachment_failed','reason':old.context_capture_review_reason,'error_type':type(error).__name__})
    bounded_timeout(1)
    result.all_offers=candidates;result.offers=select_top_offers(candidates,3,pos)
    result.sources_found=source_count(candidates,pos)
    for offer in result.offers:
        if not offer.screenshot_context:offer.context_capture_review_reason=offer.context_capture_review_reason or 'Снимок новой выбранной цены ожидается; ограниченный проход завершён'
    return result,diagnostics


def attach_row(position,row,browser):
    from ..schemas import OfferIn
    from ..app import _offer_to_dict
    projected=position.model_copy(update={'offers':[OfferIn.model_validate(o) for o in row.get('candidates') or row.get('offers') or []]}).to_result()
    projected.all_offers=list(projected.offers)
    result,diagnostics=attach_result(projected,browser)
    changed=dict(row,candidates=[_offer_to_dict(o,position.type_mark) for o in result.all_offers],offers=[_offer_to_dict(o,position.type_mark) for o in result.offers],found=result.sources_found)
    changed['diagnostics']=[*row.get('diagnostics',[]),*diagnostics]
    if row.get('status') in {'complete','partial','failed'}:
        target=row.get('target',5)
        changed['status']='complete' if changed['found']>=target else 'partial' if changed['found'] else 'failed'
        from ..app import _current_research_reason
        changed['reason']=_current_research_reason(row.get('reason',''),changed['found'],target,changed['candidates'])
    changed['attachment_pass_completed']=True
    return changed
