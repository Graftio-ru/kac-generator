"""Real retained own-result DOM profiles; candidate ranking never quote authority."""
import hashlib,json,time,unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from engine import browser
from engine.search import supplier_discovery as own,search_engine as engine,session
from engine.search.article_query_plan import QueryStep
from engine.search.discovery_plan import DiscoveryObservationCache
ROOT=Path(__file__).resolve().parents[2];PACKET=ROOT/'sources/kac-generator/seven-own-search-20261013'
AUDIT=json.loads((PACKET/'own-search-bridge-audit.json').read_text())
POSITIVE=[p for p in AUDIT['profiles'] if p.get('eligible_for_scoped_provider_proposal')]
def elektro(items):return '<div class="search__container"><div id="product_list">'+items+'</div></div>'
def product(label,article,url='/product/card/'):
 return '<div class="product"><div class="head"><div class="name"><a href="'+url+'">'+label+'</a></div></div><div class="article_row"><span>'+article+'</span></div></div>'
class OwnProfilesTests(unittest.TestCase):
 def test_all_six_actual_bodies_exact_neutral_url_sets_and_counts(self):
  self.assertEqual(len(POSITIVE),6)
  for record in POSITIVE:
   body=(ROOT/record['body_path']).read_bytes();self.assertEqual(hashlib.sha256(body).hexdigest(),record['body_sha256'])
   provider=own.PROVIDERS[record['host']]
   result=own.parse_candidates(body.decode(record['declared_charset']),provider.endpoint,record['identity_hint'],provider)
   self.assertEqual(set(result),{c['url'] for c in record['candidates']},record['host']);self.assertEqual(len(result),record['observed_unique_link_count'],record['host'])
  mini=next(p for p in POSITIVE if p['host']=='minimaks.ru');result=own.parse_candidates((ROOT/mini['body_path']).read_text(),own.PROVIDERS['minimaks.ru'].endpoint,mini['identity_hint'])
  self.assertIn('1x70',result[0]);self.assertEqual(sum('5x70' in u for u in result),2)
 def test_mpo_and_only_observed_seven_providers_registered(self):
  self.assertEqual(set(own.PROVIDERS),{'electro-mpo.ru','chipdip.ru','elektro.ru','ie-moscow.ru','favorit-el.ru','minimaks.ru','olmi-connect.ru'})
  packet=ROOT/'sources/kac-generator/mpo-own-search-20261013';actual=json.loads((packet/'result.json').read_text())
  self.assertEqual(set(own.parse_candidates((packet/'response.html').read_text(),'https://www.electro-mpo.ru/search/','3526210')),{c['url'] for c in actual['candidate_links']})
 def test_separate_owned_article_ranks_without_borrowing_neighbor_or_echo(self):
  text='<input value="ABC123">'+elektro(product('Unrelated title','OTHER999','/product/first/')+product('Owned generic title','Арт.: ABC123','/product/second/'))
  result=own.parse_candidates(text,'https://www.elektro.ru/search/','ABC123')
  self.assertEqual(result,['https://www.elektro.ru/product/second/','https://www.elektro.ru/product/first/'])
  nested=product('Outer generic','OTHER999','/product/outer/').replace('</div></div><div class="article_row">','</div></div>'+product('Nested requested','ABC123','/product/inner/')+'<div class="article_row">',1)
  result=own.parse_candidates(elektro(nested),'https://www.elektro.ru/search/','ABC123')
  self.assertEqual(result[0],'https://www.elektro.ru/product/inner/')
 def test_outside_result_region_nav_form_hidden_and_crosshost_refused(self):
  valid=product('ABC123','ABC123')
  bait=valid+'<nav>'+elektro(valid)+'</nav><form>'+elektro(valid)+'</form><div hidden>'+elektro(valid)+'</div>'+elektro(product('ABC123','ABC123','https://evil.example/product/card/'))+elektro(product('ABC123','ABC123','https://user:secret@www.elektro.ru/product/card/'))+elektro(product('ABC123','ABC123','javascript:alert(1)'))+elektro(product('ABC123','ABC123','/search/?q=ABC123'))
  self.assertEqual(own.parse_candidates(bait,'https://www.elektro.ru/search/','ABC123'),[])
 def test_substitutes_and_all_received_tails_preserved_above_old_128_cap(self):
  body=elektro(''.join(product('Other product '+str(i),'OTHER'+str(i),'/product/card'+str(i)+'/') for i in range(140)))
  result=own.parse_candidates(body,'https://www.elektro.ru/search/','ABC123')
  self.assertEqual(len(result),140);self.assertTrue(result[-1].endswith('/card139/'))
 def test_normal_footer_captcha_library_not_bot_when_products_exist(self):
  body=elektro(product('ABC123','ABC123'))+'<div class="footer">Protected by reCAPTCHA</div><script>captcha()</script>'
  self.assertEqual(len(own.parse_candidates(body,'https://www.elektro.ru/search/','ABC123')),1)
 def test_all_six_normal_dispatcher_unknown_coverage_no_serp_or_price(self):
  records={p['host']:p for p in POSITIVE};wire=[];original=httpx.Client
  def handler(request):
   host=request.url.host.removeprefix('www.');record=records[host];wire.append(host)
   provider=own.PROVIDERS[host];self.assertEqual(dict(request.url.params),{provider.parameter:record['identity_hint']})
   return httpx.Response(200,headers={'content-type':'text/html; charset='+record['declared_charset']},stream=httpx.ByteStream((ROOT/record['body_path']).read_bytes()),request=request)
  with patch.object(browser.settings,'browser_proxy_server',''),patch.object(httpx,'Client',side_effect=lambda **kw:original(transport=httpx.MockTransport(handler),**kw)),patch.object(engine,'xmlriver_search',side_effect=AssertionError('No SERP')):
   for host,record in records.items():
    step=QueryStep('typed exact '+host,'exact_article',record['identity_hint'])
    with session.research_session(deadline=time.monotonic()+10):result=engine.search_on_sites(step.query,[host],query_step=step)
    self.assertEqual(len(result),record['observed_unique_link_count']);self.assertEqual(result.host_observations[host]['window'],'unknown');self.assertFalse(result.host_observations[host]['discovery_complete']);self.assertEqual(result.host_observations[host]['provider_version'],own.VERSION)
  self.assertEqual(len(wire),6)
 def test_job_cache_intent_profile_and_decoder_route_changes_invalidate(self):
  cache=DiscoveryObservationCache();calls=[]
  def create():calls.append(1);return engine.DiscoveryURLs([],origin='response',window='unknown')
  step=QueryStep('"ABC123" цена','exact_model','ABC123')
  with patch.object(browser.settings,'browser_proxy_server',''):
   cache.get(step.query,['elektro.ru'],20,create,query_step=step);cache.get(step.query,['elektro.ru'],20,create,query_step=step)
   with patch.object(own,'VERSION','different_profile_version'):cache.get(step.query,['elektro.ru'],20,create,query_step=step)
   with patch.object(browser,'supplier_http_configuration_hash',return_value='a'*64):cache.get(step.query,['elektro.ru'],20,create,query_step=step)
  self.assertEqual(len(calls),3)
