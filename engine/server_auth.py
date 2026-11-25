"""Optional single-account HTTP Basic boundary. TLS terminates at trusted proxy.

Pure import: no Settings loader, network, user database or secret generation.
"""
import base64,binascii,hashlib,hmac,json,re
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlsplit

_MACHINE=ContextVar('authenticated_runner_machine',default=False)
_ACTOR=ContextVar('authenticated_estimator_actor',default=None)
class ServerConfigurationError(ValueError):pass

@dataclass(frozen=True)
class ServerPolicy:
 mode:str='local'
 username:str=''
 origin:str=''
 iterations:int=0
 salt:bytes=b''
 password_digest:bytes=b''
 machine:object=None


def policy_from_settings(settings):
 mode=getattr(settings,'deployment_mode','local')
 if mode=='local':
  if getattr(settings,'execution_mode','local')!='local':raise ServerConfigurationError('remote_execution_requires_server')
  return ServerPolicy()
 if mode!='server':raise ServerConfigurationError('server_mode_invalid')
 username=getattr(settings,'server_username','')
 if type(username)is not str or not re.fullmatch(r'[A-Za-z0-9._@-]{1,128}',username):raise ServerConfigurationError('server_username_required')
 origin=getattr(settings,'server_public_origin','')
 try:
  parts=urlsplit(origin);port=parts.port
  if parts.scheme!='https'or not parts.hostname or parts.username or parts.password or parts.path not in ('','/')or parts.query or parts.fragment or re.search(r'[\x00-\x20\x7f]',origin):raise ValueError()
  normalized='https://'+parts.netloc.lower()
 except (ValueError,TypeError):raise ServerConfigurationError('server_https_origin_required')from None
 raw=getattr(settings,'server_password_hash','')
 if hasattr(raw,'get_secret_value'):raw=raw.get_secret_value()
 if type(raw)is not str:raise ServerConfigurationError('server_password_hash_required')
 match=re.fullmatch(r'pbkdf2_sha256\$([0-9]{6,7})\$([a-f0-9]{32,128})\$([a-f0-9]{64})',raw)
 if not match or not 600000<=int(match[1])<=2000000 or len(match[2])%2:raise ServerConfigurationError('server_password_hash_required')
 result=ServerPolicy('server',username,normalized,int(match[1]),bytes.fromhex(match[2]),bytes.fromhex(match[3]))
 if getattr(settings,'execution_mode','local')=='remote':
  from types import SimpleNamespace
  from dataclasses import replace
  machine=policy_from_settings(SimpleNamespace(deployment_mode='server',server_username=getattr(settings,'runner_username',''),server_password_hash=getattr(settings,'runner_password_hash',''),server_public_origin=normalized))
  if machine.username==username:raise ServerConfigurationError('runner_credential_must_be_distinct')
  result=replace(result,machine=machine)
 return result


def approval_actor():
 """Only middleware-authenticated request context can claim server identity."""
 if _MACHINE.get():raise ServerConfigurationError('runner_cannot_impersonate_estimator')
 context=_approval_command_context()
 if context is not None:return context.actor
 actor=_ACTOR.get()
 if actor:return actor
 from .config import settings
 if getattr(settings,'deployment_mode','local')=='server':raise ServerConfigurationError('server_authenticated_actor_required')
 return 'local-estimator (unverified local user)'


class ServerAuthMiddleware:
 def __init__(self,app,policy):self.app=app;self.policy=policy
 async def _reply(self,send,status,detail,challenge=False):
  headers=[(b'content-type',b'application/json'),(b'cache-control',b'no-store')]
  if challenge:headers.append((b'www-authenticate',b'Basic realm="Estimator", charset="UTF-8"'))
  raw=json.dumps({'detail':detail},separators=(',',':')).encode()
  await send({'type':'http.response.start','status':status,'headers':headers});await send({'type':'http.response.body','body':raw})
 def _authorized(self,headers,policy=None):
  policy=policy or self.policy
  values=[v for k,v in headers if k.lower()==b'authorization']
  if len(values)!=1 or len(values[0])>4096:return False
  try:
   scheme,encoded=values[0].split(b' ',1)
   if scheme.lower()!=b'basic':return False
   decoded=base64.b64decode(encoded,validate=True)
   username,password=decoded.split(b':',1)
   if not password or len(password)>1024:return False
   good_user=hmac.compare_digest(username,policy.username.encode('utf-8'))
   digest=hashlib.pbkdf2_hmac('sha256',password,policy.salt,policy.iterations)
   return hmac.compare_digest(digest,policy.password_digest)and good_user
  except (ValueError,binascii.Error,UnicodeError):return False
 async def __call__(self,scope,receive,send):
  if scope['type']not in {'http','websocket'}or self.policy.mode=='local':return await self.app(scope,receive,send)
  if scope['type']=='websocket':return await send({'type':'websocket.close','code':1008})
  # Minimal liveness discloses no service/provider/data configuration.
  if scope.get('path')=='/healthz'and scope.get('method')in {'GET','HEAD'}:return await self.app(scope,receive,send)
  headers=scope.get('headers',[])
  hosts=[v.decode('latin1')for k,v in headers if k.lower()==b'host']
  if scope.get('scheme')!='https'or len(hosts)!=1 or 'https://'+hosts[0].lower()!=self.policy.origin:return await self._reply(send,400,'server_https_origin_required')
  origins=[v.decode('latin1')for k,v in headers if k.lower()==b'origin']
  sites=[v.decode('latin1')for k,v in headers if k.lower()==b'sec-fetch-site']
  modes=[v.decode('latin1')for k,v in headers if k.lower()==b'sec-fetch-mode']
  destinations=[v.decode('latin1')for k,v in headers if k.lower()==b'sec-fetch-dest']
  document_navigation=(scope.get('method')in {'GET','HEAD'}and scope.get('path')in {'/','/index.html','/base.html'}and sites in (['cross-site'],['same-site'])and modes==['navigate']and destinations==['document'])
  if len(origins)>1 or origins and origins[0]!=self.policy.origin or any(site not in {'same-origin','none'}for site in sites)and not document_navigation:return await self._reply(send,403,'server_cross_origin_denied')
  machine_path=scope.get('path','').startswith('/runner/v1/')
  policy=self.policy.machine if machine_path else self.policy
  if machine_path and policy is None:return await self._reply(send,404,'runner_disabled')
  if not self._authorized(headers,policy):return await self._reply(send,401,'authentication_required',True)
  token=_ACTOR.set(None if machine_path else 'server-basic:'+self.policy.username)
  machine_token=_MACHINE.set(machine_path)
  count=0
  body_limit=64*1024*1024 if machine_path and '/artifacts/'in scope.get('path','')else 16*1024*1024
  lengths=[v for k,v in headers if k.lower()==b'content-length']
  if len(lengths)>1 or lengths and (not lengths[0].isdigit()or int(lengths[0])>body_limit):
   _MACHINE.reset(machine_token);_ACTOR.reset(token)
   return await self._reply(send,413,'request_body_too_large')
  async def bounded_receive():
   nonlocal count
   message=await receive();count+=len(message.get('body',b''))
   if count>body_limit:raise ValueError('request_body_too_large')
   return message
  try:return await self.app(scope,bounded_receive,send)
  finally:_MACHINE.reset(machine_token);_ACTOR.reset(token)

# Private native bridge only. No public request schema accepts actor/context or
# a Python ownership callback. The future runner bridge supplies the exact actor
# stored by control HTTP authentication and rechecks its owned immutable claim.
from contextlib import contextmanager
import threading
_COMMAND_ACTOR=ContextVar('verified_runner_approval_command',default=None)
_COMMAND_ISSUER=object()

@dataclass(frozen=True)
class _ApprovalCommandContext:
 command_id:str
 kind:str
 payload_sha256:str
 actor:str
 payload_json:str
 control:object
 owner_thread:int
 issuer:object
 def check(self):
  if self.issuer is not _COMMAND_ISSUER or self.owner_thread!=threading.get_ident()or _MACHINE.get():raise ServerConfigurationError('approval_runner_context_untrusted')
  expected={'command_id':self.command_id,'kind':self.kind,'payload_sha256':self.payload_sha256,'actor':self.actor}
  if self.control()!=expected:raise ServerConfigurationError('approval_runner_claim_changed')
  return self

@contextmanager
def _runner_approval_command(command_id,kind,payload,actor,*,control):
 """Server-internal capability: caller MUST be the verified control transport.

The callback revalidates immutable server actor/request/source/lease binding;
HTTP headers or serialized client booleans cannot substitute for this scope.
 """
 if type(command_id)is not str or not re.fullmatch('[a-f0-9]{12}',command_id)or kind not in {'candidate_review','candidate_confirm','candidate_revoke'}or type(actor)is not str or not re.fullmatch('server-basic:[A-Za-z0-9._@-]{1,128}',actor)or not callable(control):raise ServerConfigurationError('approval_runner_context_invalid')
 try:raw=json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
 except(ValueError,TypeError):raise ServerConfigurationError('approval_runner_payload_invalid')from None
 if type(payload)is not dict or len(raw.encode())>16*1024*1024:raise ServerConfigurationError('approval_runner_payload_invalid')
 context=_ApprovalCommandContext(command_id,kind,hashlib.sha256(raw.encode()).hexdigest(),actor,raw,control,threading.get_ident(),_COMMAND_ISSUER);context.check()
 token=_COMMAND_ACTOR.set(context)
 try:yield context
 finally:_COMMAND_ACTOR.reset(token)

def _approval_command_context(kind=None,payload=None):
 context=_COMMAND_ACTOR.get()
 if context is None:return None
 if type(context)is not _ApprovalCommandContext:raise ServerConfigurationError('approval_runner_context_untrusted')
 context.check()
 if kind is not None and context.kind!=kind:raise ServerConfigurationError('approval_runner_kind_changed')
 if payload is not None:
  raw=json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
  if hashlib.sha256(raw.encode()).hexdigest()!=context.payload_sha256:raise ServerConfigurationError('approval_runner_payload_changed')
 return context
