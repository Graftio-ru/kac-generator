"""Durable local estimator authority, separate from immutable machine evidence."""
import hashlib,json,math,time,uuid,re
from decimal import Decimal,InvalidOperation

class ApprovalInvalid(ValueError):pass

def _field(v,k,d=''):return v.get(k,d)if isinstance(v,dict)else getattr(v,k,d)
def _json(v):return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def _hash(v):return hashlib.sha256(_json(v).encode()).hexdigest()
def _number(v):
 if type(v)not in(int,float)or not math.isfinite(v)or v<=0:raise ApprovalInvalid('approval_positive_number_required')
 return Decimal(str(v))
def _position(p,o):
 result={k:_field(p,k)for k in('name','type_mark','unit')}
 if o.get('purchase_terms'):result['quantity']=_field(p,'qty')
 return result

def _material(o,p):
 keys=('product_id','identity','url','title','manufacturer_code','raw_amount','currency','sale_unit','seller_inn','seller_name','vat','purchase_terms','region_status','region_evidence','state')
 source={k:o.get(k)for k in keys}
 source['seller_status']=(o.get('seller_evidence')or{}).get('decision',{}).get('status','unknown')
 proof=o.get('unit_equivalence')or{}
 source['conversion']={'basis':proof.get('basis'),'quantity':proof.get('quantity'),'lot_quantity':o.get('lot_quantity'),'package_quantity':o.get('package_quantity')}
 return {'source':source,'position':_position(p,o)}

def _current(store,reference,position):
 from .catalogue import identity_for_position
 if type(reference)is not dict or set(reference)!={'product_id','observation_id'}:raise ApprovalInvalid('approval_source_required')
 record=store.resolve_observation(reference['product_id'],reference['observation_id'])
 product=store.lookup(identity_for_position(position))
 if not record or not record['current']or not product or product['product_id']!=reference['product_id']:raise ApprovalInvalid('approval_current_product_required')
 observation=record['observation'];material=_material(observation,position)
 if hashlib.sha256(observation['source_text'].encode()).hexdigest()!=observation['source_sha256']:raise ApprovalInvalid('approval_source_changed')
 return observation,material

def _exists(db):return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='estimator_approval_events'").fetchone()is not None

def _decode(row):
 if not row:return None
 value=json.loads(row['payload'])
 if _hash(value)!=row['sha256']or value.get('approval_id')!=row['id']or value.get('revision')!=row['revision']:raise ApprovalInvalid('approval_record_changed')
 return value

def _latest(db,approval_id):
 if not _exists(db):return None
 return _decode(db.execute('SELECT * FROM estimator_approval_events WHERE id=? ORDER BY revision DESC LIMIT 1',(approval_id,)).fetchone())

def _firm_current_price(observation):
 """Human product/unit authority cannot make a conditional/old price firm."""
 from ..source_quote import primary_text,selected_price_context,_pricing_terms,firm_price_role_reason
 raw=observation.get('raw_amount')
 if type(raw)not in(int,float)or not math.isfinite(raw)or raw<=0:return False
 text=primary_text(observation['source_text'])
 price_text,unit_text=selected_price_context(text,raw)
 terms=observation.get('purchase_terms','')+' '+_pricing_terms(text,raw)
 if firm_price_role_reason(price_text,unit_text,terms):return False
 if re.search(r'старая\s+цена|прошлая\s+цена|до\s+скидк',price_text,re.I):return False
 if re.search(r'клуб|по\s+карт[еы]|при\s+(?:покупк|заказ)|оптов|от\s+\d+\s*(?:шт|м)',unit_text+' '+price_text+' '+terms,re.I):return False
 return True

def active_approval(store,observation,position):
 if store is None or position is None or not _firm_current_price(observation):return None
 if hashlib.sha256(observation['source_text'].encode()).hexdigest()!=observation['source_sha256']:raise ApprovalInvalid('approval_source_changed')
 material=_material(observation,position);digest=_hash(material)
 with store._connection()as db:
  if not _exists(db):return None
  rows=db.execute("SELECT e.* FROM estimator_approval_events e JOIN (SELECT id,MAX(revision) revision FROM estimator_approval_events GROUP BY id) l ON e.id=l.id AND e.revision=l.revision WHERE json_extract(e.payload,'$.material_sha256')=? ORDER BY e.rowid DESC",(digest,)).fetchall()
  for row in rows:
   value=_decode(row)
   if value['status']=='approved'and value['material_sha256']==digest and _json(value['material'])==_json(material):return value
 return None

def _machine_reasons(store,observation,position):
 old=active_approval(store,observation,position)
 if old:return old['original_machine_reasons']
 from .catalogue_projection import catalogue_scope,qualify_observation
 with catalogue_scope(store):reason=qualify_observation(observation,position).reason
 return [reason]if reason else []

def review(store,reference,position):
 from ..server_auth import _approval_command_context
 context=_approval_command_context('candidate_review')
 observation,material=_current(store,reference,position)
 approval=active_approval(store,observation,position)
 return {'candidate':{'catalogue_ref':reference,'material_sha256':_hash(material),'position':{k:_field(position,k)for k in('number','name','type_mark','unit','qty')},'product':{'title':observation['title'],'article':observation['manufacturer_code']},'seller':{'name':observation['seller_name'],'inn':observation['seller_inn']},'raw_amount':observation['raw_amount'],'currency':observation['currency'],'sale_unit':observation['sale_unit'],'observed_at':observation['observed_at'],'url':observation['url'],'machine_reasons':_machine_reasons(store,observation,position),'freshness':'Original source observation date; approval does not refresh it.'},'approval':approval}

def _command_outcome(db,context):
 if context is None:return None
 if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='estimator_command_outcomes'").fetchone()is None:return None
 row=db.execute('SELECT * FROM estimator_command_outcomes WHERE command_id=?',(context.command_id,)).fetchone()
 if not row:return None
 try:value=json.loads(row['payload'])
 except(ValueError,TypeError):raise ApprovalInvalid('approval_command_record_changed')from None
 if type(value)is not dict or set(value)!={'version','command_id','kind','payload_sha256','actor','approval'}or type(value['version'])is not int or value['version']!=1:raise ApprovalInvalid('approval_command_record_changed')
 if _hash(value)!=row['sha256']or value.get('command_id')!=context.command_id or value.get('kind')!=context.kind or value.get('payload_sha256')!=context.payload_sha256 or value.get('actor')!=context.actor:raise ApprovalInvalid('approval_command_changed')
 approval=value['approval'];latest=_latest(db,approval['approval_id'])
 if latest is None or _json(latest)!=_json(approval):raise ApprovalInvalid('approval_command_outcome_no_longer_current')
 return approval

def _record_command(db,context,approval):
 if context is None:return
 value={'version':1,'command_id':context.command_id,'kind':context.kind,'payload_sha256':context.payload_sha256,'actor':context.actor,'approval':approval}
 db.execute('CREATE TABLE IF NOT EXISTS estimator_command_outcomes(command_id TEXT PRIMARY KEY,payload TEXT NOT NULL,sha256 TEXT NOT NULL)')
 db.execute('INSERT INTO estimator_command_outcomes VALUES(?,?,?)',(context.command_id,_json(value),_hash(value)))
 context.check()  # Late lease/control loss rolls back BOTH event and receipt.

def _read_committed_command(store):
 """Private receipt-only recovery; never reruns an approval mutation."""
 from ..server_auth import _approval_command_context
 context=_approval_command_context()
 if context is None:raise ApprovalInvalid('approval_command_context_required')
 with store._connection()as db:
  value=_command_outcome(db,context)
  if value is None:raise ApprovalInvalid('approval_command_not_committed')
  if context.kind=='candidate_confirm':
   from ..schemas import PositionIn
   payload=json.loads(context.payload_json);position=PositionIn.model_validate(payload['position']).to_result().position
   observation,material=_current(store,payload['catalogue_ref'],position)
   if _json(material)!=_json(value['material'])or not _firm_current_price(observation):raise ApprovalInvalid('approval_candidate_changed')
  context.check();return value


def confirm(store,reference,position,request):
 from ..server_auth import _approval_command_context
 context=_approval_command_context('candidate_confirm',request)
 with store._connection(True)as db:
  observation,material=_current(store,reference,position)
  prior_command=_command_outcome(db,context)
  if prior_command is not None:
   if _json(material)!=_json(prior_command['material'])or not _firm_current_price(observation):raise ApprovalInvalid('approval_candidate_changed')
   return prior_command
  if request['expected_material_sha256']!=_hash(material):raise ApprovalInvalid('approval_candidate_changed')
  if request['confirmed_product_title']!=observation['title']or request['confirmed_article']!=observation['manufacturer_code']:raise ApprovalInvalid('approval_product_changed')
  raw=_number(request['confirmed_raw_amount']);factor=_number(request['units_per_sale_unit'])
  if raw!=_number(observation['raw_amount']):raise ApprovalInvalid('approval_price_changed')
  from ..source_quote import firm_price_role_reason
  if observation['currency']!='RUB'or observation['vat']=='excluded'or not _firm_current_price(observation):raise ApprovalInvalid('approval_independent_financial_requirement')
  normalized=float(raw/factor);_number(normalized)
  sale=request['confirmed_sale_unit'];wanted=request['requested_unit'];reason=request['reason']
  if type(sale)is not str or not 0<len(sale.strip())<=64 or wanted!=_field(position,'unit')or type(reason)is not str or not 1<=len(reason.strip())<=2048:raise ApprovalInvalid('approval_confirmation_incomplete')
  prior=active_approval(store,observation,position)
  from ..server_auth import approval_actor
  value={'version':1,'approval_id':prior['approval_id']if prior else uuid.uuid4().hex,'revision':prior['revision']+1 if prior else 1,'status':'approved','actor':approval_actor(),'approved_at':time.time(),'original_observed_at':observation['observed_at'],'source_reference':reference,'material':material,'material_sha256':_hash(material),'confirmed_product_title':observation['title'],'confirmed_article':observation['manufacturer_code'],'confirmed_raw_amount':float(raw),'confirmed_sale_unit':sale.strip(),'requested_unit':wanted,'units_per_sale_unit':float(factor),'normalized_price':normalized,'reason':reason.strip(),'original_machine_reasons':_machine_reasons(store,observation,position),'resolved_reasons':['product','price','sale_unit_conversion']}
  db.execute('CREATE TABLE IF NOT EXISTS estimator_approval_events(id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(id,revision))')
  db.execute('INSERT INTO estimator_approval_events VALUES(?,?,?,?)',(value['approval_id'],value['revision'],_json(value),_hash(value)));store._revision(db)
  _record_command(db,context,value)
  return value

def revoke(store,approval_id,expected_revision,reason):
 from ..server_auth import _approval_command_context
 context=_approval_command_context('candidate_revoke',{'approval_id':approval_id,'expected_revision':expected_revision,'reason':reason})
 if type(expected_revision)is not int or type(reason)is not str or not 1<=len(reason.strip())<=2048:raise ApprovalInvalid('approval_revoke_invalid')
 with store._connection(True)as db:
  prior_command=_command_outcome(db,context)
  if prior_command is not None:return prior_command
  value=_latest(db,approval_id)
  if not value or value['status']!='approved'or value['revision']!=expected_revision:raise ApprovalInvalid('approval_revision_changed')
  from ..server_auth import approval_actor
  value={**value,'revoked_by':approval_actor(),'status':'revoked','revision':value['revision']+1,'revoked_at':time.time(),'revoke_reason':reason.strip()}
  db.execute('INSERT INTO estimator_approval_events VALUES(?,?,?,?)',(value['approval_id'],value['revision'],_json(value),_hash(value)));store._revision(db);_record_command(db,context,value);return value

def valid_reference(ref):
 return type(ref)is dict and set(ref)=={'approval_id','revision'}and type(ref['approval_id'])is str and len(ref['approval_id'])==32 and all(c in '0123456789abcdef'for c in ref['approval_id'])and type(ref['revision'])is int and ref['revision']>0

def reference(value):return {'approval_id':value['approval_id'],'revision':value['revision']}if value else {}
def facts(value):
 return {'price':value['normalized_price'],'quantity':value['units_per_sale_unit'],'unit_text':value['confirmed_sale_unit'],'price_text':str(value['confirmed_raw_amount']),'unit_basis':'estimator_confirmed','unit_rationale':'Подтверждено сметчиком: '+value['reason'],'estimator_approval':value}
