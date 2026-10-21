"""Durable neutral source observations. Reading known entries never starts acquisition.

This store is provenance, not financial approval. Projection must apply the normal
identity, price-role, unit/quantity, seller and region rules to current observations.
"""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass, asdict
import hashlib,json,math,re,sqlite3,time,uuid
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit

VERSION=1
class CatalogueStoreError(RuntimeError):pass
class CatalogueConflict(CatalogueStoreError):pass

def _json(value):
 try:return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
 except (ValueError,TypeError) as error:raise ValueError('Invalid catalogue data') from error

def _text(value,limit=4096,empty=False):
 if type(value) is not str or len(value)>limit or (not empty and not value.strip()):raise ValueError('Invalid text')
 return value

def _stamp(value):
 if type(value) not in (float,int) or not math.isfinite(value) or not 0<value<=time.time():raise ValueError('Invalid observation date')
 return value

def _amount(value,*,optional=False):
 if optional and value is None:return
 if type(value) not in (float,int) or not math.isfinite(value) or value<=0:raise ValueError('Invalid raw amount')

def _url(value):
 _text(value)
 try:u=urlsplit(value);host=u.hostname;port=u.port
 except ValueError as error:raise ValueError('Invalid source URL') from error
 if u.scheme not in {'http','https'} or not host or u.username or u.password:raise ValueError('Invalid source URL')
 return urlunsplit((u.scheme,host.lower()+(':'+str(port) if port else ''),u.path or '/',u.query,''))

def safe_diagnostics(values):
 """Known server diagnostic fields only; no SDK objects, bodies or header data."""
 result=[]
 for value in (values if type(values) in (list,tuple) else ())[:32]:
  if type(value) is not dict:continue
  item={}
  for field in ('code','failure_source','failure_scope','failure_cause','capture_stage','capture_error_type','transport_error_code'):
   text=value.get(field)
   if type(text) is str and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,79}',text):item[field]=text
  for field in ('automatic_replay','after_send_uncertain'):
   if type(value.get(field)) is bool:item[field]=value[field]
  reason=value.get('reason')
  if type(reason) is str:item['reason']=reason[:2048]
  if type(value.get('http_status')) is int and 100<=value['http_status']<=599:item['http_status']=value['http_status']
  stamp=value.get('observed_at')
  if type(stamp) in (float,int) and math.isfinite(stamp) and 0<stamp<=time.time():item['observed_at']=stamp
  for field in ('url','failure_host'):
   try:
    if value.get(field):item[field]=_url(value[field]) if field=='url' else _host(value[field])
   except ValueError:pass
  if item:result.append(item)
 return result

def safe_deferred_check(value,host):
 """Typed unattempted supplier admission, separate from product observation."""
 fields=set('host status reason reason_code health_state failure_code checked_at deferred_at retry_at product_presence queries cards errors reused'.split())
 if type(value) is not dict or set(value)!=fields or value['host']!=host:raise ValueError('Invalid supplier deferral')
 _host(host)
 if value['status']!='incomplete' or value['product_presence']!='unknown':raise ValueError('Invalid supplier deferral')
 if value['reason_code'] not in {'supplier_cooldown','supplier_probe_in_progress'} or value['health_state'] not in {'unknown','healthy','cooldown','half_open'}:raise ValueError('Invalid supplier deferral')
 from .supplier_health_store import SUPPLIER_FAILURES
 if value['failure_code'] is not None and value['failure_code'] not in {v.value for v in SUPPLIER_FAILURES}:raise ValueError('Invalid supplier deferral')
 expected='Supplier check deferred: '+value['reason_code']+('; observed '+value['failure_code'] if value['failure_code'] else '')
 if value['reason']!=expected:raise ValueError('Invalid supplier deferral')
 if any(type(value[k]) is not int or value[k]!=0 for k in ('queries','cards','errors')) or type(value['reused']) is not bool:raise ValueError('Invalid supplier deferral')
 _stamp(value['deferred_at'])
 if value['checked_at'] is not None:_stamp(value['checked_at'])
 if value['reused']!=(value['checked_at'] is not None) or (value['checked_at'] is not None and value['checked_at']>value['deferred_at']):raise ValueError('Invalid supplier deferral')
 stamp=value['retry_at']
 if stamp is not None and (type(stamp) not in (int,float) or not math.isfinite(stamp) or stamp<value['deferred_at']):raise ValueError('Invalid supplier deferral')
 return json.loads(_json(value))

def supplier_defer_reason(check):
 labels={'network_timeout':'поставщик не ответил в срок','network_error':'ошибка соединения с поставщиком','http_rate_limited':'поставщик ограничил частоту запросов','http_unavailable':'сайт поставщика временно недоступен','http_forbidden':'сайт отказал в доступе','captcha':'сайт требует CAPTCHA','bot_protection':'сайт использует защиту от ботов'}
 reason='Карточка пока проверяется другим процессом' if check['reason_code']=='supplier_probe_in_progress' else 'Проверка поставщика отложена: '+labels.get(check['failure_code'],'временная недоступность сайта')
 return reason

def _host(value):
 _text(value,253)
 if value!=value.lower().removeprefix('www.') or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?',value):raise ValueError('Invalid supplier host')
 return value

def _digest(value):
 if type(value) is not str or not re.fullmatch('[0-9a-f]{64}',value):raise ValueError('Invalid hash')

@dataclass(frozen=True)
class ProductIdentity:
 kind:str
 code:str
 manufacturer:str
 descriptor:str=''
 features:tuple[tuple[str,str],...]=()
 def payload(self):
  if self.kind not in {'article','model'}:raise ValueError('Invalid identity kind')
  code=_text(self.code,512).strip();maker=_text(self.manufacturer,512,empty=self.kind=='model').strip()
  descriptor=_text(self.descriptor,2048,empty=self.kind=='article').strip()
  if type(self.features) is not tuple or any(type(v) is not tuple or len(v)!=2 for v in self.features):raise ValueError('Invalid identity features')
  features=tuple(sorted((_text(k,128),_text(v,512)) for k,v in self.features))
  if len(features)>64 or len({k for k,v in features})!=len(features):raise ValueError('Invalid identity features')
  # Article + maker is neutral; descriptors never silently repair another article.
  return dict(kind=self.kind,code=code,manufacturer=maker,descriptor=descriptor if self.kind=='model' else '',features=features if self.kind=='model' else ())
 @property
 def key(self):return hashlib.sha256(_json(self.payload()).encode()).hexdigest()

def identity_for_position(position):
 """Bound native article/maker cells, else exact model/descriptor; no buyer fields."""
 field=lambda k,default='':position.get(k,default) if isinstance(position,dict) else getattr(position,k,default)
 clean=lambda v:' '.join(str(v or '').split())
 name=clean(field('name'));mark=clean(field('type_mark'));evidence=field('source_evidence',{}) or {};reviews=field('review_fields',{}) or {}
 if type(evidence) is not dict or type(reviews) is not dict:raise ValueError('Invalid position source identity')
 cells=evidence.get('cells',{})
 if type(cells) is not dict:raise ValueError('Invalid native identity cells')
 maker=''
 if evidence.get('reader')=='native_pdf' and not any(k in reviews for k in ('name','type_mark','model','article','maker')):
  bound_mark=clean(' / '.join(dict.fromkeys(clean(cells.get(k)) for k in ('model','article') if clean(cells.get(k)))))
  if clean(cells.get('name'))==name and bound_mark==mark:
   maker=clean(cells.get('maker'))
   if clean(cells.get('article')) and maker:return ProductIdentity('article',clean(cells['article']),maker)
 descriptor=clean(field('full_name') or name)
 return ProductIdentity('model',mark or name,maker,descriptor)

@dataclass(frozen=True)
class ImageEvidence:
 observation_id:str
 source_url:str
 observed_at:float
 status:str
 sha256:str=''
 local_path:str=''
 size:int=0
 bucket:str=''
 key:str=''
 captured_at:float|None=None
 error:str=''
 context_evidence:dict|None=None
 def validate(self,observation):
  if self.observation_id!=observation.observation_id or _url(self.source_url)!=_url(observation.url) or self.observed_at!=observation.observed_at:raise ValueError('Image belongs to another observation')
  if self.status not in {'captured','missing','error'}:raise ValueError('Invalid image state')
  _text(self.error,2048,empty=True)
  if self.status=='captured':
   _digest(self.sha256)
   if not self.local_path and not (self.bucket and self.key):raise ValueError('Image reference missing')
   if type(self.size) is not int or not 0<self.size<=128*1024*1024:raise ValueError('Invalid image size')
   _stamp(self.captured_at)
   if self.captured_at<self.observed_at:raise ValueError('Image predates observation')
   if self.local_path:
    _text(self.local_path,2048);p=Path(self.local_path)
    if p.is_absolute() or '..' in p.parts:raise ValueError('Image reference must be owned relative path')
   if bool(self.bucket)!=bool(self.key):raise ValueError('Incomplete S3 reference')
   if self.bucket:
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]',_text(self.bucket,63)):raise ValueError('Invalid bucket')
    _text(self.key,2048)
    if self.key.startswith('/') or '..' in self.key.split('/') or any(c in self.key for c in ('?','#')):raise ValueError('Invalid S3 key')
  elif not self.error or self.sha256 or self.local_path or self.size or self.bucket or self.key or self.captured_at is not None:raise ValueError('Missing image needs error, not an old reference')
  if self.context_evidence is not None and (type(self.context_evidence) is not dict or len(_json(self.context_evidence))>32768):raise ValueError('Invalid context evidence')
  if self.context_evidence:
   if self.status!='captured':raise ValueError('Missing image cannot retain prior context')
   if 'url' in self.context_evidence and _url(self.context_evidence['url'])!=_url(self.source_url):raise ValueError('Context URL differs from image')
   if 'captured_at' in self.context_evidence and self.context_evidence['captured_at']!=self.captured_at:raise ValueError('Context date differs from image')
   if 'sha256' in self.context_evidence and self.context_evidence['sha256']!=self.sha256:raise ValueError('Context hash differs from image')

def validate_native_request_contract(value,identity,issuer):
 """Typed immutable producer intent; never infer roles from title/URL on refresh."""
 if value is None:return None
 fields={'version','identity_sha256','issuer','reader','model','article','mark','name','requested'}
 if type(value) is not dict or set(value)!=fields or value['version']!='native-source-constraints-v1' or value['reader']!='native_pdf':raise ValueError('Invalid native source contract')
 if value['identity_sha256']!=identity.key or value['issuer']!=issuer or issuer not in {'supplier_http','supplier_rendered'}:raise ValueError('Foreign native source contract')
 for key in ('model','article'):_text(value[key],512,empty=True)
 for key in ('mark','name','requested'):_text(value[key],2048)
 clean=lambda text:' '.join(text.split())
 mark=' / '.join(dict.fromkeys(clean(value[k]) for k in ('model','article') if clean(value[k])))
 if clean(value['mark'])!=mark or clean(value['name'])!=clean(value['requested']):raise ValueError('Unbound native source contract')
 if identity.kind=='article':
  if clean(value['article'])!=identity.code:raise ValueError('Wrong native article contract')
 elif clean(value['model'])!=identity.code or value['article']:raise ValueError('Wrong native model contract')
 if len(_json(value))>8192:raise ValueError('Oversize native source contract')
 return json.loads(_json(value))

@dataclass(frozen=True)
class SourceObservation:
 observation_id:str
 identity:ProductIdentity
 supplier_host:str
 requested_url:str
 url:str
 observed_at:float
 issuer:str
 title:str
 manufacturer_code:str
 source_text:str
 source_sha256:str
 raw_amount:float|None
 currency:str
 vat:str
 sale_unit:str
 lot_quantity:float|None
 package_quantity:float|None
 purchase_terms:str
 availability:str
 state:str
 seller_name:str
 seller_inn:str
 image:ImageEvidence
 validation_version:str
 region_status:str='unknown'
 region_evidence:str=''
 in_moscow_region:bool|None=None
 unit_basis:dict|None=None
 unit_basis_reason:str=''
 native_request_contract:dict|None=None
 seller_evidence:dict|None=None
 unit_equivalence:dict|None=None
 purchase_row:dict|None=None
 def payload(self):
  if not re.fullmatch('[0-9a-f]{32}',_text(self.observation_id,32)):raise ValueError('Invalid observation id')
  self.identity.payload();host=_host(self.supplier_host);requested=_url(self.requested_url);actual=_url(self.url)
  if any(not (urlsplit(u).hostname.removeprefix('www.')==host or urlsplit(u).hostname.endswith('.'+host)) for u in (requested,actual)):raise ValueError('Foreign source owner')
  _stamp(self.observed_at)
  if self.issuer not in {'supplier_http','supplier_rendered','approved_import'}:raise ValueError('Invalid producer issuer')
  for value in (self.title,self.currency,self.vat,self.sale_unit,self.validation_version):_text(value,2048)
  for value in (self.manufacturer_code,self.purchase_terms,self.seller_name,self.seller_inn):_text(value,8192,empty=True)
  _text(self.source_text,262144);_digest(self.source_sha256)
  if hashlib.sha256(self.source_text.encode()).hexdigest()!=self.source_sha256:raise ValueError('Source text hash mismatch')
  _amount(self.raw_amount,optional=True);_amount(self.lot_quantity,optional=True);_amount(self.package_quantity,optional=True)
  if self.raw_amount is not None:
   values=[]
   for match in re.finditer(r'(?<![\d.,])((?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?)\s*(?:₽|руб(?:\.|лей|ля|ль)?|RUB|USD|EUR|\$|€)(?![^\W\d_])',self.source_text,re.I):
    values.append(float(re.sub(r'\s','',match[1]).replace(',','.')))
   if not any(math.isclose(v,self.raw_amount,rel_tol=1e-8,abs_tol=.005) for v in values):raise ValueError('Raw amount not present in source facts')
  if self.currency not in {'RUB','USD','EUR','unknown'} or self.vat not in {'included','excluded','unknown'}:raise ValueError('Invalid price interpretation')
  if self.availability not in {'in_stock','out_of_stock','on_order','unknown'} or self.state not in {'observed','review','identity_conflict','unpriced'}:raise ValueError('Invalid observation state')
  if self.region_status not in {'','unknown','confirmed','incompatible'} or type(self.region_evidence) is not str or len(self.region_evidence)>8192 or self.in_moscow_region is not None and type(self.in_moscow_region) is not bool:raise ValueError('Invalid regional source facts')
  self.image.validate(self)
  from .purchase_preview import PREVIEW_REASONS
  if self.unit_basis_reason not in ('',*PREVIEW_REASONS) or self.unit_basis is not None and self.unit_basis_reason:raise ValueError('Invalid product preview reason')
  if self.unit_basis is not None:
   from .purchase_preview import valid_unit_basis
   from ..price_capture import _unit
   if (not valid_unit_basis(asdict(self),self.unit_basis)
       or _unit(self.sale_unit)!=self.unit_basis['sale_unit']
       or self.lot_quantity!=1 or self.package_quantity is not None):raise ValueError('Invalid product preview unit basis')
  if self.unit_equivalence is not None:
   from .factory_reference import validate_observation_proof
   validate_observation_proof(self.unit_equivalence,dict(url=actual,observed_at=self.observed_at,source_sha256=self.source_sha256,raw_amount=self.raw_amount,identity=self.identity.payload()))
  if self.purchase_row is not None:
   from .owned_purchase_row import verify
   if not verify(self.purchase_row,asdict(self)):raise ValueError('Invalid current purchase row')
  contract=validate_native_request_contract(self.native_request_contract,self.identity,self.issuer)
  if self.seller_evidence is not None:
   from ..requisites.supplier import validate_seller_evidence
   validate_seller_evidence(self.seller_evidence,product_url=actual,captured_at=self.observed_at)
   decision=self.seller_evidence['decision']
   if decision['status'] in {'role_confirmed','reference_accepted'}:
    accepted=[s for s in self.seller_evidence['sources'] if s['id'] in decision['resolved_source_ids']]
    if not any(s['raw_name']==self.seller_name and s['raw_inn']==self.seller_inn for s in accepted):raise ValueError('Seller fields differ from source evidence')
  result=asdict(self);result['identity']=self.identity.payload();result['requested_url']=requested;result['url']=actual
  if self.seller_evidence is None:result.pop('seller_evidence',None)
  if self.unit_equivalence is None:result.pop('unit_equivalence',None)
  if self.purchase_row is None:result.pop('purchase_row',None)
  if contract is None:result.pop('native_request_contract',None)
  else:result['native_request_contract']=contract
  if self.raw_amount is not None and self.state=='observed':
   from ..source_quote import selected_price_text,firm_price_role_reason
   if firm_price_role_reason(selected_price_text(self.source_text,self.raw_amount),terms_text=self.purchase_terms):result['state']='review'
  return result

@dataclass(frozen=True)
class Acquisition:
 product_id:str
 token:str
 generation:int
 allowed:bool
 reason:str
 lease_until:float=0

class CatalogueStore:
 def __init__(self,path):
  self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
  try:
   with self._connection(True) as db:
    existing={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='sqlite_sequence'")}
    required={'meta','products','aliases','acquisitions','observations','heads','coverage','attempts'}
    if existing and not required.issubset(existing):raise CatalogueStoreError('Incomplete catalogue schema')
    schema='''
    CREATE TABLE IF NOT EXISTS meta(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,revision INTEGER NOT NULL);
    INSERT OR IGNORE INTO meta VALUES(1,1,0);
    CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY,identity TEXT NOT NULL,created_at REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS aliases(alias TEXT PRIMARY KEY,product_id TEXT NOT NULL,evidence TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS acquisitions(product_id TEXT PRIMARY KEY,token TEXT NOT NULL,generation INTEGER NOT NULL,owner TEXT NOT NULL,lease_until REAL NOT NULL,status TEXT NOT NULL,error TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY,product_id TEXT NOT NULL,host TEXT NOT NULL,url TEXT NOT NULL,observed_at REAL NOT NULL,payload TEXT NOT NULL,revision INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS heads(product_id TEXT NOT NULL,host TEXT NOT NULL,url TEXT NOT NULL,current_id TEXT NOT NULL,last_good_id TEXT,PRIMARY KEY(product_id,host,url));
    CREATE TABLE IF NOT EXISTS coverage(product_id TEXT NOT NULL,host TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(product_id,host));
    CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY AUTOINCREMENT,product_id TEXT NOT NULL,host TEXT NOT NULL,url TEXT NOT NULL,attempted_at REAL NOT NULL,payload TEXT NOT NULL,revision INTEGER NOT NULL);
    '''
    if not existing:
     for statement in schema.split(';'):
      if statement.strip():db.execute(statement)
    meta=db.execute('SELECT version,revision FROM meta WHERE id=1').fetchone()
    if not meta or meta['version']!=VERSION or type(meta['revision']) is not int or meta['revision']<0:raise CatalogueStoreError('Unsupported or invalid catalogue metadata')
  except sqlite3.Error as error:raise CatalogueStoreError('Catalogue store unavailable or corrupt') from error
 @contextmanager
 def _connection(self,write=False):
  db=None
  try:
   db=sqlite3.connect(self.path,timeout=15,isolation_level=None);db.row_factory=sqlite3.Row
   db.execute('PRAGMA foreign_keys=ON');db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
   yield db
   if db.in_transaction:db.commit()
  except sqlite3.Error as error:
   if db is not None and db.in_transaction:db.rollback()
   raise CatalogueStoreError('Catalogue transaction failed') from error
  except BaseException:
   if db is not None and db.in_transaction:db.rollback()
   raise
  finally:
   if db is not None:db.close()
 def _revision(self,db):
  db.execute('UPDATE meta SET revision=revision+1 WHERE id=1');return db.execute('SELECT revision FROM meta WHERE id=1').fetchone()[0]
 def _decode(self,raw):
  try:
   result=json.loads(raw,parse_constant=lambda v: (_ for _ in ()).throw(ValueError('Nonfinite stored data')))
   if type(result) is not dict:raise ValueError('Nonobject stored record')
   return result
  except (ValueError,TypeError) as error:raise CatalogueStoreError('Invalid catalogue record') from error
 def _known(self,db,product_id):
  if not db.execute('SELECT 1 FROM products WHERE id=?',(product_id,)).fetchone():raise CatalogueConflict('Unknown catalogue product')
 def _identity_candidates(self,db,identity):
  """Server registry resolution, preserving all stored issuer IDs and payloads."""
  payload=identity.payload();key=identity.key;candidates={}
  alias=db.execute('SELECT product_id FROM aliases WHERE alias=?',(key,)).fetchone()
  for target in dict.fromkeys((key,alias[0] if alias else key)):
   row=db.execute('SELECT * FROM products WHERE id=?',(target,)).fetchone()
   if row:candidates[target]=row
   elif alias and target==alias[0]:raise CatalogueStoreError('Broken catalogue alias')
  from .manufacturer_aliases import REGISTRY,normalize
  maker=normalize(payload['manufacturer'])
  registry=next((entry for entry in REGISTRY if maker in {normalize(n) for n in entry.names}),None)
  if registry is not None:
   allowed={normalize(n) for n in registry.names}
   expected={k:v for k,v in payload.items() if k!='manufacturer'}
   # Narrow neutral identities only; no observation/history scan or key rewrite.
   rows=db.execute("SELECT * FROM products WHERE json_extract(identity,'$.kind')=? AND json_extract(identity,'$.code')=?",(payload['kind'],payload['code']))
   for row in rows:
    observed=self._identity(row['identity'],row['id'])
    if normalize(observed['manufacturer']) in allowed and {k:v for k,v in observed.items() if k!='manufacturer'}==expected:
     candidates[row['id']]=row
  return [candidates[k] for k in sorted(candidates)]
 def _ambiguous_identity(self,db,identity,candidates,revision):
  reason='В каталоге несколько записей с подтверждёнными эквивалентными названиями производителя; требуется разбор идентичности'
  ids=[r['id'] for r in candidates];hosts=set()
  for product_id in ids:
   hosts.update(r['host'] for r in db.execute('SELECT host FROM coverage WHERE product_id=?',(product_id,)))
  return dict(product_id='',identity=identity.payload(),created_at=None,known=True,revision=revision,
    heads=[],history=[],history_included=False,refresh_attempts=[],acquisition=None,
    identity_ambiguous=True,ambiguity_code='catalogue_identity_ambiguous',ambiguity_reason=reason,
    conflicting_product_ids=ids,coverage={host:dict(status='unknown',observed_at=None,
       reason=reason,code='catalogue_identity_ambiguous',product_presence='unknown') for host in sorted(hosts)})
 def acquire_unknown(self,identity,resources,*,owner,lease_seconds=120):
  payload=identity.payload();key=identity.key;hosts=tuple(dict.fromkeys(_host(h) for h in resources));_text(owner,256)
  if not hosts or len(hosts)>256 or type(lease_seconds) not in (int,float) or not 0<lease_seconds<=600:raise ValueError('Invalid acquisition allocation')
  with self._connection(True) as db:
   candidates=self._identity_candidates(db,identity)
   if len(candidates)>1:return Acquisition('','',0,False,'known_ambiguous')
   if candidates:return Acquisition(candidates[0]['id'],'',0,False,'known')
   now=time.time();token=uuid.uuid4().hex;until=now+lease_seconds
   db.execute('INSERT INTO products VALUES(?,?,?)',(key,_json(payload),now));db.execute('INSERT INTO acquisitions VALUES(?,?,?,?,?,?,?)',(key,token,1,owner,until,'running',''))
   for h in hosts:db.execute('INSERT INTO coverage VALUES(?,?,?)',(key,h,_json(dict(status='unknown',observed_at=None,reason='Not acquired',product_presence='unknown'))))
   self._revision(db);return Acquisition(key,token,1,True,'unknown_acquired',until)
 def acquire_missing(self,identity,resources,*,owner,expected_revision,mode,lease_seconds=120):
  """Explicit NEW allocation; never a read or historical frontier recovery.

  The server checks sufficiency against expected_revision before this CAS.
  A competing writer invalidates that decision rather than stealing its work.
  Source evidence, coverage, heads and dates are never rewritten here.
  """
  if type(mode) is not str or mode!='missing_data':raise ValueError('Explicit missing-data mode required')
  _text(owner,256);hosts=tuple(dict.fromkeys(_host(h) for h in resources))
  if (not hosts or len(hosts)>256 or type(expected_revision) is not int or expected_revision<0
      or type(lease_seconds) not in (int,float) or not math.isfinite(lease_seconds) or not 0<lease_seconds<=600):raise ValueError('Invalid missing-data allocation')
  with self._connection(True) as db:
   if db.execute('SELECT revision FROM meta WHERE id=1').fetchone()[0]!=expected_revision:
    return Acquisition('','',0,False,'snapshot_changed')
   candidates=self._identity_candidates(db,identity)
   if len(candidates)!=1:return Acquisition('','',0,False,'known_ambiguous' if candidates else 'unknown')
   product_id=candidates[0]['id']
   allocated={r['host'] for r in db.execute('SELECT host FROM coverage WHERE product_id=?',(product_id,))}
   if allocated!=set(hosts):return Acquisition(product_id,'',0,False,'resources_changed')
   a=db.execute('SELECT * FROM acquisitions WHERE product_id=?',(product_id,)).fetchone()
   now=time.time()
   if a and (a['owner']==owner or a['status']=='running' and a['lease_until']>now):
    return Acquisition(product_id,'',0,False,'owner_busy')
   if a and a['status'] not in {'running','failed','done','cancelled'}:
    return Acquisition(product_id,'',0,False,'not_claimable')
   token=uuid.uuid4().hex;generation=a['generation']+1 if a else 1;until=now+lease_seconds
   db.execute('INSERT OR REPLACE INTO acquisitions VALUES(?,?,?,?,?,?,?)',(product_id,token,generation,owner,until,'running',''))
   self._revision(db)
   return Acquisition(product_id,token,generation,True,'missing_data_acquired',until)
 def acquisition_owner_matches(self,product_id,*,owner,expected_identity,statuses=('running',)):
  """Private ownership audit; no tokens, mutation or supplier operations."""
  _text(owner,256)
  if type(statuses) is not tuple or not statuses or any(s not in {'running','done'} for s in statuses):raise ValueError('Invalid acquisition audit statuses')
  with self._connection() as db:
   candidates=self._identity_candidates(db,expected_identity)
   if len(candidates)!=1 or candidates[0]['id']!=product_id:return False
   row=db.execute('SELECT owner,status FROM acquisitions WHERE product_id=?',(product_id,)).fetchone()
   return bool(row and row['owner']==owner and row['status'] in statuses)
 def claim_acquisition(self,product_id,*,owner,lease_seconds=120,expected_owner=None,expected_identity=None):
  """Explicit durable worker recovery; never called by a known product READ."""
  _text(owner,256)
  if type(lease_seconds) not in (int,float) or not 0<lease_seconds<=600:raise ValueError('Invalid lease')
  with self._connection(True) as db:
   self._known(db,product_id);a=db.execute('SELECT * FROM acquisitions WHERE product_id=?',(product_id,)).fetchone()
   if not a or a['status']!='running' or a['lease_until']>time.time() or (expected_owner is not None and a['owner']!=expected_owner):return Acquisition(product_id,'',0,False,'not_claimable')
   if expected_identity is not None:
    candidates=self._identity_candidates(db,expected_identity)
    if len(candidates)!=1 or candidates[0]['id']!=product_id:return Acquisition(product_id,'',0,False,'identity_mismatch')
   token=uuid.uuid4().hex;generation=a['generation']+1;until=time.time()+lease_seconds
   db.execute('UPDATE acquisitions SET token=?,generation=?,owner=?,lease_until=? WHERE product_id=?',(token,generation,owner,until,product_id));self._revision(db)
   return Acquisition(product_id,token,generation,True,'worker_recovery',until)
 def with_expired_acquisitions(self,entries,callback,*,owner):
  """Private source migration guard; no claim/renew/token/generation mutation."""
  _text(owner,256)
  if type(entries) is not list or not entries:raise CatalogueConflict('Original allocations required')
  with self._connection(True) as db:
   seen=set()
   for entry in entries:
    product_id=entry['product_id'];identity=entry['identity']
    if product_id in seen:raise CatalogueConflict('Duplicate original allocation')
    seen.add(product_id);candidates=self._identity_candidates(db,identity)
    row=db.execute('SELECT * FROM acquisitions WHERE product_id=?',(product_id,)).fetchone()
    if (len(candidates)!=1 or candidates[0]['id']!=product_id or not row
        or row['owner']!=owner or row['generation']!=entry['generation']
        or row['status']!=entry['status'] or row['status'] not in ('running','done')
        or row['status']=='done' and not entry['complete']
        or row['status']=='running' and row['lease_until']>time.time()):raise CatalogueConflict('Original expired ownership required')
   return callback()
 def recovery_allocation(self,product_id,*,parent,identity,min_generation):
  """Read sealed-frontier allocation authority; never claim or renew."""
  with self._connection() as db:
   candidates=self._identity_candidates(db,identity);row=db.execute('SELECT * FROM acquisitions WHERE product_id=?',(product_id,)).fetchone()
   if (len(candidates)!=1 or candidates[0]['id']!=product_id or not row or row['owner']!=parent
       or row['status'] not in {'failed','running'} or type(row['generation']) is not int or row['generation']<min_generation
       or type(row['lease_until']) not in (int,float) or not math.isfinite(row['lease_until']) or row['lease_until']<0
       or row['status']=='running' and row['lease_until']>time.time()):raise CatalogueConflict('Failed or expired original allocation required')
   return dict(generation=row['generation'],allocation=dict(owner=parent,status=row['status'],lease_until=row['lease_until'],token_sha256=hashlib.sha256(row['token'].encode()).hexdigest()))
 def transfer_failed_recovery(self,entries,*,parent,child,expected_source_evidence=None,before_transfer=None):
  """Private prepared-child recovery; atomically validate all before transfer."""
  _text(parent,256);_text(child,256)
  if parent==child or type(entries) is not list or not entries:raise CatalogueConflict('Recovery allocation mapping required')
  with self._connection(True) as db:
   self._recovery_source_guard(db,expected_source_evidence)
   rows=[];seen=set();now=time.time()
   for entry in entries:
    product_id=entry['product_id'];identity=entry['identity']
    if type(entry.get('generation')) is not int or entry['generation']<1:raise CatalogueConflict('Recovery generation invalid')
    candidates=self._identity_candidates(db,identity);row=db.execute('SELECT * FROM acquisitions WHERE product_id=?',(product_id,)).fetchone()
    if (product_id in seen or len(candidates)!=1 or candidates[0]['id']!=product_id or not row):raise CatalogueConflict('Recovery identity differs')
    seen.add(product_id)
    evidence=entry.get('allocation')
    if evidence is not None:
     if (type(evidence) is not dict or set(evidence)!={'owner','status','lease_until','token_sha256'} or evidence['owner']!=parent or evidence['status'] not in {'failed','running'}
         or type(evidence['lease_until']) not in (int,float) or not math.isfinite(evidence['lease_until']) or evidence['lease_until']<0
         or type(evidence['token_sha256']) is not str or not re.fullmatch('[a-f0-9]{64}',evidence['token_sha256'])):raise CatalogueConflict('Recovery allocation evidence invalid')
     original=(row['owner']==parent and row['generation']==entry['generation'] and row['status']==evidence['status']
       and row['lease_until']==evidence['lease_until'] and hashlib.sha256(row['token'].encode()).hexdigest()==evidence['token_sha256']
       and type(row['lease_until']) in (int,float) and math.isfinite(row['lease_until']) and row['lease_until']>=0
       and (row['status']=='failed' or row['status']=='running' and row['lease_until']<=now))
    else:
     # Existing v1 plans retain failed-only semantics; never expand legacy proof.
     original=row['owner']==parent and row['status']=='failed' and row['generation']==entry['generation']
    replay=row['owner']==child and row['status']=='running' and row['generation']>=entry['generation']+1
    if not (original or replay):raise CatalogueConflict('Recovery ownership differs')
    rows.append((product_id,row,replay))
   if before_transfer is not None:before_transfer()
   admissions={};changed=False
   for product_id,row,replay in rows:
    if replay and row['lease_until']>now:
     admissions[product_id]=Acquisition(product_id,row['token'],row['generation'],True,'linked_recovery_replay',row['lease_until']);continue
    token=uuid.uuid4().hex;generation=row['generation']+1;until=now+120
    db.execute("UPDATE acquisitions SET token=?,generation=?,owner=?,lease_until=?,status='running',error='' WHERE product_id=?",(token,generation,child,until,product_id));changed=True
    admissions[product_id]=Acquisition(product_id,token,generation,True,'linked_recovery',until)
   if changed:self._revision(db)
   return admissions
 def with_recovery_admissions(self,admissions,callback,*,owner,expected_source_evidence=None):
  """No child runnable receipt before all transferred tokens remain current."""
  with self._connection(True) as db:
   self._recovery_source_guard(db,expected_source_evidence)
   if not admissions or any(self._current_acquisition(db,a,owner=owner) is None for a in admissions):raise CatalogueConflict('Recovery allocation no longer current')
   return callback()
 def with_acquisition(self,admission,callback,*,owner,expected_identity=None):
  """Catalogue then research CAS; callback must not reenter catalogue/publication."""
  _text(owner,256)
  with self._connection(True) as db:
   if not self._admitted(db,admission,admission.product_id):raise CatalogueConflict('Acquisition ownership lost')
   row=db.execute('SELECT owner FROM acquisitions WHERE product_id=?',(admission.product_id,)).fetchone()
   if row['owner']!=owner:raise CatalogueConflict('Acquisition owner differs from original job')
   if expected_identity is not None:
    candidates=self._identity_candidates(db,expected_identity)
    if len(candidates)!=1 or candidates[0]['id']!=admission.product_id:raise CatalogueConflict('Acquisition does not bind original row identity')
   return callback()
 def _current_acquisition(self,db,admission,*,owner=None):
  row=db.execute('SELECT * FROM acquisitions WHERE product_id=?',(admission.product_id,)).fetchone()
  return row if admission.allowed and row and row['status']=='running' and row['token']==admission.token and row['generation']==admission.generation and row['lease_until']>time.time() and (owner is None or row['owner']==owner) else None
 def _admitted(self,db,admission,product_id):
  return bool(admission.product_id==product_id and self._current_acquisition(db,admission))
 def validate_acquisition(self,admission,*,owner=None):
  """Fresh SQL capability read; validation never extends expiry or revision."""
  if owner is not None:_text(owner,256)
  with self._connection() as db:
   row=self._current_acquisition(db,admission,owner=owner)
   if row is None:return Acquisition(admission.product_id,'',0,False,'not_current')
   return Acquisition(admission.product_id,admission.token,admission.generation,True,'validated',row['lease_until'])
 def renew_acquisition(self,admission,*,lease_seconds=120,owner=None):
  if type(lease_seconds) not in (int,float) or not math.isfinite(lease_seconds) or not 0<lease_seconds<=600:raise ValueError('Invalid lease')
  if owner is not None:_text(owner,256)
  with self._connection(True) as db:
   if self._current_acquisition(db,admission,owner=owner) is None:return Acquisition(admission.product_id,'',0,False,'not_current')
   until=time.time()+lease_seconds
   db.execute('UPDATE acquisitions SET lease_until=? WHERE product_id=?',(until,admission.product_id));self._revision(db)
   return Acquisition(admission.product_id,admission.token,admission.generation,True,'renewed',until)
 def cancel_acquisition(self,admission,*,reason='Cancelled',owner=None):
  _text(reason,2048)
  if owner is not None:_text(owner,256)
  with self._connection(True) as db:
   if self._current_acquisition(db,admission,owner=owner) is None:return False
   db.execute('UPDATE acquisitions SET status=?,error=? WHERE product_id=?',('cancelled',reason,admission.product_id));self._revision(db);return True
 def finish_acquisition(self,admission,*,error='',owner=None,completion=None):
  _text(error,2048,empty=True)
  if owner is not None:_text(owner,256)
  from .acquisition_frontier import validate_completion_receipt,VerifiedCompletion,_COMPLETION_ISSUER
  if completion is not None and (type(completion) is not VerifiedCompletion or completion._issuer is not _COMPLETION_ISSUER):raise CatalogueConflict('Private completion issuer required')
  receipt=validate_completion_receipt(completion.payload) if completion is not None else None
  if receipt is not None and (error or owner!=receipt['job_id'] or receipt['product_id']!=admission.product_id or receipt['generation']!=admission.generation):
   raise CatalogueConflict('Completion allocation differs')
  with self._connection(True) as db:
   row=db.execute('SELECT * FROM acquisitions WHERE product_id=?',(admission.product_id,)).fetchone()
   matching=bool(admission.allowed and row and row['token']==admission.token and row['generation']==admission.generation and (owner is None or row['owner']==owner))
   if receipt is not None and matching and row['status']=='done':
    existing=self._completion(db,admission.product_id,row)
    if existing==receipt:return True
    raise CatalogueConflict('Immutable completion differs')
   if not matching or row['status']!='running' or row['lease_until']<=time.time():return False
   if receipt is not None:
    resources=[r['host'] for r in db.execute('SELECT host FROM coverage WHERE product_id=?',(admission.product_id,))]
    if set(resources)!=set(receipt['configured_hosts']):raise CatalogueConflict('Completion resource scope differs')
    # Lazy additive schema only at successful mint; never migrate a read/export.
    db.execute('CREATE TABLE IF NOT EXISTS acquisition_completions(product_id TEXT NOT NULL,generation INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(product_id,generation))')
    existing=db.execute('SELECT payload FROM acquisition_completions WHERE product_id=? AND generation=?',(admission.product_id,admission.generation)).fetchone()
    encoded=_json(receipt)
    if existing and existing['payload']!=encoded:raise CatalogueConflict('Immutable completion differs')
    db.execute('INSERT OR IGNORE INTO acquisition_completions VALUES(?,?,?)',(admission.product_id,admission.generation,encoded))
   db.execute('UPDATE acquisitions SET status=?,error=? WHERE product_id=?',('failed' if error else 'done',error,admission.product_id));self._revision(db);return True
 def _completion(self,db,product_id,allocation):
  if not allocation or allocation['status']!='done' or allocation['error']:return None
  if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='acquisition_completions'").fetchone():return None
  row=db.execute('SELECT payload FROM acquisition_completions WHERE product_id=? AND generation=?',(product_id,allocation['generation'])).fetchone()
  if not row:return None
  try:
   from .acquisition_frontier import validate_completion_receipt
   if len(row['payload'].encode())>65536:raise ValueError('Completion exceeds bound')
   value=validate_completion_receipt(json.loads(row['payload']))
   if value['product_id']!=product_id or value['generation']!=allocation['generation'] or value['job_id']!=allocation['owner']:raise ValueError('Completion allocation differs')
   return value
  except (ValueError,TypeError,KeyError) as error:raise CatalogueStoreError('Stored completion invalid') from error
 def validate_completion_snapshot(self,snapshot,identities):
  """One read transaction rechecks allocation seals, not newer price heads."""
  with self._connection() as db:
   for identity in identities:
    product=snapshot.get('products',{}).get(identity.key)
    if not product or not product.get('acquisition_completion'):raise CatalogueConflict('completion_unknown')
    row=db.execute('SELECT * FROM acquisitions WHERE product_id=?',(product['product_id'],)).fetchone()
    current=self._completion(db,product['product_id'],row)
    if current!=product['acquisition_completion']:raise CatalogueConflict('completion_generation_changed')
 def validate_final_snapshot(self,snapshot,identities,*,local_terminals=None):
  """Financial output needs a terminal current allocation, not an exhaustion receipt.

  Read-only seal: identity, owner, generation and current price heads must still
  match the server snapshot. Missing certificates are not manufactured here.
  """
  owners={}
  with self._connection() as db:
   for identity in identities:
    product=snapshot.get('products',{}).get(identity.key)
    if not product or product.get('identity_ambiguous'):raise CatalogueConflict('final_identity_invalid')
    candidates=self._identity_candidates(db,identity)
    if len(candidates)!=1 or candidates[0]['id']!=product['product_id']:raise CatalogueConflict('final_identity_changed')
    row=db.execute('SELECT * FROM acquisitions WHERE product_id=?',(product['product_id'],)).fetchone()
    allocation=product.get('acquisition') or {}
    from .local_terminal import matches
    local=matches((local_terminals or {}).get(product['product_id']),product)
    if local:
     from .acquisition_source_intent import digest
     coverage={r['host']:self._coverage(r) for r in db.execute('SELECT * FROM coverage WHERE product_id=?',(product['product_id'],))}
     if digest(coverage)!=local_terminals[product['product_id']].coverage_sha256:raise CatalogueConflict('final_local_coverage_changed')
    if (not row or (not local and (row['status']!='done' or row['error'])) or not row['owner']
        or row['owner']!=product.get('acquisition_owner')
        or type(allocation.get('generation')) is not int or allocation['generation']<1
        or {k:row[k] for k in ('status','error','generation','lease_until')}!=allocation):
     raise CatalogueConflict('final_acquisition_not_terminal')
    current={(r['host'],r['url']):r['current_id'] for r in db.execute('SELECT host,url,current_id FROM heads WHERE product_id=?',(product['product_id'],))}
    frozen={(r['host'],r['url']):r['current']['observation_id'] for r in product['heads']}
    if current!=frozen:raise CatalogueConflict('final_source_heads_changed')
    owners[product['product_id']]=row['owner']
  return owners
 def add_alias(self,alias,target,*,observed_identity,source_url,observed_at,source_title):
  """Explicit source-backed title alias; code, maker and feature contradictions forbidden."""
  a=alias.payload();t=target.payload();o=observed_identity.payload();_url(source_url);_stamp(observed_at);_text(source_title,2048)
  if o!=t or any(a[k]!=t[k] for k in ('kind','code','manufacturer','features')):raise ValueError('Alias contradicts product identity')
  if alias.kind=='model' and alias.descriptor not in source_title:raise ValueError('Alias title not observed')
  with self._connection(True) as db:
   self._known(db,target.key)
   if alias.key!=target.key and db.execute('SELECT 1 FROM products WHERE id=?',(alias.key,)).fetchone():raise CatalogueConflict('Alias conflicts with existing canonical product')
   old=db.execute('SELECT product_id FROM aliases WHERE alias=?',(alias.key,)).fetchone()
   if old and old[0]!=target.key:raise CatalogueConflict('Ambiguous alias')
   db.execute('INSERT OR REPLACE INTO aliases VALUES(?,?,?)',(alias.key,target.key,_json(dict(identity=a,observed_identity=o,source_url=source_url,observed_at=observed_at,source_title=source_title))));self._revision(db)
 def append_observation(self,product_id,observation,*,acquisition=None,coverage_status=None):
  payload=observation.payload()
  if observation.identity.key!=product_id:raise ValueError('Observation identity mismatch')
  encoded=_json(payload)
  if coverage_status is not None and coverage_status not in {'found','review'}:raise ValueError('Invalid observed coverage')
  if coverage_status=='found' and (payload['state']!='observed' or observation.raw_amount is None):raise ValueError('Found coverage needs current observed price')
  with self._connection(True) as db:
   self._known(db,product_id)
   if acquisition is not None and not self._admitted(db,acquisition,product_id):return False
   if acquisition is not None and observation.observed_at<db.execute('SELECT created_at FROM products WHERE id=?',(product_id,)).fetchone()[0]:raise ValueError('Acquisition observation predates product admission')
   if not db.execute('SELECT 1 FROM coverage WHERE product_id=? AND host=?',(product_id,observation.supplier_host)).fetchone():raise ValueError('Unconfigured observation resource')
   old=db.execute('SELECT payload FROM observations WHERE id=?',(observation.observation_id,)).fetchone()
   if old:
    if old[0]!=encoded:raise CatalogueConflict('Immutable observation collision')
    return False
   revision=self._revision(db)
   db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?)',(observation.observation_id,product_id,observation.supplier_host,payload['url'],observation.observed_at,encoded,revision))
   redirected=None
   if payload['requested_url']!=payload['url']:
    redirected=db.execute('SELECT h.*,o.observed_at FROM heads h JOIN observations o ON h.current_id=o.id WHERE h.product_id=? AND h.host=? AND h.url=?',(product_id,observation.supplier_host,payload['requested_url'])).fetchone()
    if redirected and redirected['observed_at']>=observation.observed_at:return False
    if redirected:db.execute('DELETE FROM heads WHERE product_id=? AND host=? AND url=?',(product_id,observation.supplier_host,payload['requested_url']))
   head=db.execute('SELECT h.*,o.observed_at FROM heads h JOIN observations o ON h.current_id=o.id WHERE h.product_id=? AND h.host=? AND h.url=?',(product_id,observation.supplier_host,payload['url'])).fetchone()
   if head and head['observed_at']>=observation.observed_at:return False
   good=payload['state']=='observed' and observation.raw_amount is not None
   last=observation.observation_id if good else head['last_good_id'] if head else redirected['last_good_id'] if redirected else None
   db.execute('INSERT OR REPLACE INTO heads VALUES(?,?,?,?,?)',(product_id,observation.supplier_host,payload['url'],observation.observation_id,last))
   if coverage_status:
    from .purchase_preview import preview_reason_text
    reason=preview_reason_text(observation.unit_basis_reason) if coverage_status=='review' else ''
    coverage=_json(dict(status=coverage_status,observed_at=observation.observed_at,reason=reason or 'Source observation recorded',product_presence='observed',observation_id=observation.observation_id))
    prior=self._decode(db.execute('SELECT payload FROM coverage WHERE product_id=? AND host=?',(product_id,observation.supplier_host)).fetchone()[0])
    if not prior.get('observed_at') or observation.observed_at>=prior['observed_at']:db.execute('UPDATE coverage SET payload=? WHERE product_id=? AND host=?',(coverage,product_id,observation.supplier_host))
   return True
 def record_refresh_attempt(self,product_id,host,url,*,attempted_at,outcome,reason='',observation_id=None,acquisition=None):
  _host(host);url=_url(url);_stamp(attempted_at);_text(reason,2048,empty=True)
  if outcome not in {'observed','network_error','parse_error','account_error','cancelled','unknown'}:raise ValueError('Invalid refresh outcome')
  if (outcome=='observed')!=(observation_id is not None):raise ValueError('Observed attempt needs actual observation')
  with self._connection(True) as db:
   if acquisition is not None and not self._admitted(db,acquisition,product_id):raise CatalogueConflict('Acquisition ownership lost before proof attempt publication')
   self._known(db,product_id)
   if not db.execute('SELECT 1 FROM coverage WHERE product_id=? AND host=?',(product_id,host)).fetchone() or not (urlsplit(url).hostname.removeprefix('www.')==host or urlsplit(url).hostname.endswith('.'+host)):raise ValueError('Unconfigured refresh resource')
   source_url=None
   if observation_id:
    row=db.execute('SELECT * FROM observations WHERE id=? AND product_id=? AND host=?',(observation_id,product_id,host)).fetchone()
    if not row:raise ValueError('Refresh observation missing')
    source=self._observation(row)
    if url not in {source['url'],source['requested_url']}:raise ValueError('Refresh source URL not related to attempt')
    source_url=source['url']
   revision=self._revision(db);db.execute('INSERT INTO attempts(product_id,host,url,attempted_at,payload,revision) VALUES(?,?,?,?,?,?)',(product_id,host,url,attempted_at,_json(dict(outcome=outcome,reason=reason,observation_id=observation_id,source_url=source_url)),revision))
 def publish_coverage(self,product_id,host,*,status,observed_at=None,reason='',product_presence='unknown',observation_id=None,diagnostics=None,acquisition=None,deferred_check=None):
  if deferred_check is not None:deferred_check=safe_deferred_check(deferred_check,host)
  _host(host);_text(reason,2048,empty=True)
  if status not in {'unknown','deferred','error','review','found','no_match'} or product_presence not in {'unknown','observed'}:raise ValueError('Invalid coverage')
  if status not in {'unknown','deferred'}:_stamp(observed_at)
  elif observed_at is not None:_stamp(observed_at)
  if status=='found' and not observation_id:raise ValueError('Found coverage needs observation')
  if status=='no_match' and product_presence!='unknown':raise ValueError('No match does not prove absence')
  with self._connection(True) as db:
   if acquisition is not None and not self._admitted(db,acquisition,product_id):return False
   self._known(db,product_id)
   if not db.execute('SELECT 1 FROM coverage WHERE product_id=? AND host=?',(product_id,host)).fetchone():raise ValueError('Unconfigured coverage resource')
   if observation_id:
    observed=db.execute('SELECT * FROM observations WHERE id=? AND product_id=? AND host=?',(observation_id,product_id,host)).fetchone()
    if not observed:raise ValueError('Coverage observation missing')
    if observed_at!=observed['observed_at']:raise ValueError('Coverage date differs from source')
    if status=='found' and (product_presence!='observed' or self._observation(observed)['state']!='observed' or self._observation(observed)['raw_amount'] is None):raise ValueError('Found coverage must bind actual source facts')
    if status=='found' and not db.execute('SELECT 1 FROM heads WHERE product_id=? AND current_id=?',(product_id,observation_id)).fetchone():raise ValueError('Found coverage cannot restore historical observation')
   current=self._decode(db.execute('SELECT payload FROM coverage WHERE product_id=? AND host=?',(product_id,host)).fetchone()[0])
   if current.get('observed_at') and (observed_at is None or observed_at<current['observed_at']):return False
   payload=dict(status=status,observed_at=observed_at,reason=reason,product_presence=product_presence,observation_id=observation_id,diagnostics=safe_diagnostics(diagnostics))
   if deferred_check is not None:payload['deferred_check']=deferred_check
   db.execute('UPDATE coverage SET payload=? WHERE product_id=? AND host=?',(_json(payload),product_id,host));self._revision(db);return True
 def _coverage(self,row):
  payload=self._decode(row['payload'])
  if payload.get('deferred_check') is not None:
   try:safe_deferred_check(payload['deferred_check'],row['host'])
   except ValueError:raise CatalogueStoreError('Catalogue supplier deferral is corrupt') from None
  return payload
 def _identity(self,raw,product_id):
  try:
   payload=self._decode(raw);identity=ProductIdentity(**{**payload,'features':tuple(tuple(v) for v in payload['features'])})
   if identity.key!=product_id:raise ValueError('Identity key mismatch')
   return identity.payload()
  except (KeyError,TypeError,ValueError,AttributeError) as error:raise CatalogueStoreError('Invalid catalogue identity') from error
 def _observation(self,row):
  try:
   payload=self._decode(row['payload']);identity=payload['identity'];identity={**identity,'features':tuple(tuple(v) for v in identity['features'])}
   observed=SourceObservation(**{**payload,'identity':ProductIdentity(**identity),'image':ImageEvidence(**payload['image'])})
   canonical=observed.payload()
   if observed.observation_id!=row['id'] or observed.identity.key!=row['product_id'] or observed.supplier_host!=row['host'] or canonical['url']!=row['url'] or observed.observed_at!=row['observed_at']:raise ValueError('Stored source binding mismatch')
   return canonical
  except (KeyError,TypeError,ValueError,AttributeError) as error:raise CatalogueStoreError('Invalid catalogue observation') from error
 def resolve_observation(self,product_id,observation_id):
  """Resolve trusted stored facts, not a client-supplied payload, in one revision."""
  with self._connection() as db:
   product=db.execute('SELECT identity FROM products WHERE id=?',(product_id,)).fetchone()
   if not product:return None
   row=db.execute('SELECT * FROM observations WHERE product_id=? AND id=?',(product_id,observation_id)).fetchone()
   if not row:return None
   return dict(revision=db.execute('SELECT revision FROM meta WHERE id=1').fetchone()[0],product_id=product_id,identity=self._identity(product['identity'],product_id),observation=self._observation(row),current=bool(db.execute('SELECT 1 FROM heads WHERE product_id=? AND current_id=?',(product_id,observation_id)).fetchone()))
 def read_coverage(self,product_id,host,*,acquisition=None,owner=None):
  """One fresh configured host, without unrelated heads/history/host decoding."""
  host=_host(host)
  if owner is not None:_text(owner,256)
  with self._connection() as db:
   product=db.execute('SELECT identity FROM products WHERE id=?',(product_id,)).fetchone()
   if not product:raise CatalogueConflict('Unknown catalogue product')
   self._identity(product['identity'],product_id)
   if acquisition is not None and (acquisition.product_id!=product_id or self._current_acquisition(db,acquisition,owner=owner) is None):raise CatalogueConflict('Acquisition ownership lost')
   row=db.execute('SELECT * FROM coverage WHERE product_id=? AND host=?',(product_id,host)).fetchone()
   if row is None:raise CatalogueConflict('Unconfigured coverage resource')
   payload=self._coverage(row)
   if payload.get('observation_id'):
    observation=db.execute('SELECT * FROM observations WHERE product_id=? AND host=? AND id=?',(product_id,host,payload['observation_id'])).fetchone()
    if not observation:raise CatalogueStoreError('Invalid coverage observation')
    actual=self._observation(observation)
    if payload.get('observed_at')!=actual['observed_at']:raise CatalogueStoreError('Invalid coverage observation date')
    if payload.get('status')=='found' and not db.execute('SELECT 1 FROM heads WHERE product_id=? AND host=? AND current_id=?',(product_id,host,payload['observation_id'])).fetchone():raise CatalogueStoreError('Invalid current coverage observation')
   return payload
 def lookup(self,identity,*,include_history=False):
  return self.read_snapshot([identity],include_history=include_history)['products'][identity.key]
 def _recovery_source_evidence(self,db):
  from .acquisition_source_intent import digest
  observations=[tuple(row) for row in db.execute('SELECT * FROM observations ORDER BY id')]
  heads=[tuple(row) for row in db.execute('SELECT * FROM heads ORDER BY product_id,host,url')]
  return dict(observations_sha256=digest(observations),observation_count=len(observations),heads_sha256=digest(heads))
 def _recovery_source_guard(self,db,expected):
  if expected is not None and self._recovery_source_evidence(db)!=expected:raise CatalogueConflict('Startup source evidence changed')
 def recovery_source_evidence(self):
  """Exact immutable SQL source inventory for explicit startup adoption only."""
  with self._connection() as db:return self._recovery_source_evidence(db)

 def all_snapshot(self,*,include_history=False):
  """Managed background inventory; current canonical products in one revision."""
  return self.read_snapshot(None,include_history=include_history)
 def read_snapshot(self,identities=None,*,include_history=False):
  """One revision of current heads/last-good; full history is explicit audit only."""
  if type(include_history) is not bool:raise ValueError('Invalid history mode')
  result={}
  with self._connection() as db:
   revision=db.execute('SELECT revision FROM meta WHERE id=1').fetchone()[0]
   inventory=identities is None
   if identities is None:identities=[ProductIdentity(**self._identity(r['identity'],r['id'])) for r in db.execute('SELECT id,identity FROM products ORDER BY id')]
   for identity in identities:
    key=identity.key
    candidates=([db.execute('SELECT * FROM products WHERE id=?',(key,)).fetchone()] if inventory else self._identity_candidates(db,identity))
    if len(candidates)>1:
     result[key]=self._ambiguous_identity(db,identity,candidates,revision);continue
    product=candidates[0] if candidates else None
    if product is None:result[key]=None;continue
    product_id=product['id']
    head_rows=list(db.execute('SELECT * FROM heads WHERE product_id=?',(product_id,)))
    if include_history:
     observations={r['id']:self._observation(r) for r in db.execute('SELECT * FROM observations WHERE product_id=? ORDER BY revision',(product_id,))}
    else:
     ids=sorted({r[field] for r in head_rows for field in ('current_id','last_good_id') if r[field]})
     observations={}
     for offset in range(0,len(ids),400):
      chunk=ids[offset:offset+400];marks=','.join('?' for _ in chunk)
      observations.update({r['id']:self._observation(r) for r in db.execute(f'SELECT * FROM observations WHERE product_id=? AND id IN ({marks})',(product_id,*chunk))})
    if any(r['current_id'] not in observations or r['last_good_id'] and r['last_good_id'] not in observations for r in head_rows):raise CatalogueStoreError('Broken catalogue head')
    heads=[dict(host=r['host'],url=r['url'],current=observations[r['current_id']],last_good=observations.get(r['last_good_id'])) for r in head_rows]
    allocation=db.execute('SELECT * FROM acquisitions WHERE product_id=?',(product_id,)).fetchone()
    acquisition={k:allocation[k] for k in ('status','error','generation','lease_until')} if allocation else None
    completion=self._completion(db,product_id,allocation)
    result[key]=dict(product_id=product_id,identity=self._identity(product['identity'],product_id),created_at=product['created_at'],known=True,revision=revision,heads=heads,history=list(observations.values()) if include_history else [],history_included=include_history,coverage={r['host']:self._coverage(r) for r in db.execute('SELECT * FROM coverage WHERE product_id=?',(product_id,))},refresh_attempts=[dict(host=r['host'],url=r['url'],attempted_at=r['attempted_at'],**self._decode(r['payload'])) for r in db.execute('SELECT * FROM attempts WHERE product_id=? ORDER BY attempted_at,id',(product_id,))],acquisition=dict(acquisition) if acquisition else None,acquisition_owner=allocation['owner'] if allocation else None,acquisition_completion=completion)
  return dict(revision=revision,products=result)


def require_catalogue_completion(snapshot,positions,known,*,configured_hosts,primary_hosts,components_sha256,runtime_sha256):
 """Final-only guard on a trusted frozen server snapshot, never client metadata."""
 from ..equipment_output_policy import is_graftio_equipment
 from .acquisition_frontier import validate_completion_receipt,CompletionUnknown
 missing=[]
 for index in known:
  position=positions[index]
  if getattr(position,'is_unique',False) or is_graftio_equipment(position):continue
  identity=identity_for_position(position);product=snapshot.get('products',{}).get(identity.key)
  receipt=product.get('acquisition_completion') if product else None
  try:
   if not receipt:raise CompletionUnknown('completion_unknown')
   value=validate_completion_receipt(receipt);allocation=product.get('acquisition') or {}
   if (allocation.get('status')!='done' or allocation.get('error') or allocation.get('generation')!=value['generation']
       or product['product_id']!=value['product_id'] or identity.key!=value['requested_identity_sha256']
       or value['configured_hosts']!=list(configured_hosts) or value['primary_hosts']!=list(primary_hosts)
       or value['components_sha256']!=components_sha256
       or value['runtime_sha256']!=(runtime_sha256(value) if callable(runtime_sha256) else runtime_sha256)):raise CompletionUnknown('completion_binding_changed')
  except (CompletionUnknown,KeyError,TypeError,ValueError) as error:
   missing.append(str(getattr(position,'number',index))+': '+str(error))
 if missing:raise CatalogueConflict('catalogue_completion_required: '+'; '.join(missing))
