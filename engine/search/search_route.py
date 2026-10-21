"""One job-owned same-account XML route; no settings changes or engine cascade."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json,math,time
from urllib.parse import urlsplit
from .failure_policy import SearchServiceUnavailable,SearchTransportAttempt,ResearchControlError

VERSION='xmlriver-route-v1'
PROTOCOL='xmlriver-numeric-xml-v1'
_ROUTE=ContextVar('job_search_route',default=None)

def _numeric_primary(endpoint):
    if type(endpoint) is not str:return False
    try:p=urlsplit(endpoint);port=p.port
    except ValueError:return False
    return port in (None,443) and p.scheme=='https' and p.hostname in {'xmlriver.com','www.xmlriver.com'} and p.path=='/search_yandex/xml' and not (p.username or p.password or p.query or p.fragment)

def validate_route(profile):
    if type(profile) is not dict or set(profile)!={'backend','endpoint','locale','document_limit','protocol_version'}:
        raise SearchServiceUnavailable('configuration_invalid')
    if any(type(profile[k]) is not str for k in ('backend','endpoint','protocol_version')) or profile['protocol_version']!=PROTOCOL or type(profile['document_limit']) is not int or profile['document_limit']!=10 or type(profile['locale']) is not dict:
        raise SearchServiceUnavailable('configuration_invalid')
    if profile['backend']=='xmlriver-google':
        valid=profile['endpoint']=='https://xmlriver.com/search/xml' and profile['locale']=={'lr':'ru','country':2643} and type(profile['locale'].get('country')) is int
    elif profile['backend']=='xmlriver-yandex':
        locale=profile['locale'];valid=_numeric_primary(profile['endpoint']) and set(locale)=={'lr'} and type(locale['lr']) is int and locale['lr']>=0
    else:valid=False
    if not valid:raise SearchServiceUnavailable('configuration_invalid')
    return deepcopy(profile)

def _validated_preference(preference,configured_endpoint):
    if type(preference) is not str or preference not in {'auto','xmlriver-google'}:
        raise SearchServiceUnavailable('configuration_invalid')
    if preference=='xmlriver-google' and not _numeric_primary(configured_endpoint):
        raise SearchServiceUnavailable('configuration_invalid')
    return preference

def select_route(capabilities,configured_endpoint,configured_lr, *, preference='auto'):
    preference=_validated_preference(preference,configured_endpoint)
    if not _numeric_primary(configured_endpoint):return None
    if type(configured_lr) is not int or configured_lr<0:raise SearchServiceUnavailable('configuration_invalid')
    if type(capabilities) is not dict or type(capabilities.get('engine')) is not dict or type(capabilities.get('balance_positive')) is not bool:
        raise SearchServiceUnavailable('invalid_structure',validation_branch='capability_shape')
    if not capabilities['balance_positive']:raise SearchServiceUnavailable('account_balance')
    engines=capabilities['engine']
    if any(type(engines.get(k)) is not int or engines[k] not in (0,1) for k in ('yandex','google')):
        raise SearchServiceUnavailable('invalid_structure',validation_branch='engine_flags')
    if preference=='xmlriver-google':
        if engines['google']!=1:raise SearchServiceUnavailable('selected_engine_unavailable')
        return validate_route(dict(backend='xmlriver-google',endpoint='https://xmlriver.com/search/xml',locale={'lr':'ru','country':2643},document_limit=10,protocol_version=PROTOCOL))
    if engines['yandex']==1:
        return validate_route(dict(backend='xmlriver-yandex',endpoint=configured_endpoint,locale={'lr':configured_lr},document_limit=10,protocol_version=PROTOCOL))
    if engines['google']==1:
        return validate_route(dict(backend='xmlriver-google',endpoint='https://xmlriver.com/search/xml',locale={'lr':'ru','country':2643},document_limit=10,protocol_version=PROTOCOL))
    raise SearchServiceUnavailable('engine_unavailable')

@contextmanager
def search_route_scope(profile):
    token=_ROUTE.set(validate_route(profile) if profile is not None else None)
    try:yield
    finally:_ROUTE.reset(token)

def active_search_route():return deepcopy(_ROUTE.get())

def resolve_initial_route(*,deadline=None):
    """At most one bounded admin GET; caller must first exclude known-only jobs."""
    import httpx
    from ..config import settings
    from ..llm import _REQUEST_GUARD
    from .session import bounded_timeout,deadline_scope,transport_started
    preference=getattr(settings,'xmlriver_backend_preference','auto')
    preference_until=None
    if type(preference) is not str or preference!='auto':
        # New operator-choice refusals must not override a pre-existing global
        # stop, cancellation or deadline. Legacy auto eligibility stays intact.
        preference_until=min(time.monotonic()+10,deadline) if deadline is not None else time.monotonic()+10
        with deadline_scope(preference_until):
            guard=_REQUEST_GUARD.get()
            if guard is not None:guard.check()
            bounded_timeout(max(0,preference_until-time.monotonic()))
        preference=_validated_preference(preference,settings.xmlriver_url)
    if not _numeric_primary(settings.xmlriver_url):return None,None
    if not settings.xmlriver_user and not settings.xmlriver_key:
        if preference=='xmlriver-google':raise SearchServiceUnavailable('configuration_missing')
        return None,None
    if not (settings.xmlriver_user and settings.xmlriver_key):raise SearchServiceUnavailable('configuration_missing')
    until=preference_until if preference_until is not None else (min(time.monotonic()+10,deadline) if deadline is not None else time.monotonic()+10)
    guard=_REQUEST_GUARD.get()
    def check():
        if guard is not None:guard.check()
        return bounded_timeout(max(0,until-time.monotonic()))
    with deadline_scope(until):
        remaining=check();observed=time.time();started=time.monotonic()
        try:
            with httpx.Client(timeout=httpx.Timeout(remaining,connect=min(10,remaining)),follow_redirects=False) as client:
                check();transport_started('search_account_api')
                with client.stream('GET','https://xmlriver.com/api/get_info/',params={'user':settings.xmlriver_user,'key':settings.xmlriver_key},headers={'Accept-Encoding':'identity'}) as response:
                    if response.status_code!=200:
                        category='account_access' if response.status_code in (401,403) else 'account_balance' if response.status_code==402 else 'http_error'
                        raise SearchServiceUnavailable(category,status=response.status_code)
                    if response.headers.get('content-encoding','').strip().lower() not in ('','identity'):raise SearchServiceUnavailable('invalid_structure',validation_branch='content_encoding')
                    chunks=[];size=0
                    for chunk in response.iter_raw():
                        check();size+=len(chunk)
                        if size>65536:raise SearchServiceUnavailable('invalid_structure',validation_branch='response_size')
                        chunks.append(chunk)
                    check();payload=json.loads(b''.join(chunks))
            if type(payload) is not dict:raise SearchServiceUnavailable('invalid_structure',validation_branch='payload_type')
            if payload.get('error'):
                code=str(payload['error'].get('code','')) if type(payload['error']) is dict else ''
                raise SearchServiceUnavailable('account_balance' if code=='200' else 'account_access')
            balance=payload.get('balance')
            if type(balance) not in (int,float):raise SearchServiceUnavailable('invalid_structure',validation_branch='balance_type')
            if not math.isfinite(balance):raise SearchServiceUnavailable('invalid_structure',validation_branch='balance_nonfinite')
            capabilities=dict(engine=payload.get('engine'),balance_positive=balance>0)
            profile=select_route(capabilities,settings.xmlriver_url,settings.xmlriver_lr,preference=preference)
            check()
            receipt=dict(observed_at=observed,http_status=200,account_api_calls=1,search_requests=0,engine={k:capabilities['engine'][k] for k in ('yandex','google')},balance_positive=True,selection_reason='operator_selected_google' if preference=='xmlriver-google' else 'primary_enabled' if profile['backend']=='xmlriver-yandex' else 'primary_engine_disabled')
            return profile,receipt
        except ResearchControlError:raise
        except json.JSONDecodeError:raise SearchServiceUnavailable('invalid_structure',validation_branch='json_decode') from None
        except UnicodeError:raise SearchServiceUnavailable('invalid_structure',validation_branch='unicode_decode') from None
        except (TypeError,ValueError):raise SearchServiceUnavailable('invalid_structure',validation_branch='structure_exception') from None
        except httpx.HTTPError as error:
            # Exact known classes only: subclass names and exception/request data
            # may contain credentials and must never enter persisted diagnostics.
            safe_types = {
                httpx.ConnectTimeout:'ConnectTimeout', httpx.ReadTimeout:'ReadTimeout',
                httpx.WriteTimeout:'WriteTimeout', httpx.PoolTimeout:'PoolTimeout',
                httpx.ConnectError:'ConnectError', httpx.ReadError:'ReadError',
                httpx.WriteError:'WriteError', httpx.CloseError:'CloseError',
                httpx.ProxyError:'ProxyError', httpx.RemoteProtocolError:'RemoteProtocolError',
                httpx.LocalProtocolError:'LocalProtocolError', httpx.UnsupportedProtocol:'UnsupportedProtocol',
                httpx.DecodingError:'DecodingError', httpx.TooManyRedirects:'TooManyRedirects',
                httpx.HTTPStatusError:'HTTPStatusError', httpx.HTTPError:'HTTPError',
            }
            elapsed=time.monotonic()-started
            elapsed=max(0.0,elapsed) if math.isfinite(elapsed) else 0.0
            attempt=SearchTransportAttempt(safe_types.get(type(error),'HTTPError'),observed,elapsed,False)
            raise SearchServiceUnavailable('transport_error',transport_attempts=(attempt,),
                transport_elapsed_seconds=elapsed) from None
