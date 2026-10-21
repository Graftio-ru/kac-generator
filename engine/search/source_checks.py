"""Explicit position-by-supplier accounting; empty search is not stock absence."""
from urllib.parse import urlparse
from ..offer_selection import offer_is_comparable



def position_binding(position):
    import hashlib,json
    field=lambda k,default='':position.get(k,default) if isinstance(position,dict) else getattr(position,k,default)
    return hashlib.sha256(json.dumps([field('number',0),field('name'),field('type_mark'),field('unit'),float(field('qty',0)),field('discipline')],ensure_ascii=False,allow_nan=False).encode()).hexdigest()

def quote_proof(offer,position):
    from . import base_db
    from ..models import PriceOffer,SpecPosition
    if isinstance(offer,dict):offer=PriceOffer(**offer)
    if isinstance(position,dict):position=SpecPosition(**{k:position.get(k,0 if k in {'number','qty'} else '') for k in ('number','name','type_mark','unit','qty','discipline')})
    import hashlib
    return hashlib.sha256(base_db._observation_fingerprint(offer,position).encode()).hexdigest()

def producer_receipt(offer,position,*,job_id,host,started_at,validated_at):
    """Called only after actual callback attestation matches final projection."""
    from . import base_db
    return dict(version=1,origin='server_card_producer',job_id=job_id,host=host,url=offer.url,
        position_binding=position_binding(position),proof=quote_proof(offer,position),
        observed_at=base_db._capture_time(offer),operation_started_at=started_at,validated_at=validated_at)

def valid_quote_receipt(receipt,*,position=None,job_id=None,host=None):
    import math,re,time
    keys={'version','origin','job_id','host','url','position_binding','proof','observed_at','operation_started_at','validated_at'}
    if type(receipt) is not dict or set(receipt)!=keys or type(receipt['version']) is not int or receipt['version']!=1 or receipt['origin']!='server_card_producer':return False
    if not all(type(receipt[k]) is str and re.fullmatch('[0-9a-f]{64}',receipt[k]) for k in ('position_binding','proof')):return False
    if type(receipt['job_id']) is not str or not re.fullmatch('[0-9a-f]{12}',receipt['job_id']):return False
    from .product_identity import canonical_card_url,card_host
    if not canonical_card_url(receipt['url']) or type(receipt['host']) is not str or not (card_host(receipt['url'])==receipt['host'] or card_host(receipt['url']).endswith('.'+receipt['host'])):return False
    if any(type(receipt[k]) not in (int,float) or not math.isfinite(receipt[k]) or not 0<receipt[k]<=time.time()+300 for k in ('observed_at','operation_started_at','validated_at')):return False
    if receipt['validated_at']<receipt['operation_started_at']:return False
    try:
        return (position is None or receipt['position_binding']==position_binding(position)) and (job_id is None or receipt['job_id']==job_id) and (host is None or receipt['host']==host)
    except (ValueError,TypeError,OverflowError):return False

def matching_quote_receipt(receipt,offers,position,*,job_id=None,host=None):
    if not valid_quote_receipt(receipt,position=position,job_id=job_id,host=host):return False
    for offer in offers:
        try:
            url=offer.get('url') if isinstance(offer,dict) else offer.url
            if url==receipt['url'] and offer_is_comparable(offer,position) and quote_proof(offer,position)==receipt['proof']:return True
        except (ValueError,TypeError):continue
    return False

def current_quote_finished(check,*,position=None,job_id=None,offers=None):
    if check.get('status')!='found' or check.get('coverage_obligation')!='current_quote_confirmed':return False
    receipt=check.get('current_quote_receipt')
    if not valid_quote_receipt(receipt,position=position,job_id=job_id,host=check.get('host')):return False
    if type(check.get('checked_at')) not in (int,float) or check['checked_at']!=receipt['observed_at']:return False
    return offers is None or matching_quote_receipt(receipt,offers,position,job_id=job_id,host=check.get('host'))

def processed_review(check):
    """A bounded search settled for review; this is not complete discovery or stock proof."""
    return (check.get('status') == 'review' and check.get('processing_exhausted') is True
            and check.get('coverage_obligation') == 'individual_exhausted'
            and check.get('discovery_complete') is False and check.get('product_presence') == 'unknown'
            and check.get('individual_success') is True
            and type(check.get('individual_queries')) is int and check['individual_queries'] > 0
            and check.get('individual_window') in {'unknown','truncated_possible'}
            and not check.get('errors') and not check.get('card_errors'))


def source_checks(hosts, attempts, row, *, terminal=False, stopped=False, processing_exhausted=False):
    result=[]
    for host in dict.fromkeys(hosts):
        def belongs(url):
            try:
                name=(urlparse(url or '').hostname or '').lower().removeprefix('www.')
            except ValueError:
                return False
            return name == host or name.endswith('.'+host)
        counts=attempts.get(host,{})
        queries=counts.get('queries',0)
        errors=counts.get('errors',0)
        cards=counts.get('cards',0)
        offers=[o for o in row.get('candidates',[]) if belongs(o.get('url'))]
        issues=[d.get('reason','') for d in row.get('diagnostics',[]) if belongs(d.get('url'))]
        context = next((d for d in reversed(row.get('diagnostics',[]))
                        if belongs(d.get('url')) and d.get('failure_source')), None)
        context_fields = ({k:v for k,v in context.items() if k.startswith('failure_')
                          or k.startswith('search_') or k in {'http_status','observed_at','transport_error_code'}} if context else {})
        grouped = bool(counts.get('grouped_queries'))
        group_card_confirmed = (counts.get('group_success') and counts.get('group_window') == 'below_limit'
                                and counts.get('identity_confirmed') and not counts.get('individual_queries'))
        individual_confirmed = (counts.get('individual_success')
                                and counts.get('individual_window') == 'below_limit')
        discovery_complete = bool(group_card_confirmed or individual_confirmed)
        current=matching_quote_receipt(counts.get('current_quote_receipt'),offers,row,host=host)
        blocking_codes = {'search_budget','discovery_deferred','job_budget','job_cancelled',
                          'search_capacity_deferred','internal_error','search_error'}
        blockers = any((d.get('failure_scope') == 'global' or d.get('code') in blocking_codes
                       or (d.get('failure_source') in {'supplier_http','supplier_navigation'}
                           and d.get('failure_cause') in {'network_timeout','network_error'}
                           and (not d.get('url') or belongs(d.get('url')))))
                       for d in row.get('diagnostics', []))
        exhausted_review = bool(terminal and processing_exhausted and not stopped and not blockers
            and counts.get('individual_success') is True and type(counts.get('individual_queries')) is int
            and counts['individual_queries'] > 0
            and counts.get('individual_window') in {'unknown','truncated_possible'}
            and not counts.get('errors') and not counts.get('card_errors'))
        if row.get('status')=='skipped':
            status,reason='skipped',row.get('reason','Поиск пропущен')
        elif current:
            status,reason='found','Текущая карточка проверена; допустимое предложение подтверждено, полнота каталога неизвестна'
        elif counts.get('deferred_check'):
            check = counts['deferred_check']
            from datetime import datetime, timezone
            def stamp(value):
                return datetime.fromtimestamp(value, timezone.utc).isoformat() if isinstance(value,(int,float)) else 'не проверено'
            reason = (check['reason'] + '; дата проверки: ' + stamp(check.get('checked_at'))
                      + '; следующая попытка: ' + stamp(check.get('retry_at')))
            result.append({**check, **context_fields, 'host':host, 'reason':reason, 'queries':queries,
                           'cards':cards, 'errors':errors, 'retry_after':check.get('retry_at'),
                           'product_presence':'unknown'})
            continue
        elif counts.get('cached_check') and (counts['cached_check'].get('coverage_obligation')!='current_quote_confirmed' or current_quote_finished(counts['cached_check'],position=row,offers=offers)):
            cached=counts['cached_check']
            result.append({**cached,'host':host,'queries':0,'cards':0,'reused':True,
                'reason':cached['reason']+'; использован результат предыдущей проверки'})
            continue
        elif not terminal and not stopped:
            status,reason=('running','Проверка выполняется') if queries or cards else ('pending','Ожидает проверки')
        elif not any(offer_is_comparable(o) for o in offers) and not counts.get('identity_confirmed') and any(d.get('code')=='search_capacity_deferred' for d in row.get('diagnostics',[])):
            status,reason='incomplete','Поисковый API временно занят; поиск отложен, наличие товара неизвестно'
        elif not queries and not cards:
            status,reason='incomplete','Поисковый запрос к этому сайту не выполнен'
        elif exhausted_review and not discovery_complete:
            status,reason='review','Все запланированные запросы и карточки обработаны; выдача ограничена или её полнота неизвестна. Наличие товара не подтверждено; сверка ресурса требует проверки.'
        elif grouped and not discovery_complete:
            status,reason='incomplete','Групповая выдача не подтверждает полноту проверки отдельного сайта; индивидуальный поиск не завершён или ограничен'
            if issues:reason += '; '+ '; '.join(dict.fromkeys(issues))
        elif counts.get('individual_success') and counts.get('individual_window') != 'below_limit':
            status,reason='incomplete','Полнота индивидуальной выдачи неизвестна или выдача ограничена; наличие товара неизвестно'
        elif any(offer_is_comparable(o) for o in offers):
            status,reason='found','Найдены сопоставимые предложения'
            if issues:
                reason += '; есть ошибки проверки: '+ '; '.join(dict.fromkeys(issues))
        elif offers:
            status,reason='review','Найденные цены требуют проверки'
            if any(o.get('refresh_failed') for o in offers):
                reason += '; прежняя цена не подтверждена при обновлении и не выбрана автоматически'
            if issues:
                reason += '; '+ '; '.join(dict.fromkeys(issues))
        elif any((d.get('code')=='search_query_failed' or d.get('search_response_failures')) and belongs(d.get('url')) for d in row.get('diagnostics',[])):
            status,reason='review','Поисковый запрос не завершён; наличие товара неизвестно'
            if issues:reason += '; '+ '; '.join(dict.fromkeys(issues))
        elif any(d.get('code')=='llm_operation_failed' and belongs(d.get('url')) for d in row.get('diagnostics',[])):
            status,reason='review','Ответ API модели не подтверждён; проверка карточки не завершена'
            if issues:reason += '; '+ '; '.join(dict.fromkeys(issues))
        elif any(d.get('code')=='model_output_invalid' and belongs(d.get('url')) for d in row.get('diagnostics',[])):
            status,reason='review','Ответ модели требует проверки; сопоставимая цена не подтверждена'
            if issues:reason += '; '+ '; '.join(dict.fromkeys(issues))
        elif any(d.get('code')=='internal_error' and belongs(d.get('url')) for d in row.get('diagnostics',[])):
            status,reason='error','Внутренняя ошибка обработки; наличие товара неизвестно'
            if issues:reason += '; '+ '; '.join(dict.fromkeys(issues))
        elif counts.get('identity_confirmed'):
            status,reason='review','Карточка товара подтверждена; сопоставимая цена не подтверждена'
            if issues:
                reason += '; '+ '; '.join(dict.fromkeys(issues))
        elif stopped and not counts.get('completed'):
            status,reason='incomplete','Проверка не завершена: задание остановлено'
        elif errors or issues:
            status,reason='error','; '.join(dict.fromkeys(issues)) or 'Ошибка поискового запроса'
        else:
            status,reason='no_match','Поиск выполнен; подходящая карточка не найдена. Это не подтверждает отсутствие товара.'
        check = {'host':host,'status':status,'reason':reason,
                       'queries':queries,'errors':errors,'cards':cards,
                       'card_errors':counts.get('card_errors',0), 'product_presence':'unknown'}
        check.update(context_fields)
        if current:
            check.update(current_quote_receipt=counts['current_quote_receipt'],discovery_complete=discovery_complete,coverage_obligation='current_quote_confirmed',checked_at=counts['current_quote_receipt']['observed_at'])
        if not current and (grouped or counts.get('individual_queries')):
            check['discovery_complete'] = discovery_complete
            check['coverage_obligation'] = ('individual_confirmed' if individual_confirmed else
                'group_card_confirmed' if group_card_confirmed else 'individual_pending')
        if exhausted_review and not discovery_complete and status == 'review':
            check['processing_exhausted'] = True
            check['coverage_obligation'] = 'individual_exhausted'
        for key in ('grouped_queries','individual_queries','group_wire_inclusions','search_wire_calls',
                    'group_success','group_window','group_represented','individual_success',
                    'individual_window','individual_candidates','identity_confirmed','search_reused','own_search_positive','serp_fallback_attempted'):
            if key in counts: check[key] = counts[key]
        if not current and counts.get('checked_at'):
            check['checked_at'] = counts['checked_at']
        if counts.get('empty_search_queries'):
            check['empty_search_queries'] = counts['empty_search_queries']
            check['empty_search_observed_at'] = counts['empty_search_observed_at']
            check['empty_search_xml_code'] = '15'
        if terminal and not current and (queries or cards):
            import time
            check['checked_at'] = counts.get('checked_at',time.time())
            ttl = {'no_match':86400,'error':900}.get(status)
            if ttl:
                check['ttl_seconds'] = ttl
                check['expires_at'] = check['checked_at']+ttl
                from datetime import datetime, timezone
                check['reason'] += '; дата проверки: '+datetime.fromtimestamp(check['checked_at'],timezone.utc).isoformat()
        result.append(check)
    return result
