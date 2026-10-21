"""Actual bounded HTTP producer, source identity and lazy normal collector."""
import base64,copy,json,tempfile,time,unittest
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch,MagicMock
import httpx
from engine import browser,source_quote,llm,config
from engine.search import http_card as card,agent,session,product_identity as identity
from engine.models import Requisites
from engine.offer_selection import offer_is_comparable
URL='https://supplier.example/item'

def html(price=120,sku='ABC123',generic=False):
 seller='<div itemprop="seller" itemscope itemtype="https://schema.org/Organization"><span itemprop="name">ООО Supplier</span><span itemprop="taxID">7842224734</span></div>'
 if generic:
  data={'@type':'Product','name':'Cable ABC123','sku':sku,'url':URL,'offers':{'@type':'Offer','price':price,'priceCurrency':'RUB','url':URL}}
  return '<script type="application/ld+json">'+json.dumps(data)+'</script><main><h1>Cable ABC123</h1><div>Артикул: '+sku+'</div><div class="price">'+str(price)+' ₽</div><p>Цена за 1 м</p>'+seller+'</main>'
 return '<main itemscope itemtype="https://schema.org/Product"><h1>Cable ABC123</h1><div itemprop="sku">'+sku+'</div><section itemprop="offers" itemscope itemtype="https://schema.org/Offer"><meta itemprop="priceCurrency" content="RUB"><span itemprop="price">'+str(price)+'</span> ₽<p>Цена за 1 м</p>'+seller+'</section></main>'

def bind_main_facts(page):
 """CDP source fixture derived only from the fake page's observed text/title."""
 from engine.source_quote import primary_text
 from engine.search.product_identity import main_card_article
 body=page.inner_text.return_value; title=page.locator.return_value.first.inner_text.return_value
 article=main_card_article(primary_text(body)); count=page.locator.return_value.count.return_value
 facts=dict(kind='complete' if article and count==1 else 'partial',missing_fields=[] if article and count==1 else ['article'],owner_bound=count==1,document_url=page.url,document_title=page.title.return_value,main_title=title if count==1 else '',article=article if count==1 else '',owner_text=body,owner_truncated=False,body=body,body_truncated=False,observed_at=time.time())
 observation=dict(document_url=page.url,title=page.title.return_value,body=body,body_truncated=False,title_truncated=False,headings_truncated=False,main_title=title if count==1 else '',all_h1=count,visible_h1=count,main_h1=count)
 def send(method,params):
  value=dict(facts,observed_at=time.time()) if params.get('awaitPromise') else observation
  return {'result':{'value':value}}
 page.context.new_cdp_session.return_value.send.side_effect=send
 return facts

class HttpCardTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  for target,name in [(card,'CACHE_DIR'),(agent,'CACHE_DIR'),(config,'CACHE_DIR')]:
   p=patch.object(target,name,self.root);p.start();self.addCleanup(p.stop)
  card._PARSED.clear()
  p=patch.object(browser.settings,'browser_proxy_server','');p.start();self.addCleanup(p.stop)
 def clients(self,handler):
  original=httpx.Client
  return patch.object(httpx,'Client',side_effect=lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
 def response(self,text,url=URL):return httpx.Response(200,headers={'content-type':'text/html; charset=utf-8'},text=text,request=httpx.Request('GET',url))
 def test_actual_http_collector_static_no_browser_no_model_with_exact_source(self):
  for generic in (False,True):
   calls=[]
   with self.clients(lambda r:(calls.append(str(r.url)) or self.response(html(generic=generic)))),patch.object(browser.Browser,'_start',side_effect=AssertionError('Static card must not launch browser')),patch.object(llm,'complete_json',side_effect=AssertionError('Static card must not call model')),session.research_session(llm_guard=llm.LLMRequestGuard(),deadline=time.monotonic()+30),agent.capture_card_diagnostics() as diagnostics:
    with browser.Browser() as owned:offers,checked=agent._collect_offers(owned,[URL],1,'ABC123','Cable','м',supplier_hosts={'supplier.example'})
   self.assertEqual(calls,[URL]);self.assertEqual(checked,1);self.assertEqual(len(offers),1,diagnostics);offer=offers[0]
   self.assertTrue(offer_is_comparable(offer,{'unit':'м','type_mark':'ABC123','name':'Cable'}),source_quote.quote_review_reason(offer,{'unit':'м','type_mark':'ABC123','name':'Cable'}));self.assertEqual(offer.identity_evidence['provenance'],'http_main_product');self.assertEqual(offer.price_with_vat,120);self.assertFalse(offer.screenshot_context);self.assertIn('после отбора',offer.context_capture_review_reason)
 def test_owned_code_roles_title_only_and_retained_tamper(self):
  def variant(labels='',title='Cable ABC123'):
   return html().replace('<div itemprop="sku">ABC123</div>',labels).replace('<h1>Cable ABC123</h1>','<h1>'+title+'</h1>')
  for labels,basis,article in (('', 'title_only',''),('<p>Номер поставщика: VENDOR99</p>','title_only',''),('<p>Артикул производителя: ABC123</p><p>Номер поставщика: OTHER99</p>','manufacturer_code','ABC123'),('<span itemprop="mpn">ABC123</span><span itemprop="sku">OTHER99</span><p>Номер поставщика: OTHER99</p>','manufacturer_code','ABC123')):
   body=variant(labels);observed=card.main_product(body,URL);self.assertIsNotNone(observed,labels);self.assertEqual(observed['identity_basis'],basis);self.assertEqual(observed['article'],article)
   record,ref=card.save_response(self.response(body),URL);proof=identity.http_identity(record,observed,ref,'ABC123','Cable','supplier.example');self.assertTrue(identity.valid_identity(proof,'ABC123','Cable'),labels)
   bad=copy.deepcopy(proof);bad['http_source']['code_observations']=[{'role':'manufacturer_code','label':'MPN','value':'ABC123'}];self.assertFalse(identity.valid_identity(bad))
   if basis=='title_only':
    bad=copy.deepcopy(proof);bad['http_source'].pop('code_profile');self.assertFalse(identity.valid_identity(bad))
  for labels,title in (('<p>Артикул производителя: OTHER99</p><p>Номер поставщика: ABC123</p>','Cable ABC123'),('<p>Номер поставщика: ABC123</p>','Cable OTHER99')):
   body=variant(labels,title);observed=card.main_product(body,URL);record,ref=card.save_response(self.response(body),URL);proof=identity.http_identity(record,observed,ref,'ABC123','Cable','supplier.example');self.assertFalse(identity.valid_identity(proof,'ABC123','Cable'))
  # Unlabelled legacy SKU semantics remain authoritative, not reclassified.
  observed=card.main_product(html(sku='OTHER99'),URL);self.assertEqual(observed['identity_basis'],'legacy_article')
  self.assertIsNone(card.main_product(variant('<p>MPN: ABC123</p><p>MPN: OTHER99</p>'),URL))
  self.assertIsNone(card.main_product(variant()+variant(),URL))
  self.assertIsNone(card.main_product(variant().replace('<span itemprop="price">120</span>','<meta itemprop="price" content="120">'),URL))
  self.assertIsNone(card.main_product(variant().replace('<span itemprop="price">120</span>','<del itemprop="price">120</del>'),URL))
  self.assertIsNone(card.main_product(variant().replace('<span itemprop="price">120</span>','<h2>Related products</h2><span itemprop="price">120</span>'),URL))
 def test_actual_saved_mpo_owned_title_collector_no_browser_or_model(self):
  import hashlib
  staging=Path(__file__).resolve().parents[2]/'sources/kac-generator/facts-photos-export-actual-2026-10-13'
  saved=json.loads((staging/'live/cache/http_cards/5672477482b743ecb69e21002c17adf9.json').read_text());body=base64.b64decode(saved['body_base64'])
  self.assertEqual(hashlib.sha256(body).hexdigest(),'5ccf8586667cbd59854804569871df022c16da458a3b98982bc097dbd4dabb29')
  requested=json.loads((staging/'selection.json').read_text())
  if isinstance(requested,dict):requested=requested['cards']
  item=requested[0];url=item['url'];host=item['host'];observed=card.main_product(body.decode(),saved['url']);self.assertIsNotNone(observed);self.assertEqual(observed['identity_basis'],'title_only');self.assertEqual(observed['article'],'');self.assertTrue(any(o['role']=='vendor_code' for o in observed['code_observations']))
  calls=[]
  with patch.object(agent,'fetch_requisites_from_site',return_value=None),patch.object(agent,'fetch_requisites',return_value=None),self.clients(lambda request:(calls.append(str(request.url)) or httpx.Response(saved['status'],headers={'content-type':'text/html; charset=utf-8'},content=body,request=request))),patch.object(browser.Browser,'_start',side_effect=AssertionError('Saved static response must not render')),patch.object(llm,'complete_json',side_effect=AssertionError('Saved static response must not call model')),session.research_session(llm_guard=llm.LLMRequestGuard(),deadline=time.monotonic()+30),agent.capture_card_diagnostics() as diagnostics:
   with browser.Browser() as owned:offers,checked=agent._collect_offers(owned,[url],1,item['type_mark'],item['name'],item['unit'],supplier_hosts={host},expected_qty=item['qty'])
  self.assertEqual(calls,[url]);self.assertEqual(checked,1);self.assertEqual(len(offers),1,diagnostics);offer=offers[0]
  self.assertEqual(offer.identity_evidence['provenance'],'http_main_product');self.assertEqual(offer.listed_price_with_vat,observed['listed_price']);self.assertTrue(identity.valid_identity(offer.identity_evidence,item['type_mark'],item['name']));self.assertEqual(source_quote.quote_review_reason(offer,item),'')
  self.assertFalse(offer.screenshot_context);self.assertFalse(offer_is_comparable(offer,item));self.assertTrue(any(d['code']=='seller_unconfirmed' for d in diagnostics),diagnostics)
 def test_shell_browser_fallback_once_and_page_still_closes(self):
  page=MagicMock();page.url=page._kac_document_url=URL;page._kac_response_status=200;page._kac_retry_after=None
  page.title.return_value='Cable ABC123';page.inner_text.return_value='Cable ABC123\nАртикул: ABC123\n120 ₽\nЦена за 1 м';page.locator.return_value.count.return_value=1;page.locator.return_value.first.inner_text.return_value='Cable ABC123'
  bind_main_facts(page)
  entries=[];closed=[]
  class Owned:
   @contextmanager
   def page(self,url):
    entries.append(url)
    try:yield page
    finally:closed.append(url)
  malformed=html(generic=True).replace(json.dumps(URL),'3',1)
  for response_kind in ('malformed','shell','compressed'):
   entries.clear();closed.clear()
   def fallback_response(request):
    if response_kind=='compressed':return httpx.Response(200,headers={'content-encoding':'gzip'},stream=httpx.ByteStream(b'not readable HTML'),request=request)
    return self.response(malformed if response_kind=='malformed' else '<main><div id=app></div></main>')
   with self.clients(fallback_response),patch.object(agent,'wl_requisites_for',return_value=Requisites(name='Supplier',inn='7842224734')),patch('engine.review_context.capture_context',return_value=({},'No fixture attachment')),session.research_session(llm_guard=llm.LLMRequestGuard(),deadline=time.monotonic()+30),agent.capture_card_diagnostics():
    offers,checked=agent._collect_offers(Owned(),[URL],1,'ABC123','Cable','м',supplier_hosts={'supplier.example'})
   self.assertEqual(entries,[URL]);self.assertEqual(closed,entries);self.assertEqual(len(offers),1)
 def test_shared_text_business_refusals_precede_http_eligibility(self):
  cases=[('dkc.market','<p>арт. ABC123</p><p>Нет в наличии</p>','out_of_stock'),('cable.ru','<p>цены как минимальные; окончательная цена уточняется</p>','minimum_price'),('lanitnord.ru','<p>Артикул: ABC123</p><p>ПО ЗАПРОСУ</p>','price_on_request'),('satro-paladin.com','<p>Вся кабельная продукция продаётся бухтами</p><p>Длина бухты, м</p><p>100</p>','minimum_order')]
  for host,extra,code in cases:
   url=f'https://{host}/product/ABC123';body=html().replace('</section>',extra+'</section>')
   with self.subTest(host=host),self.clients(lambda request:httpx.Response(200,headers={'content-type':'text/html'},text=body,request=request)),patch.object(browser.Browser,'_start',side_effect=AssertionError('Declined source must not start browser')),patch.object(llm,'complete_json',side_effect=AssertionError('Declined source must not call model')),session.research_session(llm_guard=llm.LLMRequestGuard(),deadline=time.monotonic()+30),agent.capture_card_diagnostics() as diagnostics:
    with browser.Browser() as owned:offers,checked=agent._collect_offers(owned,[url],1,'ABC123','Cable','м',supplier_hosts={host},expected_qty=1)
   self.assertEqual(offers,[]);self.assertEqual(checked,1);self.assertTrue(any(d['code']==code for d in diagnostics),diagnostics)
 def rendered_page(self):
  page=MagicMock();page.url=page._kac_document_url=URL;page._kac_response_status=200;page._kac_retry_after=None
  page.title.return_value='Cable ABC123';page.inner_text.return_value='Cable ABC123\nАртикул: ABC123\n120 ₽\nЦена за 1 м';page.locator.return_value.count.return_value=1;page.locator.return_value.first.inner_text.return_value='Cable ABC123'
  bind_main_facts(page)
  return page
 def test_same_lease_http_failure_render_success_and_final_failure(self):
  from engine.search.supplier_health_store import SupplierHealthStore
  for fail_render in (False,True):
   store=SupplierHealthStore(self.root/('health-'+str(fail_render)+'.sqlite3'));health=session.PersistentSupplierHealth(store,hosts=('supplier.example',));entries=[];closed=[];page=self.rendered_page()
   class Owned:
    @contextmanager
    def page(self,url):
     entries.append(url)
     if fail_render:
      error=httpx.ReadTimeout('Fixture final render timeout',request=httpx.Request('GET',url));browser._supplier_failure(error,url,'supplier_navigation');raise error
     try:yield page
     finally:closed.append(url)
   def timeout(request):raise httpx.ReadTimeout('Fixture HTTP timeout',request=request)
   with self.clients(timeout),patch.object(store,'acquire',wraps=store.acquire) as acquire,patch.object(store,'finish',wraps=store.finish) as finish,patch.object(agent,'wl_requisites_for',return_value=Requisites(name='Supplier',inn='7842224734')),patch('engine.review_context.capture_context',return_value=({},'No fixture attachment')),session.research_session(health,deadline=time.monotonic()+30),agent.capture_card_diagnostics() as diagnostics:
    offers,checked=agent._collect_offers(Owned(),[URL],1,'ABC123','Cable','м',supplier_hosts={'supplier.example'})
   self.assertEqual(acquire.call_count,1);self.assertEqual(finish.call_count,1);self.assertEqual(entries,[URL]);self.assertEqual(checked,1)
   attempt=next(d['http_transport_attempt'] for d in diagnostics if d['code']=='http_transport_attempt');self.assertEqual(attempt['failure_cause'],'network_timeout');self.assertTrue(attempt['fallback_performed'])
   snap=store.snapshot('supplier.example')
   if fail_render:self.assertEqual(offers,[]);self.assertEqual(snap['reason'],'network_timeout');self.assertEqual(snap['state'],'cooldown')
   else:
    self.assertEqual(len(offers),1);self.assertEqual(closed,entries);self.assertTrue(attempt['recovered']);self.assertEqual(snap['state'],'healthy');self.assertIsNone(snap['reason']);self.assertFalse(any(d.get('failure_source')=='supplier_http' for d in diagnostics))
 def test_proxy_and_unannotated_error_never_render(self):
  for exception in (httpx.ProxyError('Fixture proxy'),httpx.ReadTimeout('unbound timeout')):
   with patch.object(agent,'supplier_http_get',side_effect=exception),patch.object(agent,'_capture_rendered_card',side_effect=AssertionError('Forbidden fallback')),session.research_session(deadline=time.monotonic()+30):
    with self.assertRaises(type(exception)):agent._capture_card(None,URL,URL,'supplier.example','ABC123','Cable','м',1,None,set(),None)
 def test_unresolved_html_saved_before_parser_and_failed_render(self):
  body='<main><h1>Unresolved supplier page</h1></main>'
  with self.clients(lambda request:self.response(body)),patch.object(agent,'_capture_rendered_card',side_effect=httpx.ReadTimeout('Fixture renderer timeout')),session.research_session(deadline=time.monotonic()+30),agent.capture_card_diagnostics() as diagnostics:
   with self.assertRaises(httpx.ReadTimeout):agent._capture_card(None,URL,URL,'supplier.example','ABC123','Cable','м',1,None,set(),None)
  observation=next(d for d in diagnostics if d['code']=='http_capture_observation');ref=observation['http_response_source'];raw=(self.root/ref['ref']).read_bytes();self.assertEqual(__import__('hashlib').sha256(raw).hexdigest(),ref['record_sha256']);record=json.loads(raw);self.assertEqual(base64.b64decode(record['body_base64']).decode(),body);self.assertEqual(record['url'],URL);self.assertGreater(observation['observed_at'],0)
 def test_actual_normal_publisher_recovered_http_attempt_not_host_blocker(self):
  from engine import app,schemas
  from engine.search import base_db,source_registry
  from engine.schemas import PositionIn
  from engine.test_all_sites import AllSitesTests
  original=app._research_row;registry=source_registry.SourceRegistry(self.root/'recovered-registry.sqlite3');page=self.rendered_page();entries=[]
  @contextmanager
  def rendered(owner,url):
   entries.append(url)
   yield page
  def timeout(request):raise httpx.ReadTimeout('Fixture HTTP timeout',request=request)
  with patch.object(app,'CACHE_DIR',self.root),patch.object(schemas,'CACHE_DIR',self.root),patch.object(base_db,'BASE_FILE',self.root/'recovered-base.json'),patch('engine.search.source_registry.SourceRegistry',return_value=registry),self.clients(timeout),patch.object(browser.Browser,'page',rendered),patch.object(agent,'wl_requisites_for',return_value=Requisites(name='Supplier',inn='7842224734')),patch('engine.review_context.capture_context',return_value=({},'Fixture attachment unavailable')),patch.object(llm,'complete_json',side_effect=AssertionError('No model')):
   job=AllSitesTests().run_job([PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)],['supplier.example'],lambda *a,**kw:[URL],lambda p:original(p,use_cache=False))
  row=job['rows'][0];self.assertEqual(entries,[URL,URL]);self.assertEqual(row['found'],1,row);self.assertEqual(row['source_checks'][0]['status'],'found');self.assertEqual(row['source_checks'][0]['product_presence'],'unknown');self.assertTrue(any(d.get('http_transport_attempt',{}).get('recovered') for d in row['diagnostics']),row['diagnostics'])
 def test_identity_and_quote_bind_actual_owned_response_and_tamper(self):
  record,ref=card.save_response(self.response(html()),URL);observed=card.main_product(html(),URL);proof=identity.http_identity(record,observed,ref,'ABC123','Cable','supplier.example')
  self.assertTrue(identity.valid_identity(proof,'ABC123','Cable'))
  with patch.object(card,'MainParser',wraps=card.MainParser) as parser:
   for _ in range(5):self.assertTrue(identity.valid_identity(proof,'ABC123','Cable'))
   self.assertEqual(parser.call_count,0)
  bad=copy.deepcopy(proof);bad['http_source']['body_sha256']='0'*64;self.assertFalse(identity.valid_identity(bad))
  bad=copy.deepcopy(proof);bad['main_article']='OTHER';self.assertFalse(identity.valid_identity(bad))
  path=self.root/ref['ref'];stored=json.loads(path.read_text());stored['body_base64']=base64.b64encode(base64.b64decode(stored['body_base64']).replace(b'Cable',b'Cabal')).decode();path.write_text(json.dumps(stored));self.assertFalse(identity.valid_identity(proof))
 def test_unknown_json_types_depth_hidden_old_and_jsonld_bait(self):
  for value in ({'@type':100},{'@type':None},[[[{'@type':'Product','name':'Bait'}]]]):
   text='<script type="application/ld+json">'+json.dumps(value)+'</script>'+html()
   self.assertIsNone(card.main_product(text,URL))
  self.assertIsNone(card.main_product(html().replace('<span itemprop="price">120</span>','<del itemprop="price">120</del>'),URL))
  self.assertIsNone(card.main_product(html().replace('<span itemprop="price">120</span>','<span class="old-price" itemprop="price">120</span>'),URL))
  self.assertIsNotNone(card.main_product(html().replace('<section itemprop','<section class style itemprop'),URL))
  bait=html(generic=True).replace('120 ₽','100 ₽');self.assertIsNone(card.main_product(bait,URL))
  self.assertIsNone(card.main_product(html().replace('RUB','USD'),URL))
 def test_bounded_http_redirect_bytes_and_cancel_no_wire(self):
  calls=[]
  with self.clients(lambda r:(calls.append(str(r.url)) or httpx.Response(302,headers={'location':'https://foreign.example/item'},request=r))):
   with self.assertRaises(browser.SupplierHttpLimit):browser.supplier_http_get(URL,max_bytes=100)
  self.assertEqual(calls,[URL])
  with self.clients(lambda r:httpx.Response(200,content=b'A'*101,request=r)):
   with self.assertRaises(browser.SupplierHttpLimit):browser.supplier_http_get(URL,max_bytes=100)
  calls=[]
  with self.clients(lambda r:(calls.append(str(r.url)) or self.response(html()))),session.research_session(cancelled=lambda:True):
   with self.assertRaises(session.SearchBudgetExceeded):browser.supplier_http_get(URL,max_bytes=100)
  self.assertEqual(calls,[])
 def test_raw_upstream_drip_deadline_and_compression_refusal(self):
  clock=[0.0];read=[]
  class Drip(httpx.SyncByteStream):
   def __iter__(self):
    for index in range(30):
     clock[0]+=1;read.append(index);yield b'x'
  def handler(request):
   self.assertEqual(request.headers['accept-encoding'],'identity')
   return httpx.Response(200,stream=Drip(),request=request)
  with self.clients(handler),patch.object(time,'monotonic',side_effect=lambda:clock[0]):
   with self.assertRaises(httpx.ReadTimeout):browser.supplier_http_get(URL,max_bytes=100)
  self.assertLess(len(read),30);self.assertLessEqual(clock[0],15)
  read.clear()
  with self.clients(lambda request:httpx.Response(200,headers={'content-encoding':'br'},stream=Drip(),request=request)):
   with self.assertRaises(browser.SupplierHttpRepresentation):browser.supplier_http_get(URL,max_bytes=100)
  self.assertEqual(read,[])
 def test_http_child_timeout_continues_next_card_parent_cancel_stops(self):
  slow='https://slow.example/item';good='https://good.example/item';clock=[100.0];calls=[]
  class Drip(httpx.SyncByteStream):
   def __iter__(self):
    for _ in range(30):clock[0]+=1;yield b'x'
  def handler(request):
   calls.append(str(request.url))
   return httpx.Response(200,stream=Drip(),request=request) if str(request.url)==slow else self.response(html(),good)
  class Owned:
   @contextmanager
   def page(self,url):
    error=httpx.ReadTimeout('Final rendered failure',request=httpx.Request('GET',url));browser._supplier_failure(error,url,'supplier_navigation');raise error
    yield
  with self.clients(handler),patch.object(time,'monotonic',side_effect=lambda:clock[0]),session.research_session(deadline=160),agent.capture_card_diagnostics() as diagnostics:
   offers,_=agent._collect_offers(Owned(),[slow,good],2,'ABC123','Cable','м',supplier_hosts={'slow.example','good.example'})
  self.assertEqual(calls,[slow,good]);self.assertEqual(len(offers),1);self.assertTrue(any(d['code']=='supplier_local_error' for d in diagnostics),diagnostics)
  for parent,cancel in ((110,None),(160,lambda:clock[0]>=105)):
   clock[0]=100;calls.clear()
   with self.clients(handler),patch.object(time,'monotonic',side_effect=lambda:clock[0]),session.research_session(deadline=parent,cancelled=cancel):
    offers,_=agent._collect_offers(Owned(),[slow,good],2,'ABC123','Cable','м',supplier_hosts={'slow.example','good.example'})
   self.assertEqual(calls,[slow]);self.assertEqual(offers,[])
 def test_actual_normal_app_static_source_checks_without_browser_or_model(self):
  from engine import app,schemas
  from engine.search import base_db,source_registry
  from engine.schemas import PositionIn
  from engine.test_all_sites import AllSitesTests
  original=app._research_row;registry=source_registry.SourceRegistry(self.root/'registry.sqlite3');calls=[]
  with patch.object(app,'CACHE_DIR',self.root),patch.object(schemas,'CACHE_DIR',self.root),patch.object(base_db,'BASE_FILE',self.root/'base.json'),patch('engine.search.source_registry.SourceRegistry',return_value=registry),self.clients(lambda r:(calls.append(str(r.url)) or self.response(html()))),patch.object(browser.Browser,'_start',side_effect=AssertionError('No Browser')),patch.object(llm,'complete_json',side_effect=AssertionError('No model')):
   job=AllSitesTests().run_job([PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)],['supplier.example'],lambda *a,**kw:[URL],lambda p:original(p,use_cache=False))
  row=job['rows'][0];self.assertEqual(job['status'],'done');self.assertEqual(calls,[URL]);self.assertEqual(row['cards_attempted'],1);self.assertEqual(row['found'],1);self.assertEqual(row['candidates'][0]['identity_evidence']['provenance'],'http_main_product');self.assertEqual(row['source_checks'][0]['status'],'found');self.assertEqual(row['source_checks'][0]['product_presence'],'unknown')
if __name__=='__main__':unittest.main()
