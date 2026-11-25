"""Evidence-backed main-card identity, independent of quote admission."""
from .failure_policy import ResearchControlError
import math
from contextvars import ContextVar
from contextlib import contextmanager
import re
import time
from urllib.parse import urlsplit, urlunsplit


_CURRENT_ALTERNATIVES=ContextVar('current_position_alternatives',default=None)

def policy_digest(identity,articles,names):
    import hashlib,json
    value=dict(version='position-alternatives-v1',identity_sha256=identity.key,original_article=identity.code,alternative_articles=articles,alternative_names=names)
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=True,separators=(',',':')).encode()).hexdigest()if articles or names else ''

def alternative_policy(position=None):
    from ..models import normalize_alternatives
    import hashlib,json
    from .catalogue import identity_for_position
    if position is None:
        active=_CURRENT_ALTERNATIVES.get()
        return dict(active) if active else dict(alternative_articles=[],alternative_names=[],original_article='',sha256='')
    field=lambda k,d='':position.get(k,d)if isinstance(position,dict)else getattr(position,k,d)
    articles=normalize_alternatives(field('alternative_articles',[]),article=True)
    names=normalize_alternatives(field('alternative_names',[]))
    identity=identity_for_position(position)
    value=dict(version='position-alternatives-v1',identity_sha256=identity.key,original_article=identity.code,alternative_articles=articles,alternative_names=names)
    value['sha256']=policy_digest(identity,articles,names)
    return value

def legacy_article_request(position):
    """Legacy single literal SKU has no separately typed native model to replace."""
    field=lambda k,d=None:position.get(k,d)if isinstance(position,dict)else getattr(position,k,d)
    return not field('source_evidence',{})and not field('review_fields',{})and bool(re.fullmatch(r'[A-Za-zА-Яа-я0-9._-]{3,}',field('type_mark','')))

@contextmanager
def request_alternatives_scope(position):
    policy=alternative_policy(position);policy['_legacy_article']=legacy_article_request(position)
    token=_CURRENT_ALTERNATIVES.set(policy)
    native_token=_NATIVE_REQUEST_CONTEXT.set(_native_constraints(position))
    try:yield
    finally:
        _NATIVE_REQUEST_CONTEXT.reset(native_token)
        _CURRENT_ALTERNATIVES.reset(token)

def position_alternatives(function):
    from functools import wraps
    @wraps(function)
    def invoke(browser,pos,*args,**kwargs):
        with request_alternatives_scope(pos):return function(browser,pos,*args,**kwargs)
    return invoke

def permitted_article(position,article,*,original=None):
    policy=alternative_policy(position)
    original=original if original is not None else policy['original_article']
    if type(article)is not str or not article.strip():return None
    exact=lambda value:value.strip().casefold()
    if exact(article)==exact(original):return dict(original_article=original,observed_article=article,matched_article=original,match_basis='original',alternatives_policy_sha256=policy['sha256'])
    for allowed in policy['alternative_articles']:
        if exact(article)==exact(allowed):return dict(original_article=original,observed_article=article,matched_article=allowed,match_basis='estimator_alternative',alternatives_policy_sha256=policy['sha256'])
    return None


def owned_article_source(observation):
    """Per-use immutable ownership, never authorization or a cached decision."""
    from .http_card import locate_primary_characteristics,read_identity_source
    source,reason=locate_primary_characteristics(url=observation['url'],observed_at=observation['observed_at'],title=observation['title'],article=observation['manufacturer_code'],source_sha256=observation['source_sha256'],primary=True)
    if reason:return None
    retained=read_identity_source(source)
    if not retained:return None
    record,owned=retained
    norm=lambda value:' '.join(value.split())
    if (record['url']!=observation['url']or record['observed_at']!=observation['observed_at']or owned['title']!=observation['title']or owned['article']!=observation['manufacturer_code']or norm(owned['text'])!=norm(observation['source_text'])):return None
    return source


def article_match_evidence(observation,position):
    match=permitted_article(position,observation['manufacturer_code'])
    if not match:return {}
    if match['match_basis']=='estimator_alternative':
        source=owned_article_source(observation)
        if not source:return {}
        match['source_binding']=source
    match['observation_id']=observation['observation_id'];match['source_sha256']=observation['source_sha256'];match['url']=observation['url'];match['observed_at']=observation['observed_at']
    return match


def canonical_card_url(url):
    if not isinstance(url, str):
        return ''
    try:
        p = urlsplit(url or '')
        if p.scheme not in {'http', 'https'} or not p.hostname or p.username or p.password:
            return ''
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or '/', p.query, ''))
    except ValueError:
        return ''


def card_host(url):
    return (urlsplit(canonical_card_url(url)).hostname or '').removeprefix('www.')


def observed_identity_article(facts):
    """Resolve current labelled DOM facts; never use specification/old proof."""
    from ..purchase_scope import scope_observed_article, labelled_article_sources_valid
    if not isinstance(facts,dict) or type(facts.get('version')) is not int or facts['version']!=2:
        return '',False
    owner= facts.get('owner');rows=facts.get('facts');values=facts.get('values')
    if not isinstance(owner,str) or not owner or not isinstance(rows,list) or len(rows)>64:
        return '',False
    if not isinstance(values,list) or any(not isinstance(v,str) for v in values):return '',False
    if any(not isinstance(f,dict) or f.get('kind')!='labelled_article' or not labelled_article_sources_valid(f) for f in rows):return '',False
    actual=[f.get('value') for f in rows]
    if any(not isinstance(v,str) for v in actual) or set(values)!=set(actual):return '',False
    if not rows:return '',not values
    scope={'identity_facts_version':1,'identity_scope_id':owner,'identity_facts':rows,'own_articles':values}
    article=scope_observed_article(scope)
    return article,bool(article)



_NATIVE_MODEL_INTENT='catalogue-native-model-role-v2'
_NATIVE_MODEL_CONTEXT=ContextVar('catalogue_native_model_intent',default=None)
_NATIVE_ARTICLE_INTENT='catalogue-native-article-constraints-v1'
_NATIVE_REQUEST_CONTEXT=ContextVar('catalogue_native_request_constraints',default=None)

@contextmanager
def _resolved_model_role(observation, identity):
    """Private callers must supply an immutable server-resolved record."""
    payload=identity.payload() if hasattr(identity,'payload') else identity
    binding=None
    if (type(observation) is dict and type(payload) is dict
        and observation.get('validation_version')==_NATIVE_MODEL_INTENT
        and observation.get('identity')==payload and payload.get('kind')=='model'):
        binding=(payload.get('code'),payload.get('descriptor'),payload.get('descriptor'))
    article_binding=None
    if (type(observation) is dict and type(payload) is dict
        and observation.get('validation_version')==_NATIVE_ARTICLE_INTENT
        and observation.get('identity')==payload and payload.get('kind')=='article'
        and observation.get('state')=='observed'):
        from .catalogue import ProductIdentity,validate_native_request_contract
        try:
            native=validate_native_request_contract(observation.get('native_request_contract'),
                ProductIdentity(**{**payload,'features':tuple(tuple(v) for v in payload.get('features',()))}),observation.get('issuer'))
        except (ValueError,TypeError,KeyError):native=None
        if native:
            article_binding=dict(mark=payload.get('code'),name=payload.get('descriptor') or observation.get('title',''),
                article=native['article'],model=native['model'],requested=native['requested'],contract=native)
    request_token=_NATIVE_REQUEST_CONTEXT.set(article_binding)
    token=_NATIVE_MODEL_CONTEXT.set(binding)
    try:yield
    finally:
        _NATIVE_MODEL_CONTEXT.reset(token)
        _NATIVE_REQUEST_CONTEXT.reset(request_token)


@contextmanager
def _original_native_model_role(store,identity,position):
    """Initial capture retains the real server-native position, never fake cells."""
    role=native_request_role(position)
    from .catalogue import identity_for_position
    expected=store.lookup(identity_for_position(position)) if role in {'model','article'} else None
    genuine=getattr(position,'source_evidence',{}) or {}
    valid=role in {'model','article'} and genuine.get('reader')=='native_pdf' and expected and expected['product_id']==identity.key and expected['identity']==identity.payload()
    if not valid:
        yield;return
    request_token=_NATIVE_REQUEST_CONTEXT.set(_native_constraints(position))
    token=_NATIVE_MODEL_CONTEXT.set((identity.code,identity.descriptor,position.name) if role=='model' else None)
    try:yield
    finally:
        _NATIVE_MODEL_CONTEXT.reset(token)
        _NATIVE_REQUEST_CONTEXT.reset(request_token)


def _active_model_role(mark,name=None):
    binding=_NATIVE_MODEL_CONTEXT.get()
    return bool(binding and binding[0]==mark and (name is None or binding[2]==name))


def _active_model_interface(mark,code):
    binding=_NATIVE_MODEL_CONTEXT.get()
    token=re.fullmatch(r'rs(\d{3})',code,re.I)
    return bool(binding and binding[0]==mark and token and re.search(r'(?<![A-Za-z0-9])RS[- ]?'+token[1]+r'(?![A-Za-z0-9])',binding[1]+' '+binding[2],re.I))


def native_request_role(position):
    """Only bound immutable native cells distinguish a model from an article."""
    from .catalogue import identity_for_position
    field=lambda k,default='': position.get(k,default) if isinstance(position,dict) else getattr(position,k,default)
    evidence=field('source_evidence',{}) or {}
    reviews=field('review_fields',{}) or {}
    binding=_NATIVE_MODEL_CONTEXT.get()
    if not evidence and not reviews and binding and binding[:2]==(str(field('type_mark')),str(field('full_name') or field('name'))):return 'model'
    if type(evidence) is not dict or type(reviews) is not dict or evidence.get('reader')!='native_pdf':return 'unknown'
    cells=evidence.get('cells')
    if type(cells) is not dict or any(k in reviews for k in ('name','type_mark','model','article','maker')):return 'unknown'
    clean=lambda value:' '.join(str(value or '').split())
    mark=clean(' / '.join(dict.fromkeys(clean(cells.get(k)) for k in ('model','article') if clean(cells.get(k)))))
    if clean(cells.get('name'))!=clean(field('name')) or mark!=clean(field('type_mark')):return 'unknown'
    identity=identity_for_position(position)
    if identity.kind=='article':return 'article'
    return 'model' if clean(cells.get('model')) and not clean(cells.get('article')) else 'unknown'


def _native_constraints(position=None, *, mark=None, name=None):
    """Only original bound native cells or private resolved intent carry roles."""
    field=lambda key,default='':position.get(key,default) if isinstance(position,dict) else getattr(position,key,default)
    evidence=field('source_evidence',{}) or {}
    reviews=field('review_fields',{}) or {}
    if evidence or reviews:
        if native_request_role(position)=='unknown':return None
        cells=evidence['cells'];clean=lambda value:' '.join(str(value or '').split())
        return dict(mark=clean(field('type_mark')),name=clean(field('name')),
            model=clean(cells.get('model')),article=clean(cells.get('article')),
            requested=clean(cells.get('name')))
    wanted=str(field('type_mark')) if position is not None else mark
    label=str(field('name')) if position is not None else name
    active=_NATIVE_REQUEST_CONTEXT.get()
    if active and (wanted,label)==(active['mark'],active['name']):return active
    return None


def current_discovery_mark(expected,text):
    """Only current explicit full SKU hints affect early filters; admission stays native."""
    policy=_CURRENT_ALTERNATIVES.get()
    if not policy:return expected
    binding=_NATIVE_REQUEST_CONTEXT.get()
    original=policy['original_article']
    if expected not in {original,binding['mark']if binding else original}:return expected
    if not binding and not policy.get('_legacy_article',False):return expected
    if binding and not binding['article']:return expected  # A model is never an article permission.
    matches=[value for value in policy['alternative_articles']if re.search(r'(?<![\w.-])'+re.escape(value)+r'(?![\w.-])',text,re.I)]
    if len(matches)!=1:return expected
    return ' / '.join(v for v in (binding['model']if binding else '',matches[0])if v)


def native_comparison_mark(position=None, *, mark=None,name=None,article=None):
    binding=_native_constraints(position,mark=mark,name=name)
    if binding and binding['article']:
        match=permitted_article(position,article,original=binding['article'])if article else None
        actual=article if match else binding['article']
        return ' / '.join(v for v in (binding['model'],actual) if v)
    return (position.get('type_mark','') if isinstance(position,dict) else getattr(position,'type_mark','')) if position is not None else mark


def _active_request_model_code(mark,code):
    binding=_NATIVE_REQUEST_CONTEXT.get()
    if not binding or binding['mark']!=mark:return False
    # Compare the heuristic's candidate with a complete printed word from the
    # verified source model cell; do not invent model codes from the URL/title.
    tokens=re.findall(r'[A-Za-zА-Яа-я0-9]+',binding['model'].casefold())
    return code.casefold() in tokens


def native_article_reason(position,title,article, *, mark=None,name=None):
    binding=_native_constraints(position,mark=mark,name=name)
    if not binding:
        evidence=position.get('source_evidence',{}) if isinstance(position,dict) else getattr(position,'source_evidence',{})
        if isinstance(evidence,dict) and evidence.get('reader')=='native_pdf':return 'Связь запроса с исходными ячейками спецификации не подтверждена'
        return ''
    expected=binding['article']
    if not expected:return ''
    # Supplier-number labels are never passed as article. Full literal primary
    # title codes remain independent evidence, without using URL/model aliases.
    exact=lambda value: ' '.join(str(value).split()).casefold()
    if article and exact(article)!=exact(expected) and not permitted_article(position,article,original=expected):return 'Артикул производителя основного товара противоречит спецификации'
    printed=bool(re.search(r'(?<![\w.-])'+re.escape(expected)+r'(?![\w.-])',title,re.I))
    if not article and not printed:return 'Артикул производителя не подтверждён основным товаром'
    if binding['model'] and binding['model']!=expected:
        from .agent import _type_mark_match_status
        if _type_mark_match_status(binding['model'],title) is not True:
            return 'Модель основного товара не подтверждена спецификацией'
    return ''


def native_source_spec_reason(position,title,source_text,article='', *, mark=None,name=None):
    reason=native_article_reason(position,title,article,mark=mark,name=name)
    if reason:return reason
    binding=_native_constraints(position,mark=mark,name=name)
    if not binding:return ''
    from ..source_quote import primary_text
    from decimal import Decimal
    primary=primary_text(source_text)
    clean_number=lambda value:str(Decimal(value.replace(',','.')).normalize())
    va=lambda text:{clean_number(v) for v in re.findall(r'(?<![\w.,-])(\d+(?:[.,]\d+)?)\s*(?:ВА|VA)(?!\w)',text,re.I)}
    classes=lambda text:{clean_number(v)+suffix.casefold() for v,suffix in re.findall(r'(?:класс(?:\s+точности)?|accuracy\s+class)\s*[:：-]?\s*(\d+(?:[.,]\d+)?)\s*([sS]?)(?![\w.]|,\d)',text,re.I)}
    for label,extract in (('Мощность ВА',va),('Класс точности',classes)):
        wanted=extract(binding['requested'])
        if not wanted:continue
        # Read only explicitly typed primary title/properties, never code suffixes.
        seen=extract(title+'\n'+primary)
        if len(wanted)!=1:return label+' запрошенного исполнения требует проверки'
        if seen-wanted:return label+' основного товара противоречит спецификации'
        if not wanted.issubset(seen):return label+' запрошенного исполнения не подтверждён основным товаром'
    return ''


_INTERFACE_NEGATIVE={'нет','без','отсутствует','отсутствующий','отсутствие','no','not','without','none','false','absent','unsupported','unavailable','disabled','0'}
_INTERFACE_OPTIONAL={'optional','option','опция','возможно','может','maybe','unknown','unclear','unspecified','неизвестно','неподтвержден'}
_INTERFACE_AFFIRMATIVE={'yes','true','present','available','supported','support','да','есть','поддерживается','поддерживает','предусмотрен','установлен','наличии','в'}
_INTERFACE_CONNECTIVE={'is','does','for','interface','интерфейс','интерфейса','поддержки','не','по','запросу','upon','request'}

def _interface_words(value):
    value=re.sub(r'RS[- ]?(\d{3})',r'rs\1',value,flags=re.I).casefold()
    return re.sub(r'[^\w]+',' ',value).split()

def _interface_value_status(value, interface):
    """Shared vocabulary for both declared values and adjacent title qualifiers."""
    words=set(_interface_words(value));clause=' '.join(_interface_words(value))
    if words&_INTERFACE_NEGATIVE or re.search(r'\bне\s+(?:поддерж\w*|предусмотр\w*|установ\w*|доступ\w*)',clause):return 'negative'
    if words&_INTERFACE_OPTIONAL or re.search(r'опциональн\w*|по\s+запросу|upon\s+request',clause):return 'unknown'
    if 'rs'+interface not in words:return 'unknown'
    if any(not re.fullmatch(r'rs\d{3}',word) and word not in _INTERFACE_AFFIRMATIVE for word in words):return 'unknown'
    return 'positive'


def _title_interface_status(title, match, interface):
    # Collect only adjacent qualifier words before the actual marker; product
    # names and other unrecognized nouns stop that prefix, rather than borrowing
    # an unrelated 'without' declaration elsewhere in the heading.
    before=re.split(r'[,;\n]',title[max(0,match.start()-80):match.start()])[-1]
    prefix=[];vocabulary=_INTERFACE_NEGATIVE|_INTERFACE_OPTIONAL|_INTERFACE_AFFIRMATIVE|_INTERFACE_CONNECTIVE
    for word in reversed(_interface_words(before)):
        if word not in vocabulary and not word.startswith('опциональн'):break
        prefix.insert(0,word)
    after=re.split(r'[,;\n]',title[match.end():match.end()+80])[0]
    return _interface_value_status(' '.join(prefix)+' RS'+interface+' '+after,interface)


def native_model_spec_reason(position, title, source_text):
    if native_request_role(position)!='model':return ''
    field=lambda k:position.get(k,'') if isinstance(position,dict) else getattr(position,k,'')
    requested=str(field('full_name') or field('name'))
    # A typed property owns one bounded value line. Consume that line with
    # its label: a standalone RS485 value must not become the next label and
    # borrow an unrelated following property's caption as its value.
    properties=list(re.finditer(
        r'(?im)^[ \t]*(Интерфейс|Interface|Порт|Port|Связь|Возможность[ \t]+передачи[ \t]+данных|RS[- ]?\d{3})'
        r'(?=[ \t:：\r\n]|$)[ \t]*[:：]?[ \t]*(?:\r?\n[ \t]*)*([^\r\n]+)(?=\r?$)',source_text))
    for interface in set(re.findall(r'(?<![A-Za-z0-9])RS[- ]?(\d{3})(?![A-Za-z0-9])',requested,re.I)):
        token=r'(?<![A-Za-z0-9])RS[- ]?'+re.escape(interface)+r'(?![A-Za-z0-9])'
        occurrences=list(re.finditer(token,title,re.I));affirmative=bool(occurrences)
        for match in occurrences:
            status=_title_interface_status(title,match,interface)
            if status=='negative':return 'Интерфейс основного товара явно отсутствует или не поддерживается'
            if status!='positive':return 'Интерфейс запрошенного исполнения не подтверждён основным товаром'
        for property in properties:
            label,value=property.groups()
            # A recognized but overlong value is unknown evidence, never an
            # absent property that an affirmative title may override.
            if len(value)>256:return 'Интерфейс запрошенного исполнения не подтверждён основным товаром'
            direct=re.fullmatch(r'RS[- ]?(\d{3})',label,re.I)
            if direct and direct[1]!=interface:continue
            value=(label+' '+value) if direct else value
            status=_interface_value_status(value,interface)
            if status=='negative':return 'Интерфейс основного товара явно отсутствует или не поддерживается'
            if status!='positive':return 'Интерфейс запрошенного исполнения не подтверждён основным товаром'
            affirmative=True
        if not affirmative:return 'Интерфейс запрошенного исполнения не подтверждён основным товаром'
    units={'а':'a','a':'a','в':'v','v':'v'}
    values=lambda text:{(m[0].replace(',','.'),units[m[1].lower()]) for m in re.findall(r'(?<![\w.,])(\d+(?:[.,]\d+)?)\s*([АаВвAVav])(?![\w])',text)}
    expected=values(requested);observed=values(title)
    for unit in {u for n,u in expected}:
        wanted={n for n,u in expected if u==unit};seen={n for n,u in observed if u==unit}
        if wanted and seen and wanted.isdisjoint(seen):return 'Электрические параметры основного товара противоречат запрошенному исполнению'
        if wanted and not wanted.issubset(seen):return 'Электрические параметры запрошенного исполнения не подтверждены основным товаром'
    return ''


def valid_identity(evidence, expected_mark=None, expected_name=None, host=None, *, request_position=None, source_text=None):
    """Recheck retained proof; booleans and a quote's URL alone are never proof."""
    if not isinstance(evidence, dict) or evidence.get('version') != 1:
        return False
    provenance=evidence.get('provenance')
    if provenance=='http_main_product':
        if type(evidence.get('version')) is not int or not valid_http_source(evidence):return False
    elif provenance!='rendered_main_card':return False
    wanted = str(evidence.get('expected_mark') or '')
    name = str(evidence.get('expected_name') or '')
    if not wanted or (expected_mark is not None and wanted != expected_mark):
        return False
    if expected_name is not None and name != expected_name:
        return False
    if request_position is not None:
        field=lambda k:request_position.get(k,'') if isinstance(request_position,dict) else getattr(request_position,k,'')
        if str(field('type_mark'))!=wanted or str(field('name'))!=name:return False
    stamp = evidence.get('observed_at')
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return False
    try:
        observed = float(stamp)
        if not math.isfinite(observed) or observed <= 0 or observed > time.time()+300:
            return False
    except (ValueError, TypeError):
        return False
    source = canonical_card_url(evidence.get('source_url'))
    actual = canonical_card_url(evidence.get('canonical_url'))
    if not source or not actual or card_host(source) != card_host(actual):
        return False
    supplier = evidence.get('host')
    if not supplier or (host is not None and host != supplier):
        return False
    if not (card_host(actual) == supplier or card_host(actual).endswith('.'+supplier)):
        return False
    from .agent import (_is_listing_url, _type_mark_match_status,
                        _conflicting_page_article, _conflicting_model_code_in_title,
                        _conflicting_numeric_article, _conflicting_full_model_code,
                        _conflicting_network_cable_spec, _different_explicit_article, _different_full_skus_in_url)
    for url in (source, actual):
        if _is_listing_url(url) or re.search(r'/(?:category|categories)(?:/|$)|/catalog/?$', urlsplit(url).path, re.I) or re.search(r'/(?:login|signin|auth|captcha|challenge|search|recommendations?)(?:[/?.]|$)', url, re.I):
            return False
    title = str(evidence.get('main_title') or '')
    article = str(evidence.get('main_article') or '')
    if not title.strip():
        return False
    if 'article_facts' in evidence:
        observed,valid=observed_identity_article(evidence['article_facts'])
        if not valid or observed!=article:return False
    if native_article_reason(request_position,title,article,mark=wanted,name=name):return False
    if source_text is not None and native_source_spec_reason(request_position,title,source_text,article,mark=wanted,name=name):return False
    native_binding=_native_constraints(request_position,mark=wanted,name=name)
    matched=permitted_article(request_position,article,original=native_binding['article']if native_binding else wanted)
    if matched and matched['match_basis']=='estimator_alternative' and not native_binding:
        policy=alternative_policy(request_position)
        legacy=legacy_article_request(request_position)if request_position is not None else policy.get('_legacy_article',False)
        if not legacy or wanted!=policy['original_article']:return False
    checked_article=(wanted if native_request_role(request_position)=='model' or native_binding and not native_binding['article'] else article if matched else native_binding['article']if native_binding else wanted)
    checks_mark=' / '.join(v for v in (native_binding['model'],checked_article) if v) if native_binding and native_binding['article'] else checked_article
    model_role=native_request_role(request_position)=='model' or request_position is None and _active_model_role(wanted,name) or bool(native_binding and native_binding['model'] and not native_binding['article'])
    if ((not model_role and _conflicting_page_article(checks_mark, article,request_role='unknown'))
        or _conflicting_model_code_in_title(checks_mark, title)
        or _conflicting_model_code_in_title(checks_mark, urlsplit(actual).path)
        or _conflicting_numeric_article(checks_mark, title)
        or _conflicting_full_model_code(checks_mark, title)
        or _conflicting_network_cable_spec(name, checks_mark, title)
        or _different_explicit_article(checks_mark, title, actual)
        or _type_mark_match_status(checks_mark, actual) is False
        or _different_full_skus_in_url(checks_mark, actual)):
        return False
    # The URL/model response cannot certify identity: require a main-card match.
    return _type_mark_match_status(checks_mark, title if model_role else title+' '+article) is True


def valid_http_source(evidence):
    from .http_card import read_source
    retained=read_source(evidence.get('http_source') or {})
    if not retained:return False
    record,observed=retained
    if not observed:return False
    source=evidence.get('http_source',{})
    if source.get('code_profile') is not None:
        if source.get('code_profile')!='owned_codes_v1' or source.get('identity_basis')!=observed.get('identity_basis') or source.get('code_observations')!=observed.get('code_observations'):return False
    elif observed.get('identity_basis')=='title_only':return False
    return bool(canonical_card_url(record.get('requested_url'))==canonical_card_url(evidence.get('source_url'))
        and canonical_card_url(record.get('url'))==canonical_card_url(evidence.get('canonical_url'))
        and type(record.get('observed_at')) in (int,float) and record['observed_at']==evidence.get('observed_at')
        and observed['title']==evidence.get('main_title') and observed['article']==evidence.get('main_article')
        and observed['scope_path']==evidence.get('http_source',{}).get('scope_path')
        and observed['scope_sha256']==evidence.get('http_source',{}).get('scope_sha256'))


def http_identity(record,observed,source,expected_mark,expected_name,host):
    if record.get('role')=='http_primary_dom':
        from .http_card import http_primary_source
        source=http_primary_source(source)
        if source is None:return {}
    evidence=dict(version=1,provenance='http_main_product',source_url=canonical_card_url(record['requested_url']),canonical_url=canonical_card_url(record['url']),host=host,expected_mark=expected_mark,expected_name=expected_name,main_title=observed['title'],main_article=observed['article'],observed_at=record['observed_at'],http_source={**source,'scope_path':observed['scope_path'],'scope_sha256':observed['scope_sha256'],'code_profile':'owned_codes_v1','identity_basis':observed['identity_basis'],'code_observations':observed['code_observations']})
    return evidence if valid_identity(evidence,expected_mark,expected_name,host) else {}


def rendered_identity(source_url, actual_url, main_title, main_article, expected_mark,
                      expected_name, host, *, observed_at=None):
    evidence = dict(version=1, provenance='rendered_main_card',
                    source_url=canonical_card_url(source_url),
                    canonical_url=canonical_card_url(actual_url), host=host,
                    expected_mark=expected_mark, expected_name=expected_name,
                    main_title=main_title, main_article=main_article, observed_at=time.time() if observed_at is None else observed_at)
    return evidence if valid_identity(evidence, expected_mark, expected_name, host) else {}


def main_card_article(text):
    """Bound printed article to the primary card, before related items."""
    primary = re.split(r'(?im)^\s*(?:Рекомендуем|Рекомендации|Похожие товары|С этим товаром|Сопутствующие товары|Вы смотрели)', text)[0]
    match = re.search(r'(?im)^\s*(?:Артикул(?: производителя| товара)?|арт\.|Код производителя)\s*[:：]?\s*\n?\s*([A-Za-zА-Яа-я0-9._-]{3,})(?=\s|$)', primary)
    return match.group(1) if match else ''


def capture_current_identity(page, path, source_url, expected_mark, expected_name,
                             host, anchor_title, on_refusal=None):
    """Independently observe current primary DOM and pixels, never price proof.

    A supplied purchase heading is an exact anchor. Without one, identity is
    validated independently against the requested SKU. A successful screenshot
    is followed by a fresh DOM and document/protection check before publication.
    """
    from pathlib import Path
    from .session import bounded_timeout
    from .agent import (_looks_like_bot_check, _http_response_is_blocked,
                        _conflicting_page_article, _type_mark_match_status)
    from ..price_capture import DOM_SNAPSHOT
    # Use the same visible/excluded DOM rules as purchase capture, but retain
    # identity when the main card has no price or an unqualified sale unit.
    script = DOM_SNAPSHOT

    diagnostics = {'budget_ms':0, 'budget_scope':'identity_stabilization', 'observations':[], 'outcome':'pending'}
    page._kac_identity_diagnostics = diagnostics
    started = time.monotonic()

    def observe(phase, begin):
        if len(diagnostics['observations']) < 18:
            diagnostics['observations'].append({'phase':phase,
                'elapsed_ms':round((time.monotonic()-begin)*1000,3),
                'since_start_ms':round((time.monotonic()-started)*1000,3)})

    def refuse(code):
        diagnostics['outcome'] = code
        diagnostics['elapsed_ms'] = round((time.monotonic()-started)*1000,3)
        if on_refusal:
            on_refusal(code)

    def snapshot():
        begin = time.monotonic()
        raw = page.evaluate(script,{'identity_only':True,'document_url':getattr(page,'_kac_document_url',None)})
        observe('identity_dom',begin)
        if not isinstance(raw, dict) or not raw.get('title'):
            refuse('identity_dom_unreadable')
            return None
        document = canonical_card_url(getattr(page, '_kac_document_url', ''))
        current = canonical_card_url(getattr(page, 'url', ''))
        actual = canonical_card_url(raw.get('url'))
        if raw.get('url') == 'about:blank' and not current:
            actual = document
        if (not actual or (current and actual != current) or (document and actual != document)
                or card_host(actual) != card_host(source_url)
                or _type_mark_match_status(expected_mark,actual) is False):
            if actual:
                refuse('redirect_unconfirmed')
            return {}
        text = str(raw.get('text') or '')
        title = str(raw.get('title') or '')
        begin = time.monotonic()
        body = page.inner_text('body')
        page_title = page.title()
        observe('identity_protection',begin)
        status = getattr(page, '_kac_response_status', None)
        if (_looks_like_bot_check(body, page_title)
                or _http_response_is_blocked(status, body)):
            refuse('captcha' if _looks_like_bot_check(body,page_title) else 'http_blocked')
            return {}
        facts=raw.get('article_facts')
        if 'article_facts' in raw:
            article,facts_valid=observed_identity_article(facts)
            if not facts_valid:
                refuse('article_mismatch')
                return {}
        else:article=main_card_article(text)  # Legacy adapters remain explicit; current DOM always supplies facts.
        if (anchor_title and title != anchor_title) or _conflicting_page_article(expected_mark,article):
            refuse('article_mismatch')
            return {}
        proof = rendered_identity(source_url, actual, title, article,
                                  expected_mark, expected_name, host)
        if proof and 'article_facts' in raw:
            proof['article_facts']=facts
            if not valid_identity(proof,expected_mark,expected_name,host):return {}
        # Observation date changes on every read, not the stability signature.
        return {k:v for k,v in proof.items() if k != 'observed_at'}

    budget = bounded_timeout(2)
    diagnostics['budget_ms'] = round(budget*1000,3)
    deadline = time.monotonic() + budget
    previous = None
    while time.monotonic() < deadline:
        stage = 'identity_dom_unreadable'
        try:
            current = snapshot()
            if current is None:
                return {}
            if time.monotonic() >= deadline:
                break
            if current and current == previous:
                path = Path(path)
                stage = 'identity_screenshot_failed'
                begin = time.monotonic()
                page.screenshot(path=str(path), full_page=True,
                                timeout=max(1,int(bounded_timeout(8)*1000)))
                observe('identity_screenshot',begin)
                stage = 'identity_post_capture_failed'
                if snapshot() != current or not path.is_file():
                    refuse('identity_post_capture_changed' if path.is_file() else 'identity_capture_file_missing')
                    return {}
                diagnostics['outcome'] = 'captured'
                diagnostics['elapsed_ms'] = round((time.monotonic()-started)*1000,3)
                import hashlib
                return {**current, 'observed_at':time.time(),
                        'identity_screenshot':path.name,
                        'identity_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
            previous = current or None
            page.wait_for_timeout(max(1,int(bounded_timeout(.3)*1000)))
        except ResearchControlError:
            diagnostics['outcome'] = 'control_interrupted'
            diagnostics['elapsed_ms'] = round((time.monotonic()-started)*1000,3)
            raise
        except Exception:
            refuse(stage)
            return {}
    refuse('identity_stability_timeout')
    return {}

def current_offer_article_evidence(offer,position):
    """Display from a current immutable server record, never client metadata."""
    from . import catalogue_projection as projection
    from .catalogue import identity_for_position
    field=lambda k,d=None:offer.get(k,d)if isinstance(offer,dict)else getattr(offer,k,d)
    reference=field('catalogue_ref',{})or {}
    store=projection._SCOPED_STORE.get()or projection._STORE
    if store is None or set(reference)!={'product_id','observation_id'}:return {}
    try:
        if identity_for_position(position).key!=reference['product_id']:return {}
        record=store.resolve_observation(reference['product_id'],reference['observation_id'])
        if not record or not record['current']:return {}
        return article_match_evidence(record['observation'],position)
    except (KeyError,ValueError,TypeError):return {}
