"""Distinct compatible model attempts, one shared operation and no answer shopping."""
from copy import deepcopy
import json,time

TEXT_BACKUPS=frozenset({'deepseek-ai/DeepSeek-V4-Pro','deepseek-ai/DeepSeek-V4-Flash'})
VISION_BACKUPS=frozenset({'Qwen/Qwen3.6-35B-A3B','qwen/qwen3-vl-30b-a3b-instruct'})

class ModelDict(dict):
    """Parsed JSON keys stay unchanged; trusted transport metadata is out of band."""
class ModelList(list):pass

def candidates(primary,configured,kind):
    from .llm import LLMUnavailable
    if not isinstance(primary,str) or not primary or len(primary)>160:raise LLMUnavailable('Cloud.ru: неверная конфигурация модели')
    if configured is None:return (primary,)
    if not isinstance(configured,str) or len(configured)>1024:raise LLMUnavailable('Cloud.ru: неверный список резервных моделей')
    values=[v.strip() for v in configured.split(',') if v.strip()]
    allowed=VISION_BACKUPS if kind=='vision' else TEXT_BACKUPS
    if any(v not in allowed for v in values):raise LLMUnavailable('Cloud.ru: резервная модель не подтверждена для типа входных данных')
    reserve=[v for v in dict.fromkeys(values) if v!=primary]
    if len(reserve)>1:raise LLMUnavailable('Cloud.ru: допускается только одна отдельная резервная модель')
    return tuple([primary]+reserve)

def payload_for(payload,model,kind):
    result=deepcopy(payload);result['model']=model
    if kind is not None:
        result.pop('thinking',None);result.pop('top_p',None);result.pop('chat_template_kwargs',None);result['temperature']=0
        if kind=='text_fast' and model.casefold()=='deepseek-ai/deepseek-v4-flash':result['thinking']={'type':'disabled'}
        if kind=='vision' and model.casefold() in ('qwen/qwen3-vl-30b-a3b-instruct','qwen/qwen3.6-35b-a3b'):result.update(temperature=.7,top_p=.8)
        if kind=='vision' and model.casefold()=='qwen/qwen3.6-35b-a3b':result['chat_template_kwargs']={'enable_thinking':False}
    return result

def run(payload,models,kind,timeout_seconds,operation):
    from .llm import LLMOperationFailed
    from .search.session import bounded_timeout
    from .telemetry import record_stage
    attempts=[]
    for index,model in enumerate(models):
        remaining=bounded_timeout(timeout_seconds)
        allocation=remaining/(len(models)-index)
        started=time.monotonic()
        try:result=operation(payload_for(payload,model,kind),allocation)
        except LLMOperationFailed as error:
            # Only missing-response transport or explicit transient HTTP status.
            transient=(error.cause=='transport_error' or error.cause=='response_error' and (error.http_status==408 or isinstance(error.http_status,int) and 500<=error.http_status<=599))
            wire=getattr(error,'provider_attempts',())
            row={'model':model,'outcome':'failed','cause':error.cause,'http_status':error.http_status,'elapsed_seconds':round(time.monotonic()-started,6),'requested_timeout_seconds':allocation,'after_send_uncertain':any(a.endpoint_send_started and a.outcome=='error' for a in wire)}
            attempts.append(row);error.cloudru_model_attempts=tuple(attempts)
            record_stage('cloudru_model_attempt',row['elapsed_seconds'],ok=False,label=json.dumps(row,sort_keys=True))
            bounded_timeout(1)  # Owned cancellation/deadline/account first-stop remains authoritative.
            if not transient or index+1==len(models):raise
        else:
            bounded_timeout(1)
            row={'model':model,'outcome':'response','elapsed_seconds':round(time.monotonic()-started,6),'requested_timeout_seconds':allocation}
            attempts.append(row);record_stage('cloudru_model_attempt',row['elapsed_seconds'],label=json.dumps(row,sort_keys=True))
            result.model_id=model;result.model_attempts=tuple(attempts)
            return result
    raise AssertionError('Empty Cloud.ru model selection')

def parsed_metadata(value,text):
    if isinstance(value,dict):value=ModelDict(value)
    elif isinstance(value,list):value=ModelList(value)
    else:return value
    value.model_metadata={'model':text.model_id,'attempts':list(text.model_attempts)}
    return value
