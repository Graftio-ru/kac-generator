"""Private durable decoding: integrity is not current financial approval.

This module returns plain preserved JSON, never an observation object or a flag
that can bypass SourceObservation.payload at publication time. It does not parse
supplier DOM or consult current price/unit/attachment eligibility.
"""
import copy,hashlib,json,math,re
from dataclasses import asdict

COMMON={'version','role','source','url','observed_at','title','article','source_sha256','raw_amount','currency','sale_unit'}
V1=COMMON|{'purchase_quantity','price_text','unit_text','row_path','cash_path','input_path','unit_path','purchase_control_path','row_sha256'}
V2=COMMON|{'purchase_quantity','price_denominator','price_text','unit_text','terms_text','quotations','selected_quote','monetary_roles','structured_evidence','row_path','row_sha256','input_path','purchase_control_path','owner_path','owner_sha256','text_projection'}

def _need(value,code):
 if not value:raise ValueError(code)
def _same(a,b):
 # Existing writers admit equal finite integer/float amounts and epochs; never
 # conflate booleans with numbers or rewrite their original JSON spelling.
 if _number(a)and _number(b):return a==b
 return type(a)is type(b)and a==b
def _number(v):return type(v)in (int,float)and math.isfinite(v)
def _bound_json(value,limit):
 raw=json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':'))
 _need(len(raw.encode())<=limit,'stored_proof_oversize')
def _path(v,nullable=False):
 _need(nullable and v is None or type(v)is list and 0<len(v)<=128 and all(type(n)is int and 0<=n<=1000000 for n in v),'stored_proof_node_path')
def _source(value,stamp):
 from .catalogue import validate_primary_source,_digest,_stamp
 _need(type(value)is dict,'stored_purchase_source_shape')
 base={k:v for k,v in value.items()if k!='photo_binding'};validate_primary_source(base)
 _need(_same(base['observed_at']if base.get('role')=='http_primary_dom'else base['main_facts']['observed_at'],stamp),'stored_purchase_frame_epoch')
 if 'photo_binding'in value:
  binding=value['photo_binding'];_need(type(binding)is dict and set(binding)=={'after_main_facts','context'},'stored_photo_shape')
  after=binding['after_main_facts'];context=binding['context']
  _need(type(after)is dict and set(after)=={'ref','sha256','observed_at'} and type(after['ref'])is str and re.fullmatch(r'main_facts/[a-f0-9]{32}\.json',after['ref']),'stored_photo_frame')
  _digest(after['sha256']);_stamp(after['observed_at'])
  _need(type(context)is dict and context.get('version')==1 and type(context.get('version'))is int and context.get('role')=='review_context','stored_photo_context')
  _digest(context.get('sha256'));_stamp(context.get('captured_at'))
  _need(stamp<=context['captured_at']<=after['observed_at'] and after['observed_at']-stamp<=30,'stored_photo_epoch')
 return value

def _purchase(proof,observation):
 from .catalogue import _digest,_text,_amount,_stamp,_url
 _need(type(proof)is dict and COMMON.issubset(proof),'stored_purchase_shape');_bound_json(proof,131072)
 version=proof['version'];_need(type(version)is int and version>0 and proof['role']=='current_owned_purchase_row','stored_purchase_version')
 if version==1:_need(set(proof)==V1,'stored_purchase_v1_shape')
 elif version==2:_need(set(proof)in (V2,V2-{'structured_evidence'}),'stored_purchase_v2_shape')
 else:_need(set(proof).issubset(V1|V2),'stored_purchase_unknown_shape')
 for key in ('url','title','article','source_sha256','observed_at','raw_amount','currency','sale_unit'):
  target={'article':'manufacturer_code'}.get(key,key)
  _need(_same(proof[key],observation[target]),'stored_purchase_observation_binding')
 _url(proof['url']);_text(proof['title'],2048);_text(proof['article'],8192,empty=True);_digest(proof['source_sha256']);_stamp(proof['observed_at']);_amount(proof['raw_amount'])
 if version in {1,2}:
  lot=observation.get('lot_quantity');expected=proof.get('price_denominator',1)if version==2 else 1
  _need(lot is None or _number(lot)and lot==expected,'stored_purchase_lot_binding')
  package=observation.get('package_quantity');_need(package is None or _number(package)and package==1,'stored_purchase_package_binding')
 _source(proof['source'],proof['observed_at'])
 if 'photo_binding'in proof['source']:
  _need(proof['source']['photo_binding']['context'].get('url')==proof['url'],'stored_photo_url')
 for key in ('purchase_quantity','price_denominator'):
  if key in proof:_amount(proof[key])
 if version==1:_need(type(proof['purchase_quantity'])is int and proof['purchase_quantity']==1,'stored_purchase_v1_quantity')
 for key in ('row_path','cash_path','input_path','unit_path','purchase_control_path','owner_path'):
  if key in proof:_path(proof[key],nullable=version==1 and key in {'input_path','purchase_control_path'})
 for key in ('row_sha256','owner_sha256'):
  if key in proof:_digest(proof[key])
 for key in ('price_text','unit_text','terms_text','text_projection'):
  if key in proof:_text(proof[key],8192,empty=key=='terms_text')
 if version==2:
  _need(proof['text_projection']=='neutral-visible-owner-v1','stored_purchase_projection')
  quotes=proof['quotations'];selected=proof['selected_quote']
  _need(type(quotes)is list and 1<=len(quotes)<=64 and type(selected)is int and 0<=selected<len(quotes),'stored_purchase_quote_shape')
  for q in quotes:
   _need(type(q)is dict and set(q)=={'amount','currency','unit','denominator','price_path','denominator_path','literal'},'stored_purchase_quote_shape')
   for key in ('amount','currency','unit','denominator','literal'):_text(q[key],8192)
   _path(q['price_path']);_path(q['denominator_path'])
  from decimal import Decimal
  q=quotes[selected]
  from decimal import InvalidOperation
  try:bound=q['currency']==proof['currency'] and Decimal(q['amount'])==Decimal(str(proof['raw_amount'])) and Decimal(q['denominator'])==Decimal(str(proof['price_denominator']))
  except (InvalidOperation,ValueError,TypeError):raise ValueError('stored_purchase_quote_binding')from None
  _need(bound,'stored_purchase_quote_binding')
  _need(type(proof['monetary_roles'])is list and len(proof['monetary_roles'])<=128,'stored_purchase_roles')
  for role in proof['monetary_roles']:
   _need(type(role)is dict and set(role)=={'path','amount','currency','role','role_path'},'stored_purchase_roles')
   _path(role['path']);_path(role['role_path'])
   for key in ('amount','currency','role'):_text(role[key],256)
  _need(type(proof.get('structured_evidence',[]))is list and len(proof.get('structured_evidence',[]))<=128,'stored_purchase_structured')
  for entry in proof.get('structured_evidence',[]):
   _need(type(entry)is dict and set(entry)=={'script_path','product_sha256','offer'},'stored_purchase_structured')
   _path(entry['script_path']);_digest(entry['product_sha256'])
   if entry['offer']is not None:
    offer=entry['offer'];_need(type(offer)is dict and set(offer)=={'sha256','contextual_fields'}and type(offer['contextual_fields'])is list and len(offer['contextual_fields'])<=64 and all(type(v)is str and len(v)<=256 for v in offer['contextual_fields']),'stored_purchase_structured_offer');_digest(offer['sha256'])

UNIT_FIELDS={'version','kind','source_sha256','url','observed_at','raw_amount','currency','price_quantity','selected_cash','cash_span','quantity_span','subtotal_span','selected_quantity','subtotal','sale_unit'}
def _unit_basis(value,observation):
 from .catalogue import _text,_amount
 _need(type(value)is dict and set(value)==UNIT_FIELDS,'stored_unit_basis_shape');_bound_json(value,32768)
 _need(type(value['version'])is int and value['version']>0 and type(value['kind'])is str,'stored_unit_basis_version')
 for k in ('source_sha256','url','observed_at','raw_amount','currency'):_need(_same(value[k],observation[k]),'stored_unit_basis_binding')
 from ..price_capture import _unit
 _need(_unit(value['sale_unit'])==_unit(observation['sale_unit']),'stored_unit_basis_unit_binding')
 _need(_same(observation.get('lot_quantity'),1)and observation.get('package_quantity')is None,'stored_unit_basis_lot_binding')
 _need(type(value['price_quantity'])is int and value['price_quantity']==1,'stored_unit_basis_quantity')
 for k in ('selected_quantity','subtotal'):_amount(value[k])
 _need(math.isclose(value['selected_quantity']*value['raw_amount'],value['subtotal'],rel_tol=1e-8,abs_tol=.01),'stored_unit_basis_subtotal')
 for k in ('cash_span','quantity_span','subtotal_span'):
  span=value[k];_need(type(span)is list and len(span)==2 and all(type(n)is int for n in span) and 0<=span[0]<span[1]<=len(observation['source_text']),'stored_unit_basis_span')
 cash=value['selected_cash'];_need(type(cash)is dict and set(cash)=={'version','source','url','observed_at','title','listed_price','price_text'},'stored_selected_cash_shape')
 _need(type(cash['version'])is int and cash['version']==1 and cash['source']=='product_card_text','stored_selected_cash_version')
 for k,target in (('url','url'),('observed_at','observed_at'),('title','title'),('listed_price','raw_amount')):_need(_same(cash[k],observation[target]),'stored_selected_cash_binding')
 _text(cash['price_text'],8192)

def decode_stored_observation(raw):
 from .catalogue import SourceObservation,ProductIdentity,ImageEvidence,validate_native_request_contract,validate_primary_source,_PrimarySourceAbsent,_text,_json
 _need(type(raw)is dict,'stored_observation_shape');_bound_json(raw,1024*1024)
 identity=raw['identity'];identity=ProductIdentity(**{**identity,'features':tuple(tuple(v)for v in identity['features'])})
 observed=SourceObservation(**{**raw,'identity':identity,'image':ImageEvidence(**raw['image'])})
 observed._validate_integrity()
 validate_native_request_contract(observed.native_request_contract,identity,observed.issuer)
 _text(observed.unit_basis_reason,256,empty=True)
 _need(not(observed.unit_basis is not None and observed.unit_basis_reason),'stored_unit_basis_reason')
 if observed.unit_basis is not None:_unit_basis(observed.unit_basis,raw)
 if observed.purchase_row is not None:_purchase(observed.purchase_row,raw)
 if observed.unit_equivalence is not None:
  from .factory_reference import validate_observation_proof
  validate_observation_proof(observed.unit_equivalence,dict(url=observed.url,observed_at=observed.observed_at,source_sha256=observed.source_sha256,raw_amount=observed.raw_amount,identity=identity.payload()))
 if observed.primary_source is not _PrimarySourceAbsent.VALUE:validate_primary_source(observed.primary_source)
 if observed.seller_evidence is not None:
  from ..requisites.supplier import _seller_evidence_shape,_seller_digest
  value=observed.seller_evidence;_seller_evidence_shape(value,product_url=observed.url,captured_at=observed.observed_at)
  seen=set()
  for source in value['sources']:
   _need(source['id']==_seller_digest({k:v for k,v in source.items()if k!='id'})and source['id']not in seen,'stored_seller_hash');seen.add(source['id'])
   if source['origin']=='published_seller_terms':_need(source['binding']['context_sha256']==hashlib.sha256(source['role_text'].encode()).hexdigest(),'stored_seller_context_hash')
  if value['decision']['status']in {'role_confirmed','reference_accepted'}:
   accepted=[s for s in value['sources']if s['id']in value['decision']['resolved_source_ids']]
   _need(any(s['raw_name']==observed.seller_name and s['raw_inn']==observed.seller_inn for s in accepted),'stored_seller_observation_binding')
 return copy.deepcopy(raw)

def unsupported_qualification(observation):
 p=observation.get('purchase_row');u=observation.get('unit_basis')
 from .purchase_preview import PREVIEW_REASONS
 if observation.get('unit_basis_reason')and observation['unit_basis_reason']not in PREVIEW_REASONS:return 'stored_preview_reason_unsupported'
 if p is not None and p['version']not in {1,2}:return 'stored_purchase_version_unsupported'
 if u is not None and (u['version']!=1 or u['kind']!='owned_product_preview'):return 'stored_unit_basis_version_unsupported'
 return None

def validate_stored_attempt(value,row):
 from .catalogue import _host,_url,_stamp,_digest
 _need(type(value)is dict,'stored_attempt_shape');_bound_json(value,131072);_host(row['host']);url=_url(row['url']);_stamp(row['attempted_at'])
 from urllib.parse import urlsplit
 _need(urlsplit(url).hostname.removeprefix('www.')==row['host']or urlsplit(url).hostname.endswith('.'+row['host']),'stored_attempt_source_binding')
 item=value.get('card_operation')
 if item is None:return
 _need(type(item)is dict and set(item)=={'version','operation_id','state','evidence'}and type(item['version'])is int and item['version']==1 and type(item['operation_id'])is str and re.fullmatch('[0-9a-f]{32}',item['operation_id'])and item['state']in {'pending','resolved'}and type(item['evidence'])is dict,'stored_card_journal_shape')
 _bound_json(item['evidence'],8192)
 capture=item['evidence'].get('captured_input')
 if capture is not None:
  _need(type(capture)is dict and set(capture)=={'ref','record_sha256','snapshot_sha256','card_id','url','captured_at'}and type(capture['ref'])is str and re.fullmatch(r'captured_inputs/[0-9a-f]{32}\.json',capture['ref'])and capture['url']==url,'stored_capture_journal_binding')
  _digest(capture['record_sha256']);_digest(capture['snapshot_sha256']);_stamp(capture['captured_at']);_need(type(capture['card_id'])is str and re.fullmatch('[0-9a-f]{32}',capture['card_id']),'stored_capture_journal_card')
