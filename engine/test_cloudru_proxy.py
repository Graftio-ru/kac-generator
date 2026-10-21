"""Production Cloud-specific opt-in route contracts; socket denied by runner."""
import json,ssl,unittest
from unittest.mock import patch
import httpx
from engine import cloudru,llm
from engine.search import session
from engine.test_cloudru import response

class CloudProxyTests(unittest.TestCase):
    def setUp(self):
        for name,value in [('cloudru_api_key','offline-key'),('cloudru_proxy_server',''),('llm_provider','cloudru')]:
            p=patch.object(cloudru.settings,name,value);p.start();self.addCleanup(p.stop)
    def test_empty_cloud_route_is_direct_without_ambient_proxy_resolution(self):
        clients=[]
        def send(client,request,**kw):
            clients.append(client)
            self.assertFalse(client.trust_env)
            self.assertEqual(type(client._transport_for_url(request.url)._pool).__name__,'ConnectionPool')
            self.assertEqual(client._transport_for_url(request.url)._pool._ssl_context.verify_mode,ssl.CERT_REQUIRED)
            self.assertTrue(client._transport_for_url(request.url)._pool._ssl_context.check_hostname)
            self.assertEqual(request.headers['Authorization'],'Bearer offline-key')
            self.assertEqual(json.loads(request.content),{'model':'offline'})
            self.assertAlmostEqual(request.extensions['timeout']['read'],120,delta=1)
            self.assertIn('trace',request.extensions)
            return response('{}')
        with patch.object(httpx.Client,'send',send),patch('httpx._client.get_environment_proxies',side_effect=AssertionError('Cloud direct must not consult environment proxies')):
            self.assertEqual(cloudru._request({'model':'offline'},120),'{}')
        self.assertTrue(clients[0].is_closed)
    def test_actual_client_selects_explicit_proxy_tls_no_ambient_and_retains_payload_usage(self):
        clients=[]
        def send(client,request,**kw):
            clients.append(client)
            self.assertFalse(client.trust_env);self.assertFalse(client.follow_redirects)
            transport=client._transport_for_url(request.url)
            self.assertEqual(type(transport._pool).__name__,'HTTPProxy')
            self.assertEqual(transport._pool._ssl_context.verify_mode,ssl.CERT_REQUIRED)
            self.assertEqual(transport._pool._proxy_url.host,b'127.0.0.1');self.assertEqual(transport._pool._proxy_url.port,2080)
            self.assertEqual(request.headers['Authorization'],'Bearer offline-key')
            self.assertEqual(json.loads(request.content),{'model':'offline','messages':[]})
            self.assertAlmostEqual(request.extensions['timeout']['read'],120,delta=1)
            return response('{}')
        with patch.object(cloudru.settings,'cloudru_proxy_server','http://127.0.0.1:2080'),patch.object(httpx.Client,'send',send),patch.object(httpx,'post') as default,patch.object(cloudru,'record_llm_usage') as usage:
            self.assertEqual(cloudru._request({'model':'offline','messages':[]},120),'{}')
            default.assert_not_called();usage.assert_called_once_with('cloudru','offline',{'prompt_tokens':12,'completion_tokens':5})
        self.assertTrue(clients[0].is_closed)
    def test_proxy_timeout_is_operation_local_closes_and_never_falls_back(self):
        clients=[]
        def send(client,request,**kw):clients.append(client);raise httpx.ReadTimeout('offline')
        guard=llm.LLMRequestGuard()
        with patch.object(cloudru.settings,'cloudru_proxy_server','http://127.0.0.1:2080'),patch.object(httpx.Client,'send',send),patch.object(httpx,'post') as default,llm.llm_request_scope(guard):
            with self.assertRaises(llm.LLMOperationFailed) as first:cloudru._request({'model':'offline'},120)
            with self.assertRaises(llm.LLMOperationFailed) as second:cloudru._request({'model':'offline'},120)
            self.assertIsNot(first.exception,second.exception);self.assertIsNone(guard.failure);self.assertEqual(len(clients),2);default.assert_not_called()
        self.assertTrue(all(client.is_closed for client in clients))
    def test_proxy_deadline_expired_before_client_allocation_does_not_latch_provider(self):
        guard=llm.LLMRequestGuard()
        with patch.object(cloudru.settings,'cloudru_proxy_server','http://127.0.0.1:2080'),patch.object(session.time,'monotonic',return_value=100),session.deadline_scope(99),llm.llm_request_scope(guard),patch.object(httpx,'Client') as client:
            with self.assertRaises(session.SearchBudgetExceeded):cloudru._request({'model':'offline'},120)
            client.assert_not_called();self.assertIsNone(guard.failure)
    def test_missing_key_does_not_allocate_proxy_client(self):
        with patch.object(cloudru.settings,'cloudru_api_key',''),patch.object(cloudru.settings,'cloudru_proxy_server','http://127.0.0.1:2080'),patch.object(httpx,'Client') as client:
            with self.assertRaises(llm.LLMUnavailable):cloudru._request({'model':'offline'},120)
            client.assert_not_called()
    def test_other_provider_keeps_default_environment_route_and_scoped_cleanup(self):
        from engine.provider_transport import provider_post
        clients=[]
        def send(client,request,**kw):
            clients.append(client);self.assertTrue(client.trust_env)
            self.assertEqual(type(client._transport_for_url(request.url)._pool).__name__,'HTTPProxy')
            return response('{}')
        with patch('httpx._client.get_environment_proxies',return_value={'all://':'http://ambient.invalid:2080'})as environment,patch.object(httpx.Client,'send',send):
            provider_post('https://provider.invalid',provider='deepseek',headers={},json={},timeout=httpx.Timeout(120))
        environment.assert_called_once();self.assertTrue(clients[0].is_closed)
    def test_direct_opaque_timeout_is_one_attempt_per_operation_and_closes(self):
        clients=[];guard=llm.LLMRequestGuard()
        def send(client,request,**kw):clients.append(client);raise httpx.ConnectTimeout('opaque')
        with patch.object(httpx.Client,'send',send),llm.llm_request_scope(guard):
            with self.assertRaises(llm.LLMOperationFailed)as first:cloudru._request({'model':'offline'},120)
            with self.assertRaises(llm.LLMOperationFailed)as later:cloudru._request({'model':'offline'},120)
        self.assertEqual(len(clients),2);self.assertTrue(all(client.is_closed for client in clients));self.assertIsNot(first.exception,later.exception);self.assertIsNone(guard.failure)
        self.assertFalse(first.exception.provider_attempts[0].proven_presend)

if __name__=='__main__':unittest.main()
