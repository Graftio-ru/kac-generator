"""Neutral own-result hints exclude only explicit contradictions before capture."""
import json,time,unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch
from engine.models import SpecPosition
from engine.search import supplier_discovery as own
from engine.search.search_engine import DiscoveryURLs
from engine.search.discovery_plan import DiscoveryObservationCache
from engine.search.article_query_plan import QueryStep

class DiscoveryCandidateHintTests(unittest.TestCase):
 def chip(self,title='Cable ABC123',article='',brand='',extra=''):
  labels=(f'<span>Артикул производителя: {article}</span>' if article else '')+(f'<span>Производитель: {brand}</span>' if brand else '')
  return '<div id="search_results"><table id="itemlist"><tr class="with-hover"><td class="h_name"><div class="name"><a href="/product/observed-card">'+title+'</a></div>'+labels+extra+'</td></tr></table></div>'
 def hint(self,body):
  return next(iter(own.parse_candidate_hints(body,'https://www.chipdip.ru/search?searchtext=ABC123','ABC123').values()))
 def reason(self,hint,mark='ABC123',maker='Maker'):
  return own.candidate_hint_reason(asdict(hint),hint.url,'chipdip.ru',SpecPosition(number=1,name='Cable',type_mark=mark,unit='м',qty=1),maker,mark)
 def test_explicit_codes_and_brand_conflicts_but_vendor_numbers_do_not_become_articles(self):
  self.assertEqual(self.reason(self.hint(self.chip(article='OTHER999'))),'discovery_article_conflict')
  self.assertEqual(self.reason(self.hint(self.chip(brand='OtherMaker'))),'discovery_manufacturer_conflict')
  self.assertEqual(self.reason(self.hint(self.chip(title='Cable different wording',extra='<span>Код товара: 9999999</span>'))),'')
  self.assertEqual(self.reason(self.hint(self.chip(title='Alternative without full code'))),'')
  self.assertEqual(self.reason(self.hint(self.chip(title='Tray 3526210ZL')),mark='3526210'),'discovery_identity_conflict')
 def test_owner_isolation_hidden_related_and_duplicate_contradictory_owners_are_not_laundered(self):
  body=self.chip(article='OTHER999',extra='<div class="related-products"><span>Артикул производителя: ABC123</span><a href="/product/bait">ABC123</a></div><span hidden>Производитель: Maker</span>')
  hint=self.hint(body);self.assertEqual(hint.manufacturer_article,'OTHER999');self.assertEqual(hint.brand,'');self.assertEqual(len(own.parse_candidate_hints(body,'https://www.chipdip.ru/search','ABC123')),1)
  body=self.chip()+self.chip(title='Other product')
  hint=self.hint(body);self.assertEqual(hint.source_role,'ambiguous_result_owner');self.assertEqual(self.reason(hint),'')
 def test_actual_retained_seven_provider_urls_and_neutral_payloads(self):
  root=Path(__file__).resolve().parents[2];audit=json.loads((root/'sources/kac-generator/seven-own-search-20261013/own-search-bridge-audit.json').read_text())
  total=0
  for profile in audit['profiles']:
   if profile['host'] not in own.PROVIDERS:continue
   provider=own.PROVIDERS[profile['host']];body=(root/profile['body_path']).read_bytes().decode(profile.get('declared_charset') or 'utf-8')
   hints=own.parse_candidate_hints(body,provider.endpoint,profile['identity_hint'],provider)
   self.assertEqual(set(hints),{c['url'] for c in profile['candidates']},profile['host']);total+=len(hints)
   for hint in hints.values():self.assertEqual(hint.source_role,'search_result_owner');self.assertTrue(hint.title);self.assertNotIn('price',asdict(hint));self.assertEqual(len(hint.source_sha256),64)
  self.assertGreater(total,15)
 def test_cache_deepcopy_keeps_json_safe_neutral_hints_and_profile_intent(self):
  hint=self.hint(self.chip());cache=DiscoveryObservationCache();calls=[];step=QueryStep('"ABC123" цена','exact_article','ABC123')
  def create():
   calls.append(1);return DiscoveryURLs([hint.url],origin='supplier_own_search',host_observations={'chipdip.ru':dict(origin='supplier_own_search',window='unknown',diagnostics=[],candidate_hints={hint.url:asdict(hint)})})
  first,_=cache.get(step.query,['chipdip.ru'],20,create,query_step=step);json.dumps(first.host_observations);first.host_observations['chipdip.ru']['candidate_hints'][hint.url]['title']='mutated'
  second,reused=cache.get(step.query,['chipdip.ru'],20,create,query_step=step);self.assertTrue(reused);self.assertEqual(second.host_observations['chipdip.ru']['candidate_hints'][hint.url]['title'],hint.title);self.assertEqual(len(calls),1)
 def test_malformed_or_nonown_hints_are_not_rejection_authority(self):
  hint=self.hint(self.chip());value=asdict(hint);value['source_url']=3
  self.assertEqual(own.candidate_hint_reason(value,hint.url,'chipdip.ru',SpecPosition(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)),'')
  value=asdict(hint);value['source_role']='serp_snippet';value['manufacturer_article']='OTHER999'
  self.assertEqual(own.candidate_hint_reason(value,hint.url,'chipdip.ru',SpecPosition(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)),'')
