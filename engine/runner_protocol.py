"""Versioned remote research boundary; no business authority or Settings imports.

Closed parse/research/known-card/build and candidate approval commands share one
durable dispatch. Control never owns a native catalogue; capture/base commands
remain unavailable. Approval recovery reads only committed native receipts.
Published values are immutable runner snapshots, never freshly checked prices.
"""
import hashlib,json,re
from pathlib import PurePosixPath
from typing import TypedDict, cast
from urllib.parse import urlsplit
MAX_JSON=16*1024*1024
MAX_ARTIFACT=64*1024*1024
MAX_TOTAL=256*1024*1024
MAX_MEMBERS=128
class ProtocolError(ValueError):pass

def canonical(value):
 try:return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
 except (ValueError,TypeError):raise ProtocolError('runner_json_invalid')from None

def digest(value):return hashlib.sha256(canonical(value)).hexdigest()
def identifier(value):
 if type(value)is not str or not re.fullmatch('[a-f0-9]{12}',value):raise ProtocolError('runner_job_invalid')
 return value

def bounded(value):
 raw=canonical(value)
 if len(raw)>MAX_JSON:raise ProtocolError('runner_json_too_large')
 return json.loads(raw)

def manifest(value):
 if type(value)is not list or len(value)>MAX_MEMBERS:raise ProtocolError('runner_manifest_invalid')
 names=set();total=0
 for entry in value:
  if type(entry)is not dict or set(entry)!={'name','sha256','bytes','content_type'}:raise ProtocolError('runner_manifest_invalid')
  name=entry['name']
  if type(name)is not str or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',name)or name in names or '..'in name:raise ProtocolError('runner_artifact_name_invalid')
  names.add(name)
  if type(entry['sha256'])is not str or not re.fullmatch('[a-f0-9]{64}',entry['sha256']):raise ProtocolError('runner_artifact_hash_invalid')
  if type(entry['bytes'])is not int or not 0<=entry['bytes']<=MAX_ARTIFACT:raise ProtocolError('runner_artifact_size_invalid')
  if entry['content_type']not in {'application/json','application/zip','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','image/png','image/jpeg','application/pdf'}:raise ProtocolError('runner_artifact_type_invalid')
  total+=entry['bytes']
 if total>MAX_TOTAL:raise ProtocolError('runner_artifacts_too_large')
 return bounded(value)

def public_snapshot(value,job_id,entries=()):
 value=bounded(value)
 if type(value)is not dict or value.get('job_id')!=job_id:raise ProtocolError('runner_snapshot_invalid')
 # Client-facing cache locators can ONLY resolve registered immutable bytes.
 by_name={e['name']:e for e in entries}
 def walk(item):
  if isinstance(item,dict):
   for key,child in list(item.items()):
    if key in {'screenshot_path','screenshot_url','context_capture_path','context_capture_url','screenshot_product','screenshot_context','screenshot_requisites'}and isinstance(child,str):
     name=PurePosixPath(child.replace('\\','/')).name
     item[key]=f'/runner-results/{job_id}/{by_name[name]["sha256"]}'if name in by_name else ''
    else:walk(child)
  elif isinstance(item,list):
   for child in item:walk(child)
 walk(value)
 value['runner_snapshot']=True
 value['runner_snapshot_current_validation']=False
 return value


# Closed commands; upload names are display metadata, never server paths.
INPUT_SUFFIXES={'.pdf','.docx','.xlsx','.xlsm','.xls','.png','.jpg','.jpeg','.tif','.tiff'}
def command(value: object) -> dict:
 """Validate and return a bounded closed dispatch envelope without side effects.

 Legacy research input is wrapped as version1. Known-card targets are checked
 here before queue persistence; full native positions are validated by the
 executor. ProtocolError means refusal, never an empty/successful command.
 Existing parse/build/candidate command validation is unchanged.
 """
 value=bounded(value)
 if type(value)is not dict:raise ProtocolError('runner_command_invalid')
 if 'version'not in value:
  if set(value)!={'positions','acquisition_mode'}:raise ProtocolError('runner_command_invalid')
  return {'version':1,'kind':'research','payload':value}
 supported_kinds = {
  'parse', 'research', 'known_cards', 'known_cards_new_capture', 'build',
  'candidate_review', 'candidate_confirm', 'candidate_revoke',
 }
 if (
  set(value) != {'version', 'kind', 'payload'}
  or type(value['version']) is not int
  or value['version'] != 1
  or value['kind'] not in supported_kinds
  or type(value['payload']) is not dict
 ):
  raise ProtocolError('runner_command_invalid')
 if value['kind'] == 'known_cards':
  known_cards_payload(value['payload'])
 if value['kind'] == 'known_cards_new_capture':
  new_capture_payload(value['payload'])
 if value['kind'].startswith('candidate_'):
  payload=value['payload'];kind=value['kind']
  fields={'approval_id','expected_revision','reason'}if kind=='candidate_revoke'else{'position','catalogue_ref'}
  if kind=='candidate_confirm':fields|={'expected_material_sha256','confirmed_product_title','confirmed_article','confirmed_raw_amount','confirmed_sale_unit','requested_unit','units_per_sale_unit','reason'}
  if set(payload)!=fields:raise ProtocolError('runner_approval_payload_invalid')
  if kind!='candidate_revoke'and(type(payload['position'])is not dict or type(payload['catalogue_ref'])is not dict or set(payload['catalogue_ref'])!={'product_id','observation_id'}):raise ProtocolError('runner_approval_source_invalid')
 if value['kind']=='research' and set(value['payload'])!={'positions','acquisition_mode'}:raise ProtocolError('runner_research_invalid')
 if value['kind']=='parse':
  q=value['payload']
  if set(q)!={'object_name','discipline','unique_names','sources'}or any(type(q[k])is not str for k in ('object_name','discipline','unique_names')):raise ProtocolError('runner_parse_invalid')
  sources=q['sources']
  if type(sources)is not list or not 1<=len(sources)<=20:raise ProtocolError('runner_inputs_invalid')
  seen=set();total=0
  for x in sources:
   if type(x)is not dict or set(x)!={'kind','index','filename','suffix','sha256','bytes'}:raise ProtocolError('runner_input_invalid')
   if x['kind']not in {'spec','letter'}or type(x['index'])is not int or not 0<=x['index']<20 or (x['kind'],x['index'])in seen:raise ProtocolError('runner_input_index_invalid')
   seen.add((x['kind'],x['index']))
   if type(x['filename'])is not str or len(x['filename'])>255 or any(ord(c)<32 for c in x['filename'])or x['suffix']not in INPUT_SUFFIXES or PurePosixPath(x['filename']).suffix.lower()!=x['suffix']:raise ProtocolError('runner_input_name_invalid')
   if type(x['sha256'])is not str or not re.fullmatch('[a-f0-9]{64}',x['sha256'])or type(x['bytes'])is not int or not 0<x['bytes']<=MAX_ARTIFACT:raise ProtocolError('runner_input_size_invalid')
   total+=x['bytes']
  for kind in ('spec','letter'):
   indices=sorted(x['index']for x in sources if x['kind']==kind)
   if indices!=list(range(len(indices))):raise ProtocolError('runner_input_index_invalid')
  if total>MAX_TOTAL:raise ProtocolError('runner_inputs_too_large')
 return value


class KnownCardTarget(TypedDict):
    """One URL bound to its zero-based original position index."""

    index: int
    url: str


class KnownCardsPayload(TypedDict):
    """Validated frontier structure; PositionIn validates each native record later."""

    positions: list[dict]
    targets: list[KnownCardTarget]
    extract: bool


MAX_KNOWN_CARD_TARGETS = 2
MAX_KNOWN_CARD_URL_LENGTH = 4096


def known_cards_payload(value: object) -> KnownCardsPayload:
    """Validate exact keys, index coverage, and distinct credential-free HTTPS URLs.

    Returns the original payload without deriving URLs or changing source data.
    ProtocolError rejects malformed values before persistence or transport. The
    final cast follows runtime validation of every declared structural field;
    native PositionIn validation remains the executor's separate responsibility.
    """
    if type(value) is not dict or set(value) != {"positions", "targets", "extract"}:
        raise ProtocolError("runner_known_cards_invalid")
    positions = value["positions"]
    targets = value["targets"]
    if (
        type(positions) is not list
        or not 1 <= len(positions) <= MAX_KNOWN_CARD_TARGETS
        or any(type(position) is not dict for position in positions)
        or type(targets) is not list
        or len(targets) != len(positions)
        or type(value["extract"]) is not bool
    ):
        raise ProtocolError("runner_known_cards_invalid")
    seen_indices: set[int] = set()
    seen_urls: set[str] = set()
    for target in targets:
        if (
            type(target) is not dict
            or set(target) != {"index", "url"}
            or type(target["index"]) is not int
            or not 0 <= target["index"] < len(positions)
            or target["index"] in seen_indices
        ):
            raise ProtocolError("runner_known_cards_invalid")
        url = target["url"]
        try:
            if (
                type(url) is not str
                or not 1 <= len(url) <= MAX_KNOWN_CARD_URL_LENGTH
                or re.search(r"[\\\x00-\x20\x7f]", url)
            ):
                raise ValueError()
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
                or parsed.port not in (None, 443)
                or url in seen_urls
            ):
                raise ValueError()
        except ValueError:
            raise ProtocolError("runner_known_card_url_invalid") from None
        seen_indices.add(target["index"])
        seen_urls.add(url)
    return cast(KnownCardsPayload, value)


def new_capture_payload(value: object) -> dict:
    """Validate a closed server-stamped command without treating JSON as capability.

    The original known-card DTO remains unchanged. The executor independently
    binds actor/source/time to its authenticated claim before issuing authority.
    """
    if type(value) is not dict or set(value) != {"positions", "targets", "extract", "authorization"}:
        raise ProtocolError("runner_new_capture_invalid")
    known_cards_payload({key: value[key] for key in ("positions", "targets", "extract")})
    from .search.known_capture_intent import validate_authority
    try:
        validate_authority(value["authorization"], value["targets"])
    except ValueError:
        raise ProtocolError("runner_new_capture_invalid") from None
    return value
