"""Documented provider result cap is independent of a caller's preferred count."""
import tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from engine.search import search_engine as search
from engine.search.discovery_cache import DiscoveryCache
from engine.search.session import research_session
from engine.llm import LLMRequestGuard

NUMERIC='https://xmlriver.com/search_yandex/xml'


def response(count=10, missing_urls=0, extra=''):
    docs=''.join('<doc>'+('' if i<missing_urls else f'<url>https://a.example/item{i}</url>')+'</doc>' for i in range(count))
    return httpx.Response(200,text=f'<yandexsearch><response>{docs}{extra}</response></yandexsearch>',request=httpx.Request('GET',NUMERIC))


class XMLRiverResultWindowTests(unittest.TestCase):
    def collect(self,n=20,count=10,endpoint=NUMERIC,missing_urls=0,extra=''):
        with patch.object(search.settings,'xmlriver_user','fixture'),patch.object(search.settings,'xmlriver_key','fixture'), \
             patch.object(search.settings,'xmlriver_url',endpoint),patch.object(search.DiscoveryCache,'get',return_value=None), \
             patch.object(search.DiscoveryCache,'put'),patch.object(search.httpx,'get',return_value=response(count,missing_urls,extra)) as wire, \
             research_session(deadline=time.monotonic()+30,llm_guard=LLMRequestGuard()):
            result=search.xmlriver_search('fixture',n)
        return result,wire.call_args.kwargs['params']

    def test_numeric_profile_n5_n20_n100_all_wire10_and_raw10_is_truncated(self):
        for n in (5,20,100):
            with self.subTest(n=n):
                result,params=self.collect(n=n)
                self.assertEqual(10,params['groupby']);self.assertEqual(10,result.limit)
                self.assertEqual(10,result.raw_count);self.assertEqual('truncated_possible',result.window)

    def test_known_profile_nine_docs_below_verified_cap_and_counts_missing_url_docs(self):
        result,_=self.collect(count=9)
        self.assertEqual('below_limit',result.window);self.assertEqual(9,result.raw_count)
        result,_=self.collect(count=10,missing_urls=3)
        self.assertEqual(7,len(result));self.assertEqual(10,result.raw_count)
        self.assertEqual('truncated_possible',result.window)

    def test_unverified_endpoint_and_composite_grouping_do_not_inherit_numeric_cap(self):
        for endpoint in ('https://xmlriver.com/yandex/xml','https://xmlstock.com/yandex/xml/',
                         'https://unverified.example/search_yandex/xml','https://xmlriver.com/search_yandex/xml/','http://xmlriver.com/search_yandex/xml'):
            with self.subTest(endpoint=endpoint):
                result,params=self.collect(endpoint=endpoint)
                self.assertEqual(20,params['groupby']);self.assertIsNone(result.limit)
                self.assertEqual('unknown',result.window)

    def test_out_of_doc_urls_are_not_complete_provider_window(self):
        result,_=self.collect(count=0,extra='<url>https://a.example/not-a-doc</url>')
        self.assertEqual(0,result.raw_count);self.assertEqual('unknown',result.window)

    def test_site_filter_retains_actual_child_cap_before_url_filter(self):
        with patch.object(search.settings,'xmlriver_user','fixture'),patch.object(search.settings,'xmlriver_key','fixture'), \
             patch.object(search.settings,'xmlriver_url',NUMERIC),patch.object(search.DiscoveryCache,'get',return_value=None), \
             patch.object(search.DiscoveryCache,'put'),patch.object(search.httpx,'get',return_value=response()) as wire, \
             research_session(deadline=time.monotonic()+30,llm_guard=LLMRequestGuard()):
            result=search.search_on_sites('fixture',['unrepresented.example'],n=20)
        self.assertEqual([],result);self.assertEqual(10,result.raw_count);self.assertEqual(10,result.limit)
        self.assertEqual('truncated_possible',result.window);self.assertEqual(10,wire.call_args.kwargs['params']['groupby'])

    def test_old_requested_count_cache_read_is_unknown_without_repaying(self):
        key_method=DiscoveryCache.key
        with tempfile.TemporaryDirectory() as directory:
            cache=DiscoveryCache(Path(directory)/'cache.sqlite3')
            cache.put(cache.key(NUMERIC,'fixture',search.settings.xmlriver_lr,'fixture',20),['https://a.example/item'])
            with patch.object(search,'DiscoveryCache',return_value=cache) as factory, \
                 patch.object(search.settings,'xmlriver_user','fixture'),patch.object(search.settings,'xmlriver_key','fixture'), \
                 patch.object(search.settings,'xmlriver_url',NUMERIC),patch.object(search.httpx,'get') as wire, \
                 research_session(deadline=time.monotonic()+30,llm_guard=LLMRequestGuard()):
                factory.key.side_effect=key_method
                result=search.xmlriver_search('fixture',20)
            wire.assert_not_called();self.assertEqual(['https://a.example/item'],result)
            self.assertEqual('cache',result.origin);self.assertEqual('unknown',result.window)
            self.assertIsNone(result.raw_count);self.assertEqual(10,result.limit)

    def test_fresh_write_uses_effective_profile_key_and_xml15_is_explicit_empty(self):
        with patch.object(search.settings,'xmlriver_user','fixture'),patch.object(search.settings,'xmlriver_key','fixture'), \
             patch.object(search.settings,'xmlriver_url',NUMERIC),patch.object(search.DiscoveryCache,'get',return_value=None), \
             patch.object(search.DiscoveryCache,'put') as put,patch.object(search.httpx,'get',return_value=response()), \
             research_session(deadline=time.monotonic()+30,llm_guard=LLMRequestGuard()):
            search.xmlriver_search('fixture',20)
        self.assertEqual(DiscoveryCache.key(NUMERIC,'fixture',search.settings.xmlriver_lr,'fixture',10),put.call_args.args[0])
        empty=httpx.Response(200,text='<yandexsearch><response><error code="15"/></response></yandexsearch>')
        with patch.object(search.settings,'xmlriver_user','fixture'),patch.object(search.settings,'xmlriver_key','fixture'), \
             patch.object(search.settings,'xmlriver_url',NUMERIC),patch.object(search.DiscoveryCache,'get',return_value=None), \
             patch.object(search.DiscoveryCache,'put') as put,patch.object(search.httpx,'get',return_value=empty), \
             research_session(deadline=time.monotonic()+30,llm_guard=LLMRequestGuard()):
            result=search.xmlriver_search('fixture',20)
        self.assertEqual([],result);self.assertEqual('15',result.empty_code);self.assertEqual('below_limit',result.window)
        self.assertEqual(10,result.limit);put.assert_not_called()

if __name__=='__main__':unittest.main()
