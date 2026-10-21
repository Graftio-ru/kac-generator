"""Observed supplier-owned public discovery; no price or coverage authority."""
from dataclasses import dataclass,asdict
import hashlib,re,time
from urllib.parse import urlencode,urljoin,urlsplit
from .article_query_plan import QueryStep,build_article_query_plan
from .product_identity import canonical_card_url,card_host
from .http_card import MainParser,visible
from .session import bounded_timeout,deadline_scope,supplier_transport,transport_observed,http_outcome,SupplierDeferred
from .failure_policy import ResearchControlError

VERSION='supplier_own_v3'

@dataclass(frozen=True)
class ElementRule:
 tag:str=''
 classes:tuple[str,...]=()
 identifier:str=''
 def matches(self,node):
  return (not self.tag or node.tag==self.tag) and all(c in node.attrs.get('class','').split() for c in self.classes) and (not self.identifier or node.attrs.get('id')==self.identifier)

@dataclass(frozen=True)
class ProductLinksProfile:
 owner:ElementRule
 regions:tuple[ElementRule,...]
 anchor:ElementRule=ElementRule('a')
 anchor_parents:tuple[ElementRule,...]=()
 code_regions:tuple[ElementRule,...]=()
 path_prefix:str='/product/'
 min_segments:int=2

@dataclass(frozen=True)
class SupplierProvider:
 host:str
 endpoint:str
 parameter:str
 parser:str
 links:ProductLinksProfile

R=ElementRule
PROVIDERS={
 'electro-mpo.ru':SupplierProvider('electro-mpo.ru','https://www.electro-mpo.ru/search/','q','mpo_product_links_v2',ProductLinksProfile(R('section',('s-products__item',)),(R('div',('search__content-list',)),),anchor_parents=(R('div',('s-products__item__text',)),),path_prefix='/catalog/',min_segments=4)),
 'chipdip.ru':SupplierProvider('chipdip.ru','https://www.chipdip.ru/search','searchtext','chipdip_product_links_v1',ProductLinksProfile(R('tr',('with-hover',)),(R('table',identifier='itemlist'),R('div',identifier='search_results')),anchor_parents=(R('div',('name',)),R('td',('h_name',))))),
 'elektro.ru':SupplierProvider('elektro.ru','https://www.elektro.ru/search/','search','elektro_product_links_v1',ProductLinksProfile(R('div',('product',)),(R('div',identifier='product_list'),R('div',('search__container',))),anchor_parents=(R('div',('name',)),R('div',('head',))),code_regions=(R('div',('article_row',)),))),
 'ie-moscow.ru':SupplierProvider('ie-moscow.ru','https://www.ie-moscow.ru/search','q','ie_moscow_product_links_v1',ProductLinksProfile(R('div',('product',)),(R('div',identifier='products-pjax'),R('div',('search-page',))),anchor=R('a',('product-name',)),min_segments=3)),
 'favorit-el.ru':SupplierProvider('favorit-el.ru','https://favorit-el.ru/search/','q','favorit_product_links_v1',ProductLinksProfile(R('div',('itemRow','item')),(R('div',identifier='catalogTableList'),),anchor=R('a',('name',)),code_regions=(R('div',('article',)),),path_prefix='/catalog/')),
 'minimaks.ru':SupplierProvider('minimaks.ru','https://www.minimaks.ru/catalog/','q','minimaks_product_links_v1',ProductLinksProfile(R('div',('catalog-list-item',)),(R('div',('js-catalog-container','catalog-list__container')),),anchor=R('a',('title-list-item',)))),
 'olmi-connect.ru':SupplierProvider('olmi-connect.ru','https://olmi-connect.ru/poisk/','searchword','olmi_product_links_v1',ProductLinksProfile(R('div',('products-js','product')),(R('div',('js-search-grid','products__grid_search')),),anchor_parents=(R('div',('product__name',)),),path_prefix='/shop/',min_segments=3)),
}

def discovery_intent_key():
 from ..browser import supplier_http_configuration_hash
 return VERSION,supplier_http_configuration_hash()


def trusted_query_step(position,query):
 """Mirror the existing source-cell binding; never extract a SKU from query."""
 evidence=position.source_evidence or {};cells=evidence.get('cells',{});clean=lambda x:' '.join(str(x or '').split())
 native=(evidence.get('reader')=='native_pdf' and isinstance(cells,dict) and clean(cells.get('name'))==clean(position.name) and clean(cells.get('unit'))==clean(position.unit) and clean(' / '.join(dict.fromkeys(clean(cells.get(k)) for k in ('model','article') if clean(cells.get(k)))))==clean(position.type_mark) and not any(k in (position.review_fields or {}) for k in ('name','type_mark','model','article','maker')))
 plan=build_article_query_plan(position.name,clean(cells.get('model')) if native else position.type_mark,article=clean(cells.get('article')) if native else '',manufacturer=clean(cells.get('maker')) if native else '',unit=position.unit)
 return next((step for step in plan.steps if step.query==query),None)

def valid_step(step,query):
 return type(step) is QueryStep and step.query==query and step.kind in {'exact_article','exact_model','identity_context'} and type(step.identity_hint) is str and 0<len(step.identity_hint)<=256 and not any(ord(c)<32 for c in step.identity_hint)

def _ancestor(node,rule,stop=None):
 while node is not None and node is not stop:
  if rule.matches(node):return node
  node=node.parent
 return None

def _chain(node,rules,stop=None):
 for rule in rules:
  node=_ancestor(node,rule,stop)
  if node is None:return False
  node=node.parent
 return True

def _public(node):
 while node is not None:
  if node.tag in {'form','header'}:return False
  node=node.parent
 return True

def _owned_code(node,owner,profile):
 # A code cell cannot borrow nested/neighbor product facts or input values.
 if node is not owner and profile.owner.matches(node):return ''
 if not visible(node):return ''
 return ''.join(_owned_code(part,owner,profile) if hasattr(part,'tag') else part for part in node.text)

def _discovery_key(value):
 from ..source_codes import discovery_script_alias
 value=re.sub(r'(?<=\d)[х×](?=\d)','x',value,flags=re.I)
 value=value.translate(str.maketrans({c:'-' for c in '‐‑‒–—−'}))
 return ''.join(discovery_script_alias(value).casefold().split())

def _rank(hint,label,codes):
 texts=[label]+codes
 if any(hint.casefold() in text.casefold() for text in texts):return 0
 key=_discovery_key(hint)
 return 1 if key and any(key in _discovery_key(text) for text in texts) else 2

@dataclass(frozen=True)
class CandidateHint:
 url:str
 title:str
 manufacturer_article:str
 brand:str
 supplier_codes:tuple[str,...]
 rank:int
 source_role:str
 source_url:str
 source_sha256:str
 observed_at:float


def _identity_labels(owner,profile):
 # Positive leaf/local label only; whole owner text cannot invent a maker article.
 articles=[];brands=[];codes=[]
 for field in owner.descendants():
  if _ancestor(field,profile.owner) is not owner or not visible(field):continue
  text=' '.join(_owned_code(field,owner,profile).split())
  if not text or len(text)>256:continue
  article=re.fullmatch(r'(?:артикул\s+производителя|manufacturer\s+(?:article|part\s+number)|mpn)\s*[:：]\s*([A-Za-zА-Яа-я0-9._-]{3,128})',text,re.I)
  brand=re.fullmatch(r'(?:производитель|бренд|manufacturer|brand)\s*[:：]\s*([A-Za-zА-Яа-я0-9][A-Za-zА-Яа-я0-9 ._-]{1,127})',text,re.I)
  if article:articles.append(article.group(1))
  if brand:brands.append(brand.group(1).strip())
  if 'brand' in field.attrs.get('itemprop','').split() and field.tag not in {'meta','script'}:brands.append(text)
  if any(rule.matches(field) for rule in profile.code_regions):codes.append(text)
 articles=list(dict.fromkeys(articles));brands=list(dict.fromkeys(brands))
 return articles[0] if len(articles)==1 else '',brands[0] if len(brands)==1 else '',tuple(dict.fromkeys(codes))


def candidate_hint_reason(value,url,host,position,manufacturer='',article=''):
 """Discovery can reject explicit contradictions, never admit a price or equivalence."""
 if type(value) is not dict or set(value)!=set(CandidateHint.__dataclass_fields__):return ''
 if any(type(value.get(key)) is not str for key in ('url','source_url','source_sha256','title','manufacturer_article','brand','source_role')):return ''
 if value.get('source_role')!='search_result_owner' or value.get('url')!=url or card_host(url)!=host:return ''
 provider=PROVIDERS.get(host)
 if provider is None or card_host(value.get('source_url',''))!=host or urlsplit(value['source_url']).path!=urlsplit(provider.endpoint).path:return ''
 if type(value.get('source_sha256')) is not str or not re.fullmatch(r'[a-f0-9]{64}',value['source_sha256']):return ''
 if type(value.get('observed_at')) not in (float,int) or not 0<value['observed_at']<=time.time():return ''
 if any(type(value.get(key)) is not str or len(value[key])>4096 for key in ('title','manufacturer_article','brand')):return ''
 from .agent import (_conflicting_page_article,_conflicting_numeric_article,_conflicting_full_model_code,_conflicting_model_code_in_title,_conflicting_network_cable_spec)
 title=value['title'];mark=position.type_mark
 if value['manufacturer_article'] and _conflicting_page_article(article or mark,value['manufacturer_article']):return 'discovery_article_conflict'
 if any((_conflicting_numeric_article(mark,title),_conflicting_full_model_code(mark,title),_conflicting_model_code_in_title(mark,title),_conflicting_network_cable_spec(position.name,mark,title))):return 'discovery_identity_conflict'
 if manufacturer and value['brand']:
  from .manufacturer_aliases import manufacturer_equal
  if not manufacturer_equal(manufacturer,value['brand']):return 'discovery_manufacturer_conflict'
 return ''


def parse_candidate_hints(text,endpoint,hint,provider=None):
 provider=provider or PROVIDERS.get(card_host(endpoint))
 if provider is None:return {}
 tree=MainParser(text);profile=provider.links;found={};stamp=time.time();digest=hashlib.sha256(text.encode('utf-8')).hexdigest()
 for index,node in enumerate(tree.root.descendants()):
  if index%256==0:bounded_timeout(1)
  if not profile.anchor.matches(node) or not visible(node) or not _public(node):continue
  owner=_ancestor(node.parent,profile.owner)
  if owner is None or not visible(owner) or not _chain(owner.parent,profile.regions):continue
  if not _chain(node.parent,profile.anchor_parents,owner):continue
  label=' '.join(node.value().split())
  if not label:continue
  raw=node.attrs.get('href','');target=canonical_card_url(urljoin(endpoint,raw));parts=urlsplit(target)
  if not target or card_host(target)!=provider.host or parts.query or not parts.path.startswith(profile.path_prefix) or len([p for p in parts.path.split('/') if p])<profile.min_segments:continue
  article,brand,codes=_identity_labels(owner,profile)
  rank=_rank(hint,label,list(codes))
  value=CandidateHint(target,label[:4096],article,brand,codes,rank,'search_result_owner' if len(label)<=4096 else 'ambiguous_result_owner',endpoint,digest,stamp)
  if target not in found:found[target]=value
  elif (found[target].title,found[target].manufacturer_article,found[target].brand)!=(value.title,value.manufacturer_article,value.brand):
   previous=found[target];found[target]=CandidateHint(target,'','','',tuple(dict.fromkeys(previous.supplier_codes+codes)),min(previous.rank,rank),'ambiguous_result_owner',endpoint,digest,stamp)
 # No result-tail cap: received page/parser bounds already limit memory.
 if found:return dict(sorted(found.items(),key=lambda item:item[1].rank))
 if re.search(r'(?:captcha|verify you are human|подтвердите.{0,30}человек|проверка пользователя)',tree.root.value(),re.I):raise ValueError('observed_protection')
 return {}


def parse_candidates(text,endpoint,hint,provider=None):
 return list(parse_candidate_hints(text,endpoint,hint,provider))


def _discover_once(provider,step,*,deadline=None):
 """One bounded ordinary supplier HTTP lease; only delivered links are returned."""
 from ..browser import supplier_http_get
 endpoint=provider.endpoint+'?'+urlencode({provider.parameter:step.identity_hint});started=time.time()
 observation={'origin':'supplier_own_search','window':'unknown','observed_at':None,'raw_count':None,'limit':None,'own_http_calls':0,'serp_wire_calls':0,'diagnostics':[],'provider_profile':provider.parser,'provider_version':VERSION,'discovery_complete':False}
 try:
  with deadline_scope(deadline),supplier_transport(endpoint):
   bounded_timeout(1);observation['own_http_calls']=1
   response=supplier_http_get(endpoint,max_bytes=2*1024*1024)
   bounded_timeout(1)
   if card_host(str(response.url))!=provider.host:raise ValueError('redirect_unbound')
   transport_observed(http_outcome(response.status_code));response.raise_for_status()
   if 'html' not in response.headers.get('content-type','').lower():raise ValueError('unsupported_content_type')
   try:
    hints=parse_candidate_hints(response.text,str(response.url),step.identity_hint,provider);urls=list(hints)
   except ValueError as error:
    if error.args==('observed_protection',):
     from .supplier_health_store import Outcome
     transport_observed(Outcome.BOT_PROTECTION)
    raise
   observation['candidate_urls']=list(urls);observation['candidate_hints']={url:asdict(value) for url,value in hints.items()}
   observation.update(observed_at=time.time(),raw_count=len(urls),http_status=response.status_code)
   if not urls:observation['diagnostics']=[{'code':'supplier_discovery_unresolved','reason':'Собственный поиск не предоставил подтверждённые ссылки; наличие неизвестно','url':provider.endpoint,'observed_at':observation['observed_at']}]
   return urls,observation
 except SupplierDeferred as error:
  from .catalogue import safe_deferred_check,supplier_defer_reason
  check=safe_deferred_check(error.check,provider.host)
  diagnostic=dict(code='supplier_deferred',reason=supplier_defer_reason(check),failure_source='supplier_transport',failure_scope='site',failure_cause=check['reason_code'],url=provider.endpoint)
  if check['failure_code']:diagnostic['transport_error_code']=check['failure_code']
  observation.update(deferred_check=check,diagnostics=[diagnostic],observed_at=None)
  return [],observation
 except ResearchControlError:raise
 except Exception as error:
  context=getattr(error,'_kac_supplier_failure',None)
  if getattr(context,'scope',None)=='global':raise
  diagnostic={'code':'supplier_discovery_failed','reason':'Собственный поиск поставщика не подтверждён; используется обычная выдача при доступном бюджете','url':provider.endpoint,'observed_at':time.time(),'error_type':type(error).__name__}
  if isinstance(error,ValueError) and error.args==('observed_protection',):diagnostic.update(failure_source='supplier_discovery',failure_scope='site',failure_cause='bot_protection')
  if context is not None:diagnostic.update(context.diagnostic(diagnostic['reason'],code='supplier_discovery_failed'))
  observation['diagnostics']=[diagnostic];observation['observed_at']=diagnostic['observed_at'];return [],observation


def _supplier_admission_wait(seconds):
 from threading import Event
 Event().wait(seconds)


def discover(provider,step,*,deadline=None,wait_for_supplier=True):
 """Historical contention queue; indexed scheduler requests one admission only."""
 if type(wait_for_supplier) is not bool:raise ValueError('Explicit supplier wait boolean required')
 contention_deadline=min(time.monotonic()+120,deadline) if deadline is not None else time.monotonic()+120
 while True:
  bounded_timeout(1)
  result,observation=_discover_once(provider,step,deadline=deadline)
  check=observation.get('deferred_check')
  if not wait_for_supplier or not check or check['reason_code']!='supplier_probe_in_progress' or time.monotonic()>=contention_deadline:return result,observation
  _supplier_admission_wait(min(1,contention_deadline-time.monotonic(),bounded_timeout(1)))
