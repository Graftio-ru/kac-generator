"""Typed safe transport cause retention; no requests are sent."""
import errno,json,socket,ssl,unittest
from dataclasses import asdict
from unittest.mock import patch
import httpx
from engine.provider_transport import safe_transport_details,ProviderAttempt,_run_provider
from engine.search import search_engine as search
from engine.search.failure_policy import SearchServiceUnavailable
from engine.research_publication import valid_label

class SafeTransportTests(unittest.TestCase):
 def wrap(self,cause):
  error=httpx.ConnectError('SECRET https://user:password@host/query?key=SECRET');error.__cause__=cause;return error
 def test_known_typed_causes_and_no_messages(self):
  for cause,category in [(socket.gaierror(socket.EAI_NONAME,'SECRET'),'dns'),(ssl.SSLError('SECRET'),'tls'),(httpx.ProxyError('SECRET'),'proxy'),(httpx.ReadTimeout('SECRET'),'timeout'),(ConnectionRefusedError(errno.ECONNREFUSED,'SECRET'),'connect')]:
   value=safe_transport_details(self.wrap(cause));self.assertEqual(value['transport_category'],category);self.assertNotIn('SECRET',json.dumps(value))
  self.assertEqual(safe_transport_details(self.wrap(ConnectionRefusedError(errno.ECONNREFUSED,'SECRET')))['transport_errno'],errno.ECONNREFUSED)
 def test_http_status_and_unknown_untrusted_chain(self):
  response=httpx.Response(503,request=httpx.Request('GET','https://private.invalid/?key=SECRET'));e=httpx.HTTPStatusError('SECRET',request=response.request,response=response)
  self.assertEqual(safe_transport_details(e),dict(transport_category='http_status',transport_errno=None,transport_http_status=503))
  class Bad(RuntimeError):
   def __str__(self):raise AssertionError('never format')
  e=Bad();e.__cause__=e;self.assertEqual(safe_transport_details(e)['transport_category'],'unknown')
  for _ in range(18):other=Bad();other.__cause__=e;e=other
  self.assertEqual(safe_transport_details(e)['transport_category'],'unknown')
 def test_native_search_firststop_and_trace_roundtrip_preserve_policy(self):
  error=self.wrap(socket.gaierror(socket.EAI_NONAME,'SECRET'))
  with patch.object(httpx,'get',side_effect=error)as wire:
   with self.assertRaises(SearchServiceUnavailable)as caught:search._search_get('https://unused.invalid')
  self.assertEqual(wire.call_count,1);failure=caught.exception;self.assertEqual(failure.context.cause,'transport_error');self.assertFalse(failure.transport_attempts[0].retryable)
  first=json.loads(json.dumps(failure.diagnostic()));self.assertEqual(first['search_transport_attempts'][0]['transport_category'],'dns');self.assertNotIn('SECRET',json.dumps(first))
  from engine.search.failure_policy import SearchTransportReport
  report=SearchTransportReport('a'*32,1,failure.transport_attempts,.1)
  label=json.dumps(report.diagnostic());self.assertTrue(valid_label('search_transport_evidence',label));bad=json.loads(label);bad['search_transport_attempts'][0]['transport_category']='SECRET';self.assertFalse(valid_label('search_transport_evidence',json.dumps(bad)))
 def test_provider_native_exit_retains_category_and_safe_label(self):
  error=self.wrap(ssl.SSLError('SECRET'))
  def fail(*a):raise error
  with patch('engine.provider_transport._supported',return_value=False):
   with self.assertRaises(Exception)as caught:_run_provider('cloudru',object(),fail)
  attempt=caught.exception.provider_attempts[0];self.assertEqual(attempt.transport_category,'tls');self.assertFalse(attempt.proven_presend)
  label=json.dumps(asdict(attempt));self.assertTrue(valid_label('provider_transport',label));self.assertNotIn('SECRET',label)
 def test_legacy_labels_still_admitted_and_mixed_causes_unknown(self):
  value=asdict(ProviderAttempt('a'*32,'cloudru',1,'unknown','error','ConnectError',1.,.1,False,False));self.assertTrue(valid_label('provider_transport',json.dumps(value)))
  for key in ['transport_category','transport_errno','transport_http_status']:value.pop(key)
  self.assertTrue(valid_label('provider_transport',json.dumps(value)))
  error=self.wrap(ssl.SSLError('SECRET'));error.__context__=socket.gaierror(socket.EAI_NONAME,'SECRET');self.assertEqual(safe_transport_details(error)['transport_category'],'unknown')

 def test_native_account_gate_retains_same_typed_details(self):
  from engine.config import settings
  from engine.search.search_route import resolve_initial_route
  from contextlib import ExitStack
  error=self.wrap(socket.gaierror(socket.EAI_NONAME,'SECRET'))
  class Client:
   def __init__(self,*a,**k):pass
   def __enter__(self):return self
   def __exit__(self,*a):pass
   def stream(self,*a,**k):raise error
  with ExitStack()as stack:
   for key,value in [('xmlriver_user','offline'),('xmlriver_key','offline'),('xmlriver_url','https://xmlriver.com/search_yandex/xml'),('xmlriver_backend_preference','auto')]:stack.enter_context(patch.object(settings,key,value))
   stack.enter_context(patch.object(httpx,'Client',Client))
   with self.assertRaises(SearchServiceUnavailable)as caught:resolve_initial_route()
  value=json.loads(json.dumps(caught.exception.diagnostic()));attempt=value['search_transport_attempts'][0]
  self.assertEqual(value['failure_cause'],'transport_error');self.assertEqual(attempt['transport_category'],'dns');self.assertFalse(attempt['retryable']);self.assertEqual(len(value['search_transport_attempts']),1);self.assertNotIn('SECRET',json.dumps(value))
