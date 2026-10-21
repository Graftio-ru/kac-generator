"""Dated primary-document links are discovery obligations, never quote authority."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from copy import deepcopy
import base64,hashlib,json,math,re,time
from urllib.parse import urljoin,urlsplit
from ..config import CACHE_DIR
from .session import bounded_timeout
from .product_identity import canonical_card_url,card_host
from .http_card import MainParser,_semantic_visible,_semantic_text,itemtype
from .whitelist import whitelist_hosts

MAX_CHILDREN=10
MAX_SOURCE_AGE=120
_RECEIVER=ContextVar('independent_card_link_receiver',default=None)
@contextmanager
def receive_sources(receiver):
 token=_RECEIVER.set(receiver)
 try:yield
 finally:_RECEIVER.reset(token)
def delivered(record,source):
 receiver=_RECEIVER.get()
 if receiver is not None:
  bounded_timeout(0);receiver(record,source)

def _safe_url(value):
 if type(value) is not str or len(value)>8192 or re.search(r'[\\\x00-\x20\x7f]',value):return None
 try:
  parts=urlsplit(value)
  if parts.scheme!='https' or parts.username or parts.password or parts.port not in (None,443) or parts.fragment:return None
 except ValueError:return None
 canonical=canonical_card_url(value)
 return canonical if canonical==value else None

def source_record(source):
 if type(source) is not dict or set(source)!={'ref','record_sha256','body_sha256'} or not re.fullmatch(r'http_cards/[0-9a-f]{32}\.json',source.get('ref','')):raise ValueError('card_link_source_invalid')
 root=CACHE_DIR.resolve();path=CACHE_DIR/source['ref']
 if path.is_symlink() or path.parent.is_symlink() or path.resolve().parent!=root/'http_cards' or path.stat().st_size>4*1024*1024:raise ValueError('card_link_source_invalid')
 raw=path.read_bytes()
 if hashlib.sha256(raw).hexdigest()!=source['record_sha256']:raise ValueError('card_link_source_changed')
 record=json.loads(raw)
 required={'version','requested_url','url','observed_at','status','content_type','encoding','body_base64','body_sha256'}
 if set(record)!=required or type(record['version']) is not int or record['version']!=1 or type(record['status']) is not int or record['status']!=200 or 'html' not in record['content_type'].lower():raise ValueError('card_link_source_invalid')
 body=base64.b64decode(record['body_base64'],validate=True)
 if len(body)>2*1024*1024 or hashlib.sha256(body).hexdigest()!=record['body_sha256'] or record['body_sha256']!=source['body_sha256']:raise ValueError('card_link_body_changed')
 if not _safe_url(record['requested_url']) or not _safe_url(record['url']) or card_host(record['url'])!=card_host(record['requested_url']):raise ValueError('card_link_source_url')
 return record,body

def discover(source,position,parent_url,*,at=None):
 """Re-derive the whole bounded visible anchor set; no guessed SKU authority."""
 record,body=source_record(source);at=time.time() if at is None else at
 stamp=record['observed_at'];host=card_host(parent_url)
 if (type(stamp) not in (int,float) or not math.isfinite(stamp) or type(at) not in (int,float) or not math.isfinite(at) or record['requested_url']!=parent_url or host not in whitelist_hosts()):raise ValueError('card_link_source_stale_or_foreign')
 field=lambda k:position.get(k,'') if isinstance(position,dict) else getattr(position,k,'')
 mark=field('type_mark')
 if type(mark) is not str or not re.fullmatch(r'[\w][\w./-]{2,255}',mark):return dict(owner=None,candidates=[],incomplete=False)
 try:text=body.decode(record['encoding'],errors='strict')
 except (UnicodeError,LookupError):return dict(owner=None,candidates=[],incomplete=True)
 derived=deepcopy(_extract(text,record['url'],parent_url,mark))
 # Link freshness follows the native neutral-cache window. An old source may
 # still be usable by the independent financial parser; never reject that
 # parent merely because discovery cannot authorize a new child now.
 if not 0<=at-stamp<=MAX_SOURCE_AGE and derived['candidates']:
  return dict(owner=derived['owner'],candidates=[],incomplete=True)
 return derived

def _hint_landmark(node,owner):
 while node is not owner:
  if node.tag in {'header','nav','footer','aside'} or set(node.attrs.get('role','').lower().split()) & {'banner','navigation','contentinfo','complementary'}:return True
  node=node.parent
 return False

def _structural_hint_owner(parser,nodes,heading):
 """A non-financial discovery scope; never body or an unrelated landmark."""
 # This additional layout requires a wholly closed parse. Existing semantic
 # main/article ownership retains its historical behavior above this fallback.
 if len(parser.stack)!=1 or any(n.tag in {'main','article'} and _semantic_visible(n) for n in nodes):return None
 void={'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
 for index,node in enumerate(nodes):
  if index%256==0:bounded_timeout(0)
  if node.tag=='root' or node.tag in void:continue
  if (parser.source[node.end-len(node.tag)-3:node.end].lower()!='</'+node.tag+'>'
      and parser.source[node.end-2:node.end]!='/>' ):return None
 products=[n for n in nodes if itemtype(n,'Product') and _semantic_visible(n)]
 if len(products)!=1:return None
 product=products[0]
 owner=heading
 while owner is not None and product not in owner.descendants():owner=owner.parent
 if owner is None or owner.tag in {'root','html','body'} or not _semantic_visible(owner):return None
 # Cross-document header/navigation references do not establish a product
 # discovery container merely because an outer div encloses both branches.
 if _hint_landmark(owner,parser.root) or any(_hint_landmark(node,owner) for node in (heading,product)):return None
 return owner

@lru_cache(maxsize=8)
def _extract(text,url,parent_url,mark):
 # Cache immutable parsing only; source integrity/time/approved-host checks run
 # before every use, including durable receipt verification.
 token=re.compile(r'(?<![\w./-])'+re.escape(mark)+r'(?![\w./-])',re.I)
 try:parser=MainParser(text)
 except (ValueError,UnicodeError,LookupError,RecursionError):return dict(owner=None,candidates=[],incomplete=True)
 nodes=list(parser.root.descendants())
 if len(nodes)>40000 or any(n.tag in {'main','article','h1','a'} or itemtype(n,'Product') for n in parser.stack[1:]):return dict(owner=None,candidates=[],incomplete=True)
 headings=[n for n in nodes if n.tag=='h1' and _semantic_visible(n)]
 if len(headings)!=1:return dict(owner=None,candidates=[],incomplete=True)
 heading=headings[0]
 roots=[n for n in nodes if (n.tag in {'main','article'} or itemtype(n,'Product')) and _semantic_visible(n) and heading in n.descendants()]
 # Explicit main/article owns related-card links as hints, not their prices.
 main=[n for n in roots if n.tag=='main'];articles=[n for n in roots if n.tag=='article']
 owner=main[0] if len(main)==1 else articles[0] if len(articles)==1 else roots[0] if len(roots)==1 else None
 structural=False
 if owner is None and not roots:
  owner=_structural_hint_owner(parser,nodes,heading);structural=owner is not None
 if owner is None:return dict(owner=None,candidates=[],incomplete=True)
 canon=[n.attrs.get('href','') for n in nodes if n.tag=='link' and n.attrs.get('rel','').lower()=='canonical']
 if canon and (len(set(canon))!=1 or _safe_url(urljoin(url,canon[0]))!=url):return dict(owner=None,candidates=[],incomplete=True)
 candidates=[];seen={parent_url,url};overflow=False
 for anchor in owner.descendants():
  if anchor.tag!='a' or not _semantic_visible(anchor) or structural and _hint_landmark(anchor,owner):continue
  label=_semantic_text(anchor).strip()
  if not label or not token.search(label):continue
  if len(label)>4096:overflow=True;continue
  raw=anchor.attrs.get('href','')
  if not raw or '#' in raw or re.search(r'[\\\x00-\x20\x7f]',raw):continue
  target=_safe_url(urljoin(url,raw))
  if not target or card_host(target)!=card_host(parent_url) or target in seen:continue
  seen.add(target)
  if len(candidates)>=MAX_CHILDREN:overflow=True;continue
  candidates.append(dict(url=target,host=card_host(parent_url),hint={}))
 identity=dict(path=owner.path(),sha256=hashlib.sha256(parser.source[owner.start:owner.end].encode()).hexdigest(),title=_semantic_text(heading).strip())
 if structural:identity['scope']='h1_product_common_ancestor_discovery_hint_v1'
 return dict(owner=identity,candidates=candidates,incomplete=overflow)
