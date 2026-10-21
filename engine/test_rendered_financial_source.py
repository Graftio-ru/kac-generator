"""Offline synthetic-current-DOM qualification, never a historical render replay."""
import copy,hashlib,json,time,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from PIL import Image
from engine import config,llm
from engine.search import agent,http_card,factory_reference as factory
from engine.search.catalogue import CatalogueStore
from engine.search.catalogue_images import CatalogueImageStore
from engine.search.catalogue_producer import capture_and_publish,CataloguePublicationError
from engine.search.card_batch import CardSnapshot
from engine.search.catalogue_projection import _facts,project_product,catalogue_scope
from engine.search.selected_proofs import capture_selected_proofs,final_offers,proof_ready
from engine import test_factory_kit_equivalence as baseline
BASE=None
class RenderedFinancialSourceTests(unittest.TestCase):
 setUp=baseline.FactoryKitEquivalenceTests.setUp
 acquire=baseline.FactoryKitEquivalenceTests.acquire
 def dom_snapshot(self,body=None,*,image=True):
  # Saved public HTTP bytes are deliberately a SYNTHETIC current DOM input.
  # This is not the missing actual190 DOM, nor an assertion about its pixels.
  body=(self.body if body is None else body).decode(self.seller['encoding'])
  owned=http_card.main_product(body,self.url,check_controls=False);self.assertIsNotNone(owned)
  stamp=time.time()-.2;root=self.root/'main_facts';root.mkdir(exist_ok=True)
  def frame(at):return dict(version=1,role='main_card_facts',kind='complete',missing_fields=[],document_url=self.url,requested_url=self.url,document_title=owned['title'],main_title=owned['title'],article=owned['article'],owner_text=owned['text'],purchase_text=owned['text'],body=owned['text'],owner_bound=True,owner_truncated=False,body_truncated=False,observed_at=at,control_captions=[])
  def retain(frame):
   frame['rendered_source']=http_card.save_rendered_dom(body,frame,self.url)
   path=root/(__import__('uuid').uuid4().hex+'.json');raw=json.dumps(frame,ensure_ascii=False,allow_nan=False).encode();path.write_bytes(raw)
   return dict(ref=str(path.relative_to(self.root)),sha256=hashlib.sha256(raw).hexdigest(),observed_at=frame['observed_at'])
  before=frame(stamp);bref=retain(before);after=frame(stamp+.2);aref=retain(after)
  context={};photo=''
  if image:
   path=self.root/'synthetic-current-dom.png';Image.new('RGB',(20,20),'white').save(path);raw=path.read_bytes();photo=str(path)
   context=dict(version=1,role='review_context',filename=path.name,sha256=hashlib.sha256(raw).hexdigest(),url=self.url,captured_at=stamp+.1,width=20,height=20,document_width=20,document_height=20,truncated=False)
  source=http_card.rendered_source(before,bref,after=after,after_facts=aref,context=context or None)
  self.assertIsNotNone(http_card.read_source(source,allow_rendered=True));self.assertTrue(agent._same_card_generation(before,after))
  identity=dict(version=1,provenance='rendered_main_card',source_url=self.url,canonical_url=self.url,host='elektro.ru',expected_mark=self.position.type_mark,expected_name=self.position.name,main_title=owned['title'],main_article=owned['article'],observed_at=stamp,rendered_source=source)
  facts=dict(url=self.url,requested_url=self.url,supplier_host='elektro.ru',expected_type_mark=self.position.type_mark,expected_name=self.position.name,expected_unit=self.position.unit,expected_qty=self.position.qty,text=owned['text'],purchase_text=owned['text'],title=owned['title'],main_title=owned['title'],page_article=owned['article'],identity_evidence=identity,adapter={},roll_length=None,captured_at=stamp,main_facts=bref,rendered_source=source,readiness_kind='complete',missing_fields=[],document_body=owned['text'],screenshot_context=photo,context_capture_evidence=context,context_capture_review_reason='')
  return CardSnapshot.create(facts,time.monotonic()+30),before,after
 def test_native_selected_price_photo_publish_reopen_new_rendered_proof(self):
  self.assert_selected(6595)
 def assert_selected(self,price):
  store,original=self.acquire();self.assertEqual(original.raw_amount,price);snapshot,before,after=self.dom_snapshot()
  with patch.object(agent,'_capture_rendered_card',return_value=snapshot) as render,patch.object(llm,'complete_json',side_effect=AssertionError('No model')):
   report=capture_selected_proofs(store,[self.position],browser=Mock(),image_store=CatalogueImageStore(self.root))
  self.assertEqual(render.call_count,1);self.assertEqual(report['positions'][0]['attachments'],1)
  reopened=CatalogueStore(self.root/'catalogue.sqlite3');result=project_product(reopened,self.identity,self.position)
  with catalogue_scope(reopened):
   selected=final_offers(result);self.assertEqual(len(selected),1);self.assertTrue(proof_ready(selected[0],self.position))
  current=reopened.lookup(self.identity)['heads'][0]['current'];self.assertNotEqual(current['observation_id'],original.observation_id)
  self.assertEqual(current['issuer'],'supplier_rendered');self.assertEqual(current['unit_equivalence']['supplier_source']['role'],'rendered_primary_dom');self.assertEqual(current['package_quantity'],1)
  self.assertEqual(_facts(current,self.position)[0]['price'],price);self.assertEqual(current['image']['status'],'captured');self.assertFalse(current.get('acquisition_completion'))
  # Missing/tampered attachment never fabricates image readiness or erases an
  # independently valid financial source. Its proof has no pixel authority.
  self.assertNotIn('photo_binding',current['unit_equivalence']['supplier_source'])
  changed=copy.deepcopy(current);changed['issuer']='supplier_http';self.assertIsNone(_facts(changed,self.position)[0])
  (self.root/current['image']['local_path']).write_bytes(b'changed pixels')
  with catalogue_scope(reopened):
   self.assertEqual(_facts(current,self.position)[0]['price'],price);self.assertFalse(proof_ready(selected[0],self.position))
 def test_source_role_epoch_image_count_price_and_authority_mutations_refuse(self):
  snapshot,before,after=self.dom_snapshot();source=snapshot.facts()['rendered_source']
  self.assertIsNone(http_card.read_source(source)) # Legacy HTTP-only readers cannot relabel a DOM.
  for change in [dict(role='supplier_http'),dict(body_sha256='0'*64),dict(main_facts={**source['main_facts'],'observed_at':before['observed_at']+1})]:
   self.assertIsNone(http_card.read_source({**source,**change},allow_rendered=True))
  for key,value in [('captured_at',after['observed_at']+1),('sha256','0'*64),('url','https://wrong.test/')]:
   changed=copy.deepcopy(source);changed['photo_binding']['context'][key]=value;self.assertIsNotNone(http_card.read_source(changed,allow_rendered=True));self.assertFalse(http_card.rendered_photo_valid(changed,changed['photo_binding']['context'],snapshot.facts()['screenshot_context']))
  mutation=copy.deepcopy(after);mutation['rendered_source']['body_sha256']='0'*64;self.assertFalse(agent._same_card_generation(before,mutation))
  # A different current DOM rederives a different price, never copies6595.
  changed=self.body.decode(self.seller['encoding']).replace('6 595','6 596').replace('6595','6596').encode(self.seller['encoding'])
  changed_snapshot,_,_=self.dom_snapshot(changed,image=False)
  with factory.request_scope(self.position):
   proof=factory.derive_singleton(changed_snapshot.facts()['rendered_source'],self.position);self.assertEqual(proof['listed_price'],6596)
  count=self.body.decode(self.seller['encoding']).replace('data-pack-size="1"','data-pack-size="2"').encode(self.seller['encoding'])
  # Bound source is retained; singleton inference must fail closed.
  frame=snapshot.facts();record=http_card.read_source(source,allow_rendered=True)[0]
  count_before=copy.deepcopy(before);count_before['observed_at']=time.time()
  count_before['rendered_source']=http_card.save_rendered_dom(count.decode(self.seller['encoding']),count_before,self.url)
  path=self.root/'main_facts'/(__import__('uuid').uuid4().hex+'.json');raw=json.dumps(count_before,ensure_ascii=False).encode();path.write_bytes(raw)
  count_source={**count_before['rendered_source'],'main_facts':dict(ref=str(path.relative_to(self.root)),sha256=hashlib.sha256(raw).hexdigest(),observed_at=count_before['observed_at'])}
  with factory.request_scope(self.position),self.assertRaises(factory.FactoryEvidenceError):factory.derive_singleton(count_source,self.position)
  factory.approve_origin('DKC','https://www.dkc.ru','task186-root-dkc-origin-20261019-r1',2,False)
  with factory.request_scope(self.position),self.assertRaises(factory.FactoryEvidenceError):factory.derive_singleton(source,self.position)
 def test_actual190_missing_dom_is_not_reconstructed_or_promoted(self):
  witness=json.loads((BASE/'EPIC-0001-TASK-0190/quality-financial/elektro-selected-image-evidence-loss.json').read_text())
  current=witness['current_observation'];self.assertIsNone(current.get('unit_equivalence'));self.assertIsNone(current.get('package_quantity'));self.assertIsNone(_facts(current,self.position)[0])
  for item in witness['snapshots'].values():self.assertFalse(item['facts'].get('rendered_source'))

 def test_different_reviewed_generic_identity_native_selected_photo_and_reopen(self):
  from engine.search.catalogue import identity_for_position
  data=BASE/'EPIC-0001-TASK-0186/generic-reference-v3';source=json.loads((data/'source-input.json').read_text());review=json.loads((BASE/'EPIC-0001-TASK-0186/reference-review/generic-receipt-v3.json').read_text())
  with patch.object(time,'time',return_value=max(self.review['provenance']['reviewed_at'],review['provenance']['reviewed_at'])+1):
   factory.approve_origin(**source['authority']);reference=factory.import_reference((data/'factory.html').read_bytes(),**source['source']);factory.activate_reference(reference,review,(BASE/'EPIC-0001-TASK-0186/reference-review/generic-technical-review-evidence-v3.json').read_bytes())
   from engine.models import SpecPosition
   position=json.loads(json.dumps(self.position.model_dump(),ensure_ascii=False).replace('36785KZL','56785KZL').replace('DKC','ACME'));position['source_evidence']['source_hash']=hashlib.sha256(b'synthetic-second-request').hexdigest();self.position=SpecPosition.model_validate(position);self.identity=identity_for_position(self.position)
   self.body=(data/'supplier.html').read_bytes();self.url=source['supplier_url'];self.seller={**self.seller,'observed_at':source['supplier_observed_at'],'encoding':'utf-8'}
   self.assert_selected(120)
 def test_native_coherent_reader_retains_dom_and_purchase_change_breaks_photo_epoch(self):
  owned=http_card.main_product(self.body.decode(self.seller['encoding']),self.url,check_controls=False)
  fields=dict(kind='complete',missing_fields=[],document_url=self.url,document_title=owned['title'],main_title=owned['title'],article=owned['article'],owner_text=owned['text'],purchase_text=owned['text'],owner_bound=True,owner_truncated=False,body=owned['text'],body_truncated=False,observed_at=time.time(),control_captions=[],rendered_dom=self.body.decode(self.seller['encoding']))
  class Channel:
   def __init__(this,value):this.value=value
   def send(this,method,params):
    self.assertEqual(method,'Runtime.evaluate');self.assertIn('capture_dom',params['expression']);return {'result':{'value':copy.deepcopy(this.value)}}
   def detach(this):pass
  page=Mock();page.url=self.url;page.context.new_cdp_session.side_effect=lambda _:Channel(fields)
  before,bref=agent._wait_main_card_facts(page,self.url,max_wait=.1,immediate=True)
  self.assertTrue(before.get('rendered_source'));self.assertNotIn('rendered_dom',json.loads((self.root/bref['ref']).read_text()))
  fields['rendered_dom']=fields['rendered_dom'].replace('data-pack-size="1"','data-pack-size="2"')
  after,aref=agent._wait_main_card_facts(page,self.url,max_wait=.1,immediate=True)
  self.assertFalse(agent._same_card_generation(before,after))
  source=http_card.rendered_source(before,bref,after=after,after_facts=aref,context={'captured_at':time.time()});self.assertIsNone(source)

 def test_prepublication_image_bytes_or_binding_failure_retains_native_financial_quote(self):
  store,original=self.acquire()
  for mode in ('corrupt_png','missing_png','bad_binding'):
   with self.subTest(mode=mode):
    snapshot,before,after=self.dom_snapshot();facts=snapshot.facts()
    if mode=='corrupt_png':Path(facts['screenshot_context']).write_bytes(b'corrupt before publication')
    elif mode=='missing_png':Path(facts['screenshot_context']).unlink()
    else:facts['rendered_source']['photo_binding']['context']['sha256']='0'*64;snapshot=CardSnapshot.create(facts,snapshot.deadline)
    self.assertIsNotNone(http_card.read_source(facts['rendered_source'],allow_rendered=True))
    with patch.object(agent,'_capture_rendered_card',return_value=snapshot),patch.object(llm,'complete_json',side_effect=AssertionError('No model')):
     report=capture_selected_proofs(store,[self.position],browser=Mock(),image_store=CatalogueImageStore(self.root))
    reopened=CatalogueStore(self.root/'catalogue.sqlite3');result=project_product(reopened,self.identity,self.position)
    with catalogue_scope(reopened):
     chosen=final_offers(result);self.assertEqual(len(chosen),1);self.assertEqual(chosen[0].price_with_vat,6595);self.assertFalse(proof_ready(chosen[0],self.position));self.assertFalse(chosen[0].screenshot_context)
    current=reopened.lookup(self.identity)['heads'][0]['current'];self.assertEqual(_facts(current,self.position)[0]['price'],6595);self.assertEqual(current['image']['status'],'missing');self.assertEqual(report['positions'][0]['attachments'],0)
 def test_serializer_text_controls_and_clone_share_computed_visibility(self):
  # Static architecture guard only. Real JS fixtures execute separately192.
  script=agent._MAIN_FACTS_READINESS_JS
  self.assertEqual(script.count('getComputedStyle('),1)
  self.assertIn('const computedInvisible=e=>{for(let p=e;p;p=p.parentElement)',script)
  self.assertIn("style.visibility==='collapse'",script);self.assertIn('opacity<=0',script)
  self.assertIn('return Boolean(e&&computedInvisible(e))',script)
  self.assertIn("computedInvisible(node))copy.setAttribute('hidden','')",script)
  self.assertIn('const previewVisible=e=>Boolean(e&&!excluded(e))',script)

 def test_explicit_external_technical_scope_and_conflicts_are_rederived(self):
  from engine.search.catalogue import identity_for_position
  data=BASE/'EPIC-0001-TASK-0186/generic-reference-v3';source=json.loads((data/'source-input.json').read_text());review=json.loads((BASE/'EPIC-0001-TASK-0186/reference-review/generic-receipt-v3.json').read_text())
  factory.approve_origin(**source['authority']);reference=factory.import_reference((data/'factory.html').read_bytes(),**source['source']);factory.activate_reference(reference,review,(BASE/'EPIC-0001-TASK-0186/reference-review/generic-technical-review-evidence-v3.json').read_bytes())
  from engine.models import SpecPosition
  position=json.loads(json.dumps(self.position.model_dump(),ensure_ascii=False).replace('36785KZL','56785KZL').replace('DKC','ACME'));position['source_evidence']['source_hash']=hashlib.sha256(b'synthetic-second-request').hexdigest();self.position=SpecPosition.model_validate(position);self.identity=identity_for_position(self.position)
  body=(data/'supplier.html').read_text();self.url=source['supplier_url'];self.seller={**self.seller,'observed_at':source['supplier_observed_at'],'encoding':'utf-8'}
  description='Сборочная единица предназначена для изменения направления кабельной трассы. В комплект входят крепежные элементы и соединительные пластины.'
  # A different generic layout: the whole technical region is outside the
  # original purchase article and explicitly referenced by the single Product.
  body=body.replace('<div itemscope itemtype="https://schema.org/Product">','<div itemscope itemref="technical" itemtype="https://schema.org/Product">').replace('<div>'+description+'</div>','').replace('</article>','</article><section id="technical" itemprop="description">'+description+'</section>')
  owned=http_card.main_product(body,self.url,check_controls=False);self.assertIsNotNone(owned);self.assertIn(description,owned['factory_owner_text'])
  self.body=body.encode();self.assert_selected(120)
  # The entire intervening closure remains evidence; no selection of only the
  # affirmative description and no missing/hidden/ambiguous binding promotion.
  for changed in [body.replace('id="technical"','id="missing"'),body.replace('id="technical"','hidden id="technical"'),body.replace('</main>','<div id="technical" itemprop="description">duplicate</div></main>')]:
   self.assertIsNone(http_card.main_product(changed,self.url,check_controls=False))
  changed=body.replace('</article>','</article><p>Изделие отсутствует</p>');self.body=changed.encode()
  snapshot,_,_=self.dom_snapshot(image=False)
  with factory.request_scope(self.position),self.assertRaises(factory.FactoryEvidenceError):factory.derive_singleton(snapshot.facts()['rendered_source'],self.position)

 def test_capture_status_and_writer_refusal_are_durable_fixed_diagnostics(self):
  owned=http_card.main_product(self.body.decode(self.seller['encoding']),self.url,check_controls=False)
  fields=dict(kind='complete',missing_fields=[],document_url=self.url,document_title=owned['title'],main_title=owned['title'],article=owned['article'],owner_text=owned['text'],purchase_text=owned['text'],owner_bound=True,owner_truncated=False,body=owned['text'],body_truncated=False,observed_at=time.time(),control_captions=[],rendered_dom=self.body.decode(self.seller['encoding']),rendered_capture_status='captured',rendered_capture_budget={'nodes':100,'bytes':12345})
  class Channel:
   def send(this,method,params):return {'result':{'value':copy.deepcopy(fields)}}
   def detach(this):pass
  page=Mock();page.url=self.url;page.context.new_cdp_session.return_value=Channel()
  for error,status in [(None,'stored'),(ValueError('private URL/key not reportable'),'validation_refused'),(OSError('private path not reportable'),'storage_failed')]:
   with patch.object(http_card,'save_rendered_dom',wraps=http_card.save_rendered_dom,side_effect=error):result,ref=agent._wait_main_card_facts(page,self.url,max_wait=.1,immediate=True)
   stored=json.loads((self.root/ref['ref']).read_text());self.assertEqual(stored['rendered_writer_status'],status);self.assertEqual(stored['rendered_capture_status'],'captured');self.assertEqual(stored['rendered_capture_budget'],fields['rendered_capture_budget']);self.assertNotIn('private',json.dumps(stored));self.assertEqual(bool(stored.get('rendered_source')),error is None)
  fields.update(rendered_dom='',rendered_capture_status='byte_limit',rendered_capture_budget={'nodes':101,'bytes':2097153})
  result,ref=agent._wait_main_card_facts(page,self.url,max_wait=.1,immediate=True);self.assertEqual(result['rendered_writer_status'],'not_attempted');self.assertNotIn('rendered_source',result)

 def test_projection_retains_identity_conflicts_and_bounds_owned_content(self):
  # Static serializer contract. Real DOM execution belongs to TASK195.
  script=agent._MAIN_FACTS_READINESS_JS
  self.assertNotIn('cloneNode(true)',script)
  for symbol in ['identityRoot||owner','itemref','application/ld+json','node_limit','byte_limit','rendered_capture_budget','new TextEncoder()']:self.assertIn(symbol,script)
  self.assertIn('if(!deep&&!ancestors.has(node))return null',script)
  self.assertIn('copy.appendChild(next)',script)
  # A second declared identity cannot vanish through narrower capture scope.
  body=self.body.decode(self.seller['encoding']);changed=body.replace('</body>','<div itemscope itemtype="https://schema.org/Product"><meta itemprop="mpn" content="OTHER"></div></body>')
  self.assertIsNone(http_card.main_product(changed,self.url,check_controls=False))

 def test_actual196_empty_styles_keep_proof_but_meaningful_epoch_changes_refuse(self):
  # Original actual196 records are READ ONLY. Their original sources/proof are
  # rederived; normalized comparisons below are explicitly counterfactual, not
  # new published observations or a historical screenshot attestation.
  import re
  actual=BASE/'EPIC-0001-TASK-0196/next-actual-run/CACHE_DIR'
  names=['aa91c1d748ba4e62a9069772abfa1b6a.json','9ca3a62a7db8443eba01dc22e79d0c54.json']
  frames=[];bodies=[];refs=[]
  for name in names:
   path=actual/'main_facts'/name;raw=path.read_bytes();frame=json.loads(raw)
   dest=self.root/'main_facts'/name;dest.parent.mkdir(exist_ok=True);dest.write_bytes(raw)
   source=actual/frame['rendered_source']['ref'];target=self.root/frame['rendered_source']['ref'];target.parent.mkdir(exist_ok=True);target.write_bytes(source.read_bytes())
   record=json.loads(source.read_text());bodies.append(__import__('base64').b64decode(record['body_base64']).decode());frames.append(frame);refs.append(dict(ref='main_facts/'+name,sha256=hashlib.sha256(raw).hexdigest(),observed_at=frame['observed_at']))
  with patch.object(time,'time',return_value=frames[-1]['observed_at']+1),factory.request_scope(self.position):
   for frame,ref in zip(frames,refs):
    source={**frame['rendered_source'],'main_facts':ref};retained=http_card.read_source(source,allow_rendered=True);self.assertIsNotNone(retained)
    proof=factory.derive_singleton(source,self.position);self.assertEqual(proof['listed_price'],6595);self.assertEqual(proof['quantity'],1)
  self.assertFalse(agent._same_card_generation(*frames))
  nodes=[list(http_card.MainParser(body,False).root.descendants()) for body in bodies]
  self.assertEqual(len(nodes[0]),len(nodes[1]));changes=[]
  for before,after in zip(*nodes):
   self.assertEqual(before.tag,after.tag);self.assertEqual([v for v in before.text if isinstance(v,str)],[v for v in after.text if isinstance(v,str)])
   if before.attrs!=after.attrs:
    self.assertNotIn('style',before.attrs);self.assertEqual(after.attrs['style'],'');self.assertEqual(before.attrs,{k:v for k,v in after.attrs.items() if k!='style'});self.assertEqual(http_card._semantic_visible(before),http_card._semantic_visible(after));changes.append(before.path())
  self.assertEqual(len(changes),12);self.assertEqual(bodies[1].count(' style=""'),bodies[0].count(' style=""')+12)
  # This fixture-only transformation is deliberately not a second production
  # projector. Actual DOM clone behavior runs independently in TASK198.
  normalized=bodies[1].replace(' style=""','');self.assertEqual(normalized,bodies[0].replace(' style=""',''))
  def coherent_with(body,frame):
   result=copy.deepcopy(frame);result['rendered_source']['body_sha256']=hashlib.sha256(body.encode()).hexdigest();return result
  before=coherent_with(normalized,frames[0]);after=coherent_with(normalized,frames[1]);self.assertTrue(agent._same_card_generation(before,after))
  for changed in [normalized.replace('6595','6596').replace('6 595','6 596'),normalized.replace('data-pack-size="1"','data-pack-size="2"'),normalized.replace('36785KZL','36785KZL/S'),normalized.replace('</h1>','</h1><p>Изделие отсутствует</p>'),normalized.replace('<h1','<h1 style="opacity:0"'),normalized.replace('<h1','<h1 style="color:red"'),normalized.replace('<h1','<h1 hidden')]:
   self.assertNotEqual(changed,normalized);self.assertFalse(agent._same_card_generation(before,coherent_with(changed,frames[1])))
  script=agent._MAIN_FACTS_READINESS_JS
  self.assertIn("if(copy.hasAttribute('style')&&copy.getAttribute('style').trim()==='')copy.removeAttribute('style');",script)
  self.assertLess(script.index('computedInvisible(node))copy.setAttribute'),script.index("if(copy.hasAttribute('style')"))
  self.assertIn("rendered_source',{}).get('body_sha256')",__import__('inspect').getsource(agent._same_card_generation))
 def native_generation_capture(self,mode):
  """Real producer and SQLite, with only offline page/photo delivery replaced."""
  from contextlib import contextmanager
  from engine import review_context
  from engine.search.card_attachments import capture_current
  before_snapshot,before,unchanged_after=self.dom_snapshot();oldfacts=before_snapshot.facts()
  body=self.body.decode(self.seller['encoding'])
  if mode=='legacy_unsupported':
   # A real unsupported structural layout, not an invalid copy of a supported
   # receipt. Legacy text remains independently observed, but creates no typed
   # factory proof and cannot gain eligibility through the compatibility gate.
   from html import escape
   body='<html><body><main><h1>'+escape(before['main_title'])+'</h1><p>Артикул производителя: 36785KZL</p><p>6 595 ₽</p><p>Единица измерения: упак</p></main></body></html>'
   parser=http_card.MainParser(body,False);main=next(n for n in parser.root.descendants() if n.tag=='main');text=http_card._semantic_text(main).strip()
   before.update(owner_text=text,purchase_text=text,body=text)
   before['rendered_source']=http_card.save_rendered_dom(body,before,self.url)
   path=self.root/'main_facts'/(__import__('uuid').uuid4().hex+'.json');path.write_text(json.dumps(before,ensure_ascii=False,allow_nan=False))
   self.assertIsNone(http_card.main_product(body,self.url,check_controls=False))
  replacements={'price':lambda v:v.replace('6 595','6 596').replace('6595','6596'),'count':lambda v:v.replace('data-pack-size="1"','data-pack-size="2"'),'assembly':lambda v:v.replace('</h1>','</h1><p>Изделие отсутствует</p>'),'identity':lambda v:v.replace('36785KZL','36785KZL/S')}
  if mode=='unchanged':after=copy.deepcopy(unchanged_after)
  else:
   changed=replacements.get(mode,lambda v:v)(body)
   owned=http_card.main_product(changed,self.url,check_controls=False)
   after=copy.deepcopy(before);after['observed_at']=before['observed_at']+.2
   if owned:after.update(owner_text=owned['text'],purchase_text=owned['text'],main_title=owned['title'],article=owned['article'],body=owned['text'])
   after['rendered_source']=http_card.save_rendered_dom(changed,after,self.url)
   if mode=='foreign_url':after['document_url']='https://foreign.test/card'
   elif mode=='truncated':after['owner_truncated']=True
   elif mode=='unowned':after['owner_bound']=False
   elif mode=='invalid_source':after['rendered_source']['record_sha256']='0'*64
   path=self.root/'main_facts'/(__import__('uuid').uuid4().hex+'.json');path.write_text(json.dumps(after,ensure_ascii=False,allow_nan=False))
  def reference(frame):
   for path in (self.root/'main_facts').glob('*.json'):
    raw=path.read_bytes()
    if json.loads(raw)==frame:return dict(ref=str(path.relative_to(self.root)),sha256=hashlib.sha256(raw).hexdigest(),observed_at=frame['observed_at'])
   raise AssertionError('owned synthetic frame not found')
  bref=reference(before);aref=reference(after);current=[before];calls=[]
  page=Mock();page.url=self.url;page._kac_document_url=self.url;page._kac_response_status=200
  page.locator.return_value.count.return_value=1;page.locator.return_value.first.inner_text.side_effect=lambda **kw:current[0]['main_title']
  page.inner_text.side_effect=lambda *a,**kw:current[0]['owner_text']
  @contextmanager
  def delivered(url):yield page
  browser=Mock();browser.page=delivered
  def wait(*a,**kw):
   frame=before if not calls else after;calls.append('wait');return copy.deepcopy(frame),copy.deepcopy(bref if frame is before else aref)
  def photo(*a,**kw):
   current[0]=after;calls.append('photo')
   target=Path(a[1]);target.write_bytes(Path(oldfacts['screenshot_context']).read_bytes())
   context=copy.deepcopy(oldfacts['context_capture_evidence']);context['filename']=target.name
   return context,''
  def observation(*a,**kw):return dict(observation_status='observed'),{}
  store=CatalogueStore(self.root/('native-'+mode+'.sqlite3'));store.acquire_unknown(self.identity,['elektro.ru'],owner='offline199')
  def native(*a,**kw):return capture_current(*a,**kw,photos=True,extract=False)
  with patch.object(agent,'_wait_main_card_facts',side_effect=wait),patch.object(agent,'_render_observation',side_effect=observation),patch.object(review_context,'capture_context',side_effect=photo),patch.object(llm,'complete_json',side_effect=AssertionError('No model')):
   value=capture_and_publish(store,self.identity,self.position,self.url,browser=browser,image_store=CatalogueImageStore(self.root,client=None,bucket=''),capture=native)
  saved=CatalogueStore(self.root/('native-'+mode+'.sqlite3')).resolve_observation(self.identity.key,value.observation_id)['observation'] if value else None
  return saved,calls

 def test_native_neutral_changed_price_publishes_new_financial_without_old_photo(self):
  saved,calls=self.native_generation_capture('price')
  self.assertIsNotNone(saved);self.assertEqual(saved['raw_amount'],6596);self.assertEqual(_facts(saved,self.position)[0]['price'],6596)
  self.assertEqual(saved['image']['status'],'missing');self.assertEqual(calls.count('photo'),1);self.assertEqual(calls.count('wait'),2)
  self.assertNotIn('photo_binding',saved['unit_equivalence']['supplier_source']);self.assertFalse(saved.get('acquisition_completion'))

 def test_native_neutral_invalid_after_cannot_copy_financial_or_photo(self):
  for mode in ['count','assembly','identity','invalid_source','foreign_url','truncated','unowned']:
   with self.subTest(mode=mode):
    saved,calls=self.native_generation_capture(mode)
    if saved is not None:
     self.assertIsNone(_facts(saved,self.position)[0]);self.assertEqual(saved['image']['status'],'missing')
    self.assertEqual(calls.count('photo'),1)

 def test_native_neutral_unchanged_keeps_independently_owned_photo(self):
  saved,calls=self.native_generation_capture('unchanged')
  self.assertIsNotNone(saved);self.assertEqual(_facts(saved,self.position)[0]['price'],6595);self.assertEqual(saved['image']['status'],'captured');self.assertEqual(calls.count('photo'),1)
  self.assertEqual(agent._MAIN_FACTS_READINESS_JS,(BASE/'EPIC-0001-TASK-0197/serializer.js').read_text())

 def test_native_neutral_legacy_unsupported_stays_review_and_typed_after_corruption_refuses(self):
  saved,calls=self.native_generation_capture('legacy_unsupported')
  self.assertIsNotNone(saved);self.assertIsNone(_facts(saved,self.position)[0]);self.assertIsNone(saved.get('unit_equivalence'));self.assertEqual(calls.count('photo'),1)
  saved,calls=self.native_generation_capture('invalid_source')
  self.assertIsNone(saved);self.assertEqual(calls.count('photo'),1)

 def financial_no_photo_fixture(self):
  from contextlib import contextmanager
  from engine.search.card_attachments import collect_current
  from engine import source_quote,review_context
  owned=http_card.main_product(self.body.decode(self.seller['encoding']),self.url,check_controls=False);self.assertIsNotNone(owned)
  fields=dict(kind='complete',missing_fields=[],document_url=self.url,document_title=owned['title'],main_title=owned['title'],article=owned['article'],owner_text=owned['text'],purchase_text=owned['text'],owner_bound=True,owner_truncated=False,body=owned['text'],body_truncated=False,observed_at=time.time(),control_captions=[],rendered_dom=self.body.decode(self.seller['encoding']),rendered_capture_status='captured',rendered_capture_budget={'nodes':100,'bytes':len(self.body)})
  calls=[];packets=[];capture_flags=[]
  class Channel:
   def send(this,method,params):
    arguments=json.loads(params['expression'].rsplit(')(',1)[1][:-1]);capture_flags.append(arguments['capture_dom']);calls.append('evaluate')
    value=copy.deepcopy(fields)
    if not arguments['capture_dom']:value.update(rendered_dom='',rendered_capture_status='not_requested',rendered_capture_budget={'nodes':0,'bytes':0})
    return {'result':{'value':value}}
   def detach(this):pass
  page=Mock();page.url=self.url;page._kac_document_url=self.url;page._kac_response_status=200
  page.context.new_cdp_session.side_effect=lambda _:Channel()
  page.locator.return_value.count.return_value=1;page.locator.return_value.first.inner_text.return_value=owned['title'];page.inner_text.return_value=owned['text']
  @contextmanager
  def delivered(url):calls.append('page');yield page
  browser=Mock();browser.page=delivered
  from engine.search.product_identity import rendered_identity
  def identity(*a):return rendered_identity(self.url,self.url,owned['title'],owned['article'],self.position.type_mark,self.position.name,'elektro.ru',observed_at=fields['observed_at'])
  from engine.browser import SupplierHttpRepresentation
  store=CatalogueStore(self.root/'no-photo.sqlite3');store.acquire_unknown(self.identity,['elektro.ru'],owner='offline202')
  snapshots=[]
  def native(*a,**kw):
   original_sink=kw.pop('neutral_sink',None)
   def sink(record):
    packets.append(record)
    if original_sink:original_sink(record)
   value=collect_current(*a,**kw,extract=False,neutral_sink=sink);snapshots.append(value[0]);return value
  with patch.object(agent,'supplier_http_get',side_effect=SupplierHttpRepresentation('offline render required')) as wire,patch.object(agent,'_render_observation',return_value=({'observation_status':'observed'},{})),patch.object(source_quote,'observe_identity',side_effect=identity),patch.object(review_context,'capture_context',side_effect=AssertionError('No photograph')),patch.object(llm,'complete_json',side_effect=AssertionError('No model')):
   result=capture_and_publish(store,self.identity,self.position,self.url,browser=browser,image_store=CatalogueImageStore(self.root,client=None,bucket=''),capture=native)
  self.assertEqual(wire.call_count,1);self.assertEqual(calls,['page','evaluate']);self.assertEqual(capture_flags,[True]);self.assertEqual(len(packets),1)
  self.assertIsNotNone(result);saved=CatalogueStore(self.root/'no-photo.sqlite3').resolve_observation(self.identity.key,result.observation_id)['observation']
  self.assertEqual(_facts(saved,self.position)[0]['price'],6595);self.assertEqual(saved['image']['status'],'missing');self.assertFalse(list(self.root.glob('prod_*.png')))
  self.assertIsNotNone(saved['unit_equivalence']);self.assertTrue(snapshots[0].facts()['rendered_source'])
  return packets[0],snapshots[0],saved

 def test_photos_false_native_capture_retains_financial_dom_without_photo(self):
  packet,snapshot,saved=self.financial_no_photo_fixture()
  result=project_product(CatalogueStore(self.root/'no-photo.sqlite3'),self.identity,self.position)
  with catalogue_scope(CatalogueStore(self.root/'no-photo.sqlite3')):
   chosen=final_offers(result);self.assertEqual(len(chosen),1);self.assertFalse(proof_ready(chosen[0],self.position))
  self.assertEqual(agent._MAIN_FACTS_READINESS_JS,(BASE/'EPIC-0001-TASK-0197/serializer.js').read_text())

 def replay_raw(self,packet,*,mark=None,name=None):
  from engine.search.document_snapshot import RawDocument
  data={k:copy.deepcopy(packet[k]) for k in ('coherent','source','status','identity_observation')}
  raw=RawDocument.create('rendered',self.url,self.url,'elektro.ru','0'*64,'rendered_owner_v1:synthetic202',data,data['coherent']['observed_at'])
  return agent._snapshot_from_raw_rendered(raw,self.url,'elektro.ru',mark or self.position.type_mark,name or self.position.name,self.position.unit,self.position.qty,'',set(),time.monotonic()+30)

 def test_admitted_raw_rendered_replay_revalidates_typed_source_and_sqlite(self):
  packet,original,_=self.financial_no_photo_fixture();snapshot=self.replay_raw(packet)
  self.assertIsNotNone(snapshot);self.assertTrue(snapshot.facts()['rendered_source']);self.assertEqual(snapshot.facts()['captured_at'],original.facts()['captured_at'])
  from engine.search.card_attachments import capture_current
  store=CatalogueStore(self.root/'replay.sqlite3');store.acquire_unknown(self.identity,['elektro.ru'],owner='offline202')
  def native(*a,**kw):return capture_current(*a,**kw,photos=False,extract=False)
  with patch.object(agent,'_capture_card_live',return_value=snapshot) as reused,patch.object(agent,'supplier_http_get',side_effect=AssertionError('No new HTTP')),patch.object(llm,'complete_json',side_effect=AssertionError('No model')):
   result=capture_and_publish(store,self.identity,self.position,self.url,browser=Mock(),image_store=CatalogueImageStore(self.root,client=None,bucket=''),capture=native)
  self.assertEqual(reused.call_count,1);self.assertIsNotNone(result)
  saved=CatalogueStore(self.root/'replay.sqlite3').resolve_observation(self.identity.key,result.observation_id)['observation']
  self.assertEqual(_facts(saved,self.position)[0]['price'],6595);self.assertEqual(saved['image']['status'],'missing')

 def test_raw_rendered_source_tamper_position_and_curation_refuse(self):
  packet,_,_=self.financial_no_photo_fixture()
  for field,value in [('rendered_source',{**packet['coherent']['rendered_source'],'record_sha256':'0'*64}),('owner_truncated',True),('owner_bound',False),('observed_at',packet['coherent']['observed_at']+1),('owner_text','other owner')]:
   changed=copy.deepcopy(packet);changed['coherent'][field]=value
   self.assertIsNone(self.replay_raw(changed),field)
  wrong=self.replay_raw(packet,mark='36785KZL/S');self.assertTrue(wrong is None or not agent._deterministic_card_data(wrong) or agent._deterministic_card_data(wrong).get('price_with_vat') is None)
  source=self.replay_raw(packet).facts()['rendered_source']
  path=self.root/source['ref'];original=path.read_bytes();path.write_bytes(original+b' ')
  snapshot=self.replay_raw(packet);self.assertTrue(snapshot is None or not snapshot.facts().get('rendered_source'));path.write_bytes(original)
  factory.approve_origin('DKC','https://www.dkc.ru','task186-root-dkc-origin-20261019-r1',2,False)
  snapshot=self.replay_raw(packet)
  self.assertTrue(snapshot is None or agent._deterministic_card_data(snapshot) is None or agent._deterministic_card_data(snapshot).get('price_with_vat') is None)

 def test_generic_cached_document_keeps_typed_source_without_new_delivery(self):
  from engine.models import SpecPosition
  from engine.search.document_snapshot import RawDocument,DocumentCache
  from engine.search.session import research_session
  data=BASE/'EPIC-0001-TASK-0186/generic-reference-v3';inputs=json.loads((data/'source-input.json').read_text());review=json.loads((BASE/'EPIC-0001-TASK-0186/reference-review/generic-receipt-v3.json').read_text())
  with patch.object(time,'time',return_value=max(self.review['provenance']['reviewed_at'],review['provenance']['reviewed_at'])+1):
   factory.approve_origin(**inputs['authority']);reference=factory.import_reference((data/'factory.html').read_bytes(),**inputs['source']);factory.activate_reference(reference,review,(BASE/'EPIC-0001-TASK-0186/reference-review/generic-technical-review-evidence-v3.json').read_bytes())
   self.position=SpecPosition.model_validate(json.loads(json.dumps(self.position.model_dump(),ensure_ascii=False).replace('36785KZL','56785KZL').replace('DKC','ACME')))
   self.body=(data/'supplier.html').read_bytes();self.url='https://supplier.example/products/56785KZL';self.seller={**self.seller,'encoding':'utf-8'}
   snapshot,frame,_=self.dom_snapshot(image=False);facts=snapshot.facts();profile='rendered_owner_v1:owned202';route='0'*64;host='supplier.example'
   packet=dict(coherent=frame,source=facts['main_facts'],status=200,identity_observation=dict(main_title=frame['main_title'],main_article=frame['article'],canonical_url=self.url,observed_at=frame['observed_at']))
   document=RawDocument.create('rendered',self.url,self.url,host,route,profile,packet,frame['observed_at'])
   cache=DocumentCache(clock=time.time);self.assertTrue(cache.publish(document))
   with research_session(document_cache=cache),patch.object(agent,'_live_render_profile',return_value=profile),patch('engine.browser.supplier_http_configuration_hash',return_value=route),patch.object(agent,'_capture_card_live',side_effect=AssertionError('No new delivery')) as live:
    replay=agent._capture_card(Mock(),self.url,self.url,host,self.position.type_mark,self.position.name,self.position.unit,self.position.qty,'',set(),time.monotonic()+30)
   self.assertEqual(live.call_count,0);self.assertIsNotNone(replay);self.assertTrue(replay.facts()['rendered_source']);self.assertEqual(replay.facts()['captured_at'],frame['observed_at'])
   with factory.request_scope(self.position):self.assertEqual(agent._deterministic_card_data(replay)['price_with_vat'],120)
   # A fresh presentation/cohort cannot reuse another context's rendered data.
   with research_session(document_cache=cache),patch.object(agent,'_live_render_profile',return_value=profile+'other'),patch('engine.browser.supplier_http_configuration_hash',return_value=route),patch.object(agent,'_capture_card_live',side_effect=AssertionError('new offline delivery required')) as live:
    with self.assertRaisesRegex(AssertionError,'new offline delivery'):agent._capture_card(Mock(),self.url,self.url,host,self.position.type_mark,self.position.name,self.position.unit,self.position.qty,'',set(),time.monotonic()+30)
   self.assertEqual(live.call_count,1)

 def test_retained_dom_unknown_pack_and_expired_curation_never_gain_kit_price(self):
  packet,_,_=self.financial_no_photo_fixture();snapshot=self.replay_raw(packet);source=snapshot.facts()['rendered_source']
  with patch.object(time,'time',return_value=1792496218.653383):
   with factory.request_scope(self.position),self.assertRaises(factory.FactoryEvidenceError):factory.derive_singleton(source,self.position)
  for mode in ('missing','multiple'):
   body=self.body.decode(self.seller['encoding'])
   if mode=='missing':body=body.replace('data-pack-size="1"','').replace('data-count-per-pack="1"','')
   else:body=body.replace('data-pack-size="1"','data-pack-size="2"')
   owned=http_card.main_product(body,self.url,check_controls=False)
   if owned is None:continue # Unsupported owned layout cannot be promoted.
   current,_,_=self.dom_snapshot(body.encode(self.seller['encoding']),image=False)
   with factory.request_scope(self.position),self.assertRaises(factory.FactoryEvidenceError):factory.derive_singleton(current.facts()['rendered_source'],self.position)
