"""Тонкая обёртка над LLM для структуризации/матчинга.

Провайдер выбирается LLM_PROVIDER: "anthropic", "deepseek" или "cloudru".
Cloud.ru использует отдельный OCR сканов перед текстовой структуризацией.
DeepSeek принимает изображения через OpenAI-совместимые image_url блоки;
для них используется отдельная модель deepseek_model_vision.

Если ключ не задан — функции бросают LLMUnavailable, чтобы вызывающий код мог
переключиться на ручной/демо-режим.
"""
from __future__ import annotations

import base64
import json
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import httpx

from .config import settings
from .provider_transport import provider_post, run_provider, sdk_trace_client, attempt_scope
from .telemetry import record_llm_usage, timed_call
from .search.failure_policy import ResearchControlError

MODEL_OPERATION_TIMEOUT_SECONDS = 120
MODEL_CONNECT_TIMEOUT_SECONDS = 10


def model_transport_timeout(seconds):
    """Recheck controls at wire admission, after serialization/payload work."""
    import math
    from .search.session import bounded_timeout
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('Model operation timeout must be finite and positive')
    remaining = bounded_timeout(seconds)
    return httpx.Timeout(remaining, connect=min(MODEL_CONNECT_TIMEOUT_SECONDS, remaining))


from dataclasses import dataclass, asdict

_OUTPUT_REASONS = {'invalid_json', 'empty_output', 'truncated_output',
                   'invalid_schema', 'invalid_rows', 'empty_positions', 'refused_output'}
_FINISH_REASONS = {'stop', 'length', 'max_tokens', 'end_turn', 'tool_use', 'content_filter', 'refusal'}

@dataclass(frozen=True)
class ModelOutputMetadata:
    reason: str
    provider: str
    finish_reason: str
    output_kind: str
    truncated: bool
    observed_at: float

class ModelOutputInvalid(ResearchControlError):
    """Known unusable model output; the affected operation needs review, not an outage."""
    def __init__(self, reason, *, provider='unknown', finish_reason=None, output_kind='json'):
        import time
        if reason not in _OUTPUT_REASONS or output_kind not in {'json','ocr','positions','cells','letter'}:
            raise ValueError('Unknown model output classification')
        provider = provider if provider in {'cloudru','deepseek','anthropic'} else 'unknown'
        finish = finish_reason if isinstance(finish_reason,str) and finish_reason in _FINISH_REASONS else 'unknown'
        self.metadata=ModelOutputMetadata(reason,provider,finish,output_kind,
            reason=='truncated_output' or finish in {'length','max_tokens'},time.time())
        super().__init__('Ответ модели требует проверки: '+reason)
        _record_model_output(self.metadata)

    def diagnostic(self):
        from dataclasses import asdict
        result = {'code':'model_output_invalid','reason':str(self),'url':'',
                'failure_source':'model_output','failure_scope':'operation',
                'failure_cause':self.metadata.reason,
                **{k:v for k,v in asdict(self.metadata).items()if k!='reason'},
                'model_output_reason':self.metadata.reason}
        if hasattr(self,'provider_attempts'):
            result['provider_transport_attempts'] = [asdict(x) for x in self.provider_attempts]
        return result

def _record_model_output(metadata):
    from dataclasses import asdict
    from .telemetry import record_stage
    record_stage('model_output',0,ok=metadata.reason=='valid_json',
                 label=json.dumps(asdict(metadata),sort_keys=True))

class _ModelText(str):
    pass

def _validated_model_text(text, *, provider, finish_reason=None, output_kind='json'):
    from .search.session import bounded_timeout
    bounded_timeout(1)
    if not isinstance(text,str):
        raise LLMOperationFailed('invalid_response_envelope')
    if isinstance(finish_reason,str) and finish_reason in {'length','max_tokens'}:
        raise ModelOutputInvalid('truncated_output',provider=provider,finish_reason=finish_reason,output_kind=output_kind)
    if isinstance(finish_reason,str) and finish_reason in {'content_filter','refusal'}:
        raise ModelOutputInvalid('refused_output',provider=provider,finish_reason=finish_reason,output_kind=output_kind)
    if not text.strip():
        raise ModelOutputInvalid('empty_output',provider=provider,finish_reason=finish_reason,output_kind=output_kind)
    result=_ModelText(text);result.finish_reason=finish_reason
    return result

def _completion_text(data, *, provider, output_kind='json'):
    from .search.session import bounded_timeout
    bounded_timeout(1)
    if (not isinstance(data,dict) or not isinstance(data.get('choices'),list) or
        not data['choices'] or not isinstance(data['choices'][0],dict) or
        not isinstance(data['choices'][0].get('message'),dict)):
        raise LLMOperationFailed('invalid_response_envelope')
    choice=data['choices'][0];message=choice['message'];content=message.get('content')
    refusal=message.get('refusal')
    # A documented explicit refusal may have content:null. Missing content or
    # type corruption is still an invalid API envelope, including length finishes.
    if ('content' in message and (isinstance(content,str) or content is None) and
        isinstance(refusal,str) and refusal.strip()):
        raise ModelOutputInvalid('refused_output',provider=provider,
            finish_reason=choice.get('finish_reason'),output_kind=output_kind)
    return _validated_model_text(content,provider=provider,
        finish_reason=choice.get('finish_reason'),output_kind=output_kind)

def _parse_model_json(text, *, provider, finish_reason=None):
    import time
    finish_reason = finish_reason if finish_reason is not None else getattr(text,'finish_reason',None)
    text=_validated_model_text(text,provider=provider,finish_reason=finish_reason)
    try:
        value=_extract_json(text)
    except json.JSONDecodeError:
        raise ModelOutputInvalid('invalid_json',provider=provider,finish_reason=finish_reason) from None
    metadata=ModelOutputMetadata('valid_json',provider,
        finish_reason if isinstance(finish_reason,str) and finish_reason in _FINISH_REASONS else 'unknown',
        'json',False,time.time())
    _record_model_output(metadata)
    return value

class LLMUnavailable(ResearchControlError):
    def __init__(self, message):
        import time
        self.observed_at = time.time()
        super().__init__(message)

    def diagnostic(self):
        from .search.failure_policy import FailureContext
        result = FailureContext('llm', 'global', 'llm_unavailable', self.observed_at,
                                getattr(self, 'http_status', None)).diagnostic(str(self), code='llm_unavailable')
        if hasattr(self,'provider_attempts'):
            result['provider_transport_attempts'] = [asdict(x) for x in self.provider_attempts]
        if hasattr(self,'global_guard_latched_at'):
            result['global_guard_latched_at'] = self.global_guard_latched_at
        return result


class LLMOperationFailed(LLMUnavailable):
    """An isolated provider operation failed; no account/outage claim or replay."""
    def __init__(self, cause, *, status=None):
        import time
        if cause not in {'transport_error', 'response_error', 'invalid_response_envelope'}:
            raise ValueError('Unknown provider operation failure')
        self.cause = cause
        self.http_status = status
        self.observed_at = time.time()
        super().__init__('Ответ API модели не подтверждён; проверка этой карточки не завершена')

    def diagnostic(self):
        from .search.failure_policy import FailureContext
        result = FailureContext('llm', 'operation', self.cause, self.observed_at,
                                self.http_status).diagnostic(str(self), code='llm_operation_failed')
        result['automatic_replay'] = False
        if hasattr(self, 'provider_attempts'):
            result['provider_transport_attempts'] = [asdict(x) for x in self.provider_attempts]
            result['after_send_uncertain'] = any(a.endpoint_send_started and a.outcome == 'error'
                                                 for a in self.provider_attempts)
        return result


def provider_usage(usage):
    """Optional malformed usage is unknown; usable model text remains usable."""
    from .telemetry import record_stage
    if usage is None:return None
    invalid = not isinstance(usage,dict)
    if not invalid:
        for key in ('input_tokens','prompt_tokens','output_tokens','completion_tokens',
                    'prompt_cache_hit_tokens','cache_read_input_tokens'):
            if key in usage and usage[key] is not None:
                value=usage[key]
                if not isinstance(value,int) or isinstance(value,bool) or value < 0:
                    invalid=True;break
    if invalid:
        record_stage('provider_usage',0,ok=False,label='optional_usage_unknown')
        return None
    return usage


def provider_http_failure(status, provider):
    """Only explicit transient response classes are operation-local."""
    if status in {401, 402, 403}:
        return LLMAccountError(status, provider)
    if status == 408 or 500 <= status <= 599:
        return LLMOperationFailed('response_error', status=status)
    # Rate/quota and request/configuration rejections retain the existing stop.
    failure = LLMUnavailable(f'{provider} API: HTTP {status}; запрос не выполнен')
    failure.http_status = status
    return failure


class LLMAccountError(LLMUnavailable):
    """Fatal account rejection with a safe public message, never response text."""

    def __init__(self, status, provider="DeepSeek"):
        self.status = status
        self.provider = provider
        self.code = ('llm_insufficient_balance' if status == 402 else 'llm_auth_error')
        message = (f'Недостаточно баланса {provider} API; пополните аккаунт и продолжите поиск'
                   if status == 402 else
                   f'{provider} API отклонил доступ (HTTP {status}); проверьте ключ и права аккаунта')
        super().__init__(message)

    def diagnostic(self):
        result = super().diagnostic()
        result.update(code=self.code, http_status=self.status,
                      failure_cause=self.code, provider=self.provider)
        return result


class LLMRequestGuard:
    """Bound provider lanes independently; latch only proven global failures."""

    def __init__(self, approved_hosts=()):
        from .search.failure_policy import RouteObservations
        self.lock = threading.RLock()
        from .search.admission import OrderedSlots
        self._lanes = {'llm': OrderedSlots(1), 'search_api': OrderedSlots(settings.search_api_workers)}
        self._local = threading.local()
        self._capacity_condition = threading.Condition(self.lock)
        self._capacity_probe = None
        self.failure = None
        self._search_deferred = None  # Ephemeral discovery-only state; never terminal failure.
        self.routes = RouteObservations(approved_hosts)

    def check(self):
        with self.lock:
            if self.failure is not None:
                raise self.failure

    def reject(self, failure):
        with self.lock:
            if self.failure is None:
                import time
                failure.global_guard_latched_at = time.time()
                self.failure = failure
                self._capacity_condition.notify_all()

    def diagnostic(self):
        with self.lock:
            return self.failure.diagnostic() if self.failure is not None else None

    def defer_search(self, failure):
        with self.lock:
            self._search_deferred = self._search_deferred or failure
            self._capacity_condition.notify_all()

    def search_deferred(self):
        with self.lock:
            return self._search_deferred.diagnostic() if self._search_deferred is not None else None

    def observe_route(self, context):
        with self.lock:
            failure = self.routes.observe(context)
            if failure is not None:
                self.reject(failure)

    @contextmanager
    def search_capacity_scope(self, enabled):
        previous = getattr(self._local, 'temporary_search', False)
        self._local.temporary_search = bool(enabled)
        try:
            yield
        finally:
            self._local.temporary_search = previous

    def check_search_capacity(self):
        if not getattr(self._local, 'temporary_search', False):
            return
        from .search.failure_policy import SearchCapacityDeferred
        with self.lock:
            self.check()
            if self._search_deferred is not None:
                raise SearchCapacityDeferred(observed_at=self._search_deferred.context.observed_at, reused=True)

    def _wait_search_probe(self, operation=None):
        from .search.session import bounded_timeout
        with self._capacity_condition:
            while self._capacity_probe is not None and self._capacity_probe != operation:
                self.check_search_capacity()
                remaining = bounded_timeout(1)
                self._capacity_condition.wait(min(.05, remaining))
            self.check_search_capacity()
            bounded_timeout(1)

    def claim_search_retry(self, operation):
        """One retry owner; another admitted XML111 waits within its own budget."""
        if not (getattr(self._local, 'temporary_search', False)
                and getattr(self._local, 'depth_search_api', 0)):
            return  # Private unguarded GET keeps its existing finite allocation.
        with self._capacity_condition:
            self._wait_search_probe(operation)
            self._capacity_probe = operation
            self._local.search_probe = operation

    def call(self, source, operation):
        from .search.session import bounded_timeout, SearchBudgetExceeded
        from .search.failure_policy import SearchCapacityDeferred
        lane_name = 'llm' if source == 'llm' else 'search_api'
        depth_key = 'depth_' + lane_name
        depth = getattr(self._local, depth_key, 0)
        lane = self._lanes[lane_name]
        acquired = False
        def check_admission():
            self.check()
            bounded_timeout(1)
        check_admission()
        if not depth:
            seconds = bounded_timeout(MODEL_OPERATION_TIMEOUT_SECONDS if source == 'llm' else 30)
            # Never hold the state lock while acquiring/waiting/releasing a lane.
            acquired = lane.acquire(timeout=seconds, check=check_admission)
            if not acquired:
                raise SearchBudgetExceeded('Ожидание канала провайдера завершено; операция не начата')
        setattr(self._local, depth_key, depth+1)
        try:
            check_admission()
            if lane_name == 'search_api' and getattr(self._local, 'temporary_search', False):
                self._wait_search_probe(getattr(self._local, 'search_probe', None))
            try:
                return self._invoke(source, operation)
            except SearchCapacityDeferred as failure:
                check_admission()
                if getattr(self._local, 'temporary_search', False):
                    self.defer_search(failure)
                raise
        finally:
            setattr(self._local, depth_key, depth)
            if not depth and lane_name == 'search_api':
                with self._capacity_condition:
                    owned = getattr(self._local, 'search_probe', None)
                    if owned is not None and self._capacity_probe == owned:
                        self._capacity_probe = None
                        self._capacity_condition.notify_all()
                    self._local.search_probe = None
            if acquired:
                lane.release()

    def _invoke(self, source, operation):
        from .search.failure_policy import SearchServiceUnavailable, SearchQueryFailed
        from .search.session import bounded_timeout, SearchBudgetExceeded, ResearchProgressError
        self.check()
        bounded_timeout(1)
        try:
            result = operation()
            with self.lock:
                self.check()
                bounded_timeout(1)
                if source != 'llm':
                    self.check_search_capacity()
                return result
        except (SearchBudgetExceeded, ResearchProgressError):
            raise
        except (ModelOutputInvalid, LLMOperationFailed, SearchQueryFailed):
            self.check()
            bounded_timeout(1)
            raise
        except (LLMUnavailable, SearchServiceUnavailable) as failure:
            self.check()
            bounded_timeout(1)
            self.reject(failure)
            self.check()
            raise
        except ResearchControlError:
            raise
        except Exception as error:
            self.check()
            bounded_timeout(1)
            # Exhausting the card/cancellation allocation is a control outcome,
            # not evidence of a broken provider or supplier.
            if isinstance(error, httpx.TimeoutException) or type(error).__name__ == 'APITimeoutError':
                bounded_timeout(1)
            if source == 'llm' and (isinstance(error, httpx.RequestError) or
                    type(error).__name__ in {'APIConnectionError','APITimeoutError'}):
                failure = LLMOperationFailed('transport_error')
                if hasattr(error, 'provider_attempts'):
                    failure.provider_attempts = error.provider_attempts
                raise failure from None
            status = getattr(error, 'status_code', None)
            if source == 'llm' and isinstance(status, int) and not isinstance(status, bool):
                failure = provider_http_failure(status, 'API модели')
                if hasattr(error, 'provider_attempts'):
                    failure.provider_attempts = error.provider_attempts
                if isinstance(failure, LLMUnavailable) and not isinstance(failure, LLMOperationFailed):
                    self.reject(failure)
                    self.check()
                raise failure from None
            failure = (LLMUnavailable('API модели не подтвердил успешный ответ; задание остановлено')
                       if source == 'llm' else SearchServiceUnavailable('transport_error'))
            status = getattr(error, 'status_code', None)
            if isinstance(status, int):
                failure.http_status = status
            if hasattr(error, "provider_attempts"):
                failure.provider_attempts = error.provider_attempts
            self.reject(failure)
            self.check()
            raise failure from None


def guarded_llm(operation):
    from functools import wraps
    @wraps(operation)
    def guarded(*args, **kwargs):
        import inspect, time
        from .search.session import deadline_scope
        bound = inspect.signature(operation).bind_partial(*args, **kwargs)
        seconds = bound.arguments.get('timeout_seconds', MODEL_OPERATION_TIMEOUT_SECONDS)
        # Start before serialized guard admission; nested adapters inherit the minimum.
        with attempt_scope(), deadline_scope(time.monotonic()+seconds):
            guard = _REQUEST_GUARD.get()
            return (guard.call('llm', lambda: operation(*args, **kwargs)) if guard is not None
                    else operation(*args, **kwargs))
    return guarded


_REQUEST_GUARD = ContextVar('llm_request_guard', default=None)


@contextmanager
def llm_request_scope(guard):
    token = _REQUEST_GUARD.set(guard if guard is not None else _REQUEST_GUARD.get())
    try:
        yield
    finally:
        _REQUEST_GUARD.reset(token)


def _extract_json(text: str) -> Any:
    """Парсит JSON из ответа модели, срезая обёртку ```json ... ``` если есть."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip().rstrip("`").strip()
    return json.loads(text)


# ----------------------------- Anthropic (Claude) -----------------------------

def _anthropic_client(http_client=None):
    if not settings.anthropic_api_key:
        raise LLMUnavailable("ANTHROPIC_API_KEY не задан (.env)")
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise LLMUnavailable("пакет anthropic не установлен") from e
    return anthropic.Anthropic(api_key=settings.anthropic_api_key, http_client=http_client, max_retries=0)


def _image_block(path: str | Path) -> dict:
    data = base64.standard_b64encode(Path(path).read_bytes()).decode()
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": data}}


@guarded_llm
def _complete_anthropic(prompt, system, smart, max_tokens, images,
                        timeout_seconds=MODEL_OPERATION_TIMEOUT_SECONDS) -> Any:
    model = settings.model_smart if smart else settings.model_fast
    content: list[dict] = [_image_block(p) for p in (images or [])]
    content.append({"type": "text", "text": prompt})
    with sdk_trace_client() as http_client:
        client = _anthropic_client(http_client)
        try:
            def request(timeout, trace):
                configured = client.with_options(timeout=timeout, max_retries=0)
                return configured.messages.create(model=model,max_tokens=max_tokens,
                    system=system or "Ты помощник сметчика. Отвечай ТОЛЬКО валидным JSON без пояснений.",
                    messages=[{"role":"user","content":content}])
            msg = run_provider('anthropic',http_client,request)
        finally:
            client.close()
    record_llm_usage("anthropic", msg.model, msg.usage.model_dump())
    from .search.session import bounded_timeout
    bounded_timeout(1)
    text = "".join(block.text for block in msg.content if block.type == "text")
    return _parse_model_json(text,provider='anthropic',finish_reason=msg.stop_reason)


# ------------------------------- DeepSeek -------------------------------------

@guarded_llm
def _complete_deepseek(prompt, system, smart, max_tokens, images,
                       timeout_seconds: float) -> Any:
    guard = _REQUEST_GUARD.get()
    if guard is not None:
        guard.check()
    if not settings.deepseek_api_key:
        raise LLMUnavailable("DEEPSEEK_API_KEY не задан (.env)")
    model = (settings.deepseek_model_vision if images else
             settings.deepseek_model_smart if smart else settings.deepseek_model_fast)
    content = prompt
    if images:
        content = [{"type": "text", "text": prompt}]
        content.extend(_deepseek_image_block(path) for path in images)
        if len(images) > 600 or len(json.dumps(content).encode()) > 47 * 1024 * 1024:
            raise LLMUnavailable("Изображения превышают лимит одного запроса DeepSeek")
    sys_prompt = system or "Ты помощник сметчика. Отвечай ТОЛЬКО валидным JSON без пояснений."
    # response_format=json_object у DeepSeek требует слова "json" в промпте
    if "json" not in (sys_prompt + prompt).lower():
        sys_prompt += " Ответ верни в формате JSON."
    if guard is not None:
        guard.check()  # An image payload may have taken time to prepare.
    resp = provider_post(
        settings.deepseek_base_url.rstrip("/") + "/chat/completions",
        provider="deepseek",
        headers={"Authorization": f"Bearer {settings.deepseek_api_key}",
                 "Content-Type": "application/json"},
        json={
            "model": model,
            "max_tokens": max_tokens,
            **({'thinking': {'type': 'disabled'}, 'temperature': 0} if images else {}),
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": content},
            ],
        },
        timeout=model_transport_timeout(timeout_seconds),
    )
    if resp.status_code != 200:
        from .search.session import bounded_timeout
        bounded_timeout(1)
        raise provider_http_failure(resp.status_code, 'DeepSeek')
    try:
        payload = resp.json()
    except ValueError:
        from .search.session import bounded_timeout
        bounded_timeout(1)
        raise LLMOperationFailed('invalid_response_envelope') from None
    if not isinstance(payload,dict):
        from .search.session import bounded_timeout
        bounded_timeout(1)
        raise LLMOperationFailed('invalid_response_envelope')
    record_llm_usage("deepseek", payload.get("model", model), provider_usage(payload.get("usage")))
    from .search.session import bounded_timeout
    bounded_timeout(1)
    return _parse_model_json(_completion_text(payload,provider='deepseek'),provider='deepseek')


def _deepseek_image_block(path):
    """Local pixels only; never upload files or expose image/request data in logs."""
    import io
    from PIL import Image
    data = Path(path).read_bytes()
    with Image.open(io.BytesIO(data)) as img:
        if max(img.size) > 8192:
            raise LLMUnavailable("Изображение превышает 8192 пикселя по стороне")
        mime = {'JPEG': 'image/jpeg', 'PNG': 'image/png',
                'GIF': 'image/gif', 'WEBP': 'image/webp'}.get(img.format)
        if mime is None:
            buffer = io.BytesIO()
            img.convert('RGB').save(buffer, format='PNG')
            data, mime = buffer.getvalue(), 'image/png'
    if len(data) > 32 * 1024 * 1024:
        raise LLMUnavailable("Изображение превышает 32 МиБ")
    encoded = base64.standard_b64encode(data).decode()
    return {'type': 'image_url', 'image_url': {
        'url': f'data:{mime};base64,{encoded}', 'detail': 'original'}}


# ------------------------------- Диспетчер ------------------------------------

@timed_call("llm")
@guarded_llm
def complete_json(
    prompt: str,
    *,
    system: str = "",
    smart: bool = False,
    max_tokens: int = 4096,
    images: list[str] | None = None,
    timeout_seconds: float = MODEL_OPERATION_TIMEOUT_SECONDS,
    response_schema: dict | None = None,
) -> Any:
    """Запрос к LLM с ожиданием JSON-ответа. Возвращает распарсенный объект.

    smart=True → более сильная модель (matching, сложные случаи); иначе быстрая/дешёвая.
    images — локальные изображения для Anthropic или DeepSeek vision.
    """
    from .search.session import bounded_timeout
    timeout_seconds = bounded_timeout(timeout_seconds, minimum=min(5, timeout_seconds))
    if settings.llm_provider.lower() == "cloudru":
        from .cloudru import complete_json as cloudru_json
        return cloudru_json(prompt, system, smart, max_tokens, images, timeout_seconds,
                            response_schema=response_schema)
    if settings.llm_provider.lower() == "deepseek":
        return _complete_deepseek(
            prompt, system, smart, max_tokens, images, timeout_seconds
        )
    return _complete_anthropic(prompt, system, smart, max_tokens, images, timeout_seconds)
