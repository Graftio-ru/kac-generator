"""Cloud.ru transport and bounded compatible-model failover."""
from __future__ import annotations

import httpx

from .config import settings
from .llm import (LLMAccountError, LLMUnavailable, LLMOperationFailed, provider_http_failure, provider_usage, ModelOutputInvalid, _completion_text, _parse_model_json, _REQUEST_GUARD,
                  _deepseek_image_block, _extract_json, guarded_llm,
                  model_transport_timeout, MODEL_OPERATION_TIMEOUT_SECONDS)
from .provider_transport import provider_post
from .telemetry import record_llm_usage, timed_call, record_stage
import json


def response_diagnostic(data):
    """Bounded structure only; never retain any completion or reasoning text."""
    def typename(value):
        return ('missing' if value is missing else 'null' if value is None else
                'string' if type(value) is str else 'list' if type(value) is list else
                'object' if type(value) is dict else 'number' if type(value) in (int,float) else
                'boolean' if type(value) is bool else 'other')
    missing=object()
    choices=data.get('choices',missing) if type(data) is dict else missing
    choice=choices[0] if type(choices) is list and choices else missing
    message=choice.get('message',missing) if type(choice) is dict else missing
    content=message.get('content',missing) if type(message) is dict else missing
    reasoning=message.get('reasoning_content',missing) if type(message) is dict else missing
    finish=choice.get('finish_reason',missing) if type(choice) is dict else missing
    return {'envelope_type':typename(data),'choices_type':typename(choices),
            'choice_type':typename(choice),'message_type':typename(message),
            'content_type':typename(content),'content_length':len(content) if type(content) in (str,list) else None,
            'reasoning_content_type':typename(reasoning),'reasoning_content_length':len(reasoning) if type(reasoning) is str else None,
            'finish_reason':finish if type(finish) is str and finish in ('stop','length','tool_calls','content_filter','function_call') else typename(finish)}

def _request_one(payload, timeout_seconds, *, output_kind='json'):
    guard = _REQUEST_GUARD.get()
    if guard is not None:
        guard.check()
    if not settings.cloudru_api_key:
        raise LLMUnavailable('CLOUDRU_API_KEY не задан (.env)')
    url = settings.cloudru_base_url.rstrip('/') + '/chat/completions'
    try:
        response = provider_post(url,provider='cloudru',
            headers={'Authorization': f'Bearer {settings.cloudru_api_key}',
                     'Content-Type':'application/json'},json=payload,
            timeout=model_transport_timeout(timeout_seconds),
            proxy=settings.cloudru_proxy_server.strip() or None)
    except httpx.RequestError as error:
        from .search.session import bounded_timeout
        bounded_timeout(1)
        failure=LLMOperationFailed('transport_error')
        if hasattr(error,'provider_attempts'):failure.provider_attempts=error.provider_attempts
        raise failure from None
    if response.status_code >= 400:
        from .search.session import bounded_timeout
        bounded_timeout(1)
        failure=provider_http_failure(response.status_code,'Cloud.ru')
        if hasattr(response,'provider_attempts'):failure.provider_attempts=response.provider_attempts
        raise failure
    try:
        data = response.json()
    except ValueError:
        from .search.session import bounded_timeout
        bounded_timeout(1)
        raise LLMOperationFailed('invalid_response_envelope') from None
    diagnostic=response_diagnostic(data)
    record_stage('cloudru_response_envelope',0,label=json.dumps(diagnostic,sort_keys=True))
    if not isinstance(data,dict):
        from .search.session import bounded_timeout
        bounded_timeout(1)
        failure=LLMOperationFailed('invalid_response_envelope');failure.cloudru_response_diagnostic=diagnostic
        raise failure
    record_llm_usage('cloudru', payload['model'], provider_usage(data.get('usage')))
    from .search.session import bounded_timeout
    bounded_timeout(1)  # Usage survives late-response cancellation; controls remain strict.
    try:
        result=_completion_text(data,provider='cloudru',output_kind=output_kind)
    except (LLMOperationFailed,ModelOutputInvalid) as error:
        error.cloudru_response_diagnostic=diagnostic
        raise
    result.cloudru_response_diagnostic=diagnostic
    return result


@guarded_llm
def _request(payload,timeout_seconds,*,output_kind='json',compatible_models=None,model_kind=None):
    from .cloudru_failover import candidates,run
    models=candidates(payload['model'],compatible_models,model_kind)
    return run(payload,models,model_kind,timeout_seconds,
               lambda body,budget:_request_one(body,budget,output_kind=output_kind))


@guarded_llm
def complete_json(prompt, system, smart, max_tokens, images, timeout_seconds,
                  response_schema=None):
    system = system or 'Ты помощник сметчика. Отвечай только валидным JSON.'
    if 'json' not in (system + prompt).lower():
        system += ' Ответ верни в формате JSON.'
    blocks = [{'type': 'text', 'text': prompt}]
    for path in images or []:
        block = _deepseek_image_block(path)
        block['image_url'].pop('detail', None)
        blocks.append(block)
    model = (settings.cloudru_model_vision if images else
             settings.cloudru_model_smart if smart else settings.cloudru_model_fast)
    # Extraction is a fast JSON task. The live independent text request
    # exceeded 120s without an explicit thinking toggle; disabled completed.
    fast_deepseek = (not images and not smart
                    and model.casefold() == 'deepseek-ai/deepseek-v4-flash')
    # Qwen's published generation configuration uses sampling. Greedy JSON
    # decoding looped into invented metadata on the live specification pages.
    qwen_vision = (bool(images)
                   and model.casefold() == 'qwen/qwen3-vl-30b-a3b-instruct')
    content = _request({
        'model': model,
        **({'thinking': {'type': 'disabled'}} if fast_deepseek else {}),
        'max_tokens': max_tokens,
        **({'temperature': 0.7, 'top_p': 0.8} if qwen_vision else {'temperature': 0}),
        'response_format': ({'type': 'json_schema', 'json_schema': {
            'name': 'structured_result', 'strict': True, 'schema': response_schema}}
            if response_schema is not None else {'type': 'json_object'}),
        'messages': [{'role': 'system', 'content': system},
                     {'role': 'user', 'content': blocks if images else prompt}],
    }, timeout_seconds,compatible_models=(settings.cloudru_vision_fallback_models if images else settings.cloudru_text_fallback_models),model_kind='vision' if images else 'text_smart' if smart else 'text_fast')
    from .cloudru_failover import parsed_metadata
    return parsed_metadata(_parse_model_json(content,provider='cloudru'),content)


@timed_call('llm')
@guarded_llm
def recognize_page(path, timeout_seconds=MODEL_OPERATION_TIMEOUT_SECONDS):
    """One original page, using the model-specific prompt shown by Cloud.ru."""
    from .search.session import bounded_timeout
    guard = _REQUEST_GUARD.get()
    if guard is not None:
        guard.check()
    if not settings.cloudru_api_key:
        raise LLMUnavailable('CLOUDRU_API_KEY не задан (.env)')
    image = _deepseek_image_block(path)
    image['image_url'].pop('detail', None)
    return _request({
            'model': settings.cloudru_model_ocr, 'max_tokens': 4096,
            'temperature': 0,
            'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': '<image><|grounding|>Convert the document to markdown.'},
                image]}],
    }, bounded_timeout(timeout_seconds), output_kind='ocr')
