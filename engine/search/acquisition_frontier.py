"""Private original-acquisition checkpoints. These records never authorize a quote.

Remote delivery cannot be rolled back: a running operation without a committed
response remains uncertain after a crash. Only explicit server recovery may retry it.
"""
from __future__ import annotations
import hashlib
import json
import os
import math
from pathlib import Path
import re
import uuid
import tempfile
from urllib.parse import parse_qsl, urlsplit

# NEW outcome diagnostics only. These codes never make an uncertain card definitive.
UNCERTAIN_CARD_CODES = frozenset("""card_no_observation card_source_reason_unknown
card_link_parent_unsettled card_operation_budget card_control_review
llm_operation_failed catalogue_card_validation_failed
catalogue_seller_binding_invalid catalogue_operation_id_invalid catalogue_snapshot_changed
catalogue_owner_unconfirmed catalogue_article_conflict catalogue_generation_mismatch
catalogue_source_binding_invalid catalogue_source_amount_unobserved
catalogue_purchase_price_conflict catalogue_purchase_unit_conflict
empty_page captcha price_on_request out_of_stock minimum_price minimum_order
article_mismatch product_mismatch product_unconfirmed brand_unconfirmed
native_spec_unconfirmed seller_unconfirmed price_or_unit_unconfirmed
redirect_unconfirmed raw_document_invalid source_generation_changed extract_failed
card_not_found not_direct_supplier listing profile_busy site_error
http_primary_identity_conflict http_primary_model_conflict http_primary_manufacturer_conflict
supplier_card_http_error supplier_card_navigation_error supplier_card_timeout
supplier_card_transport_error supplier_card_network_timeout supplier_card_network_error
supplier_card_http_forbidden supplier_card_http_rate_limited supplier_card_http_unavailable
supplier_card_bot_protection""".split())
_UNCERTAIN_STAGES=frozenset(('capture_operation','source_decision','outcome_recovery','outcome_write'))
_UNCERTAIN_ERRORS=frozenset(('CataloguePublicationError','LLMOperationFailed','SearchBudgetExceeded',
    'ResearchControlError','ResearchProgressError','ValueError','Error','TimeoutError',
    'ReadTimeout','ConnectTimeout','ReadError','ConnectError','HTTPStatusError','RequestError'))

def uncertain_card_completion(code='card_source_reason_unknown',*,stage='outcome_write',error_type=None):
    """Fixed enums, never exception text, arbitrary diagnostics or copied URL context."""
    if type(code) is not str or len(code)>128 or code not in UNCERTAIN_CARD_CODES:code='card_source_reason_unknown'
    if type(stage) is not str or stage not in _UNCERTAIN_STAGES:stage='outcome_write'
    evidence=dict(role='card_uncertainty',code=code,stage=stage)
    if error_type is not None:
        evidence['error_type']=error_type if type(error_type) is str and error_type in _UNCERTAIN_ERRORS else 'unclassified_error'
    return dict(version=1,disposition='uncertain',evidence=evidence)

class FrontierInvalid(ValueError):
    pass

def _fail():
    raise FrontierInvalid('acquisition_frontier_invalid')

def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)

def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()

def _text(value,limit=2048):
    if type(value) is not str or not value or len(value)>limit or any(ord(c)<32 for c in value):_fail()
    return value

def _hex(value,size=64):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{%d}'%size,value):_fail()
    return value

def _int(value,minimum=0):
    if type(value) is not int or value<minimum:_fail()
    return value

def _keys(value,keys):
    if type(value) is not dict or set(value)!=set(keys.split()):_fail()

def _host(value):
    _text(value,253)
    if value!=value.lower().removeprefix('www.') or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?',value):_fail()
    return value

def _url(value):
    _text(value,8192);parts=urlsplit(value)
    if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password or parts.fragment:_fail()
    if any(re.search(r'token|secret|password|api.?key|credential|signature',key,re.I) for key,_ in parse_qsl(parts.query)):_fail()
    return value

def _belongs(url,host):
    actual=urlsplit(url).hostname.lower().removeprefix('www.')
    return actual==host or actual.endswith('.'+host)

def identity_digest(position):
    from .catalogue import identity_for_position
    return identity_for_position(position).key

_FIELDS='version job_id input_sha256 representative_index product_id requested_identity_sha256 configured_hosts planner_version route_intent_sha256 acquisition_generation acquisition_owner_job_id revision planning_phase frontier_complete queries'
_QUERY='id phase query hosts strategy state operation_id response cursor current_card'

def candidate_count(task):
    return (task['response']['count'] if task['response'] else 0)+sum(ref['count'] for ref in task.get('card_links',[]))


def record_binding(frontier):
    names=('job_id','product_id','input_sha256','requested_identity_sha256','route_intent_sha256','configured_hosts')
    if frontier['version']==5:names+=('record_origin_generation','planner_version')
    return {k:json.loads(_json(frontier[k])) for k in names}


def original_search_route(frontiers):
    """Resolve one immutable route shared by all original product reservations."""
    from .search_route import validate_route
    from .failure_policy import SearchServiceUnavailable
    routes=[]
    for value in frontiers:
        route=value.get('search_route')
        try:
            routes.append(validate_route(route) if route is not None else None)
        except SearchServiceUnavailable:
            raise FrontierInvalid('acquisition_search_route_invalid') from None
    if not routes:return None
    if any(route!=routes[0] for route in routes):
        raise FrontierInvalid('acquisition_search_route_changed')
    return routes[0]

def validate_frontier(value,*,job_id,input_sha256,positions,configured_hosts,index,expected_product_id=None,expected_generation=None):
    """Validate private server metadata against immutable original job inputs.

    Product IDs require a separate catalogue ownership check on dispatch; neither
    this validator nor a response checkpoint is financial/source authority.
    """
    if type(value) is not dict or type(value.get('version')) is not int or value['version'] not in (1,2,3,4,5):_fail()
    _keys(value,_FIELDS+(' record_origin_generation' if value['version']==5 else '')+(' search_route source_intent' if value['version'] in (3,4,5) else ' search_route' if value['version']==2 else ''))
    if value['version'] in (3,4,5):
        from .acquisition_source_intent import validate
        try:validate(value['source_intent'])
        except ValueError:_fail()
    if value['version']==2 or value['version'] in (3,4,5) and value.get('search_route') is not None:
        from .search_route import validate_route
        from .failure_policy import SearchServiceUnavailable
        try:
            if validate_route(value['search_route'])!=value['search_route']:_fail()
        except (ValueError,TypeError,SearchServiceUnavailable):_fail()
    if value['job_id']!=job_id or value['input_sha256']!=input_sha256 or value['representative_index']!=index:_fail()
    _hex(job_id,12);_hex(input_sha256);_int(index);_int(value['representative_index'])
    if index>=len(positions) or value['requested_identity_sha256']!=identity_digest(positions[index]):_fail()
    _hex(value['product_id']);_hex(value['requested_identity_sha256'])
    if expected_product_id is not None and value['product_id']!=expected_product_id:_fail()
    if type(value['configured_hosts']) is not list or value['configured_hosts']!=list(configured_hosts):_fail()
    hosts=[_host(h) for h in configured_hosts]
    if not hosts or len(hosts)>256 or len(set(hosts))!=len(hosts):_fail()
    _text(value['planner_version'],128);
    if value['version']==5 and value['planner_version']!='native-acquisition-indexed-v5':_fail()
    _hex(value['route_intent_sha256']);_int(value['revision'])
    if value['planning_phase'] not in ('initial','fallback','literal','complete') or type(value['frontier_complete']) is not bool or value['frontier_complete']!=(value['planning_phase']=='complete'):_fail()
    _int(value['acquisition_generation'],1)
    if value['version']==5 and _int(value['record_origin_generation'],1)>value['acquisition_generation']:_fail()
    if value['acquisition_owner_job_id']!=job_id:_fail()
    if expected_generation is not None and value['acquisition_generation']!=expected_generation:_fail()
    if type(value['queries']) is not list or len(value['queries'])>((2 if value['version']==5 else 1)*3*(((len(hosts)+7)//8)+3*len(hosts))):_fail()
    from .catalogue import identity_for_position
    from .article_query_plan import build_article_query_plan
    from .manufacturer_aliases import names_for
    position=positions[index];field=lambda k:position.get(k,'') if isinstance(position,dict) else getattr(position,k,'')
    identity=identity_for_position(position);allowed=set()
    for maker in set((identity.manufacturer,)+names_for(identity.manufacturer)):
        allowed.update(build_article_query_plan(field('name'),field('type_mark'),article=identity.code if identity.kind=='article' else '',manufacturer=maker,unit=field('unit')).queries)
    ids=set();admitted=set()
    for task in value['queries']:
        _keys(task,_QUERY+(' settled_cards deferred_cards' if value['version'] in (4,5) else '')+(' card_links' if 'card_links' in task else ''));_hex(task['id'])
        if 'card_links' in task:
            if value['version']!=5 or value.get('source_intent',{}).get('version')!='acquisition-source-intent-v7' or type(task['card_links']) is not list:_fail()
            count=task['response']['count'] if task.get('response') else 0;parents=set();linked=0
            for ref in task['card_links']:
                _keys(ref,'ref sha256 count parent_index parent_operation_id');_hex(ref['sha256']);_hex(ref['parent_operation_id'],32);_int(ref['parent_index']);_int(ref['count'])
                if not re.fullmatch(r'[0-9a-f]{64}\.json',ref['ref']) or ref['parent_index']>=count or ref['parent_index'] in parents:_fail()
                parents.add(ref['parent_index']);count+=ref['count'];linked+=ref['count']
            if linked>10:_fail()
        if task['id'] in ids:_fail()
        ids.add(task['id'])
        if task['phase'] not in ('group','individual','fallback','literal') or task['strategy'] not in ('own_or_serp','serp_only','own_only'):_fail()
        _text(task['query'],4096)
        if task['query'] not in allowed:_fail()
        if type(task['hosts']) is not list or not task['hosts'] or len(set(task['hosts']))!=len(task['hosts']) or any(h not in hosts for h in task['hosts']):_fail()
        for host in task['hosts']:
            key=(task['strategy'],task['phase']=='group',host,task['query'].casefold())
            if key in admitted:_fail()
            admitted.add(key)
        if task['id']!=_hash({k:task[k] for k in ('phase','query','hosts','strategy')}):_fail()
        if task['state'] not in ('pending','running','received','done'):_fail()
        operation=task['operation_id'];response=task['response'];cursor=_int(task['cursor']);card=task['current_card']
        if operation is not None:_hex(operation,32)
        if response is not None:
            _keys(response,'ref sha256 count');_hex(response['sha256']);_int(response['count'])
            if not re.fullmatch(r'[0-9a-f]{64}\.json',response['ref']):_fail()
            if cursor>candidate_count(task):_fail()
        if task['state']=='pending' and (operation is not None or response is not None or cursor or card is not None):_fail()
        if task['state']=='running' and (operation is None or response is not None or cursor or card is not None):_fail()
        if task['state'] in ('received','done') and (operation is None or response is None):_fail()
        if task['state']=='done' and (cursor!=candidate_count(task) or card is not None):_fail()
        if card is not None:
            _keys(card,'url host operation_id'+(' index' if value['version'] in (4,5) else ''));_url(card['url']);_hex(card['operation_id'],32)
            offset=_int(card['index']) if value['version'] in (4,5) else cursor
            binding=record_binding(value)
            expected_operation=_hash(dict(binding=binding,task_id=task['id'],index=offset,url=card['url'],host=card['host']))[:32]
            if card['operation_id']!=expected_operation:_fail()
            if task['state']!='received' or offset>=candidate_count(task) or offset<cursor or card['host'] not in task['hosts'] or not _belongs(card['url'],card['host']):_fail()
        if value['version'] in (4,5):
            for name in ('settled_cards','deferred_cards'):
                if type(task[name]) is not dict:_fail()
                for offset,entry in task[name].items():
                    if type(offset) is not str or not re.fullmatch('0|[1-9][0-9]*',offset):_fail()
                    if response is None or not cursor<=int(offset)<candidate_count(task):_fail()
                    if name=='settled_cards':
                        _keys(entry,'ref sha256');_hex(entry['sha256'])
                        if not re.fullmatch(r'[a-f0-9]{64}\.json',entry['ref']):_fail()
                    else:
                        from .catalogue import safe_deferred_check
                        try:safe_deferred_check(entry,entry.get('host'))
                        except (ValueError,TypeError,AttributeError):_fail()
            if set(task['settled_cards'])&set(task['deferred_cards']):_fail()
            if card is not None and str(card['index']) in task['settled_cards']:_fail()
            if task['state']=='done' and (task['settled_cards'] or task['deferred_cards']):_fail()
    if value['frontier_complete'] and (not value['queries'] or any(t['state']!='done' for t in value['queries'])):_fail()
    # Roundtrip detaches caller-owned dictionaries and rejects unsupported JSON.
    try:return json.loads(_json(value))
    except (TypeError,ValueError):_fail()

def new_frontier(*,job_id,input_sha256,positions,configured_hosts,index,product_id,route_intent_sha256,acquisition_generation,planner_version='native-acquisition-v1',search_route=None,source_intent=None,indexed=False):
    value=dict(version=1,job_id=job_id,input_sha256=input_sha256,representative_index=index,product_id=product_id,
        requested_identity_sha256=identity_digest(positions[index]),configured_hosts=list(configured_hosts),
        planner_version=planner_version,route_intent_sha256=route_intent_sha256,acquisition_generation=acquisition_generation,acquisition_owner_job_id=job_id,revision=0,planning_phase='initial',frontier_complete=False,queries=[])
    if search_route is not None:
        from .search_route import validate_route
        value.update(version=2,search_route=validate_route(search_route))
    if source_intent is not None:
        from .acquisition_source_intent import validate
        value.update(version=3,search_route=search_route,source_intent=validate(source_intent))
    if indexed:
        if source_intent is None:_fail()
        value.update(version=5,planner_version='native-acquisition-indexed-v5',record_origin_generation=acquisition_generation)
    return validate_frontier(value,job_id=job_id,input_sha256=input_sha256,positions=positions,configured_hosts=configured_hosts,index=index,expected_product_id=product_id)


def _diagnostics(values,hosts):
    from .catalogue import safe_diagnostics
    if type(values) is not list or len(values)>32:_fail()
    allowed={'code','failure_source','failure_scope','failure_cause','capture_stage','capture_error_type','transport_error_code','reason','http_status','observed_at','url','failure_host'}
    for value in values:
        if type(value) is not dict or not value or set(value)-allowed:_fail()
        if 'url' in value:
            _url(value['url'])
            if not any(_belongs(value['url'],host) for host in hosts):_fail()
            if any(key.casefold() in ('key','user','auth','access_token','password','signature') for key,_ in parse_qsl(urlsplit(value['url']).query)):_fail()
        if 'reason' in value:
            if type(value['reason']) is not str or len(value['reason'])>2048:_fail()
            # Typed server reasons may cite a public card; never credential URLs.
            for url in re.findall(r'https?://[^\s<>]+',value['reason']):
                _url(url)
                if any(key.casefold() in ('key','user','auth','access_token','password','signature') for key,_ in parse_qsl(urlsplit(url).query)):_fail()
                if not any(_belongs(url,host) for host in hosts):_fail()
        if 'failure_host' in value:_host(value['failure_host'])
    if safe_diagnostics(values)!=values:_fail()
    return values

def _host_observations(values,hosts):
    if type(values) is not list or len(values)>len(hosts):_fail()
    seen=set();required=set('host origin window observed_at empty_code success candidate_count'.split())
    for item in values:
        if type(item) is not dict or not required<=set(item) or set(item)-required-{'diagnostics','deferred_check'}:_fail()
        _host(item['host'])
        if item['host'] not in hosts or item['host'] in seen:_fail()
        seen.add(item['host'])
        if type(item['success']) is not bool:_fail()
        _int(item['candidate_count'])
        if item['success'] and item['origin']=='unknown':_fail()
        if item['origin'] not in ('supplier_own_search','response','cache','unknown') or item['window'] not in ('unknown','below_limit','at_limit','truncated_possible'):_fail()
        if item['observed_at'] is not None and (type(item['observed_at']) not in (int,float) or not math.isfinite(item['observed_at']) or item['observed_at']<=0):_fail()
        if type(item['empty_code']) is not str or len(item['empty_code'])>128:_fail()
        diagnostics=_diagnostics(item.get('diagnostics',[]),hosts)
        if diagnostics and item['success']:_fail()
        if item.get('deferred_check') is not None:
            from .catalogue import safe_deferred_check
            try:safe_deferred_check(item['deferred_check'],item['host'])
            except ValueError:_fail()
            if item['success']:_fail()

def _candidate(item,hosts):
    _keys(item,'url host hint');_url(item['url']);_host(item['host'])
    if item['host'] not in hosts or not _belongs(item['url'],item['host']):_fail()
    # Whitelisted discovery facts; never raw provider requests/prices.
    hint=item['hint']
    if type(hint) is not dict or set(hint)-{'title','manufacturer_article','brand','supplier_codes','owner_role','source_sha256','observed_at','rank','url','source_url','source_role','hint_version'}:_fail()
    if 'hint_version' in hint:
        if hint['hint_version']!=1 or type(hint['hint_version']) is not int:_fail()
        if set(hint)!={'hint_version','url','source_url','source_role','title','manufacturer_article','brand','supplier_codes','source_sha256','observed_at','rank'}:_fail()
        if hint['url']!=item['url'] or not _belongs(hint['source_url'],item['host']):_fail()
        _url(hint['source_url'])
        if hint['source_role'] not in ('search_result_owner','ambiguous_result_owner'):_fail()
    elif set(hint)&{'url','source_url','source_role'}:_fail()
    for key in ('title','manufacturer_article','brand','owner_role'):
        if key in hint and (type(hint[key]) is not str or len(hint[key])>4096):_fail()
    if 'supplier_codes' in hint and (type(hint['supplier_codes']) is not list or any(type(v) is not str or len(v)>512 for v in hint['supplier_codes']) or len(hint['supplier_codes'])>64):_fail()
    if 'source_sha256' in hint:_hex(hint['source_sha256'])
    if 'rank' in hint:_int(hint['rank'])
    if 'observed_at' in hint and (type(hint['observed_at']) not in (int,float) or not math.isfinite(hint['observed_at']) or hint['observed_at']<=0):_fail()
    if len(_json(hint).encode())>16384:_fail()

class NeutralRecords:
    """Immutable, paged neutral responses and card outcomes under one owned root."""
    PAGE_SIZE=128
    MAX_BYTES=2*1024*1024
    def __init__(self,root,frontier):
        self.root=Path(root)
        if any(p.is_symlink() for p in (self.root,*self.root.parents)):_fail()
        self.root.mkdir(parents=True,exist_ok=True)
        self.root=self.root.resolve()
        self.binding=record_binding(frontier)
    def _path(self,name):
        if not re.fullmatch(r'[0-9a-f]{64}\.json',name):_fail()
        path=self.root/name
        if path.is_symlink() or path.resolve().parent!=self.root:_fail()
        return path
    def _write(self,payload):
        raw=_json(payload).encode()
        if len(raw)>self.MAX_BYTES:_fail()
        digest=hashlib.sha256(raw).hexdigest();name=digest+'.json';path=self._path(name)
        # Publish fully written content atomically, never expose torn records.
        fd,temporary=tempfile.mkstemp(prefix='.pending-',dir=self.root)
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
            try:os.link(temporary,path)
            except FileExistsError:
                if path.read_bytes()!=raw:_fail()
        finally:os.unlink(temporary)
        return dict(ref=name,sha256=digest)
    def _read(self,ref):
        path=self._path(ref['ref'])
        if path.stat().st_size>self.MAX_BYTES:_fail()
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=ref['sha256']:_fail()
        try:payload=json.loads(raw)
        except (ValueError,UnicodeError):_fail()
        if payload.get('binding')!=self.binding:_fail()
        if payload.get('kind')=='response':
            _keys(payload,'binding kind operation_id pages count host_observations')
            _host_observations(payload['host_observations'],self.binding['configured_hosts'])
        return payload
    def write_response(self,operation_id,candidates,host_observations):
        _hex(operation_id,32)
        pages=[];chunk=[];count=0
        for item in candidates:
            _candidate(item,self.binding['configured_hosts'])
            chunk.append(item);count+=1
            if len(chunk)==self.PAGE_SIZE:
                pages.append(self._write(dict(binding=self.binding,kind='candidates',items=chunk)));chunk=[]
        if chunk:pages.append(self._write(dict(binding=self.binding,kind='candidates',items=chunk)))
        _host_observations(host_observations,self.binding['configured_hosts'])
        manifest=self._write(dict(binding=self.binding,kind='response',operation_id=operation_id,pages=pages,count=count,host_observations=host_observations))
        result=dict(manifest,count=count)
        self._index(operation_id,'response',result)
        return result
    def response(self,ref,operation_id):
        manifest=self._read(ref)
        if manifest.get('kind')!='response' or manifest.get('operation_id')!=operation_id or manifest.get('count')!=ref['count']:_fail()
        return manifest
    def candidate(self,ref,operation_id,index):
        manifest=self.response(ref,operation_id);_int(index)
        if index>=manifest['count']:_fail()
        page=self._read(manifest['pages'][index//self.PAGE_SIZE])
        if page.get('kind')!='candidates':_fail()
        item=page['items'][index%self.PAGE_SIZE]
        _candidate(item,self.binding['configured_hosts'])
        return json.loads(_json(item))
    def write_card_links(self,parent,position,source,*,known,generation,limit):
        from .card_link_discovery import discover
        from .session import bounded_timeout
        bounded_timeout(0)
        existing=self.recover_record(parent['operation_id'],'card_links')
        if existing is not None:
            if self.card_links(existing,position=position)['source']!=source:_fail()
            return existing
        from ..models import SpecPosition
        request=SpecPosition.model_validate(position.model_dump() if hasattr(position,'model_dump') else position).model_dump()
        if identity_digest(request)!=self.binding['requested_identity_sha256']:_fail()
        observed=__import__('time').time();derived=discover(source,request,parent['url'],at=observed)
        if not derived['candidates'] and not derived['incomplete']:return None
        _int(limit)
        if limit>10:_fail()
        excluded=sorted(item['url'] for item in derived['candidates'] if item['url'] in known)
        novel=[item for item in derived['candidates'] if item['url'] not in known]
        if not novel and not derived['incomplete']:return None
        payload=dict(binding=self.binding,kind='card_links',version=1,parent=parent,request=request,source=source,owner=derived['owner'],discovered_at=observed,generation=generation,excluded=excluded,limit=limit,items=novel[:limit],incomplete=derived['incomplete'] or len(novel)>limit)
        base=self._write(payload);reference=dict(base,count=len(payload['items']),parent_index=parent['index'],parent_operation_id=parent['operation_id'])
        self._index(parent['operation_id'],'card_links',reference)
        return reference
    def card_links(self,reference,*,position=None):
        from .card_link_discovery import discover
        value=self._read(reference)
        _keys(value,'binding kind version parent request source owner discovered_at generation excluded limit items incomplete')
        if value['kind']!='card_links' or type(value['version']) is not int or value['version']!=1:_fail()
        parent=value['parent'];_keys(parent,'url host operation_id index');_url(parent['url']);_host(parent['host']);_hex(parent['operation_id'],32);_int(parent['index']);_int(value['generation'],1)
        if parent['host'] not in self.binding['configured_hosts'] or not _belongs(parent['url'],parent['host']) or value['generation']<self.binding.get('record_origin_generation',1):_fail()
        if reference!={**{k:reference[k] for k in ('ref','sha256')},'count':len(value['items']),'parent_index':parent['index'],'parent_operation_id':parent['operation_id']}:_fail()
        from ..models import SpecPosition
        request=value['request']
        if _json(request)!=_json(SpecPosition.model_validate(request).model_dump()) or identity_digest(request)!=self.binding['requested_identity_sha256']:_fail()
        if position is not None and _json(request)!=_json(SpecPosition.model_validate(position.model_dump() if hasattr(position,'model_dump') else position).model_dump()):_fail()
        derived=discover(value['source'],request,parent['url'],at=value['discovered_at'])
        if value['owner']!=derived['owner'] or type(value['excluded']) is not list or sorted(set(value['excluded']))!=value['excluded'] or not set(value['excluded'])<={item['url'] for item in derived['candidates']}:_fail()
        limit=_int(value['limit'])
        if limit>10:_fail()
        novel=[item for item in derived['candidates'] if item['url'] not in value['excluded']]
        if value['items']!=novel[:limit] or type(value['incomplete']) is not bool or value['incomplete']!=(derived['incomplete'] or len(novel)>limit):_fail()
        return value
    def write_outcome(self,operation_id,url,host,*,state,observation_id=None,reason='',completion=None):
        _hex(operation_id,32);_url(url);_host(host)
        if state not in ('observed','no_facts','review','identity_conflict') or not _belongs(url,host):_fail()
        if observation_id is not None:_hex(observation_id,32)
        if state=='observed' and observation_id is None:_fail()
        if type(reason) is not str or not re.fullmatch(r'[a-z0-9_]{0,128}',reason):_fail()
        if type(completion) is dict and completion.get('disposition')=='uncertain':
            evidence=completion.get('evidence')
            safe=evidence if type(evidence) is dict and evidence.get('role')=='card_uncertainty' else {}
            code=safe.get('code',reason or 'card_source_reason_unknown')
            completion=uncertain_card_completion(code,stage=safe.get('stage','outcome_recovery' if reason=='card_link_parent_unsettled' else 'outcome_write'),error_type=safe.get('error_type'))
            reason=completion['evidence']['code']
        payload=dict(binding=self.binding,kind='outcome',operation_id=operation_id,url=url,host=host,state=state,observation_id=observation_id,reason=reason)
        if completion is not None:
            if type(completion) is not dict or len(_json(completion).encode())>4096:_fail()
            payload['completion']=completion
        result=self._write(payload)
        self._index(operation_id,'outcome',result)
        return result
    def _index(self,operation_id,kind,reference):
        name=_hash(dict(binding=self.binding,operation_id=operation_id,kind=kind))+'.json'
        raw=_json(dict(binding=self.binding,kind=kind,reference=reference)).encode();path=self._path(name)
        fd,tmp=tempfile.mkstemp(prefix='.pending-',dir=self.root)
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
            try:os.link(tmp,path)
            except FileExistsError:
                if path.read_bytes()!=raw:_fail()
        finally:os.unlink(tmp)
    def recover_record(self,operation_id,kind):
        _hex(operation_id,32)
        if kind not in ('response','outcome','card_links'):_fail()
        name=_hash(dict(binding=self.binding,operation_id=operation_id,kind=kind))+'.json';path=self._path(name)
        if not path.exists():return None
        if path.stat().st_size>4096:_fail()
        try:payload=json.loads(path.read_bytes())
        except (ValueError,UnicodeError):_fail()
        if payload.get('binding')!=self.binding or payload.get('kind')!=kind:_fail()
        reference=payload['reference'];self._read(reference)
        return reference

def validate_own_response(records, task, *, allow_catalogue_reuse=False):
    """A positive singleton must contain actual URLs bound to its supplier."""
    manifest=records.response(task['response'],task['operation_id'])
    items=[records.candidate(task['response'],task['operation_id'],i) for i in range(task['response']['count'])]
    metadata={m['host']:m for m in manifest['host_observations']}
    if set(metadata)-set(task['hosts']):_fail()
    for host,m in metadata.items():
        actual=[item['url'] for item in items if item['host']==host]
        if m['candidate_count']!=len(set(actual)) or actual and not (m['origin']=='supplier_own_search' or allow_catalogue_reuse is True and m['origin']=='cache'):_fail()
    if any(item['host'] not in metadata for item in items):_fail()
    return manifest


class FrontierController:
    """Copy-on-write transitions; caller persists each transition with row/job CAS."""
    def __init__(self,frontier,records,*,allow_catalogue_reuse=False):
        if type(allow_catalogue_reuse) is not bool:raise ValueError('Invalid catalogue reuse mode')
        self.allow_catalogue_reuse=allow_catalogue_reuse
        self.value=json.loads(_json(frontier));self.records=records
    def _task(self,task_id):
        return next(t for t in self.value['queries'] if t['id']==task_id)
    def candidate(self,task_id,index):
        task=self._task(task_id);_int(index)
        original=task['response']['count']
        if index<original:return self.records.candidate(task['response'],task['operation_id'],index)
        for ref in task.get('card_links',[]):
            receipt=self.records.card_links(ref)
            if index<original+ref['count']:return json.loads(_json(receipt['items'][index-original]))
            original+=ref['count']
        _fail()
    def accept_card_links(self,task_id,reference):
        task=self._task(task_id)
        if self.value['frontier_complete'] or self.value['version']!=5 or self.value.get('source_intent',{}).get('version')!='acquisition-source-intent-v7':_fail()
        if reference in task.get('card_links',[]):return json.loads(_json(self.value))
        card=task['current_card'];receipt=self.records.card_links(reference)
        if card is None or receipt['parent']!=card or reference['parent_index']!=card['index'] or reference['parent_operation_id']!=card['operation_id']:_fail()
        known=self.known_urls();remaining=10-sum(r['count'] for r in task.get('card_links',[]))
        if receipt['limit']!=remaining or receipt['generation']>self.value['acquisition_generation'] or not set(receipt['excluded'])<=known or any(i['url'] in known for i in receipt['items']):_fail()
        task.setdefault('card_links',[]).append(reference)
        return self._change()
    def known_urls(self):
        return {self.candidate(t['id'],i)['url'] for t in self.value['queries'] if t['response'] for i in range(candidate_count(t))}
    def _change(self):
        self.value['revision']+=1
        return json.loads(_json(self.value))
    def add_query(self,phase,query,hosts,strategy='own_or_serp'):
        if self.value['frontier_complete']:_fail()
        spec=dict(phase=phase,query=query,hosts=list(hosts),strategy=strategy);task_id=_hash(spec)
        if any(t['id']==task_id for t in self.value['queries']):return self.value,task_id
        task=dict(spec,id=task_id,state='pending',operation_id=None,response=None,cursor=0,current_card=None)
        if self.value['version'] in (4,5):task.update(settled_cards={},deferred_cards={})
        self.value['queries'].append(task)
        return self._change(),task_id
    def plan_phase(self,phase,specifications):
        """Atomic conditional expansion, including an empty considered phase."""
        order=('initial','fallback','literal','complete')
        current=self.value['planning_phase']
        if current=='complete' or phase not in order or order.index(phase)!=order.index(current)+1 or any(t['state']!='done' for t in self.value['queries']):_fail()
        if phase=='complete' and (specifications or not self.value['queries']):_fail()
        for spec in specifications:
            self.add_query(spec['phase'],spec['query'],spec['hosts'],spec.get('strategy','own_or_serp'))
        self.value['planning_phase']=phase;self.value['frontier_complete']=phase=='complete'
        return self._change()
    def start_query(self,task_id):
        task=self._task(task_id)
        if task['state']!='pending':_fail()
        task.update(state='running',operation_id=uuid.uuid4().hex)
        return self._change()
    def accept_response(self,task_id,response):
        task=self._task(task_id)
        if task['state']!='running':_fail()
        manifest=self.records.response(response,task['operation_id'])
        if any(item['host'] not in task['hosts'] for item in manifest['host_observations']):_fail()
        for i in range(response['count']):
            if self.records.candidate(response,task['operation_id'],i)['host'] not in task['hosts']:_fail()
        if self.value['version']==5 and task['strategy']=='own_only':validate_own_response(self.records,dict(task,response=response),allow_catalogue_reuse=self.allow_catalogue_reuse)
        task.update(response=response,state='done' if response['count']==0 else 'received')
        return self._change()
    def start_card(self,task_id,index=None):
        task=self._task(task_id)
        if task['state']!='received':_fail()
        if task['current_card'] is None:
            offset=task['cursor'] if index is None else index
            if self.value['version'] not in (4,5) and offset!=task['cursor']:_fail()
            if type(offset) is not int or not task['cursor']<=offset<candidate_count(task):_fail()
            if self.value['version'] in (4,5) and str(offset) in task['settled_cards']:_fail()
            item=self.candidate(task_id,offset)
            operation_id=_hash(dict(binding=self.records.binding,task_id=task['id'],index=offset,url=item['url'],host=item['host']))[:32]
            task['current_card']=dict(url=item['url'],host=item['host'],operation_id=operation_id)
            if self.value['version'] in (4,5):
                task['current_card']['index']=offset
                task['deferred_cards'].pop(str(offset),None)
            return self._change()
        if index is not None and index!=task['current_card'].get('index',task['cursor']):_fail()
        # Existing operation ID survives crash; caller reconciles catalogue first.
        return json.loads(_json(self.value))
    def completed_cards(self):
        """Rebuild seen/outcomes from committed prefixes, never source prices."""
        for task in self.value['queries']:
            if task['response'] is None:continue
            for index in list(range(task['cursor']))+sorted(int(i) for i in task.get('settled_cards',{})):
                item=self.candidate(task['id'],index)
                operation_id=_hash(dict(binding=self.records.binding,task_id=task['id'],index=index,url=item['url'],host=item['host']))[:32]
                yield dict(item,operation_id=operation_id,outcome=self.records.recover_record(operation_id,'outcome'))
    def retry_uncertain_query(self,task_id):
        """Explicit recovery only, after server claim/stop checks and no local commit."""
        task=self._task(task_id)
        if task['state']!='running' or self.records.recover_record(task['operation_id'],'response') is not None:_fail()
        task['operation_id']=uuid.uuid4().hex
        return self._change()
    def takeover(self,generation):
        """Caller must atomically guard this revision with actual catalogue claim."""
        _int(generation,1)
        if generation<=self.value['acquisition_generation']:_fail()
        self.value['acquisition_generation']=generation
        return self._change()
    def settle_card(self,task_id,outcome_ref):
        task=self._task(task_id);card=task['current_card']
        if card is None:_fail()
        outcome=self.records._read(outcome_ref)
        if outcome.get('kind')!='outcome' or any(outcome.get(k)!=card[k] for k in ('operation_id','url','host')):_fail()
        if self.value['version'] in (4,5):
            offset=str(card['index']);task['settled_cards'][offset]=outcome_ref
            task['deferred_cards'].pop(offset,None)
            while str(task['cursor']) in task['settled_cards']:
                task['settled_cards'].pop(str(task['cursor']));task['cursor']+=1
        else:task['cursor']+=1
        task['current_card']=None
        if task['cursor']==candidate_count(task):task['state']='done'
        return self._change()

    def defer_card(self,task_id,check):
        from .catalogue import safe_deferred_check
        task=self._task(task_id);card=task['current_card']
        if self.value['version'] not in (4,5) or card is None:_fail()
        task['deferred_cards'][str(card['index'])]=safe_deferred_check(check,card['host'])
        task['current_card']=None
        return self._change()

    def candidate_indices(self,task_id,parked=()):
        task=self._task(task_id)
        if task['current_card'] is not None:
            return [task['current_card'].get('index',task['cursor'])]
        return [i for i in range(task['cursor'],candidate_count(task))
                if str(i) not in task.get('settled_cards',{}) and i not in parked]


def validate_indexed_transition(old,new):
    """CAS guards out-of-order state without weakening historical prefix rules."""
    if new['cursor']<old['cursor'] or old['response'] is not None and new['response']!=old['response']:_fail()
    if old['state']=='done' and new!=old:_fail()
    if old['response'] is not None and new['operation_id']!=old['operation_id']:_fail()
    previous=old.get('card_links',[]);current=new.get('card_links',[])
    if current[:len(previous)]!=previous or len(current)<len(previous):_fail()
    if len(current)>len(previous) and (old['current_card'] is None or any(ref['parent_operation_id']!=old['current_card']['operation_id'] for ref in current[len(previous):])):_fail()
    for index,ref in old['settled_cards'].items():
        if int(index)>=new['cursor'] and new['settled_cards'].get(index)!=ref:_fail()
    card=old['current_card']
    permitted=set(old['settled_cards'])|({str(card['index'])} if card is not None else set())
    if any(str(i) not in permitted for i in range(old['cursor'],new['cursor'])):_fail()
    added=set(new['settled_cards'])-set(old['settled_cards'])
    if added-({str(card['index'])} if card is not None else set()):_fail()
    deferred_added=set(new['deferred_cards'])-set(old['deferred_cards'])
    if deferred_added-({str(card['index'])} if card is not None else set()):_fail()
    removed=set(old['deferred_cards'])-set(new['deferred_cards'])
    if removed-({str(new['current_card']['index'])} if new['current_card'] is not None else set()):_fail()
    if card is not None and new['current_card']!=card:
        i=str(card['index'])
        if int(i)>=new['cursor'] and i not in new['settled_cards'] and i not in new['deferred_cards']:_fail()
    # No unsupported query state rollback; uncertain query retries keep running.
    order={'pending':0,'running':1,'received':2,'done':3}
    if order[new['state']]<order[old['state']]:_fail()


_COMPLETION_ISSUER = object()

class VerifiedCompletion:
    __slots__ = ('_encoded','_issuer')
    def __init__(self,payload,issuer):
        if issuer is not _COMPLETION_ISSUER:raise ValueError('Private completion issuer required')
        self._encoded=_json(payload);self._issuer=issuer
    @property
    def payload(self):return json.loads(self._encoded)


class CompletionUnknown(ValueError):
    """Finite server plan is not proved exhausted; raw observations remain usable."""


def validate_completion_receipt(value):
    import time
    fields = {'version','product_id','job_id','input_sha256','requested_identity_sha256',
              'generation','configured_hosts','primary_hosts','planner_version',
              'source_intent_sha256','runtime_sha256','search_route','components_sha256','frontier_sha256','records_sha256',
              'query_count','card_count','completed_at','sha256'}
    if type(value) is not dict or set(value) != fields or value['version'] != 'acquisition-completion-v1':
        raise CompletionUnknown('completion_invalid')
    for key in ('product_id','input_sha256','requested_identity_sha256','source_intent_sha256',
                'runtime_sha256','components_sha256','frontier_sha256','records_sha256','sha256'):
        if type(value[key]) is not str or not re.fullmatch('[a-f0-9]{64}',value[key]):
            raise CompletionUnknown('completion_invalid')
    if (type(value['job_id']) is not str or not re.fullmatch('[a-f0-9]{12}',value['job_id'])
        or type(value['generation']) is not int or value['generation'] < 1
        or any(type(value[k]) is not int or not 0 <= value[k] <= 1000000 for k in ('query_count','card_count'))
        or value['query_count'] < 1 or value['planner_version'] not in ('native-acquisition-indexed-v4','native-acquisition-indexed-v5')):
        raise CompletionUnknown('completion_invalid')
    hosts=value['configured_hosts'];primary=value['primary_hosts']
    if (type(hosts) is not list or not 1 <= len(hosts) <= 256
        or any(type(h) is not str or _host(h)!=h for h in hosts) or len(set(hosts))!=len(hosts)
        or type(primary) is not list or any(h not in hosts for h in primary) or len(set(primary))!=len(primary)):
        raise CompletionUnknown('completion_invalid')
    if value['search_route'] is not None:
        from .search_route import validate_route
        try:validate_route(value['search_route'])
        except (ValueError,TypeError):raise CompletionUnknown('completion_invalid')
    stamp=value['completed_at']
    if type(stamp) not in (int,float) or not math.isfinite(stamp) or not 0 < stamp <= time.time()+300:
        raise CompletionUnknown('completion_invalid')
    if _hash({k:v for k,v in value.items() if k!='sha256'}) != value['sha256']:
        raise CompletionUnknown('completion_invalid')
    return json.loads(_json(value))


def certify_completion(frontier, context, records, intent, *, resolve_hosts, verify_identity=lambda *args: False, verify_reuse=lambda *args: False, allow_catalogue_reuse=False):
    """Replay the exact bounded planner from private inputs and immutable records.

    resolve_hosts(ids) is a SERVER callback resolving same-product observations;
    it returns confirmed and currently comparable hosts at the replay boundary.
    No caller's coverage/count/complete flag is completion authority.
    """
    import time
    from .acquisition_source_intent import validate,record_seal,digest
    from .discovery_plan import StagedDiscoveryPlan,completion_plan_tasks,expand_discovery_tasks,DiscoveryTask,own_prefix
    from .article_query_plan import build_article_query_plan
    from .catalogue import identity_for_position
    try:
        intent=validate(intent)
        index=frontier['representative_index']
        from ..models import SpecPosition
        position=SpecPosition.model_validate(context['positions'][index])
        hosts=context['configured_hosts'];primary=context['primary_hosts']
        value=validate_frontier(frontier,job_id=frontier['job_id'],input_sha256=context['input_sha256'],
            positions=context['positions'],configured_hosts=hosts,index=index,
            expected_product_id=frontier['product_id'],expected_generation=frontier['acquisition_generation'])
        if (value['version'] not in (4,5) or value['source_intent']!=intent or not value['frontier_complete']
            or value['planner_version']!=('native-acquisition-indexed-v5' if value['version']==5 else 'native-acquisition-indexed-v4')):
            raise CompletionUnknown('completion_unsupported_frontier')
        identity=identity_for_position(position)
        if identity.key!=value['product_id']:raise CompletionUnknown('completion_foreign_product')
        field=lambda k:position.get(k,'') if isinstance(position,dict) else getattr(position,k,'')
        plan=build_article_query_plan(field('name'),field('type_mark'),article=identity.code if identity.kind=='article' else '',manufacturer=identity.manufacturer,unit=field('unit'))
        planner=StagedDiscoveryPlan(plan.queries,primary,[h for h in hosts if h not in primary],strong_article=identity.kind=='article')
        if planner.hosts!=hosts:raise CompletionUnknown('completion_scope_changed')
        steps={step.query:step for step in plan.steps}
        cursor=0;observations={};confirmed=set();resolved=set();observation_ids=[];prior={};cards=0;pending_retries=set()
        linked_controller=FrontierController(value,records)
        def consume(task):
            nonlocal cards,confirmed,resolved
            if task['state']!='done' or task.get('deferred_cards') or task['current_card'] is not None:
                raise CompletionUnknown('completion_unsettled')
            manifest=validate_own_response(records,task,allow_catalogue_reuse=allow_catalogue_reuse) if value['version']==5 and task['strategy']=='own_only' else records.response(task['response'],task['operation_id'])
            metadata={m['host']:m for m in manifest['host_observations']}
            parts=own_prefix(value['queries'],task) if value['version']==5 else []
            retained={m['host'] for part in parts for m in records.response(part['response'],part['operation_id'])['host_observations']}
            selected=(set(task['hosts'])-resolved)|retained
            for part in parts:
                for m in records.response(part['response'],part['operation_id'])['host_observations']:
                    if m['candidate_count']>0 and metadata.get(m['host'])!=m:raise CompletionUnknown('completion_own_parent_changed')
            prefix=[];seen_urls=set()
            for part in parts:
                for i in range(part['response']['count']):
                    item=records.candidate(part['response'],part['operation_id'],i)
                    if item['url'] not in seen_urls:prefix.append(item);seen_urls.add(item['url'])
            if task['response']['count']<len(prefix) or any(records.candidate(task['response'],task['operation_id'],i)!=item for i,item in enumerate(prefix)):raise CompletionUnknown('completion_own_parent_tail_changed')
            if set(metadata)!=selected:raise CompletionUnknown('completion_missing_resource_evidence')
            for host,m in metadata.items():
                if value['version']==5 and task['strategy']=='own_only':
                    if m['origin']=='supplier_own_search' and m['candidate_count']>0:observations.setdefault(host,{})['own_search_positive']=True
                    continue
                deferred=m.get('deferred_check')
                if deferred:
                    from .catalogue import safe_deferred_check
                    safe_deferred_check(deferred,host)
                    pending_retries.add((task['query'],host))
                    observations.setdefault(host,{})['group_success' if task['phase']=='group' else 'individual_success']=False
                    continue
                if (not m['success'] or m.get('diagnostics')
                    or m['origin'] not in ('response','cache','supplier_own_search')
                    or m['observed_at'] is None):raise CompletionUnknown('completion_uncertain_query')
                if task['strategy']=='own_only':pending_retries.discard((task['query'],host))
                o=observations.setdefault(host,{})
                if m['origin']=='supplier_own_search' and m['candidate_count']>0:o['own_search_positive']=True
                o.update(dict(group_success=True,group_window=m['window']) if task['phase']=='group' else
                         dict(individual_success=True,individual_window=m['window'],individual_candidates=m['candidate_count']))
                if task['strategy']=='serp_only' and not parts:o['serp_fallback_attempted']=True
            for ref in task.get('card_links',[]):
                receipt=records.card_links(ref,position=position)
                if receipt['incomplete']:raise CompletionUnknown('completion_uncertain_card')
                parent=receipt['parent'];expected=_hash(dict(binding=records.binding,task_id=task['id'],index=parent['index'],url=parent['url'],host=parent['host']))[:32]
                if receipt['generation']>value['acquisition_generation'] or expected!=parent['operation_id'] or linked_controller.candidate(task['id'],parent['index'])['url']!=parent['url'] or not set(receipt['excluded'])<=linked_controller.known_urls():raise CompletionUnknown('completion_foreign_operation')
            for offset in range(candidate_count(task)):
                item=linked_controller.candidate(task['id'],offset)
                operation=_hash(dict(binding=records.binding,task_id=task['id'],index=offset,url=item['url'],host=item['host']))[:32]
                reference=records.recover_record(operation,'outcome')
                if reference is None:raise CompletionUnknown('completion_missing_tail')
                outcome=records._read(reference)
                if any(outcome.get(k)!=v for k,v in dict(operation_id=operation,url=item['url'],host=item['host']).items()):
                    raise CompletionUnknown('completion_foreign_outcome')
                typed=outcome.get('completion')
                if type(typed) is not dict or set(typed)!={'version','disposition','evidence'} or type(typed['version']) is not int or typed['version']!=1:
                    raise CompletionUnknown('completion_legacy_outcome')
                disposition=typed['disposition'];evidence=typed['evidence'];definitive=False
                if task['strategy']=='own_only' and metadata[item['host']]['origin']=='cache' and disposition!='catalogue_reuse':
                    raise CompletionUnknown('completion_invalid_catalogue_reuse')
                if disposition=='observed' and outcome['state']=='observed' and outcome['observation_id']:
                    if outcome['observation_id']!=operation:raise CompletionUnknown('completion_foreign_operation')
                    if evidence!={}:raise CompletionUnknown('completion_invalid_outcome')
                    observation_ids.append(dict(observation_id=outcome['observation_id'],url=item['url'],host=item['host']))
                    confirmed,resolved=map(set,resolve_hosts(observation_ids));definitive=True
                elif disposition=='catalogue_reuse' and outcome['state']=='observed' and outcome['observation_id']:
                    # Reuse carries the original immutable observation, never a
                    # manufactured current-operation observation or timestamp.
                    if (allow_catalogue_reuse is not True or type(evidence) is not dict or set(evidence)!={'product_id','observation_id','observation_sha256'}
                        or evidence['product_id']!=frontier['product_id'] or evidence['observation_id']!=outcome['observation_id']
                        or not verify_reuse(evidence,item['url'],item['host'])):
                        raise CompletionUnknown('completion_invalid_catalogue_reuse')
                    observation_ids.append(dict(observation_id=outcome['observation_id'],url=item['url'],host=item['host']))
                    confirmed,resolved=map(set,resolve_hosts(observation_ids));definitive=True
                elif disposition=='deterministic_rejection' and outcome['state'] in ('review','identity_conflict') and outcome['observation_id'] is None:
                    from .candidate_admission import admit_with_hint
                    decision=admit_with_hint(item['url'],item['host'],position,item['hint'])
                    if decision.rejected and evidence==dict(role='candidate_admission',code=decision.reason,response_sha256=task['response']['sha256']):
                        definitive=True
                    elif (type(evidence) is dict and set(evidence)=={'role','code','source'}
                          and evidence['role']=='http_primary_identity'
                          and outcome['state']=='identity_conflict'
                          and verify_identity(evidence,item['url'],position)):
                        definitive=True
                elif disposition=='duplicate' and outcome['state']=='no_facts' and outcome['observation_id'] is None and evidence=={}:
                    definitive=prior.get(item['url'],False)
                if not definitive:raise CompletionUnknown('completion_uncertain_card')
                prior[item['url']]=True;cards+=1
        for phase in ('initial','fallback','literal'):
            expected=completion_plan_tasks(planner,phase,observations,confirmed,resolved)
            if value['version']==5:
                physical=expand_discovery_tasks([DiscoveryTask(s['phase'],s['query'],tuple(s['hosts']),route=s['strategy']) for s in expected],steps)
                expected=[dict(phase=t.phase,query=t.query,hosts=list(t.hosts),strategy=t.route) for t in physical]
            for spec in expected:
                if cursor>=len(value['queries']):raise CompletionUnknown('completion_missing_obligation')
                task=value['queries'][cursor];cursor+=1
                if any(task[k]!=spec[k] for k in spec):raise CompletionUnknown('completion_plan_changed')
                consume(task)
            while value['version']!=5 and cursor<len(value['queries']) and value['queries'][cursor]['strategy']=='own_only':
                task=value['queries'][cursor]
                if task['phase']!='individual' or len(task['hosts'])!=1 or (task['query'],task['hosts'][0]) not in pending_retries:
                    raise CompletionUnknown('completion_unverified_retry_obligation')
                cursor+=1;consume(task)
                if task['hosts'][0] in resolved:pending_retries.discard((task['query'],task['hosts'][0]))
                planner.admitted.add(('own_only',False,task['hosts'][0],task['query'].casefold()))
        if cursor!=len(value['queries']) or pending_retries:raise CompletionUnknown('completion_unverified_retry_obligation')
        seal=record_seal(records,value)
        receipt=dict(version='acquisition-completion-v1',product_id=value['product_id'],job_id=value['job_id'],
            input_sha256=value['input_sha256'],requested_identity_sha256=value['requested_identity_sha256'],
            generation=value['acquisition_generation'],configured_hosts=hosts,primary_hosts=primary,
            planner_version=value['planner_version'],source_intent_sha256=intent['sha256'],runtime_sha256=intent['runtime_sha256'],search_route=value['search_route'],components_sha256=digest(intent['components']),
            frontier_sha256=digest(value),records_sha256=digest(seal),query_count=len(value['queries']),card_count=cards,completed_at=time.time())
        receipt['sha256']=_hash(receipt)
        return VerifiedCompletion(validate_completion_receipt(receipt),_COMPLETION_ISSUER)
    except CompletionUnknown:raise
    except (ValueError,KeyError,TypeError,AttributeError,IndexError,OSError) as error:
        raise CompletionUnknown('completion_evidence_invalid') from error
