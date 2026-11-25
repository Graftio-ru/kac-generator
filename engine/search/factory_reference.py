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
 if re.match(r'^Перегородка\s+SEP\b',facts['title'],re.I):
  description=_norm(facts['description'])
  if re.search(r'\bбез\b|\bтолько\b|отдельно|неполн|перепак|расфас|часть комплекта',description,re.I)or not any(re.fullmatch(r'поставляется в разогнутом виде в комплекте\s+с винтами [a-zа-я0-9_-]{2,64} и гайками [a-zа-я0-9_-]{2,64}',_norm(v))for v in re.split(r'[.!?\n]',facts['description'])):raise FactoryEvidenceError('factory_sep_complete_hardware_unconfirmed')
 _check_primary_acquisition_claims(facts['claim_context'])
 def excerpt(path,scope,text):return dict(scope_path=path,scope_sha256=scope,text=text,text_sha256=_hash(text.encode()))
 assertions=[v.strip() for v in re.split(r'[.!?\n]',facts['description']) if _complete_assembly_assertion(v)and _components(v)]
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
 result=dict(name=field('name'),mark=field('type_mark'),unit='компл',manufacturer=identity.manufacturer,article=identity.code)
 if re.match(r'^Перегородка\s+SEP\b',result['name'],re.I):
  result['full_name']=field('full_name')or result['name'];source=field('source_evidence',{})or{};result['source_name']=(source.get('cells')or{}).get('name')or result['name']
 return result

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
 if re.search(r'креп[её]ж|метиз|винт(?:ы|ами|ов)|болт(?:ы|ами|ов)|гайк(?:и|ами|ок)',text,re.I):found.add('fasteners')
 if re.search(r'соединительн\w*\s+пластин',text,re.I):found.add('connection_plates')
 return found

def _primary_acquisition_claim(sentence):
 """Primary-product disposition is not a neutral physical characteristic.

 Strong supply/disposition predicates bind an explicit product actor in either
 order. Inclusion predicates require the actor before the predicate, avoiding
 authority from a system's unrelated list of accessories.
 """
 actor=re.search(r'\b(?:издели[а-я]*|товар[а-я]*|детал[а-я]*|аксессуар[а-я]*|продукт[а-я]*|комплект[а-я]*)\b',sentence,re.I)
 if not actor:return False
 disposition=re.search(r'\b(?:постав[а-я]*|прода[а-я]*|приобрет[а-я]*|покуп[а-я]*|заказ[а-я]*|отсутств[а-я]*|исключ[а-я]*|изъя[а-я]*|отдельн[а-я]*|не\s+предусмотр[а-я]*)\b',sentence,re.I)
 inclusion=re.search(r'\b(?:включает|включают|входит|входят|содержит|содержат|включ[её]н(?:о|а)?)\b',sentence,re.I)
 return 'disposition' if disposition else ('membership' if inclusion and actor.start()<inclusion.start() else None)

def _check_primary_acquisition_claims(text):
 for sentence in re.split(r'[.!?\n]',text):
  kind=_primary_acquisition_claim(sentence)
  if (kind=='disposition'and not re.fullmatch(r'поставляется в разогнутом виде в комплекте\s+с винтами [a-zа-я0-9_-]{2,64} и гайками [a-zа-я0-9_-]{2,64}',_norm(sentence)))or(kind and not _complete_assembly_assertion(sentence)):
   raise FactoryEvidenceError('factory_primary_acquisition_claim_unknown')

def _complete_assembly_assertion(sentence):
 """One complete-product membership claim with a closed component list.

 Existing kit assertions keep their contract. The additional grammatical form
 names a complete product as subject; a generic composition/optional parts
 sentence or a cropped affirmative prefix is never sufficient.
 """
 if re.search(r'в комплект(?:е| уже)?\s+(?:уже\s+)?вход',sentence,re.I):return True
 text=_norm(sentence)
 if text=='комплектный аксессуар':return True
 if re.fullmatch(r'поставляется в разогнутом виде в комплекте\s+с винтами [a-zа-я0-9_-]{2,64} и гайками [a-zа-я0-9_-]{2,64}',text):return True
 match=re.fullmatch(r'в состав комплектного (?:аксессуара|изделия|набора) вход(?:ит|ят)\s+(.+)',text)
 if not match:return False
 tail=match[1]
 purpose=r'(?:(?:качественного|надежного)\s+(?:и\s+)?){0,2}монтажа'
 tail=re.sub(r'^(?:детали,\s*необходимые|все\s+необходимое)\s+для\s+'+purpose+r':\s*','',tail)
 tail=re.sub(r',\s*необходимые\s+для\s+'+purpose+r'$','',tail)
 component=r'(?:соединительные пластины|(?:необходимые\s+)?крепежные элементы|детали крепежа|метизы)'
 return bool(re.fullmatch(component+r'(?:\s*(?:,|и)\s*'+component+r')+',tail) and _components(tail)=={'fasteners','connection_plates'})

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
 _check_primary_acquisition_claims(text)
 _check_primary_acquisition_claims(description)
 # Validate every composition-related manufacturer description sentence,
 # independently of whether the supplier repeats its affirmative part.
 for sentence in re.split(r'[.!?\n]',description):
  if not re.search(relevant,sentence,re.I):continue
  if re.search(negative,sentence,re.I) or not _complete_assembly_assertion(sentence):raise FactoryEvidenceError('factory_assembly_claim_unknown')
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

def _cross_source_package_witness(owned,technical,reference,supplier_source):
 candidate=owned.get('factory_package_candidate')
 if type(candidate)is not dict or set(candidate)!={'version','role','purchase_contract'} or type(candidate['version'])is not int or candidate['version']!=1 or candidate['role']!='factory_package_inference_candidate':raise FactoryEvidenceError('factory_package_witness_missing')
 quote=candidate['purchase_contract']
 if type(quote)is not dict or _json(quote)!=_json(owned.get('purchase_contract')) or type(quote.get('version'))is not int or quote.get('version')!=2 or quote.get('role')!='current_owned_purchase_row' or quote.get('sale_unit')!='уп' or type(quote.get('price_denominator'))not in(int,float)or quote['price_denominator']!=1 or quote.get('raw_amount')!=owned['listed_price']:raise FactoryEvidenceError('factory_package_quote_changed')
 features=[v for v in technical['facts']['features']if re.fullmatch(r'В упаковке|Количество в упаковке|Pack quantity|Units per package',str(v.get('label','')).rstrip(':：').strip(),re.I)]
 if len(features)!=1 or not re.fullmatch(r'1(?:[.,]0+)?\s*(?:уп|упак|упаковка)\.?',features[0]['value'].strip(),re.I):raise FactoryEvidenceError('factory_single_package_unconfirmed')
 return dict(version=1,role='cross_source_factory_package',quantity=1,manufacturer=technical['manufacturer'],article=technical['article'],factory_reference=reference,factory_review=technical['review'],official_package_feature=features[0],supplier_source=supplier_source,supplier_scope_sha256=owned['scope_sha256'],supplier_text_sha256=_hash(owned['text'].encode()),purchase_contract=quote)


def _complete_assembly_lexemes(text):
 # Closed lexical abbreviations consume their own dots. No trailing text or
 # punctuation is removed from an otherwise unknown assembly assertion.
 value=re.sub(r'(?<!\w)компл\.(?=\s|$)', 'комплекте', text, flags=re.I)
 return re.sub(r'(?<!\w)креп[её]жн\.\s+элемент\.(?=\s|,|$)', 'крепежные элементы', value, flags=re.I)

def _assembly_title_claim(text):
 value=_norm(_complete_assembly_lexemes(text)).rstrip(' .')
 value=re.sub(r'^в\s+(?:комплекте|компл\.?)(?:\s+входят)?\s*(?:с\s+)?','',value)
 value=re.sub(r',?\s+(?:и\s+)?необходим\w*\s*для\s+монтажа$','',value).strip()
 phrases=[('connection_plates',r'соединительн(?:ые|ыми|ых)\s+пластин(?:ы|ами|)') ,('fasteners',r'креп[её]жн(?:ые|ыми|ых|\.)\s+элемент(?:ы|ами|ов|\.)|метиз(?:ы|ами|ов)')]
 components=[]
 while value:
  matched=False
  for role,pattern in phrases:
   m=re.match('(?:'+pattern+')'+r'(?=\s|,|$)',value)
   if m:
    if role in components:raise FactoryEvidenceError('factory_title_claim_ambiguous')
    components.append(role);value=value[m.end():].strip();value=re.sub(r'^(?:,\s*|и\s+)','',value);matched=True;break
  if not matched:raise FactoryEvidenceError('factory_title_claim_unknown')
 if not components:raise FactoryEvidenceError('factory_title_claim_unknown')
 return sorted(components)

def _title_product_semantics(title,manufacturer,article):
 from .manufacturer_aliases import names_for
 value=' '.join(_complete_assembly_lexemes(title).split()).strip()
 # Identity removal is limited to exact already-bound edge fields.
 value=re.sub(r'^'+re.escape(article)+r'\s*[,;:]\s*','',value,flags=re.I)
 for name in sorted(names_for(manufacturer),key=len,reverse=True):
  value=re.sub(r'\s+'+re.escape(name)+r'\s+'+re.escape(article)+r'$','',value,flags=re.I)
  value=re.sub(r'^'+re.escape(name)+r'\s+'+re.escape(article)+r'\s*[,;:]\s*','',value,flags=re.I)
 value=re.sub(r'\s+'+re.escape(article)+r'$','',value,flags=re.I)
 trailing_coating=None
 tail=re.search(r',\s*(цинк[- ]ламельный|горячий\s+цинк|оцинкованный\s+по\s+методу\s+Сендзимира)$',value,re.I)
 if tail:trailing_coating='zinc_lamellar'if re.search('ламел',tail[1],re.I)else'sendzimir'if re.search('сендзимир',tail[1],re.I)else'hot_zinc';value=value[:tail.start()]
 parts=re.split(r'\bв\s+(?:комплекте|компл\.?)\b',value,maxsplit=1,flags=re.I)
 core=parts[0].strip(' ,');components=_assembly_title_claim('в комплекте '+parts[1])if len(parts)>1 else []
 count=None;m=re.search(r'\((\d+)\s*шт\.?\)',core,re.I)
 if m:count=int(m[1]);core=core[:m.start()]+core[m.end():]
 sep=re.fullmatch(r'Перегородка\s+SEP\s+для\s+углов\s+(CS|CD)\s+45\s*,?\s*H(\d{1,4})',core,re.I)
 if sep:
  return dict(family='перегородка',designation='SEP-'+sep[1].upper(),geometry='H'+str(int(sep[2])),angle='45',plane='вертикальный',direction='внутренний'if sep[1].upper()=='CS'else'внешний',coating=trailing_coating,components=components,component_count=count)
 family=re.match(r'^(Угол|Пластина)(?:\s+(?:для\s+лотка|крепежная))?\s+',core,re.I)
 if not family:raise FactoryEvidenceError('factory_title_family_unknown')
 kind=family[1].casefold();core=core[family.end():]
 fields={'family':kind,'component_count':count,'components':components}
 patterns=[('geometry',r'\d+(?:[.,]\d+)?\s*[xх×/]\s*\d+(?:[.,]\d+)?'),('angle',r'\d+(?:[.,]\d+)?\s*(?:°|градусов|град\.?)(?!\w)'),('plane',r'горизонтальный|вертикальный'),('direction',r'внешний|внутренний'),('coating',r'цинк[- ]ламельный|горячий\s+цинк|оцинкованный\s+по\s+методу\s+Сендзимира')]
 for role,pattern in patterns:
  matches=list(re.finditer(pattern,core,re.I))
  if len(matches)>1:raise FactoryEvidenceError('factory_title_claim_ambiguous')
  fields[role]=None
  if matches:
   m=matches[0];raw=m[0]
   if role=='geometry':raw=re.sub(r'\s','',raw).replace('х','x').replace('×','x').replace('/','x').replace(',','.')
   elif role=='angle':raw=re.match(r'\d+(?:[.,]\d+)?',raw)[0].replace(',','.')
   elif role=='coating':raw='zinc_lamellar'if re.search('ламел',raw,re.I)else'sendzimir'if re.search('сендзимир',raw,re.I)else'hot_zinc'
   else:raw=_norm(raw)
   fields[role]=raw;core=core[:m.start()]+' '+core[m.end():]
 designation=re.fullmatch(r'\s*([A-Z]{1,8}(?:\s*(?:H)?\d{1,4})?)\s*[, ]*',core)
 if not designation:raise FactoryEvidenceError('factory_title_designation_unknown')
 fields['designation']=re.sub(r'\s','',designation[1])
 if trailing_coating:
  if fields['coating']and fields['coating']!=trailing_coating:raise FactoryEvidenceError('factory_title_coating_conflict')
  fields['coating']=trailing_coating
 if kind=='угол'and not all(fields[k]for k in ('geometry','angle','plane')):raise FactoryEvidenceError('factory_title_engineering_missing')
 return fields

def _semantic_feature_value(label,value):
 from decimal import Decimal
 text=_norm(value);label=_norm(label)
 units={'мм':('length','0.001'),'см':('length','0.01'),'м':('length','1'),'мкм':('length','0.000001'),'кг':('mass','1'),'г':('mass','0.001'),'°':('angle','1'),'град':('angle','1'),'уп':('package','1'),'упак':('package','1'),'упаковка':('package','1'),'шт':('piece','1')}
 suffix=re.search(r',\s*(мм|см|мкм|м|кг|г|°)$',label);declared=suffix[1]if suffix else None
 m=re.fullmatch(r'([+-]?\d+(?:[.,]\d+)?)\s*(мм|см|мкм|м|кг|г|°|град|упак|упаковка|уп|шт)?\.?',text)
 if m:
  unit=m[2]or declared
  if declared and m[2]and units[declared]!=units[m[2]]:raise FactoryEvidenceError('factory_feature_unit_conflict')
  return ('numeric',units[unit][0]if unit else'unitless',format((Decimal(m[1].replace(',','.'))*Decimal(units[unit][1]if unit else'1')).normalize(),'f'))
 enums={'да','нет','yes','no','сталь','алюминий','пластик','нержавеющая сталь','сталь оцинкованная','горизонтальный','вертикальный внешний','вертикальный внутренний','вертикально ниспадающий (внешний)','вертикально восходящий (внутренний)','горячий цинк','цинк-ламельное покрытие','оцинкованная по методу сендзимира','оцинковка горячим способом по методу сендзимира','сталь оцинкованная по методу сендзимира'}
 if text not in enums:raise FactoryEvidenceError('factory_feature_value_unknown')
 return ('enum',text)

def _semantic_feature_role(label):
 label=re.sub(r',\s*(?:мм|см|мкм|м|кг|г|°)$','',_norm(label).rstrip(':：'))
 roles={'толщина материала изделия':'толщина материала','ширина кабельного лотка':'ширина','высота кабельного лотка':'высота','материал изделия':'материал','вид/ марка материала':'материал','поворот (угол)':'угол','отклонение (угол)':'угол','защитное покрытие поверхности':'покрытие','количество в упаковке':'количество упаковки','кол-во в упаковке':'количество упаковки','в упаковке':'количество упаковки','фасовка':'количество упаковки'}
 return roles.get(label,label)

def _account_semantic_features(owned,technical,ledger=None,request=None):
 from .manufacturer_aliases import manufacturer_equal
 from ..price_capture import _unit
 result=[];official=technical['facts']['features'];engineering={'толщина материала','ширина','высота','длина','внутренний радиус','радиус','вес','масса','угол','материал','покрытие','изменение направления','исполнение изгиба','количество упаковки','упаковка','кол-во в транспортной упаковке','количество в транспортной упаковке','длина упаковки','ширина упаковки','высота упаковки','вес упаковки','объем упаковки'}
 for feature in owned['factory_features']:
  declared=[e for e in (ledger or {}).get('entries',[]) if _json(e['feature'])==_json(feature)]
  if len(declared)>1:raise FactoryEvidenceError('factory_feature_role_ambiguous')
  if declared and declared[0]['role']!='technical':
   result.append({'supplier_feature':feature,'role':declared[0]['role'],'source_binding':declared[0]['binding']});continue
  label=_norm(feature['label']).rstrip(':：');value=feature['value'].strip();role=_semantic_feature_role(label);decision={'supplier_feature':feature}
  sep_request=bool(request and re.match(r'^Перегородка\s+SEP\b',request['name'],re.I))
  if label in {'артикул','модель','артикул производителя','код производителя','manufacturer code','mpn'}:
   if value!=technical['article']:raise FactoryEvidenceError('factory_feature_identity_conflict')
   decision.update(role='bound_article',canonical=value)
  elif label in {'бренд','производитель','manufacturer','brand'}:
   if not manufacturer_equal(technical['manufacturer'],value):raise FactoryEvidenceError('factory_feature_identity_conflict')
   decision.update(role='bound_manufacturer',canonical=technical['manufacturer'])
  elif label in {'единица измерения','sale unit'}:
   if not re.fullmatch(r'уп|упак\.?|упаковка',value,re.I)or _unit(value)!='уп':raise FactoryEvidenceError('factory_feature_unit_conflict')
   decision.update(role='selling_unit',canonical='уп')
  elif label in {'остаток','количество на складе'}:
   if not re.fullmatch(r'\d+(?:[.,]\d+)?(?:\s*(?:уп|упак|шт)\.?)?',value,re.I):raise FactoryEvidenceError('factory_feature_value_unknown')
   decision.update(role='non_authoritative_stock',canonical=value)
  elif sep_request and label=='климатическое исполнение':
   token=lambda x:set(re.findall(r'(?:УХЛ|ОМ|ХЛ|У|Т)\d',x.upper()))
   matches=[v for v in official if _norm(v['label']).rstrip(':：')==label]
   if not matches or any(token(value)!=token(v['value'])for v in matches)or re.sub(r'(?:УХЛ|ОМ|ХЛ|У|Т)\d|[,\s]','',value.upper()):raise FactoryEvidenceError('factory_climate_conflict')
   decision.update(role='bound_climate',canonical=sorted(token(value)),official_features=matches)
  elif sep_request and label=='комплектация':
   if not re.fullmatch(r'перегородка\s*\|\s*крепежные элементы',_norm(value)):raise FactoryEvidenceError('factory_assembly_feature_conflict')
   decision.update(role='bound_assembly_subset',raw_value=value)
  elif sep_request and label=='тип изделия':
   if _norm(value)!='перегородка':raise FactoryEvidenceError('factory_type_conflict')
   decision.update(role='bound_product_family',canonical='перегородка')
  elif sep_request and label=='толщина материала изделия'and re.fullmatch(r'\d+(?:[.,]\d+)?',value):
   if re.search(r'толщин',request['name'],re.I):raise FactoryEvidenceError('factory_requested_thickness_unaccounted')
   decision.update(role='unrequested_unitless_thickness_reference_warning',raw_value=value)
  elif sep_request and label=='рекомендованный проектный ассортимент'and _norm(value)in {'да','нет'}:
   if re.search(r'ассортимент',request['name'],re.I):raise FactoryEvidenceError('factory_requested_reference_unaccounted')
   decision.update(role='unrequested_assortment_reference',raw_value=value)
  elif sep_request and label=='дополнительная информация'and re.fullmatch(r'масса цинкового слоя не ниже \d+(?:[.,]\d+)? г/м2\s*\|\s*[cс]оответствует ГОСТ \d+-\d+',value,re.I):
   if re.search(r'масса|ГОСТ|цинков.*сло',request['name'],re.I):raise FactoryEvidenceError('factory_requested_reference_unaccounted')
   decision.update(role='unrequested_coating_reference_warning',raw_value=value)
  elif sep_request and role in {'материал','вид/марка материала'}and _norm(value)=='сталь':
   matches=[v for v in official if _semantic_feature_role(v['label'])in {'материал','вид/марка материала'}]
   if not matches or any(not re.fullmatch(r'сталь(?: оцинкованная по методу сендзимира)?',_norm(v['value']))for v in matches):raise FactoryEvidenceError('factory_material_conflict')
   decision.update(role='bound_base_material',raw_value=value,official_features=matches)
  else:
   if role not in engineering:raise FactoryEvidenceError('factory_feature_role_unknown')
   sep_request=bool(request and re.match(r'^Перегородка\s+SEP\b',request['name'],re.I))
   if sep_request and role in {'вес','масса'}and re.fullmatch(r'\d+(?:[.,]\d+)?(?:\s*(?:кг|г))?',value,re.I)and not re.search(r'вес|масса',request['name'],re.I):
    result.append(dict(decision,role='unrequested_weight_reference_warning',raw_value=value,official_features=[v for v in official if _semantic_feature_role(v['label'])==role]));continue
   def parsed_feature(label,v):
    if sep_request and _semantic_feature_role(label)=='покрытие'and re.fullmatch(r'оцинкован(?:ная|о) по методу сендзимира|оцинковка горячим способом по методу сендзимира',_norm(v)):return ('enum','sendzimir')
    return _semantic_feature_value(label,v)
   source_value=parsed_feature(feature['label'],value);matches=[v for v in official if _semantic_feature_role(v['label'])==role]
   if not matches:raise FactoryEvidenceError('factory_feature_official_missing')
   parsed=[parsed_feature(v['label'],v['value'])for v in matches]
   if sep_request and role=='покрытие'and _norm(value)=='горячий цинк'and not re.search(r'HDZ|горячеоцинк|горячий цинк',request['name']+' '+owned['title']+' '+technical['article'],re.I):
    detail=[v for v in owned['factory_features']if _semantic_feature_role(v['label'])=='покрытие'and re.fullmatch(r'оцинкован(?:ная|о) по методу сендзимира',_norm(v['value']))]
    official_bridge=[v for v in matches if _norm(v['value'])=='оцинковка горячим способом по методу сендзимира']
    if detail and official_bridge:result.append(dict(decision,role='bound_generic_coating_warning',raw_value=value,supplier_specific_features=detail,official_features=official_bridge));continue
   if any(v!=source_value for v in parsed):raise FactoryEvidenceError('factory_feature_conflict')
   decision.update(role=role,canonical=source_value,official_features=matches)
  result.append(decision)
 return result


def _node_parents(node,owner):
 while node is not None:
  yield node
  if node is owner:break
  node=node.parent

def _whole_product_semantic_witness(owned,technical,request,source,record):
 factory=technical['facts'];supplier=_title_product_semantics(owned['title'],technical['manufacturer'],technical['article']);official=_title_product_semantics(factory['title']+' '+technical['manufacturer']+' '+technical['article'],technical['manufacturer'],technical['article'])
 for key in ('family','designation','geometry','angle','plane','direction'):
  if supplier[key]!=official[key]:raise FactoryEvidenceError('factory_title_engineering_conflict')
 if supplier['family']=='перегородка':
  requested=_title_product_semantics(request['name'],technical['manufacturer'],technical['article'])
  if any(requested[k]!=official[k]for k in ('family','designation','geometry','angle','plane','direction')):raise FactoryEvidenceError('factory_requested_product_conflict')
  if requested.get('coating')is not None and requested['coating']!=official.get('coating'):raise FactoryEvidenceError('factory_requested_variant_conflict')
  for constraint in {request['name'],request.get('full_name',request['name']),request.get('source_name',request['name'])}:
   check=_title_product_semantics(constraint,technical['manufacturer'],technical['article'])
   if any(check[k]!=official[k]for k in ('family','designation','geometry','angle','plane','direction'))or(check.get('coating')is not None and check['coating']!=official.get('coating')):raise FactoryEvidenceError('factory_requested_variant_conflict')
   if re.search(r'вес|масса|толщин|ГОСТ|ассортимент|цинков.*сло',constraint,re.I):raise FactoryEvidenceError('factory_requested_reference_unaccounted')
 if supplier['component_count']is not None and supplier['component_count']!=official['component_count']:raise FactoryEvidenceError('factory_title_component_conflict')
 if supplier['coating']is not None and supplier['coating']!=official['coating']:raise FactoryEvidenceError('factory_title_coating_conflict')
 allowed=_components(factory['description'])
 if not set(supplier['components'])<=allowed:raise FactoryEvidenceError('factory_title_component_conflict')
 _check_primary_acquisition_claims(factory['description'])
 from .http_card import _legacy_structural_owner,_semantic_text,_semantic_visible,_factory_field_roles
 tree=_legacy_structural_owner(source,record,owned)
 ledger=_factory_field_roles(tree['_owner'],owned['factory_features'],owned.get('purchase_contract'),inline_identity=source.get('role')=='http_primary_dom')
 if 'factory_field_roles'in owned and _json(owned['factory_field_roles'])!=_json(ledger):raise FactoryEvidenceError('factory_field_roles_changed')
 feature_accounting=_account_semantic_features(owned,technical,ledger,request)
 if source.get('role')=='http_primary_dom':
  from .http_card import _http_product_claims
  covered_features,coverage=_http_product_claims(tree['_parser'],tree['_owner'],owned.get('purchase_contract'))
  if _json(covered_features)!=_json(owned['factory_features'])or _json(coverage)!=_json(owned.get('factory_node_ledger')):raise FactoryEvidenceError('factory_claim_coverage_changed')
  if any(e['role']=='primary_negative'for e in coverage['entries']):raise FactoryEvidenceError('factory_primary_negative_claim')
  for line in owned['factory_owner_text'].splitlines():_check_primary_acquisition_claims(line)
  unknown=[e for e in coverage['entries']if e['role']=='primary_unknown']
  if unknown:raise FactoryEvidenceError('factory_primary_claim_unknown')
  return dict(version=3,role='source_bound_whole_product_semantics',request=request,manufacturer=technical['manufacturer'],article=technical['article'],factory_review=technical['review'],supplier_title=owned['title'],factory_title=factory['title'],supplier_scope_sha256=owned['scope_sha256'],supplier_text_sha256=_hash(owned['text'].encode()),supplier_fields=supplier,official_fields=official,coverage_version=1,coverage_sha256=_hash(_json(coverage).encode()),coverage_entries=len(coverage['entries']),feature_accounting_sha256=_hash(_json(feature_accounting).encode()))
 nodes=tree['_nodes'];field_paths={tuple(feature[k])for feature in owned['factory_features']for k in ('label_path','value_path')}
 def bound_field_block(block):
  matches=[n for n in nodes if _semantic_visible(n)and _norm(' '.join(v for v in n.text if isinstance(v,str)))==_norm(block)]
  return bool(matches)and all(any(tuple(parent.path())in field_paths for parent in _node_parents(n,tree['_owner']))for n in matches)
 assertions=[]
 for block in owned['factory_prose_blocks']:
  if _norm(block)in {_norm(owned['title']),_norm(factory['description'])}:continue
  if bound_field_block(block):continue
  claim=_assembly_title_claim(block)
  if not set(claim)<=allowed:raise FactoryEvidenceError('factory_title_component_conflict')
  assertions.append({'text':block,'components':claim})
 known={_norm(owned['title']),*(_norm(v['text'])for v in assertions)}
 for line in owned['factory_owner_text'].splitlines():
  if _norm(line)not in known:_check_primary_acquisition_claims(line)
 return dict(version=2,role='source_bound_whole_product_semantics',request=request,manufacturer=technical['manufacturer'],article=technical['article'],factory_review=technical['review'],supplier_title=owned['title'],factory_title=factory['title'],supplier_scope_sha256=owned['scope_sha256'],supplier_text_sha256=_hash(owned['text'].encode()),supplier_fields=supplier,official_fields=official,field_provenance={key:'supplier_and_official'if supplier[key]is not None and supplier[key]!=[]else'official_only'if official[key]is not None and official[key]!=[]else'not_asserted'for key in official},official_only_components=sorted(allowed-set(supplier['components'])),supplier_assertions=assertions,feature_accounting=feature_accounting,field_roles=ledger)


def derive_singleton(supplier_source,position):
 """Re-derive both immutable source roles and exact native requested assembly."""
 request=_bound_request(position)
 from .http_card import read_source,http_primary_source
 retained=read_source(supplier_source,allow_rendered=True)
 if retained and retained[0].get('role')=='http_primary_dom':
  supplier_source=http_primary_source(supplier_source)
  if supplier_source is None:raise FactoryEvidenceError('factory_supplier_source_missing')
 if not retained or not retained[1]:raise FactoryEvidenceError('factory_supplier_source_missing')
 record,owned=retained
 if not owned.get('factory_reference') or owned.get('identity_basis')not in {'manufacturer_code','catalogue_correlated_manufacturer_code'}:raise FactoryEvidenceError('factory_supplier_role_missing')
 reference=owned['factory_reference'];technical=read_reference(reference);factory=technical['facts']
 if owned.get('identity_basis')=='catalogue_correlated_manufacturer_code':
  expected=dict(version=1,role='catalogue_correlated_manufacturer_code',article=technical['article'],manufacturer=owned.get('manufacturer'),reference=reference,review=technical['review'],supplier_scope_sha256=owned['scope_sha256'],supplier_text_sha256=_hash(owned['text'].encode()))
  if _json(owned.get('catalogue_correlated_identity'))!=_json(expected):raise FactoryEvidenceError('factory_catalogue_identity_changed')
 from .manufacturer_aliases import manufacturer_equal
 if not manufacturer_equal(technical['manufacturer'],request['manufacturer']) or not manufacturer_equal(technical['manufacturer'],owned.get('manufacturer','')) or request['article']!=technical['article'] or owned['article']!=technical['article'] or request['mark']!=technical['article']:raise FactoryEvidenceError('factory_identity_conflict')
 semantic=None
 if _norm(owned['title'])!=_norm(factory['title']+' '+technical['manufacturer']+' '+technical['article']) or any(e['role']!='technical'for e in owned.get('factory_field_roles',{}).get('entries',[])):semantic=_whole_product_semantic_witness(owned,technical,request,supplier_source,record)
 dimensions=lambda text:{re.sub(r'\s','',v) for v in re.findall(r'\d+(?:[.,]\d+)?\s*[/×x]\s*\d+(?:[.,]\d+)?',text)}
 nucleus=lambda value:re.findall(r'[\w]+',_norm(value))[:3]
 if nucleus(request['name'])!=nucleus(factory['title']):raise FactoryEvidenceError('factory_requested_product_conflict')
 # Factory full-context semantics are independently reviewed against this exact
 # immutable reference; supplier assertions still undergo native accounting.
 if semantic is None:_assembly_claims(owned.get('factory_owner_text',''),owned['title'],factory['description'],owned.get('factory_features',()),owned.get('factory_prose_blocks',()))
 if dimensions(request['name'])!=dimensions(factory['title']) or _coating(request['name'])!=_coating(factory['title']):raise FactoryEvidenceError('factory_requested_variant_conflict')
 required=_components(request['name'])
 if not required or not re.search(r'в комплект',request['name'],re.I):raise FactoryEvidenceError('factory_requested_assembly_unknown')
 def assertions(text):return [v.strip() for v in re.split(r'[.!?\n]',text) if _complete_assembly_assertion(v)and _components(v)]
 factory_assertions=assertions(factory['text']);seller_assertions=assertions(owned.get('factory_owner_text',''))
 shared=[v for v in factory_assertions if (semantic is not None or _norm(v) in {_norm(s) for s in seller_assertions}) and required<=_components(v)]
 if len({_norm(v) for v in shared})!=1:raise FactoryEvidenceError('factory_complete_assembly_unconfirmed')
 if 'pack_counts'in owned and owned['pack_counts']!={'count_per_pack':'1','pack_size':'1'}:raise FactoryEvidenceError('factory_sale_package_conflict')
 inferred='pack_counts'not in owned
 source={k:v for k,v in supplier_source.items() if k!='photo_binding'} if record.get('role')in {'rendered_primary_dom','http_primary_dom'} else {k:supplier_source[k] for k in ['ref','record_sha256','body_sha256']}
 result=dict(version=3 if semantic is not None else 2 if inferred else 1,basis='derived_factory_singleton_kit',quantity=1,request=request,supplier_source=source,factory_reference=reference,factory_review=technical['review'],supplier_url=record['url'],observed_at=record['observed_at'],source_sha256=_hash(owned['text'].encode()),scope_sha256=owned['scope_sha256'],purchase_scope_sha256=owned['purchase_scope_sha256'],factory_scope_sha256=factory['scope_sha256'],assembly_excerpt=shared[0],listed_price=owned['listed_price'],normalized_price=owned['listed_price'])
 if inferred:result['package_witness']=_cross_source_package_witness(owned,technical,reference,source)
 if semantic is not None:result['whole_product_witness']=semantic
 return result

def verify_proof(proof,position):
 if type(proof) is not dict or proof.get('basis')!='derived_factory_singleton_kit' or type(proof.get('version')) is not int or proof.get('version')not in(1,2,3) or type(proof.get('quantity')) is not int or proof.get('quantity')!=1 or any(type(proof.get(k)) not in (int,float) or not math.isfinite(proof[k]) for k in ['listed_price','normalized_price','observed_at']) or type(proof.get('supplier_source')) is not dict:raise FactoryEvidenceError('factory_proof_invalid')
 derived=derive_singleton(proof.get('supplier_source'),position)
 if _json(proof)!=_json(derived):raise FactoryEvidenceError('factory_proof_changed')
 return derived

def validate_observation_proof(proof,observation):
 """Typed durable binding only; business verification separately re-reads refs."""
 fields={'version','basis','quantity','request','supplier_source','factory_reference','factory_review','supplier_url','observed_at','source_sha256','scope_sha256','purchase_scope_sha256','factory_scope_sha256','assembly_excerpt','listed_price','normalized_price'}
 if type(proof)is dict and type(proof.get('version'))is int:
  if proof['version']==2:fields=fields|{'package_witness'}
  if proof['version']==3:
   fields=fields|{'whole_product_witness'}
   if 'package_witness'in proof:fields=fields|{'package_witness'}
 if type(proof) is not dict or set(proof)!=fields or type(proof['version']) is not int or proof['version']not in(1,2,3) or proof['basis']!='derived_factory_singleton_kit' or type(proof['quantity']) is not int or proof['quantity']!=1 or len(_json(proof))>16384:raise FactoryEvidenceError('factory_proof_shape_invalid')
 if 'package_witness'in proof:
  w=proof['package_witness'];expected={'version','role','quantity','manufacturer','article','factory_reference','factory_review','official_package_feature','supplier_source','supplier_scope_sha256','supplier_text_sha256','purchase_contract'}
  if type(w)is not dict or set(w)!=expected or type(w['version'])is not int or w['version']!=1 or w['role']!='cross_source_factory_package' or type(w['quantity'])is not int or w['quantity']!=1 or _json(w['supplier_source'])!=_json(proof['supplier_source'])or _json(w['factory_reference'])!=_json(proof['factory_reference'])or _json(w['factory_review'])!=_json(proof['factory_review'])or w['supplier_scope_sha256']!=proof['scope_sha256']or w['supplier_text_sha256']!=proof['source_sha256']:raise FactoryEvidenceError('factory_package_witness_invalid')
  q=w['purchase_contract'];feature=w['official_package_feature']
  from .manufacturer_aliases import manufacturer_equal
  if type(q)is not dict or type(q.get('version'))is not int or q['version']!=2 or q.get('role')!='current_owned_purchase_row' or q.get('sale_unit')!='уп' or type(q.get('price_denominator'))not in(int,float)or q['price_denominator']!=1 or type(q.get('raw_amount'))not in(int,float)or q['raw_amount']!=proof['listed_price']or q.get('owner_sha256')!=proof['scope_sha256']or w['article']!=proof['request']['article']or not manufacturer_equal(w['manufacturer'],proof['request']['manufacturer']):raise FactoryEvidenceError('factory_package_witness_invalid')
  if type(feature)is not dict or not {'label','value','label_path','value_path'}<=set(feature)or not re.fullmatch(r'В упаковке|Количество в упаковке|Pack quantity|Units per package',str(feature['label']).rstrip(':：').strip(),re.I)or not re.fullmatch(r'1(?:[.,]0+)?\s*(?:уп|упак|упаковка)\.?',str(feature['value']).strip(),re.I):raise FactoryEvidenceError('factory_package_witness_invalid')
  if any(type(feature[k])is not list or not 1<=len(feature[k])<=64 or any(type(v)is not int or v<0 for v in feature[k])for k in ('label_path','value_path')):raise FactoryEvidenceError('factory_package_witness_invalid')
 if proof['version']==3:
  w=proof['whole_product_witness']
  keys={'version','role','request','manufacturer','article','factory_review','supplier_title','factory_title','supplier_scope_sha256','supplier_text_sha256','supplier_fields','official_fields','field_provenance','official_only_components','supplier_assertions','feature_accounting'}
  if type(w)is dict and type(w.get('version'))is int and w['version']==2:keys=keys|{'field_roles'}
  if type(w)is dict and w.get('version')==3:
   keys={'version','role','request','manufacturer','article','factory_review','supplier_title','factory_title','supplier_scope_sha256','supplier_text_sha256','supplier_fields','official_fields','coverage_version','coverage_sha256','coverage_entries','feature_accounting_sha256'}
   if proof['supplier_source'].get('role')!='http_primary_dom'or w.get('coverage_version')!=1 or type(w.get('coverage_entries'))is not int or not 1<=w['coverage_entries']<=8192 or any(not re.fullmatch(r'[a-f0-9]{64}',str(w.get(k,'')))for k in ('coverage_sha256','feature_accounting_sha256')):raise FactoryEvidenceError('factory_claim_coverage_invalid')
  if type(w)is not dict or set(w)!=keys or type(w['version'])is not int or w['version']not in (1,2,3) or w['role']!='source_bound_whole_product_semantics' or _json(w['request'])!=_json(proof['request'])or _json(w['factory_review'])!=_json(proof['factory_review'])or w['supplier_scope_sha256']!=proof['scope_sha256']or w['supplier_text_sha256']!=proof['source_sha256']:raise FactoryEvidenceError('factory_whole_product_witness_invalid')
 if proof['version']==3 and proof['whole_product_witness']['version']==2:
  ledger=proof['whole_product_witness']['field_roles']
  if type(ledger)is not dict or set(ledger)!={'version','role','owner_path','entries'}or type(ledger['version'])is not int or ledger['version']!=1 or ledger['role']!='source_bound_field_roles' or type(ledger['entries'])is not list or len(ledger['entries'])>256:raise FactoryEvidenceError('factory_field_roles_invalid')
  path_ok=lambda p:type(p)is list and 1<=len(p)<=64 and all(type(v)is int and v>=0 for v in p)
  if not path_ok(ledger['owner_path']):raise FactoryEvidenceError('factory_field_roles_invalid')
  for entry in ledger['entries']:
   if type(entry)is not dict or set(entry)!={'feature','role','binding'}or entry['role']not in {'technical','seller_sku','navigation','current_price','stock'}or type(entry['binding'])is not dict:raise FactoryEvidenceError('factory_field_roles_invalid')
   feature=entry['feature']
   if type(feature)is not dict or not {'label','value','label_path','value_path'}<=set(feature)or any(type(feature[k])is not str for k in ('label','value'))or any(not path_ok(feature[k])for k in ('label_path','value_path')):raise FactoryEvidenceError('factory_field_roles_invalid')
 request=proof['request'];identity=observation['identity']
 if 'issuer' in observation and observation['issuer']!=('supplier_rendered' if proof['supplier_source'].get('role')=='rendered_primary_dom' else 'supplier_http'):raise FactoryEvidenceError('factory_observation_role_changed')
 if identity['kind']!='article' or request['manufacturer']!=identity['manufacturer'] or request['article']!=identity['code'] or any(proof[k]!=observation[v] for k,v in [('supplier_url','url'),('observed_at','observed_at'),('source_sha256','source_sha256'),('listed_price','raw_amount')]) or proof['normalized_price']!=proof['listed_price']:raise FactoryEvidenceError('factory_observation_binding_changed')
 return proof
