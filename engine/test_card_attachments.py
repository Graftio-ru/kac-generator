"""Actual facts-first producer and shared finite attachment integrations, offline."""
import json,time,tempfile,unittest,hashlib
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch,MagicMock
from PIL import Image
from engine import app,config,schemas,llm,pipeline
from engine.models import SpecPosition,PositionResult,Requisites
from engine.search import agent,card_attachments as photos,session,base_db
from engine.test_http_card import bind_main_facts,html
from engine.offer_selection import select_top_offers,offer_is_comparable

class FiniteAttachmentTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.addCleanup(patch.stopall)
  for module in (agent,app,config,schemas):patch.object(module,'CACHE_DIR',self.root).start()
  patch.object(base_db,'BASE_FILE',self.root/'base.json').start()
  patch.object(agent,'wl_requisites_for',side_effect=lambda url:Requisites(name='Supplier',inn=self.inns.get(url,'7842224734'))).start()
  self.inns={};self.pages={};self.opens=[];self.shots=[]
  patch.object(llm,'complete_json',side_effect=AssertionError('No model for literal facts/attachment pass')).start()
  self.photo_patch=patch('engine.review_context.capture_context',side_effect=self.photo);self.photo_patch.start()
  self.pos=SpecPosition(number=1,name='Cable',type_mark='ABC123',unit='м',qty=2)
  owner=self
  class Browser:
   @contextmanager
   def page(inner,url):
    owner.opens.append(url);yield owner.pages[url]
  self.browser=Browser()
 def page(self,url,price=120,unit='м',quantity=1):
  page=MagicMock();page.url=page._kac_document_url=url;page._kac_response_status=200;page._kac_retry_after=None
  page.title.return_value='Cable ABC123';page.inner_text.return_value=f'Cable ABC123\nАртикул: ABC123\n{price} ₽\nЦена за {quantity} {unit}'
  page.locator.return_value.count.return_value=1;page.locator.return_value.first.inner_text.return_value='Cable ABC123';bind_main_facts(page);self.pages[url]=page;return page
 def photo(self,page,path,url):
  self.shots.append(url);Image.new('RGB',(12,12),'white').save(path);data=Path(path).read_bytes()
  return dict(version=1,role='review_context',filename=Path(path).name,sha256=hashlib.sha256(data).hexdigest(),url=url,captured_at=time.time(),width=12,height=12,document_width=12,document_height=12,truncated=False),''
 def offer(self,url,price,inn='7842224734'):
  self.inns[url]=inn;self.page(url,price)
  with session.research_session(deadline=time.monotonic()+20):
   snap=agent._capture_rendered_card(self.browser,url,url,url.split('/')[2],self.pos.type_mark,self.pos.name,self.pos.unit,self.pos.qty,None,set(),None)
   data=agent._deterministic_card_data(snap);self.assertIsNotNone(data)
   offer=agent._finish_captured_card(self.browser,snap,data)
  self.assertTrue(offer_is_comparable(offer,self.pos));return offer
 def test_candidate_no_png_and_slash_literal_before_model(self):
  url='https://supplier.example/item';page=self.page(url)
  page.inner_text.return_value='Cable ABC123\nАртикул: ABC123\n120 ₽ /м';bind_main_facts(page)
  with session.research_session(deadline=time.monotonic()+20):snap=agent._capture_rendered_card(self.browser,url,url,'supplier.example','ABC123','Cable','м',2,None,set(),None)
  self.assertEqual(self.shots,[]);self.assertEqual(snap.facts()['screenshot_context'],'');self.assertEqual(agent._deterministic_card_data(snap)['price_with_vat'],120)
 def test_fixed_queue_reselection_price_down_and_up_and_photo_binding(self):
  inns=['7842224734','7707083893','7728168971','7708503727'];urls=[f'https://s{i}.example/item' for i in range(4)]
  for price in (80,250):
   self.shots.clear();offers=[self.offer(u,p,inn) for u,p,inn in zip(urls,[200,150,100,90],inns)]
   self.opens.clear();self.page(urls[0],price);result=PositionResult(position=self.pos,offers=select_top_offers(offers,3,self.pos),all_offers=offers)
   with session.research_session(deadline=time.monotonic()+20):result,diags=photos.attach_result(result,self.browser)
   self.assertEqual(len(self.opens),3);self.assertEqual(len(self.shots),3);self.assertEqual({o.url for o in result.offers},set(urls[:3]) if price==250 else set(urls[1:]))
   fresh=next(o for o in result.all_offers if o.url==urls[0]);self.assertEqual(fresh.price_with_vat,price);self.assertEqual(fresh.source_quote_evidence['attachment_binding']['quote_sha256'],photos.quote_digest(fresh.source_quote_evidence));self.assertEqual(set(fresh.context_capture_evidence),__import__('engine.review_context',fromlist=['KEYS']).KEYS)
   if price==80:self.assertFalse(next(o for o in result.offers if o.url==urls[3]).screenshot_context)
 def test_mutation_during_photo_keeps_new_price_without_old_png(self):
  url='https://supplier.example/item';old=self.offer(url,200)
  def mutate(page,path,url):
   evidence,reason=self.photo(page,path,url);page.inner_text.return_value='Cable ABC123\nАртикул: ABC123\n80 ₽\nЦена за 1 м';bind_main_facts(page);return evidence,reason
  self.photo_patch.stop();patch('engine.review_context.capture_context',side_effect=mutate).start()
  result=PositionResult(position=self.pos,offers=[old])
  with session.research_session(deadline=time.monotonic()+20):result,_=photos.attach_result(result,self.browser)
  self.assertEqual(result.offers[0].price_with_vat,80);self.assertFalse(result.offers[0].screenshot_context);self.assertIn('изменились',result.offers[0].context_capture_review_reason);self.assertNotIn('attachment_binding',result.offers[0].source_quote_evidence)
 def test_late_generation_reapplies_stock_minimum_price_and_lot_admission(self):
  cases=[('dkc.market','арт. ABC123\nНет в наличии\nОписание','out_of_stock'),('cable.ru','цены как минимальные; окончательная цена уточняется','minimum_price'),('satro-paladin.com','Вся кабельная продукция продаётся бухтами\nДлина бухты, м\n100','minimum_order')]
  for host,condition,code in cases:
   url='https://'+host+'/item';old=self.offer(url,200);self.shots.clear();self.opens.clear()
   def mutate(page,path,url):
    evidence,reason=self.photo(page,path,url);page.inner_text.return_value='Cable ABC123\nАртикул: ABC123\n80 ₽\nЦена за 1 м\n'+condition;bind_main_facts(page);return evidence,reason
   with patch('engine.review_context.capture_context',side_effect=mutate),session.research_session(deadline=time.monotonic()+20):result,diagnostics=photos.attach_result(PositionResult(position=self.pos,offers=[old]),self.browser)
   self.assertEqual(self.opens,[url]);self.assertEqual(len(self.shots),1);self.assertEqual(result.offers,[],(host,diagnostics));self.assertEqual(result.all_offers[0].price_with_vat,200);self.assertTrue(result.all_offers[0].refresh_failed);self.assertFalse(result.all_offers[0].screenshot_product);self.assertFalse(result.all_offers[0].screenshot_context);self.assertTrue(any(d.get('code')==code for d in diagnostics),(host,diagnostics))
 def test_attachment_failure_price_retained_but_current_contradiction_blocks(self):
  url='https://supplier.example/item';old=self.offer(url,200);old.screenshot_product='old.png'
  result=PositionResult(position=self.pos,offers=[old])
  with patch.object(photos,'capture_current',side_effect=TimeoutError),session.research_session(deadline=time.monotonic()+20):result,_=photos.attach_result(result,self.browser)
  self.assertEqual(result.offers[0].price_with_vat,200);self.assertFalse(result.offers[0].screenshot_product)
  self.page(url);self.pages[url].inner_text.return_value='Cable ABC123\nАртикул: ABC123\nЦена по запросу';bind_main_facts(self.pages[url])
  # The CDP source fixture explicitly reports the observed nonnumeric condition.
  send=self.pages[url].context.new_cdp_session.return_value.send.side_effect
  def request(method,params):
   reply=send(method,params)
   if params.get('awaitPromise'):reply['result']['value']['kind']='explicit_price_on_request'
   return reply
  self.pages[url].context.new_cdp_session.return_value.send.side_effect=request
  with session.research_session(deadline=time.monotonic()+20):result,_=photos.attach_result(result,self.browser)
  self.assertEqual(result.offers,[]);self.assertEqual(result.all_offers[0].price_with_vat,200);self.assertTrue(result.all_offers[0].refresh_failed)
 def test_stop_never_opens_or_publishes_late_result(self):
  url='https://supplier.example/item';old=self.offer(url,200);self.opens.clear();result=PositionResult(position=self.pos,offers=[old]);before=result.model_dump();stopped=[False]
  with session.research_session(cancelled=lambda:True):
   with self.assertRaises(session.SearchBudgetExceeded):photos.attach_result(result,self.browser)
  self.assertEqual(self.opens,[]);self.assertEqual(result.model_dump(),before)
  def stop(*a,**k):stopped[0]=True;return None,old
  with patch.object(photos,'capture_current',side_effect=stop),session.research_session(cancelled=lambda:stopped[0]):
   with self.assertRaises(session.SearchBudgetExceeded):photos.attach_result(result,self.browser)
  self.assertEqual(result.model_dump(),before)
 def test_recapture_actual_quantity_deterministic_current_offer(self):
  url='https://supplier.example/item';self.page(url,120);self.inns[url]='7842224734'
  @contextmanager
  def factory():yield self.browser
  with patch.object(app,'_browser_factory',factory):result=app.capture(app.CaptureRequest(url=url,name='Cable',type_mark='ABC123',unit='м',qty=2))
  self.assertTrue(result['quote_qualified']);self.assertEqual(result['normalized_price_with_vat'],120);self.assertEqual(len(self.shots),1);self.assertTrue(result['source_quote_evidence']['attachment_binding']);self.assertEqual(self.opens,[url])
 def test_pipeline_skip_search_and_cli_share_actual_attachment_route(self):
  from engine import run_spec_research as cli
  url='https://supplier.example/item';offer=self.offer(url,200);self.page(url,150)
  @contextmanager
  def factory():yield self.browser
  p=schemas.PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=2,offers=[schemas.OfferIn.model_validate(app._offer_to_dict(offer))])
  report={'rows':[{'status':'partial','found':1,'target':5,'offers':[app._offer_to_dict(offer)],'candidates':[app._offer_to_dict(offer)]}]}
  with patch('engine.browser.Browser',factory):cli.attach_report(report,[p])
  self.assertEqual(report['rows'][0]['candidates'][0]['price_with_vat'],150);self.assertEqual(report['rows'][0]['status'],'partial');self.assertTrue(report['rows'][0]['offers'][0]['screenshot_context'])
  result=PositionResult(position=self.pos,offers=[offer]);job=pipeline.JobState(job_id='offline',object_name='Object')
  with patch('engine.final_eligibility.prepare_final_results') as eligibility,patch.object(pipeline,'build_tkp',return_value=(self.root/'tkp.pdf',0)),patch.object(pipeline,'build_kac',return_value=self.root/'kac.xlsx'):
   pipeline.run_pipeline(job,pipeline.JobInputs(object_name='Object',spec_results=[result],skip_search=True,use_tsn=False),factory)
  self.assertEqual(job.status,'done',job.error);self.assertEqual(result.offers[0].price_with_vat,150);self.assertTrue(result.offers[0].screenshot_context);eligibility.assert_called_once_with([result])
 def test_normal_app_final_publisher_keeps_price_change(self):
  from engine.test_all_sites import AllSitesTests
  from engine.search import source_registry
  url='https://supplier.example/item';page=self.page(url,200);self.inns[url]='7842224734';original=app._research_row
  owner=self
  class Browser:
   def is_usable(self):return True
   @contextmanager
   def page(inner,url):
    owner.opens.append(url)
    if len(owner.opens)>1:page.inner_text.return_value='Cable ABC123\nАртикул: ABC123\n80 ₽\nЦена за 1 м';bind_main_facts(page)
    yield page
  @contextmanager
  def factory():yield Browser()
  import httpx
  shell=httpx.Response(200,text='<main>JS shell</main>',request=httpx.Request('GET',url))
  registry=source_registry.SourceRegistry(self.root/'registry.sqlite3')
  with patch.object(app,'_browser_factory',factory),patch.object(agent,'supplier_http_get',return_value=shell),patch('engine.search.source_registry.SourceRegistry',return_value=registry):job=AllSitesTests().run_job([schemas.PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=2)],['supplier.example'],lambda *a,**kw:[url],lambda p:original(p,use_cache=False))
  row=job['rows'][0];self.assertEqual(row['candidates'][0]['price_with_vat'],80,row);self.assertEqual(row['offers'][0]['price_with_vat'],80);self.assertEqual(len(self.shots),1)

 def test_review_quota_and_typed_local_timeout_do_not_expand_queue(self):
  urls=[f'https://q{i}.example/item' for i in range(6)];inns=['7842224734','7707083893','7728168971','7708503727','7842224734','7707083893']
  offers=[self.offer(u,200-i,inn) for i,(u,inn) in enumerate(zip(urls,inns))]
  for offer in offers[3:]:offer.unit_review_required=True;offer.unit_review_reason='Unknown sale unit'
  result=PositionResult(position=self.pos,offers=offers[:3],all_offers=offers);called=[]
  def expired(browser,pos,url,**kw):called.append(url);raise session.SearchBudgetExceeded('Child allocation exhausted')
  with patch.object(photos,'capture_current',side_effect=expired),session.research_session(deadline=time.monotonic()+20):result,diagnostics=photos.attach_result(result,self.browser)
  self.assertEqual(len(called),4);self.assertEqual(sum(d['code']=='attachment_timeout' for d in diagnostics),4);self.assertEqual(len(result.offers),3)
 def test_http_explicit_other_main_title_refused_without_browser_model(self):
  import httpx
  from engine.search import http_card
  body=html(sku='OTHER999',generic=True).replace('Cable ABC123','Cable OTHER999')
  response=httpx.Response(200,headers={'content-type':'text/html'},text=body,request=httpx.Request('GET','https://supplier.example/item'))
  with patch.object(http_card,'CACHE_DIR',self.root),patch.object(agent,'supplier_http_get',return_value=response),session.research_session(deadline=time.monotonic()+20),agent.capture_card_diagnostics() as diagnostics:
   offers,checked=agent._collect_offers(self.browser,['https://supplier.example/item'],1,'ABC123','Cable','м',expected_qty=2,supplier_hosts={'supplier.example'})
  self.assertEqual(self.opens,[]);self.assertEqual(offers,[]);self.assertEqual(checked,1);self.assertTrue(any(d['code']=='article_mismatch' for d in diagnostics))
 def test_positive_connected_http_product_with_explicit_other_related_url(self):
  from engine.search.http_card import main_product
  related={'@type':'Product','name':'Unrelated','sku':'OTHER999','url':'https://supplier.example/other'}
  body=html(generic=True)+'<script type="application/ld+json">'+json.dumps(related)+'</script>'
  observed=main_product(body,'https://supplier.example/item');self.assertIsNotNone(observed);self.assertEqual(observed['listed_price'],120)
  related.pop('url');body=html(generic=True)+'<script type="application/ld+json">'+json.dumps(related)+'</script>'
  self.assertIsNone(main_product(body,'https://supplier.example/item'))
  # A connected main JSON-LD amount cannot borrow a recommendation's literal.
  body=html(generic=True).replace('<div class="price">120 ₽</div><p>Цена за 1 м</p>','<h2>Related products</h2><div>120 ₽</div><p>Цена за 1 м</p>')
  self.assertIsNone(main_product(body,'https://supplier.example/item'))
 def test_single_supplier_api_cached_result_still_gets_finite_photo(self):
  url='https://supplier.example/item';old=self.offer(url,200);self.page(url,150)
  result=PositionResult(position=self.pos,offers=[old],sources_found=1)
  @contextmanager
  def factory():yield self.browser
  with patch.object(app,'_browser_factory',factory),patch.object(base_db,'lookup',return_value=result),patch.object(base_db,'has_enough_sources',return_value=True),patch.object(base_db,'upsert',side_effect=lambda pos,res:res):data=app.search_position(app.SearchRequest(name='Cable',type_mark='ABC123',unit='м',qty=2,all_suppliers=False))
  self.assertEqual(data['candidates'][0]['price_with_vat'],150);self.assertTrue(data['offers'][0]['screenshot_context']);self.assertEqual(len(self.shots),1)

 def test_real_capture_client_quantity_and_qualified_price_replacement(self):
  import subprocess
  source=Path(app.__file__).parent.parent/'web/common.js'
  code=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const func=source.slice(source.indexOf('async function captureOffer('),source.indexOf('async function searchPosition('));
(async()=>{
 for(const [price,qualified] of [[80,true],[250,true],[null,false],[70,false]]) {
  const previous={price_with_vat:200,url:'https://s.example/item',source_quote_evidence:{observed_at:1},inn:'7842224734',org_name:'Old seller',kpp:'AAA',city:'Samara',status:1,listed_price_with_vat:200,price_unit:'м',normalized_price_unit:'м',product_title:'Old cable'};
  let payload;
  const ctx={positions:[{name:'Cable',type_mark:'ABC123',unit:'м',qty:12,offers:[previous]}],renderPositions(){},engineUrl(){return ''},requireOk:async()=>{},alert(){throw Error('unexpected alert')},fetch:async(url,options)=>{payload=JSON.parse(options.body);return {json:async()=>({quote_qualified:qualified,normalized_price_with_vat:price,source_quote_evidence:{observed_at:2},identity_evidence:{main_article:'ABC123'},price_unit:qualified?'м':'упаковка',listed_price_with_vat:qualified?price:300,extracted:{product_title:qualified?'New cable':'Wrong product'},unit_review_required:!qualified,unit_review_reason:qualified?'':'Wrong unit',normalized_price_unit:qualified?'м':'',requisites:{inn:'7707083893',name:'New seller',kpp:'BBB',city:'Kazan',status:2}})}}};
  vm.createContext(ctx);vm.runInContext(func,ctx);await ctx.captureOffer(0,0);
  assert.equal(payload.qty,12);assert.equal(previous.price_with_vat,qualified?price:200);
  assert.equal(previous.source_quote_evidence.observed_at,qualified?2:1);
  assert.equal(previous.inn,qualified?'7707083893':'7842224734');assert.equal(previous.refresh_failed,!qualified);assert.equal(previous.kpp,qualified?'BBB':'AAA');assert.equal(previous.city,qualified?'Kazan':'Samara');assert.equal(previous.status,qualified?2:1);assert.equal(previous.listed_price_with_vat,qualified?price:200);assert.equal(previous.price_unit,'м');assert.equal(previous.normalized_price_unit,'м');assert.equal(previous.product_title,qualified?'New cable':'Old cable');
 }
 console.log('4 actual captureOffer cases PASS');
})().catch(e=>{console.error(e);process.exit(1)});
"""
  completed=subprocess.run(['node','-e',code,str(source)],capture_output=True,text=True,timeout=10)
  self.assertEqual(completed.returncode,0,completed.stderr);self.assertIn('4 actual',completed.stdout)
 def test_actual_package_relative_links_and_generation_metadata(self):
  from engine.builders import research_package
  from zipfile import ZipFile
  from io import BytesIO
  from openpyxl import load_workbook
  url='https://supplier.example/item';offer=self.offer(url,200)
  result=PositionResult(position=self.pos,offers=[offer])
  with session.research_session(deadline=time.monotonic()+20):result,_=photos.attach_result(result,self.browser)
  current=result.offers[0];data=app._offer_to_dict(current)
  position=schemas.ResearchReportPositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=2,offers=[schemas.OfferIn.model_validate(data)],candidates=[schemas.OfferIn.model_validate(data)])
  request=schemas.ResearchReportRequest(positions=[position])
  self.assertEqual(request.positions[0].offers[0].source_quote_evidence['attachment_binding']['sha256'],current.context_capture_evidence['sha256'])
  with patch.object(research_package,'CACHE_DIR',self.root):packed=research_package.build_research_package(request)
  with ZipFile(BytesIO(packed)) as archive:
   wb=load_workbook(BytesIO(archive.read('supplier-research-summary.xlsx')))
   links=[cell.hyperlink.target for sheet in wb for row in sheet for cell in row if cell.hyperlink and cell.hyperlink.target.startswith('screenshots/')]
   self.assertTrue(links);self.assertTrue(all(link in archive.namelist() for link in links));self.assertTrue(any('context-' in link for link in links))

if __name__=='__main__':unittest.main()
