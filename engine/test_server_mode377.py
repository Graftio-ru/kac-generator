"""Optional auth wraps real native routes and static/private assets; no wires."""
import base64,hashlib,importlib,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from fastapi.testclient import TestClient
from pydantic import SecretStr
from . import config,app as native
from .server_auth import policy_from_settings,ServerConfigurationError,approval_actor

_PASSWORD='synthetic-test-password-not-production'
_SALT='0123456789abcdef0123456789abcdef'
_DIGEST=hashlib.pbkdf2_hmac('sha256',_PASSWORD.encode(),bytes.fromhex(_SALT),600000).hex()
_HASH='pbkdf2_sha256$600000$'+_SALT+'$'+_DIGEST
_HEADERS={'Authorization':'Basic '+base64.b64encode(('ivan:'+_PASSWORD).encode()).decode()}

class ServerMode377Tests(unittest.TestCase):
 def server(self,**changes):return SimpleNamespace(deployment_mode='server',server_username='ivan',server_public_origin='https://estimator.test',server_password_hash=SecretStr(_HASH),**changes)
 def setUp(self):
  self.settings=config.settings;self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.addCleanup(self.restore)
  settings=config.Settings.model_construct(deployment_mode='server',server_username='ivan',server_public_origin='https://estimator.test',server_password_hash=SecretStr(_HASH))
  self.settingpatch=patch.object(config,'settings',settings);self.settingpatch.start();importlib.reload(native)
  self.client=TestClient(native.app,base_url='https://estimator.test')
 def restore(self):
  self.client.close();self.settingpatch.stop();importlib.reload(native)
 def test_unauthenticated_every_native_state_asset_and_full_health_denied(self):
  for method,url in [('GET','/'),('GET','/common.js'),('GET','/runtime-config.js'),('GET','/health'),('GET','/openapi.json'),('GET','/base'),('GET','/cache/private.png'),('GET','/jobs/secret/download/kac'),('GET','/research/secret'),('POST','/base/save'),('POST','/candidate-approvals/confirm'),('POST','/candidate-approvals/revoke'),('POST','/parse'),('POST','/build')]:
   with self.subTest(url=url):
    r=self.client.request(method,url);self.assertEqual(r.status_code,401,r.text);self.assertIn('Basic',r.headers['www-authenticate']);self.assertNotIn('access-control-allow-origin',r.headers)
  r=self.client.get('/healthz');self.assertEqual(r.json(),{'ok':True});self.assertEqual(r.status_code,200)
 def test_authenticated_ui_sameorigin_and_runtime_config_no_secret(self):
  for path in ['/','/common.js','/runtime-config.js','/health','/openapi.json']:
   r=self.client.get(path,headers={**_HEADERS,'Origin':'https://estimator.test'});self.assertEqual(r.status_code,200,r.text[:100]);self.assertNotIn('access-control-allow-origin',r.headers)
  js=self.client.get('/runtime-config.js',headers=_HEADERS).text;self.assertIn('"mode":"server"',js);self.assertIn('"apiBase":""',js);self.assertNotIn(_HASH,js);self.assertNotIn('ivan',js);self.assertNotIn('password',js)
 def test_cross_origin_insecure_and_forged_identity_headers_deny(self):
  for headers in [{'Origin':'https://evil.test'},{'Origin':'null'},{'Sec-Fetch-Site':'cross-site'}]:self.assertEqual(self.client.get('/base',headers={**_HEADERS,**headers}).status_code,403)
  r=self.client.get('/base',headers={'X-User':'ivan','X-Authenticated-User':'ivan','X-Forwarded-User':'ivan'});self.assertEqual(r.status_code,401)
  self.assertEqual(self.client.get('/base',headers={'Authorization':'Basic '+base64.b64encode(b'ivan:wrong').decode()}).status_code,401)
  with TestClient(native.app,base_url='http://estimator.test')as client:self.assertEqual(client.get('/base',headers={**_HEADERS,'X-Forwarded-Proto':'https'}).status_code,400)
  self.assertEqual(self.client.get('/base',headers={**_HEADERS,'Host':'evil.test'}).status_code,400)
 def test_authenticated_real_confirm_and_revoke_actor_not_caller_header(self):
  from .test_estimator_approval375 import EstimatorApproval375Tests
  fixture=EstimatorApproval375Tests();fixture.setUp()
  try:
   # Fixture changes only native store/cache; real server middleware remains.
   fixture.client.close();fixture.client=TestClient(native.app,base_url='https://estimator.test',headers={**_HEADERS,'X-Authenticated-User':'forged-admin'})
   value=fixture.confirm();self.assertEqual(value['approval']['actor'],'server-basic:ivan')
   r=fixture.client.post('/candidate-approvals/revoke',json={'approval_id':value['approval']['approval_id'],'expected_revision':value['approval']['revision'],'reason':'Отмена'});self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['approval']['revoked_by'],'server-basic:ivan')
   with self.assertRaises(ServerConfigurationError):approval_actor()
  finally:fixture.doCleanups()
 def test_empty_or_bad_server_config_fails_closed_without_starting_app(self):
  for values in [{'server_username':''},{'server_password_hash':SecretStr('')},{'server_password_hash':SecretStr('plaintext-not-hash')},{'server_public_origin':'http://estimator.test'},{'server_public_origin':'https://user:pass@estimator.test'},{'server_public_origin':'https://estimator.test/path'}]:
   d=vars(self.server()).copy();d.update(values)
   with self.subTest(fields=list(values)),self.assertRaises(ServerConfigurationError):policy_from_settings(SimpleNamespace(**d))
 def test_local_mode_retains_unprotected_ui_legacy_cors_and_actor(self):
  self.client.close();self.settingpatch.stop();self.settingpatch=patch.object(config,'settings',config.Settings.model_construct());self.settingpatch.start();importlib.reload(native);self.client=TestClient(native.app)
  r=self.client.get('/health',headers={'Origin':'https://legacy-pages.test'});self.assertEqual(r.status_code,200);self.assertEqual(r.headers['access-control-allow-origin'],'*')
  self.assertEqual(self.client.get('/').status_code,200);self.assertEqual(approval_actor(),'local-estimator (unverified local user)');self.assertIn('"mode":"local"',self.client.get('/runtime-config.js').text)
 def test_current_policy_pins_auth_and_config_previous_exact_set_readonly(self):
  from .test_factory_policy_closure353 import FactoryPolicyClosure353Tests
  from .search import acquisition_source_intent as intent
  from .search.document_context import current_document_policy
  fixture=FactoryPolicyClosure353Tests();fixture.setUp()
  try:
   old=fixture.historical(intent.PRE_SERVER_SOURCE_FILES);self.assertEqual(intent.validate(old),old)
   with self.assertRaises(intent.IntentInvalid):intent.attest(fixture.runtime,old['components'])
   for name in ['server_auth.py','config.py']:
    current=intent.attest(fixture.runtime,intent.components(fixture.root));policy=current_document_policy(engine_root=fixture.root,supplier_file=fixture.suppliers);path=fixture.root/name;self.assertIn(str(path),policy);path.write_bytes(path.read_bytes()+b'\n# inert auth policy mutation\n');self.assertNotEqual(current['sha256'],intent.attest(fixture.runtime,intent.components(fixture.root))['sha256']);self.assertNotEqual(policy,current_document_policy(engine_root=fixture.root,supplier_file=fixture.suppliers))
   fixture.test_policy_upgrade_never_reopens_old_pending_supplier_url()
  finally:fixture.doCleanups()

 def test_private_existing_asset_denied_without_auth_and_available_after_auth(self):
  path=self.root/'private-fixture.png';path.write_bytes(b'synthetic-private-png')
  with patch.object(native,'CACHE_DIR',self.root):
   self.assertEqual(self.client.get('/cache/'+path.name).status_code,401)
   response=self.client.get('/cache/'+path.name,headers=_HEADERS);self.assertEqual(response.status_code,200);self.assertEqual(response.content,path.read_bytes())
 def test_malformed_basic_duplicate_headers_and_context_reset(self):
  for authorization in ['Basic !!invalid!!','Bearer ivan','Basic '+base64.b64encode(b'ivan').decode()]:self.assertEqual(self.client.get('/base',headers={'Authorization':authorization}).status_code,401)
  response=self.client.get('/base',headers=[('Authorization',_HEADERS['Authorization']),('Authorization',_HEADERS['Authorization'])]);self.assertEqual(response.status_code,401)
  self.assertEqual(self.client.get('/health',headers=_HEADERS).status_code,200)
  with self.assertRaises(ServerConfigurationError):approval_actor()

 def test_external_document_navigation_challenge_and_auth_without_crosssite_fetch(self):
  navigation={'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'navigate','Sec-Fetch-Dest':'document'}
  for method in ['GET','HEAD']:
   response=self.client.request(method,'/',headers=navigation);self.assertEqual(response.status_code,401);self.assertIn('Basic',response.headers['www-authenticate'])
   self.assertEqual(self.client.request(method,'/',headers={**navigation,**_HEADERS}).status_code,200)
  self.assertEqual(self.client.get('/research/unknown',headers={**navigation,**_HEADERS}).status_code,403)
  for method,headers in [('GET',{**navigation,'Sec-Fetch-Mode':'cors','Sec-Fetch-Dest':'empty'}),('POST',navigation),('GET',{**navigation,'Origin':'https://evil.test'})]:
   self.assertEqual(self.client.request(method,'/',headers={**headers,**_HEADERS}).status_code,403)
