"""Operator-owned immutable technical references; never supplier cash authority."""
from pathlib import Path
import hashlib,json,math,os,re,time
from urllib.parse import urlsplit
from ..config import CACHE_DIR

MAX_BYTES=8*1024*1024
class FactoryEvidenceError(ValueError):pass

def _json(v):return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def _hash(v):return hashlib.sha256(v).hexdigest()
def _root():
 root=CACHE_DIR/'factory_references';root.mkdir(parents=True,exist_ok=True)
 if root.is_symlink():raise FactoryEvidenceError('factory_namespace_invalid')
 return root

def _url(url,origin=False):
 if type(url) is not str or re.search(r'[\\\x00-\x20\x7f]',url):raise FactoryEvidenceError('factory_url_invalid')
 p=urlsplit(url)
 if p.scheme!='https' or not p.hostname or p.username or p.password or p.port not in (None,443) or p.fragment:raise FactoryEvidenceError('factory_url_invalid')
 value='https://'+p.hostname.lower()
 if origin and url.rstrip('/')!=value:raise FactoryEvidenceError('factory_origin_invalid')
 return value

def _read(path,limit):
 if path.is_symlink() or path.stat().st_size>limit:raise FactoryEvidenceError('factory_record_invalid')
 return path.read_bytes()

def _write_immutable(path,raw):
 if path.exists():
  if _read(path,max(MAX_BYTES,len(raw)))!=raw:raise FactoryEvidenceError('factory_immutable_conflict')
  return
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'wb') as stream:stream.write(raw)

def _replace(path,value):
 if path.is_symlink():raise FactoryEvidenceError('factory_index_invalid')
 tmp=path.with_suffix('.new');fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 try:
  with os.fdopen(fd,'w') as out:out.write(_json(value))
  os.replace(tmp,path)
 finally:
  if tmp.exists():tmp.unlink()

def approve_origin(manufacturer,origin,approval_id,revision=1,active=True):
 """Internal operator entrypoint. No HTTP/model caller may register authority."""
 origin=_url(origin,True)
 if type(manufacturer) is not str or not manufacturer.strip() or len(manufacturer)>128 or type(approval_id) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}',approval_id) or type(revision) is not int or revision<1 or type(active) is not bool:raise FactoryEvidenceError('factory_authority_invalid')
 path=_root()/'origins.json';data=json.loads(_read(path,65536)) if path.exists() else {}
 key=manufacturer.casefold()+'|'+origin;old=data.get(key)
 if old and (revision<old['revision'] or revision==old['revision'] and old!={'manufacturer':manufacturer,'origin':origin,'approval_id':approval_id,'revision':revision,'active':active}):raise FactoryEvidenceError('factory_authority_revision')
 data[key]=dict(manufacturer=manufacturer,origin=origin,approval_id=approval_id,revision=revision,active=active);_replace(path,data)
 return data[key]

def _authority(manufacturer,url):
 origin=_url(url);path=_root()/'origins.json'
 if not path.exists():raise FactoryEvidenceError('factory_origin_missing')
 item=json.loads(_read(path,65536)).get(manufacturer.casefold()+'|'+origin)
 if not item or item['active'] is not True:raise FactoryEvidenceError('factory_origin_inactive')
 return item

from functools import lru_cache

@lru_cache(maxsize=4)
def _technical_facts(body,article,url,parser_revision='factory-owned-technical-v1'):
 from .http_card import MainParser,Node,_semantic_visible as visible,_semantic_text,_factory_features,_factory_prose_blocks
 # Same neutral node model with a separate 8MiB/100k-node technical bound.
 class TechnicalParser(MainParser):
  def handle_starttag(self,tag,attrs):
   self.count+=1
   if self.count%256==0:
    from .session import bounded_timeout
    bounded_timeout(1)
   if self.count>100000 or len(self.stack)>128:raise FactoryEvidenceError('factory_structure_invalid')
   node=Node(tag,{k:v or '' for k,v in attrs},self.stack[-1],start=self.source_offset());self.stack[-1].children.append(node);self.stack[-1].text.append(node)
   if tag not in {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}:self.stack.append(node)
   else:node.end=node.start+len(self.get_starttag_text())
 parser=TechnicalParser(body.decode('utf-8'),False);nodes=list(parser.root.descendants());headings=[n for n in nodes if n.tag=='h1' and visible(n)]
 if len(headings)!=1:raise FactoryEvidenceError('factory_owner_ambiguous')
 owner=headings[0].parent
 code=re.compile(r'(?:^|\n)\s*(?:Код|Артикул|MPN|Manufacturer code)\s*[:：]?\s*\n?\s*([A-Za-zА-Яа-я0-9._/-]{1,128})(?=\s|$)',re.I)
 while owner is not None and owner.tag not in {'body','html','root'}:
  text=_semantic_text(owner)
  if set(code.findall(text))=={article} and re.search(r'в комплект',text,re.I) and re.search(r'В упаковке\s*[:：]?\s*\n?\s*1\s*уп',text,re.I):break
  owner=owner.parent
 if owner is None or owner.tag in {'body','html','root'}:raise FactoryEvidenceError('factory_owner_missing')
 text=_semantic_text(owner).strip()
 if len(text)>200000:raise FactoryEvidenceError('factory_owner_oversize')
 # One exact factory Product record binds a separate visible description to
 # the primary H1/code owner; description metadata alone is insufficient.
 products=[]
 for node in nodes:
  if node.tag=='script' and node.attrs.get('type')=='application/ld+json':
   try:value=json.loads(''.join(v for v in node.text if isinstance(v,str)))
   except ValueError:raise FactoryEvidenceError('factory_metadata_invalid')
   if type(value) is dict and value.get('@type')=='Product':products.append(value)
 title=headings[0].value().strip()
 if len(products)!=1 or products[0].get('sku')!=article or products[0].get('name')!=title or products[0].get('url')!=url:raise FactoryEvidenceError('factory_product_binding_invalid')
 description=products[0].get('description')
 if type(description) is not str or not description or len(description)>20000:raise FactoryEvidenceError('factory_description_invalid')
 norm=lambda value:re.sub(r'\s+',' ',value).strip()
 described=[n for n in nodes if n.tag in {'div','p','section'} and visible(n) and norm(_semantic_text(n))==norm(description)]
 described=[n for n in described if not any(other is not n and other in n.descendants() for other in described)]
 if len(described)!=1:raise FactoryEvidenceError('factory_description_not_visible')
 segment=described[0]
 context=owner
 while context is not None and segment not in context.descendants():context=context.parent
 if context is None or context.tag in {'body','html','root'} or len(_semantic_text(context))>200000:raise FactoryEvidenceError('factory_claim_scope_invalid')
 return dict(identity_text=text,claim_scope_path=context.path(),prose_blocks=_factory_prose_blocks(context),features=_factory_features(context),title=title,article=article,text=text+'\n'+_semantic_text(segment).strip(),description=_semantic_text(segment).strip(),scope_path=owner.path(),scope_sha256=_hash(parser.source[owner.start:owner.end].encode()),description_scope_path=segment.path(),description_scope_sha256=_hash(parser.source[segment.start:segment.end].encode()),claim_context=_semantic_text(context).strip(),claim_scope_sha256=_hash(parser.source[context.start:context.end].encode()))

def import_reference(body,*,url,manufacturer,article,observed_at,valid_until):
 if type(body) is not bytes or not 0<len(body)<=MAX_BYTES:raise FactoryEvidenceError('factory_body_invalid')
 authority=_authority(manufacturer,url)
 if type(article) is not str or not re.fullmatch(r'[A-Za-zА-Яа-я0-9._/-]{1,128}',article):raise FactoryEvidenceError('factory_article_invalid')
 if not all(type(v) in (int,float) and math.isfinite(v) for v in (observed_at,valid_until)) or not 0<observed_at<=time.time()+300 or not observed_at<valid_until<=observed_at+86400:raise FactoryEvidenceError('factory_time_invalid')
 root=_root();digest=_hash(body);bodypath=root/(digest+'.html');facts=_technical_facts(body,article,url)
 # Reimporting the identical old body cannot mint a newer observation epoch.
 for path in root.glob('*.json'):
  if re.fullmatch(r'[0-9a-f]{64}\.json',path.name):
   old=json.loads(_read(path,1024*1024))
   if old['body_sha256']==digest and old['observed_at']!=observed_at:raise FactoryEvidenceError('factory_observation_refresh_forbidden')
 record=dict(version=1,role='approved_factory_technical',authority=authority,url=url,manufacturer=manufacturer,article=article,observed_at=observed_at,valid_until=valid_until,body_sha256=digest,facts=facts)
 raw=_json(record).encode();ref=dict(record_sha256=_hash(raw),body_sha256=digest)
 _write_immutable(bodypath,body);_write_immutable(root/(ref['record_sha256']+'.json'),raw)
 path=root/'pending.json';pending=json.loads(_read(path,65536)) if path.exists() else {};pending[manufacturer.casefold()+'|'+article]=ref;_replace(path,pending)
 return ref

def read_pending(reference):
 from .session import bounded_timeout
 bounded_timeout(1)
 if type(reference) is not dict or set(reference)!={'record_sha256','body_sha256'} or any(type(v) is not str or not re.fullmatch('[0-9a-f]{64}',v) for v in reference.values()):raise FactoryEvidenceError('factory_reference_invalid')
 root=_root();raw=_read(root/(reference['record_sha256']+'.json'),1024*1024)
 if _hash(raw)!=reference['record_sha256']:raise FactoryEvidenceError('factory_record_changed')
 record=json.loads(raw);body=_read(root/(reference['body_sha256']+'.html'),MAX_BYTES)
 if _hash(body)!=record['body_sha256'] or record['body_sha256']!=reference['body_sha256'] or record['role']!='approved_factory_technical':raise FactoryEvidenceError('factory_body_changed')
 if _authority(record['manufacturer'],record['url'])!=record['authority']:raise FactoryEvidenceError('factory_authority_changed')
 if not 0<record['observed_at']<=time.time()+300 or not time.time()<=record['valid_until']<=record['observed_at']+86400:raise FactoryEvidenceError('factory_reference_expired')
 if _technical_facts(body,record['article'],record['url'])!=record['facts']:raise FactoryEvidenceError('factory_facts_changed')
 return record

def review_bindings(reference):
 """Source-derived inspection data only: this never activates a reference."""
 record=read_pending(reference);facts=record['facts']
 def excerpt(path,scope,text):return dict(scope_path=path,scope_sha256=scope,text=text,text_sha256=_hash(text.encode()))
 assertions=[v.strip() for v in re.split(r'[.!?\n]',facts['description']) if re.search(r'в комплект(?:е| уже)?\s+(?:уже\s+)?вход',v,re.I)]
 if len(assertions)!=1 or not _components(assertions[0]):raise FactoryEvidenceError('factory_assembly_excerpt_unknown')
 packing=re.search(r'(?:^|\n)\s*В упаковке\s*[:：]?\s*\n?\s*1\s*уп\.?',facts['identity_text'],re.I)
 if not packing:raise FactoryEvidenceError('factory_packing_excerpt_unknown')
 return {**{k:record[k] for k in ['authority','manufacturer','article','url','observed_at','valid_until']},'reference':reference,'excerpts':dict(
  identity=excerpt(facts['scope_path'],facts['scope_sha256'],facts['identity_text']),
  assembly=excerpt(facts['description_scope_path'],facts['description_scope_sha256'],assertions[0]),
  packing=excerpt(facts['scope_path'],facts['scope_sha256'],packing[0].strip()),
  description=excerpt(facts['description_scope_path'],facts['description_scope_sha256'],facts['description']),
  context=dict(scope_path=facts['claim_scope_path'],scope_sha256=facts['claim_scope_sha256']))}


def _review_validated(reference,review):
 fields={'version','role','review_revision','reference','authority','manufacturer','article','url','observed_at','valid_until','premise','excerpts','provenance'}
 if type(review) is not dict or set(review)!=fields or type(review['version']) is not int or review['version']!=1 or review['role']!='independent_factory_technical_review' or type(review['review_revision']) is not int or review['review_revision']<1 or review['premise']!='whole_factory_sku_complete_assembly':raise FactoryEvidenceError('factory_review_shape_invalid')
 binding=review_bindings(reference)
 if any(_json(review[k])!=_json(v) for k,v in binding.items()):raise FactoryEvidenceError('factory_review_source_changed')
 provenance=review['provenance']
 if type(provenance) is not dict or set(provenance)!={'review_entry_id','evidence_artifact','evidence_sha256','reviewed_at'} or type(provenance['review_entry_id']) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',provenance['review_entry_id']) or type(provenance['evidence_artifact']) is not str or not 1<=len(provenance['evidence_artifact'])<=1024 or re.search(r'[\x00-\x1f\x7f]',provenance['evidence_artifact']) or type(provenance['evidence_sha256']) is not str or not re.fullmatch(r'[0-9a-f]{64}',provenance['evidence_sha256']):raise FactoryEvidenceError('factory_review_provenance_invalid')
 stamp=provenance['reviewed_at']
 if type(stamp) not in (int,float) or not math.isfinite(stamp) or not review['observed_at']<=stamp<=min(time.time()+300,review['valid_until']):raise FactoryEvidenceError('factory_review_time_invalid')
 # Packing/assembly excerpts are parser-derived, not operator arithmetic.
 if not _components(review['excerpts']['assembly']['text']):raise FactoryEvidenceError('factory_review_components_unknown')
 return binding


def activate_reference(reference,review,evidence):
 """Separate operator-only curation boundary; import never calls this."""
 binding=_review_validated(reference,review)
 if type(evidence) is not bytes or not 0<len(evidence)<=1024*1024 or _hash(evidence)!=review['provenance']['evidence_sha256']:raise FactoryEvidenceError('factory_review_evidence_changed')
 root=_root();key=binding['manufacturer'].casefold()+'|'+binding['article']
 pending=json.loads(_read(root/'pending.json',65536))
 if pending.get(key)!=reference:raise FactoryEvidenceError('factory_pending_superseded')
 path=root/'active.json';active=json.loads(_read(path,65536)) if path.exists() else {};old=active.get(key)
 raw=_json(review).encode();digest=_hash(raw)
 if old and (review['review_revision']<old['revision'] or review['review_revision']==old['revision'] and old.get('review_sha256')!=digest):raise FactoryEvidenceError('factory_review_revision')
 _write_immutable(root/(review['provenance']['evidence_sha256']+'.review-evidence'),evidence)
 _write_immutable(root/(digest+'.review.json'),raw)
 active[key]=dict(reference=reference,review_sha256=digest,revision=review['review_revision'],status='active');_replace(path,active)
 return active[key]


def revoke_reference(reference,*,revision):
 record=read_pending(reference);path=_root()/'active.json';active=json.loads(_read(path,65536)) if path.exists() else {};key=record['manufacturer'].casefold()+'|'+record['article'];old=active.get(key)
 if type(revision) is not int or not old or old['reference']!=reference or revision<=old['revision']:raise FactoryEvidenceError('factory_review_revision')
 active[key]={**old,'revision':revision,'status':'inactive'};_replace(path,active)


def read_reference(reference):
 record=read_pending(reference);root=_root();key=record['manufacturer'].casefold()+'|'+record['article']
 pending=json.loads(_read(root/'pending.json',65536));path=root/'active.json';active=json.loads(_read(path,65536)) if path.exists() else {};item=active.get(key)
 if pending.get(key)!=reference or type(item) is not dict or item.get('reference')!=reference or item.get('status')!='active':raise FactoryEvidenceError('factory_reference_not_reviewed')
 if set(item)!={'reference','review_sha256','revision','status'} or type(item['revision']) is not int or item['revision']<1 or type(item['review_sha256']) is not str or not re.fullmatch(r'[0-9a-f]{64}',item['review_sha256']):raise FactoryEvidenceError('factory_activation_invalid')
 digest=item['review_sha256'];raw=_read(root/(digest+'.review.json'),1024*1024)
 if _hash(raw)!=digest:raise FactoryEvidenceError('factory_review_changed')
 review=json.loads(raw);_review_validated(reference,review)
 evidence=_read(root/(review['provenance']['evidence_sha256']+'.review-evidence'),1024*1024)
 if _hash(evidence)!=review['provenance']['evidence_sha256'] or item['revision']!=review['review_revision']:raise FactoryEvidenceError('factory_review_evidence_changed')
 return {**record,'review':dict(receipt_sha256=digest,revision=review['review_revision'],provenance=review['provenance'])}


def lookup_reference(manufacturer,article):
 path=_root()/'pending.json'
 if not path.exists():return None
 ref=json.loads(_read(path,65536)).get(manufacturer.casefold()+'|'+article)
 if ref:read_reference(ref)
 return ref

# Request-bound inference is installed only by native acquisition entrypoints.
from contextvars import ContextVar
from contextlib import contextmanager
_REQUEST=ContextVar('factory_kit_native_request',default=None)

def request_key(position):
 from .catalogue import identity_for_position
 from .product_identity import native_request_role
 field=lambda k,d='':position.get(k,d) if isinstance(position,dict) else getattr(position,k,d)
 if native_request_role(position)!='article':raise FactoryEvidenceError('factory_request_identity_unknown')
 identity=identity_for_position(position)
 from ..price_capture import _unit
 if _unit(field('unit'))!='компл':raise FactoryEvidenceError('factory_request_not_kit')
 return dict(name=field('name'),mark=field('type_mark'),unit='компл',manufacturer=identity.manufacturer,article=identity.code)

@contextmanager
def request_scope(position):
 token=_REQUEST.set(position)
 try:yield
 finally:_REQUEST.reset(token)

def _bound_request(position):
 try:return request_key(position)
 except FactoryEvidenceError:
  active=_REQUEST.get()
  if active is None:raise
  field=lambda p,k:p.get(k,'') if isinstance(p,dict) else getattr(p,k,'')
  from ..price_capture import _unit
  if any(field(position,k)!=field(active,k) for k in ['name','type_mark']) or _unit(field(position,'unit'))!=_unit(field(active,'unit')):raise FactoryEvidenceError('factory_request_scope_changed')
  return request_key(active)

def active_request():return _REQUEST.get()
def _norm(value):return re.sub(r'\s+',' ',value).strip().casefold().replace('ё','е')
def _components(text):
 found=set()
 if re.search(r'креп[её]ж|метиз',text,re.I):found.add('fasteners')
 if re.search(r'соединительн\w*\s+пластин',text,re.I):found.add('connection_plates')
 return found

def _coating(title):
 for value,pattern in [('zinc_lamellar',r'цинк[- ]ламел'),('hot_zinc',r'горяч\w*\s*оцинк|горячеоцинк'),('zinc',r'оцинк')]:
  if re.search(pattern,title,re.I):return value
 return ''

def _assembly_claims(text,title,description,features=(),prose_blocks=()):
 """Bounded semantic grammar for supported assembly assertions on BOTH sources.

 This is not an arbitrary prose approval: unknown relevant composition,
 omission, split/repack or package-count assertions remain review. Navigation
 and unrelated technical/stock fields supply no assembly/count authority.
 """
 # The exact incidental factory description is independently reviewed DATA.
 # An arbitrary imported description has no active review and cannot reach here.
 accepted={_norm(title),_norm(description)}
 for block in prose_blocks:
  if _norm(block).rstrip('.') not in {v.rstrip('.') for v in accepted}:raise FactoryEvidenceError('factory_untyped_owned_assertion')
 relevant=r'комплект|упаков|фасов|содерж|перепак|частич|неполн|креплен|креп[её]ж|метиз|соединительн\w*\s+пластин|\bбез\b|\bтолько\b|\bwithout\b|\bonly\b'
 negative=r'\bбез\b|\bне\b|\bтолько\b|неполн|частич|перепак|расфас|раздел|\bwithout\b|\bonly\b'
 # Validate every composition-related manufacturer description sentence,
 # independently of whether the supplier repeats its affirmative part.
 for sentence in re.split(r'[.!?\n]',description):
  if not re.search(relevant,sentence,re.I):continue
  if re.search(negative,sentence,re.I) or not re.search(r'в комплект(?:е| уже)?\s+(?:уже\s+)?вход',sentence,re.I):raise FactoryEvidenceError('factory_assembly_claim_unknown')
 neutral=set()
 for feature in features:
  label,value=feature['label'],feature['value']
  if label=='Наименование' and len(value)>=30 and _norm(title).startswith(_norm(value)):
   neutral.add(_norm(value))
  elif label=='Модель/исполнение' and re.fullmatch(r'С\s+[A-Za-zА-Яа-я0-9 ./-]{1,128}\s+в комплекте',value,re.I) and not _components(value):
   # A connector feature is not evidence for requested fasteners/plates.
   if re.search(negative,value,re.I) or re.search(r'отсутств|исключ',value,re.I):raise FactoryEvidenceError('factory_feature_conflict')
   neutral.add(_norm(value))
  elif re.fullmatch(r'С\s+.{1,128}\s+в комплекте',label,re.I):
   if value.casefold() not in {'да','yes','true','есть','включен','включено'}:raise FactoryEvidenceError('factory_feature_conflict')
   neutral.add(_norm(label))
 lines=[re.sub(r'\s+',' ',v).strip() for v in text.splitlines() if v.strip()]
 for i,line in enumerate(lines):
  if not re.search(relevant,line,re.I):continue
  if re.search(negative,line,re.I):raise FactoryEvidenceError('factory_assembly_contradiction')
  if _norm(line) in neutral:continue
  if _norm(line)==_norm(title) or _norm(line) in _norm(description):continue
  if re.fullmatch(r'(?:Длина|Ширина|Высота|Вес|Объ[её]м)\s+упаковк[иа](?:\s*,\s*(?:мм|м|кг))?',line,re.I):continue
  if re.fullmatch(r'(?:Транспортная\s+упаковка|В\s+упаковке|(?:Количество|Кол-во)\s+в\s+(?:транспортной\s+)?упаковке)\s*[:：]?',line,re.I):
   if i+1>=len(lines) or not re.fullmatch(r'1(?:[.,]0+)?(?:\s*(?:уп|шт)\.?)?',lines[i+1],re.I):raise FactoryEvidenceError('factory_package_count_conflict')
   continue
  if re.fullmatch(r'(?:Упаковка|Единица\s+измерения|упак(?:овка)?|Цена\s+за\s+1\s+упаковку)',line,re.I):continue
  raise FactoryEvidenceError('factory_assembly_claim_unknown')

def derive_singleton(supplier_source,position):
 """Re-derive both immutable source roles and exact native requested assembly."""
 request=_bound_request(position)
 from .http_card import read_source
 retained=read_source(supplier_source,allow_rendered=True)
 if not retained or not retained[1]:raise FactoryEvidenceError('factory_supplier_source_missing')
 record,owned=retained
 if not owned.get('factory_reference') or owned.get('identity_basis')!='manufacturer_code':raise FactoryEvidenceError('factory_supplier_role_missing')
 reference=owned['factory_reference'];technical=read_reference(reference);factory=technical['facts']
 if request['manufacturer'].casefold()!=technical['manufacturer'].casefold() or owned.get('manufacturer','').casefold()!=technical['manufacturer'].casefold() or request['article']!=technical['article'] or owned['article']!=technical['article'] or request['mark']!=technical['article']:raise FactoryEvidenceError('factory_identity_conflict')
 if _norm(owned['title'])!=_norm(factory['title']+' '+technical['manufacturer']+' '+technical['article']):raise FactoryEvidenceError('factory_whole_product_title_conflict')
 dimensions=lambda text:{re.sub(r'\s','',v) for v in re.findall(r'\d+(?:[.,]\d+)?\s*[/×x]\s*\d+(?:[.,]\d+)?',text)}
 nucleus=lambda value:re.findall(r'[\w]+',_norm(value))[:3]
 if nucleus(request['name'])!=nucleus(factory['title']):raise FactoryEvidenceError('factory_requested_product_conflict')
 # Factory full-context semantics are independently reviewed against this exact
 # immutable reference; supplier assertions still undergo native accounting.
 _assembly_claims(owned.get('factory_owner_text',''),owned['title'],factory['description'],owned.get('factory_features',()),owned.get('factory_prose_blocks',()))
 if dimensions(request['name'])!=dimensions(factory['title']) or _coating(request['name'])!=_coating(factory['title']):raise FactoryEvidenceError('factory_requested_variant_conflict')
 required=_components(request['name'])
 if not required or not re.search(r'в комплект',request['name'],re.I):raise FactoryEvidenceError('factory_requested_assembly_unknown')
 def assertions(text):return [v.strip() for v in re.split(r'[.!?\n]',text) if re.search(r'в комплект(?:е| уже)?\s+(?:уже\s+)?вход',v,re.I)]
 factory_assertions=assertions(factory['text']);seller_assertions=assertions(owned.get('factory_owner_text',''))
 shared=[v for v in factory_assertions if _norm(v) in {_norm(s) for s in seller_assertions} and required<=_components(v)]
 if len({_norm(v) for v in shared})!=1:raise FactoryEvidenceError('factory_complete_assembly_unconfirmed')
 if owned.get('pack_counts')!={'count_per_pack':'1','pack_size':'1'}:raise FactoryEvidenceError('factory_sale_package_conflict')
 source={k:v for k,v in supplier_source.items() if k!='photo_binding'} if record.get('role')=='rendered_primary_dom' else {k:supplier_source[k] for k in ['ref','record_sha256','body_sha256']}
 return dict(version=1,basis='derived_factory_singleton_kit',quantity=1,request=request,supplier_source=source,factory_reference=reference,factory_review=technical['review'],supplier_url=record['url'],observed_at=record['observed_at'],source_sha256=_hash(owned['text'].encode()),scope_sha256=owned['scope_sha256'],purchase_scope_sha256=owned['purchase_scope_sha256'],factory_scope_sha256=factory['scope_sha256'],assembly_excerpt=shared[0],listed_price=owned['listed_price'],normalized_price=owned['listed_price'])

def verify_proof(proof,position):
 if type(proof) is not dict or proof.get('basis')!='derived_factory_singleton_kit' or type(proof.get('version')) is not int or proof.get('version')!=1 or type(proof.get('quantity')) is not int or proof.get('quantity')!=1 or any(type(proof.get(k)) not in (int,float) or not math.isfinite(proof[k]) for k in ['listed_price','normalized_price','observed_at']) or type(proof.get('supplier_source')) is not dict:raise FactoryEvidenceError('factory_proof_invalid')
 derived=derive_singleton(proof.get('supplier_source'),position)
 if _json(proof)!=_json(derived):raise FactoryEvidenceError('factory_proof_changed')
 return derived

def validate_observation_proof(proof,observation):
 """Typed durable binding only; business verification separately re-reads refs."""
 fields={'version','basis','quantity','request','supplier_source','factory_reference','factory_review','supplier_url','observed_at','source_sha256','scope_sha256','purchase_scope_sha256','factory_scope_sha256','assembly_excerpt','listed_price','normalized_price'}
 if type(proof) is not dict or set(proof)!=fields or type(proof['version']) is not int or proof['version']!=1 or proof['basis']!='derived_factory_singleton_kit' or type(proof['quantity']) is not int or proof['quantity']!=1 or len(_json(proof))>16384:raise FactoryEvidenceError('factory_proof_shape_invalid')
 request=proof['request'];identity=observation['identity']
 if 'issuer' in observation and observation['issuer']!=('supplier_rendered' if proof['supplier_source'].get('role')=='rendered_primary_dom' else 'supplier_http'):raise FactoryEvidenceError('factory_observation_role_changed')
 if identity['kind']!='article' or request['manufacturer']!=identity['manufacturer'] or request['article']!=identity['code'] or any(proof[k]!=observation[v] for k,v in [('supplier_url','url'),('observed_at','observed_at'),('source_sha256','source_sha256'),('listed_price','raw_amount')]) or proof['normalized_price']!=proof['listed_price']:raise FactoryEvidenceError('factory_observation_binding_changed')
 return proof
