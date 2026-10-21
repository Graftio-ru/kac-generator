"""Finite job-bound route selection and normal XML consumer, no actual wires."""
import json,math,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from engine.search import search_route as routes,search_engine as search,session
from engine.search.failure_policy import SearchServiceUnavailable,SearchCapacityDeferred
from engine import llm

PRIMARY='https://xmlriver.com/search_yandex/xml'
CAPS={'engine':{'yandex':0,'google':1},'balance_positive':True}

class SearchRouteTests(unittest.TestCase):
 def setUp(self):
  for key,value in [('xmlriver_url',PRIMARY),('xmlriver_lr',213),('xmlriver_user','offline-user'),('xmlriver_key','offline-key')]:
   p=patch.object(search.settings,key,value);p.start();self.addCleanup(p.stop)
  self.google=routes.select_route(CAPS,PRIMARY,213)
 def test_positive_selection_strict_profile_and_scope_reset(self):
  self.assertEqual(self.google['locale'],{'lr':'ru','country':2643});self.assertEqual(self.google['endpoint'],'https://xmlriver.com/search/xml')
  primary=routes.select_route(dict(CAPS,engine={'yandex':1,'google':1}),PRIMARY,213);self.assertEqual(primary['backend'],'xmlriver-yandex')
  with routes.search_route_scope(self.google):
   detached=routes.active_search_route();detached['locale']['lr']='en';self.assertEqual(routes.active_search_route()['locale']['lr'],'ru')
   with routes.search_route_scope(None):self.assertIsNone(routes.active_search_route())
  self.assertIsNone(routes.active_search_route())
  for bad in (dict(self.google,document_limit=True),dict(self.google,endpoint=PRIMARY),dict(self.google,locale={'lr':213}),dict(self.google,unknown='field')):
   with self.subTest(bad=bad),self.assertRaises(SearchServiceUnavailable):routes.validate_route(bad)
 def test_disabled_malformed_balance_and_foreign_routes_never_choose_alternate(self):
  for capabilities in (dict(CAPS,balance_positive=False),dict(CAPS,engine={'yandex':0,'google':0}),dict(CAPS,engine={'yandex':False,'google':1}),{},dict(CAPS,balance_positive=1)):
   with self.subTest(capabilities=capabilities),self.assertRaises(SearchServiceUnavailable):routes.select_route(capabilities,PRIMARY,213)
  for url in ('https://xmlstock.com/search','https://xmlriver.com/yandex/xml','https://xmlriver.com:123/search_yandex/xml'):
   self.assertIsNone(routes.select_route(CAPS,url,213))
 def admin(self,payload,status=200,stream=None):
  calls=[];real=httpx.Client
  def handle(request):
   calls.append(request);return httpx.Response(status,stream=stream or httpx.ByteStream(json.dumps(payload).encode()),headers={'content-type':'application/json'})
  def client(**kwargs):return real(transport=httpx.MockTransport(handle),**kwargs)
  return calls,patch.object(httpx,'Client',side_effect=client)
 def test_resolver_single_admin_read_safe_receipt_and_no_foreign_probe(self):
  calls,client=self.admin({'engine':{'yandex':0,'google':1},'balance':100,'private':'not-retained'})
  with client:profile,receipt=routes.resolve_initial_route()
  self.assertEqual(profile,self.google);self.assertEqual(len(calls),1);self.assertEqual(receipt['search_requests'],0);self.assertEqual(receipt['account_api_calls'],1);self.assertNotIn('private',receipt);self.assertNotIn('balance',receipt);self.assertEqual(receipt['selection_reason'],'primary_engine_disabled')
  with patch.object(search.settings,'xmlriver_url','https://xmlstock.com/search'),patch.object(httpx,'Client',side_effect=AssertionError('No admin fallback')):
   self.assertEqual(routes.resolve_initial_route(),(None,None))
  with patch.object(search.settings,'xmlriver_user',''),patch.object(search.settings,'xmlriver_key',''),patch.object(httpx,'Client',side_effect=AssertionError('No account probe')):
   self.assertEqual(routes.resolve_initial_route(),(None,None))
 def test_resolver_typed_http_and_malformed_errors_have_no_retry(self):
  for payload,status,category in (({},200,'invalid_structure'),({'balance':0,'engine':CAPS['engine']},200,'account_balance'),({'error':{'code':200}},200,'account_balance'),({},403,'account_access')):
   calls,client=self.admin(payload,status)
   with client,self.assertRaises(SearchServiceUnavailable) as caught:routes.resolve_initial_route()
   self.assertEqual(caught.exception.context.cause,category);self.assertEqual(len(calls),1)
 def test_resolver_byte_cap_parent_cancel_and_global_stop(self):
  calls,client=self.admin({},stream=httpx.ByteStream(b' '*65537))
  with client,self.assertRaises(SearchServiceUnavailable):routes.resolve_initial_route()
  self.assertEqual(len(calls),1)
  with session.research_session(deadline=time.monotonic()-1),patch.object(httpx,'Client',side_effect=AssertionError('No wire')):
   with self.assertRaises(session.SearchBudgetExceeded):routes.resolve_initial_route()
  guard=llm.LLMRequestGuard();guard.reject(SearchServiceUnavailable('account_access'))
  with llm.llm_request_scope(guard),patch.object(httpx,'Client',side_effect=AssertionError('No wire')):
   with self.assertRaises(SearchServiceUnavailable):routes.resolve_initial_route()
 def test_admin_postwire_cancel_and_encoding_bound_close_without_fallback(self):
  real=httpx.Client;closed=[];guard=llm.LLMRequestGuard()
  class Stream(httpx.SyncByteStream):
   def __iter__(self):
    yield json.dumps({'balance':100,'engine':{'yandex':0,'google':1}}).encode()
    guard.reject(SearchServiceUnavailable('account_access'))
   def close(self):closed.append(True)
  calls=[]
  def handler(request):calls.append(request);return httpx.Response(200,stream=Stream())
  with llm.llm_request_scope(guard),patch.object(httpx,'Client',side_effect=lambda **kw:real(transport=httpx.MockTransport(handler),**kw)):
   with self.assertRaises(SearchServiceUnavailable):routes.resolve_initial_route()
  self.assertEqual(len(calls),1);self.assertTrue(closed)
  consumed=[]
  class Compressed(httpx.SyncByteStream):
   def __iter__(self):consumed.append(True);yield b'not a permitted representation'
   def close(self):pass
  with patch.object(httpx,'Client',side_effect=lambda **kw:real(transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={'content-encoding':'gzip'},stream=Compressed())),**kw)):
   with self.assertRaises(SearchServiceUnavailable):routes.resolve_initial_route()
  self.assertEqual(consumed,[])
 def test_account_transport_fixed_types_one_request_and_secret_free_diagnostic(self):
  secret='SYNTHETIC_CREDENTIAL_NEVER_PERSIST'
  request=httpx.Request('GET','https://'+secret+':password@xmlriver.com/api/get_info/?key='+secret,headers={'Authorization':secret},content=secret)
  unknown=type(secret,(httpx.ReadTimeout,),{})
  cases=[(getattr(httpx,name),name) for name in ('ConnectTimeout','ReadTimeout','WriteTimeout','PoolTimeout','ConnectError','ReadError','WriteError','CloseError','ProxyError','RemoteProtocolError','LocalProtocolError','UnsupportedProtocol','DecodingError','TooManyRedirects')]+[(unknown,'HTTPError'),(httpx.HTTPError,'HTTPError')]
  for cls,expected in cases:
   with self.subTest(error=expected):
    calls=[];options=[];real=httpx.Client
    error=cls(secret,request=request) if cls is not httpx.HTTPError else cls(secret)
    def handle(wire_request):calls.append(wire_request);raise error
    def client(**kw):options.append(kw);return real(transport=httpx.MockTransport(handle),**kw)
    with patch.object(httpx,'Client',side_effect=client),self.assertRaises(SearchServiceUnavailable) as caught:
     routes.resolve_initial_route()
    self.assertEqual(len(options),1);self.assertIs(options[0]['follow_redirects'],False)
    self.assertGreater(options[0]['timeout'].read,0);self.assertLessEqual(options[0]['timeout'].read,10)
    self.assertLessEqual(options[0]['timeout'].connect,10)
    failure=caught.exception;diagnostic=failure.diagnostic()
    self.assertEqual(len(calls),1);self.assertEqual(failure.context.cause,'transport_error')
    self.assertEqual(diagnostic['failure_source'],'search_api');self.assertEqual(diagnostic['failure_scope'],'global')
    self.assertEqual(diagnostic['search_transport_attempt_count'],1)
    attempt=diagnostic['search_transport_attempts'][0]
    self.assertEqual(set(attempt),{'error_type','observed_at','elapsed_seconds','retryable'})
    self.assertEqual(attempt['error_type'],expected);self.assertIs(attempt['retryable'],False)
    self.assertTrue(math.isfinite(attempt['observed_at']));self.assertGreater(attempt['observed_at'],0)
    self.assertTrue(math.isfinite(attempt['elapsed_seconds']));self.assertGreaterEqual(attempt['elapsed_seconds'],0)
    self.assertEqual(diagnostic['search_transport_elapsed_seconds'],attempt['elapsed_seconds'])
    self.assertEqual(diagnostic['observed_at'],attempt['observed_at'])
    encoded=json.dumps(diagnostic,allow_nan=False)
    for forbidden in (secret,'password','Authorization','api/get_info','key='):
     self.assertNotIn(forbidden,encoded)
 def test_account_transport_sanitizes_nonfinite_or_negative_elapsed(self):
  real=httpx.Client
  def fail(request):raise httpx.ConnectError('synthetic')
  for end in (float('nan'),float('inf'),-1.0):
   # Keep deadline checks real; replace only timestamps adjacent to the wire.
   from types import SimpleNamespace
   original=routes.time.monotonic;calls=[0]
   def clock():
    calls[0]+=1
    if calls[0]==3:return 1.0
    if calls[0]==5:return end
    return original()
   with self.subTest(end=end),patch.object(routes,'time',SimpleNamespace(time=time.time,monotonic=clock)),patch.object(httpx,'Client',side_effect=lambda **kw:real(transport=httpx.MockTransport(fail),**kw)),self.assertRaises(SearchServiceUnavailable) as caught:
    routes.resolve_initial_route()
   self.assertEqual(caught.exception.diagnostic()['search_transport_elapsed_seconds'],0.0)
 def test_account_wire_control_failure_is_preserved_without_transport_conversion(self):
  from engine.search.failure_policy import ResearchControlError
  real=httpx.Client
  for failure in (ResearchControlError('synthetic-control'),session.SearchBudgetExceeded('synthetic-deadline'),SearchServiceUnavailable('account_access')):
   calls=[]
   def handle(request):calls.append(request);raise failure
   with patch.object(httpx,'Client',side_effect=lambda **kw:real(transport=httpx.MockTransport(handle),**kw)),self.assertRaises(type(failure)) as caught:
    routes.resolve_initial_route()
   self.assertIs(caught.exception,failure);self.assertEqual(len(calls),1)
   self.assertFalse(getattr(failure,'transport_attempts',()))
 def test_account_validation_branches_are_fixed_private_and_one_request(self):
  secret='SYNTHETIC_ACCOUNT_PRIVATE_NEVER_PERSIST';good={'balance':1,'engine':{'yandex':1,'google':0}}
  cases=[('content_encoding',b'',{'content-encoding':'gzip'}),('response_size',b' '*65537,{}),
   ('json_decode',('{"private":"'+secret).encode(),{}),('unicode_decode',b'\xff',{}),
   ('payload_type',json.dumps([secret]).encode(),{}),('balance_type',json.dumps(dict(good,balance=secret)).encode(),{}),
   ('balance_nonfinite',json.dumps(dict(good,balance=float('nan'))).encode(),{}),
   ('capability_shape',json.dumps(dict(good,engine=[secret])).encode(),{}),
   ('engine_flags',json.dumps(dict(good,engine={'yandex':True,'google':0})).encode(),{})]
  real=httpx.Client
  for expected,body,headers in cases:
   with self.subTest(branch=expected):
    calls=[]
    def handle(request):calls.append(request);return httpx.Response(200,headers=headers,stream=httpx.ByteStream(body))
    with patch.object(httpx,'Client',side_effect=lambda **kw:real(transport=httpx.MockTransport(handle),**kw)),self.assertRaises(SearchServiceUnavailable) as caught:
     routes.resolve_initial_route()
    diagnostic=caught.exception.diagnostic();self.assertEqual(len(calls),1)
    self.assertEqual(diagnostic['search_validation_branch'],expected)
    self.assertEqual(diagnostic['failure_scope'],'global');self.assertEqual(diagnostic['search_error_category'],'invalid_structure')
    encoded=json.dumps(diagnostic,allow_nan=False)
    for forbidden in (secret,'api/get_info','offline-user','offline-key','content-encoding','yandex','balance'):
     # The fixed balance branch name is permitted; no numeric/account value is.
     if forbidden=='balance':continue
     self.assertNotIn(forbidden,encoded)
  for cls,expected in [(type(secret,(ValueError,),{}),'structure_exception'),
                       (type(secret,(TypeError,),{}),'structure_exception'),
                       (type(secret,(UnicodeError,),{}),'unicode_decode'),
                       (type(secret,(json.JSONDecodeError,),{}),'json_decode')]:
   error=cls(secret,secret,0) if issubclass(cls,json.JSONDecodeError) else cls(secret)
   calls=[]
   class FailedStream(httpx.SyncByteStream):
    def __iter__(self):raise error;yield b''
   def handle(request):calls.append(request);return httpx.Response(200,stream=FailedStream())
   with patch.object(httpx,'Client',side_effect=lambda **kw:real(transport=httpx.MockTransport(handle),**kw)),self.assertRaises(SearchServiceUnavailable) as caught:
    routes.resolve_initial_route()
   self.assertEqual(len(calls),1);diagnostic=caught.exception.diagnostic()
   self.assertEqual(diagnostic['search_validation_branch'],expected);self.assertNotIn(secret,json.dumps(diagnostic))
 def test_validation_branch_allowlist_default_and_mutated_subclass_privacy(self):
  from engine.search.failure_policy import _SEARCH_VALIDATION_BRANCHES
  secret='SYNTHETIC_UNKNOWN_BRANCH_NEVER_PERSIST'
  class HostileString(str):
   def __eq__(self,other):raise AssertionError('Do not invoke subclass equality')
   def __hash__(self):raise AssertionError('Do not invoke subclass hash')
  for value in (None,secret,'',{},[],1,True,HostileString('json_decode')):
   with self.subTest(kind=type(value).__name__):
    failure=SearchServiceUnavailable('invalid_structure',validation_branch=value)
    self.assertNotIn('search_validation_branch',failure.diagnostic());self.assertNotIn(secret,json.dumps(failure.diagnostic()))
  for category in ('account_balance','account_access','transport_error','configuration_invalid'):
   self.assertNotIn('search_validation_branch',SearchServiceUnavailable(category,validation_branch='json_decode').diagnostic())
  for branch in _SEARCH_VALIDATION_BRANCHES:
   self.assertEqual(SearchServiceUnavailable('invalid_structure',validation_branch=branch).diagnostic()['search_validation_branch'],branch)
  failure=SearchServiceUnavailable('invalid_structure',validation_branch='json_decode')
  for value in (secret,HostileString('json_decode'),{}):
   failure.validation_branch=value;self.assertNotIn('search_validation_branch',failure.diagnostic())
  legacy=SearchServiceUnavailable('invalid_structure').diagnostic()
  self.assertEqual(set(legacy),{'code','url','failure_source','failure_scope','failure_cause','observed_at','reason','search_error_category'})
 def test_validation_branch_native_first_stop_persists_before_discovery(self):
  from engine import app,test_catalogue_unknown as fixtures
  from engine.schemas import PositionIn
  fixture=fixtures.CatalogueUnknownTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
  position=PositionIn(number=1,name='Cable',type_mark='AAA123',unit='м',qty=1)
  failure=SearchServiceUnavailable('invalid_structure',validation_branch='engine_flags')
  with patch.object(routes,'resolve_initial_route',side_effect=failure) as resolve:
   job,public=fixture.job([position],search=lambda *a,**kw:self.fail('No discovery after account rejection'))
  self.assertEqual(resolve.call_count,1);self.assertEqual(job['status'],'error');self.assertEqual(job['completed'],0)
  self.assertEqual(job['first_stop']['search_validation_branch'],'engine_flags');self.assertEqual(fixture.calls,[]);self.assertEqual(fixture.captures,[])
  stored=json.loads(app._research_store().db.execute('SELECT payload FROM jobs WHERE job=?',(job['job_id'],)).fetchone()[0])
  self.assertEqual(stored['first_stop']['search_validation_branch'],'engine_flags')
  self.assertEqual(public['first_stop']['search_validation_branch'],'engine_flags')
 def test_operator_preference_native_settings_schema_and_auto_compatibility(self):
  from engine.config import Settings
  from pydantic import ValidationError
  self.assertEqual(Settings(_env_file=None).xmlriver_backend_preference,'auto')
  for value in ('auto','xmlriver-google'):
   self.assertEqual(Settings(xmlriver_backend_preference=value,_env_file=None).xmlriver_backend_preference,value)
  for value in ('google','xmlriver-yandex','AUTO','xmlriver-google ',1,None):
   with self.subTest(value=value),self.assertRaises(ValidationError):Settings(xmlriver_backend_preference=value,_env_file=None)
  caps={'engine':{'yandex':1,'google':1},'balance_positive':True}
  self.assertEqual(routes.select_route(caps,PRIMARY,213)['backend'],'xmlriver-yandex')
  self.assertEqual(routes.select_route(caps,PRIMARY,213,preference='auto')['backend'],'xmlriver-yandex')
  self.assertEqual(routes.select_route(caps,PRIMARY,213,preference='xmlriver-google'),self.google)
  self.assertEqual(caps['engine'],{'yandex':1,'google':1})
 def test_explicit_google_real_account_flags_receipt_and_no_silent_fallback(self):
  with patch.object(search.settings,'xmlriver_backend_preference','xmlriver-google'):
   calls,client=self.admin({'balance':100,'engine':{'yandex':1,'google':1}})
   with client:profile,receipt=routes.resolve_initial_route()
   self.assertEqual(profile,self.google);self.assertEqual(receipt['engine'],{'yandex':1,'google':1})
   self.assertEqual(receipt['selection_reason'],'operator_selected_google');self.assertEqual(len(calls),1)
   self.assertIs(receipt['balance_positive'],True);self.assertEqual(receipt['account_api_calls'],1)
   self.assertEqual(receipt['search_requests'],0)
   for payload,category in [({'balance':100,'engine':{'yandex':1,'google':0}},'selected_engine_unavailable'),
      ({'balance':100,'engine':{'yandex':0,'google':0}},'selected_engine_unavailable'),
      ({'balance':0,'engine':{'yandex':1,'google':1}},'account_balance'),
      ({'balance':-1,'engine':{'yandex':1,'google':1}},'account_balance'),
      ({'balance':'unknown','engine':{'yandex':1,'google':1}},'invalid_structure'),
      ({'balance':100,'engine':{'yandex':1,'google':True}},'invalid_structure')]:
    calls,client=self.admin(payload)
    with self.subTest(category=category),client,self.assertRaises(SearchServiceUnavailable) as caught:routes.resolve_initial_route()
    self.assertEqual(caught.exception.context.cause,category);self.assertEqual(len(calls),1)
    if category=='selected_engine_unavailable':
     self.assertIn('Выбранный поиск Google',str(caught.exception));self.assertNotIn('Основной и резервный',str(caught.exception))
 def test_explicit_preference_invalid_scope_is_prewire_and_control_priority_remains(self):
  cases=[('unknown',PRIMARY),('xmlriver-google','https://xmlstock.com/search'),
         ('xmlriver-google','https://xmlriver.com/search/xml'),('xmlriver-google','https://xmlriver.com:123/search_yandex/xml')]
  for preference,url in cases:
   with self.subTest(preference=preference,url=url),patch.object(search.settings,'xmlriver_backend_preference',preference),patch.object(search.settings,'xmlriver_url',url),patch.object(httpx,'Client',side_effect=AssertionError('No invalid-scope wire')):
    with self.assertRaises(SearchServiceUnavailable) as caught:routes.resolve_initial_route()
    self.assertEqual(caught.exception.context.cause,'configuration_invalid')
  with patch.object(search.settings,'xmlriver_backend_preference','xmlriver-google'),patch.object(search.settings,'xmlriver_user',''),patch.object(search.settings,'xmlriver_key',''),patch.object(httpx,'Client',side_effect=AssertionError('No missing-account wire')):
   with self.assertRaises(SearchServiceUnavailable) as caught:routes.resolve_initial_route()
   self.assertEqual(caught.exception.context.cause,'configuration_missing')
  for preference in ('xmlriver-google','unknown'):
   failure=SearchServiceUnavailable('account_access');guard=llm.LLMRequestGuard();guard.reject(failure)
   with patch.object(search.settings,'xmlriver_backend_preference',preference),llm.llm_request_scope(guard),patch.object(httpx,'Client',side_effect=AssertionError('No stopped wire')):
    with self.assertRaises(SearchServiceUnavailable) as caught:routes.resolve_initial_route()
    self.assertIs(caught.exception,failure)
   with patch.object(search.settings,'xmlriver_backend_preference',preference),patch.object(httpx,'Client',side_effect=AssertionError('No expired wire')):
    with self.assertRaises(session.SearchBudgetExceeded):routes.resolve_initial_route(deadline=time.monotonic()-1)
 def test_fresh_native_google_route_reason_and_real_receipt_before_discovery(self):
  from engine import app,test_catalogue_unknown as fixtures
  from engine.schemas import PositionIn
  from engine.search.search_engine import DiscoveryURLs
  resolver=routes.resolve_initial_route
  fixture=fixtures.CatalogueUnknownTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
  positions=[PositionIn(number=1,name='Cable',type_mark='AAA123',unit='м',qty=1)];seen=[]
  def discovery(query,hosts,**kwargs):
   key=next(k for k,v in app._RESEARCH_INPUTS.items() if v is positions)
   self.assertEqual(routes.active_search_route(),self.google)
   frontier=app._research_store().load_acquisition_frontier(key,0)['frontier']
   self.assertEqual(frontier['search_route'],self.google)
   header=json.loads(app._research_store().db.execute('SELECT payload FROM jobs WHERE job=?',(key,)).fetchone()[0])
   self.assertEqual(header['search_backend'],'xmlriver-google')
   self.assertEqual(header['search_backend_reason'],'operator_selected_google')
   self.assertGreater(header['search_backend_observed_at'],0)
   self.assertNotIn('_catalogue_search_receipt',header)
   receipt=app.RESEARCH_JOBS[key]['_catalogue_search_receipt']
   self.assertEqual(receipt['engine'],{'yandex':1,'google':1})
   self.assertEqual(receipt['selection_reason'],'operator_selected_google');seen.append(True)
   stamp=time.time();return DiscoveryURLs([],origin='response',window='below_limit',host_observations={h:dict(origin='response',window='below_limit',observed_at=stamp,candidate_count=0,diagnostics=[]) for h in hosts})
  calls,client=self.admin({'balance':100,'engine':{'yandex':1,'google':1}})
  with patch.object(search.settings,'xmlriver_backend_preference','xmlriver-google'),patch.object(routes,'resolve_initial_route',side_effect=resolver),client:
   job,public=fixture.job(positions,search=discovery)
  self.assertEqual(len(calls),1);self.assertTrue(seen);self.assertEqual(job['status'],'done',job.get('error'))
  self.assertEqual(public['search_backend'],'xmlriver-google');self.assertEqual(public['search_backend_reason'],'operator_selected_google')
  self.assertIsNone(routes.active_search_route())
 def xml(self,code=None,count=2):
  content='<error code="'+str(code)+'"/>' if code else '<response>'+''.join('<doc><url>https://chipdip.ru/product/c'+str(i)+'</url></doc>' for i in range(count))+'</response>'
  return httpx.Response(200,text='<yandexsearch>'+content+'</yandexsearch>')
 def test_normal_google_wire_profile_document_window_and_cache_isolation(self):
  with tempfile.TemporaryDirectory() as directory:
   from engine.search.discovery_cache import DiscoveryCache
   cache=DiscoveryCache(Path(directory)/'responses.sqlite3');calls=[]
   class CacheFactory(DiscoveryCache):
    def __init__(self):super().__init__(cache.path)
   def delivered(url,**kw):calls.append((url,kw['params']));return self.xml()
   with session.research_session(deadline=time.monotonic()+60),patch.object(search,'DiscoveryCache',CacheFactory),patch.object(search,'_search_get',side_effect=delivered),routes.search_route_scope(self.google):
    result=search.xmlriver_search('fixture-query',n=20)
    self.assertEqual(result.raw_count,2);self.assertEqual(result.limit,10);self.assertEqual(result.window,'below_limit')
    self.assertEqual(search.xmlriver_search('fixture-query').origin,'cache')
   primary=routes.select_route(dict(CAPS,engine={'yandex':1,'google':1}),PRIMARY,213)
   with session.research_session(deadline=time.monotonic()+60),patch.object(search,'DiscoveryCache',CacheFactory),patch.object(search,'_search_get',side_effect=delivered),routes.search_route_scope(primary):search.xmlriver_search('fixture-query')
   self.assertEqual(len(calls),2);self.assertEqual(calls[0][0],self.google['endpoint']);self.assertEqual(calls[0][1]['lr'],'ru');self.assertEqual(calls[0][1]['country'],2643);self.assertEqual(calls[0][1]['groupby'],10);self.assertEqual(calls[1][1]['lr'],213);self.assertNotIn('country',calls[1][1])
 def test_grouped_sites_google_operator_allowed_hosts_and_yandex_compatibility(self):
  from engine.search.discovery_cache import DiscoveryCache
  with tempfile.TemporaryDirectory() as directory:
   cache_path=Path(directory)/'responses.sqlite3';calls=[]
   class CacheFactory(DiscoveryCache):
    def __init__(self):super().__init__(cache_path)
   def delivered(url,**kw):
    calls.append((url,kw['params']))
    return httpx.Response(200,text='<yandexsearch><response><doc><url>https://chipdip.ru/product/a</url></doc><doc><url>https://elektro.ru/product/b</url></doc><doc><url>https://foreign.example/product/c</url></doc></response></yandexsearch>')
   primary=routes.select_route(dict(CAPS,engine={'yandex':1,'google':1}),PRIMARY,213)
   for label,profile,separator in [('google',self.google,' OR '),('yandex',primary,' | '),('legacy',None,' | ')]:
    with self.subTest(label=label),session.research_session(deadline=time.monotonic()+60),patch.object(search,'DiscoveryCache',CacheFactory),patch.object(search,'_search_get',side_effect=delivered),routes.search_route_scope(profile):
     result=search._serp_on_sites(label,['chipdip.ru','elektro.ru'])
     self.assertEqual(list(result),['https://chipdip.ru/product/a','https://elektro.ru/product/b'])
     self.assertEqual(result.raw_count,3);self.assertEqual(result.limit,10);self.assertEqual(result.window,'below_limit')
     self.assertEqual(set(result.host_observations),{'chipdip.ru','elektro.ru'})
     self.assertEqual(calls[-1][1]['query'],label+' (site:chipdip.ru'+separator+'site:elektro.ru)')
   self.assertEqual(len(calls),3);self.assertEqual(calls[0][0],self.google['endpoint']);self.assertEqual(calls[1][0],PRIMARY);self.assertEqual(calls[2][0],PRIMARY)
 def test_google_capacity_is_finite_and_account_error_never_cascades(self):
  guard=llm.LLMRequestGuard()
  with llm.llm_request_scope(guard),routes.search_route_scope(self.google),patch.object(httpx,'get',return_value=self.xml('111')) as wire,patch.object(search,'_wait_search_response_retry',return_value=None):
   with self.assertRaises(SearchCapacityDeferred):search.xmlriver_search('fixture-query')
  self.assertEqual(wire.call_count,2);self.assertTrue(all(call.args[0]==self.google['endpoint'] for call in wire.call_args_list));self.assertIsNotNone(guard.search_deferred())
  for code in ('200','31','102'):
   with llm.llm_request_scope(llm.LLMRequestGuard()),routes.search_route_scope(self.google),patch.object(httpx,'get',return_value=self.xml(code)) as wire:
    with self.assertRaises(SearchServiceUnavailable):search.xmlriver_search('fixture-query')
   self.assertEqual(wire.call_count,1);self.assertEqual(wire.call_args.args[0],self.google['endpoint'])
