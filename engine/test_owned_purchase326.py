"""Retained native DOM purchase authority, with no network/model acquisition."""
import base64,copy,hashlib,json,re,shutil,tempfile,time,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from engine.models import SpecPosition
from engine.search import http_card as dom,owned_purchase_row as rows
from engine.search.agent import _deterministic_card_data,_finish_captured_card
from engine.search.card_batch import CardSnapshot
from engine.search.catalogue import CatalogueStore,identity_for_position
from engine.search.catalogue_images import CatalogueImageStore
from engine.search.catalogue_producer import publish_snapshot
from engine.search.catalogue_projection import project_product,catalogue_scope,catalogue_review_reason
from engine.search.product_identity import request_alternatives_scope
from engine.source_quote import quote_review_reason
from engine.offer_selection import offer_is_comparable
from engine import config

FIXTURE=Path(__file__).resolve().parents[2]/'sources/kac-generator/atomic-simplification-2026-10-14/EPIC-0001-TASK-0326/developer'

@unittest.skipUnless((FIXTURE/'fixture-pins.json').is_file(),'external pinned TASK326 retained frames are not distributed with source')
class OwnedPurchase326Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
  shutil.copytree(FIXTURE/'fixture-cache',self.root/'cache');self.cache=self.root/'cache'
  self.old_cache=config.CACHE_DIR;config.CACHE_DIR=self.cache
  self.patch=patch.object(dom,'CACHE_DIR',self.cache);self.patch.start()
  self.position=SpecPosition.model_validate(json.loads((FIXTURE/'position.json').read_text()))
 def tearDown(self):
  self.patch.stop();config.CACHE_DIR=self.old_cache;self.temp.cleanup()
 def original(self,prefix):
  path=next((self.cache/'captured_inputs').glob(prefix+'*.json'));raw=path.read_bytes();record=json.loads(raw)
  return record,dict(ref=str(path.relative_to(self.cache)),record_sha256=hashlib.sha256(raw).hexdigest(),snapshot_sha256=record['sha256'],card_id=record['id'])
 def snapshot(self,prefix):
  record,ref=self.original(prefix)
  return CardSnapshot(record['id'],record['payload'],record['sha256'],record.get('deadline'))if prefix=='2888'else rows.replay_snapshot(ref)
 def changed(self,prefix,change):
  """Synthetic DOM mutation with coherent native frame/hash, never old evidence."""
  record,ref=self.original(prefix);facts=json.loads(record['payload']);frame=dom._read_render_frame(facts['main_facts'])
  source=frame['rendered_source'];body=json.loads((self.cache/source['ref']).read_text());html=base64.b64decode(body['body_base64']).decode()
  changed=change(html);self.assertNotEqual(changed,html,'mutation must affect retained DOM');html=changed;owned=dom._neutral_owner(html,facts['url'])
  if not owned:return None
  frame=copy.deepcopy(frame);frame.update(main_title=owned['title'],article=owned['article'],owner_text=owned['text'])
  source=dom.save_rendered_dom(html,frame,facts['url']);frame['rendered_source']=source
  raw=json.dumps(frame,ensure_ascii=False,sort_keys=True).encode();path=self.cache/'main_facts'/f'{uuid.uuid4().hex}.json';path.write_bytes(raw)
  main=dict(ref=str(path.relative_to(self.cache)),sha256=hashlib.sha256(raw).hexdigest(),observed_at=frame['observed_at'])
  facts.update(main_facts=main,text=owned['text'],main_title=owned['title'],page_article=owned['article']);facts.pop('rendered_source',None)
  facts['identity_evidence']={**facts['identity_evidence'],'main_title':owned['title'],'main_article':owned['article']};facts['identity_evidence'].pop('rendered_source',None)
  snapshot=CardSnapshot(uuid.uuid4().hex,json.dumps(facts,ensure_ascii=False,sort_keys=True),'',record.get('deadline'));snapshot=CardSnapshot(snapshot.card_id,snapshot.payload,hashlib.sha256(snapshot.payload.encode()).hexdigest(),snapshot.deadline)
  ref=snapshot.retain(self.cache);ref={k:ref[k]for k in ('ref','record_sha256','snapshot_sha256','card_id')}
  return ref
 def assert_replay_denied(self,prefix,change):
  ref=self.changed(prefix,change)
  if ref is not None:self.assertRaises(ValueError,rows.replay_snapshot,ref)
 def test_three_actual_native_finish_publish_reopen_financial(self):
  before={str(p.relative_to(self.cache)):hashlib.sha256(p.read_bytes()).hexdigest()for p in self.cache.rglob('*')if p.is_file()}
  store=CatalogueStore(self.root/'catalogue.sqlite3');identity=identity_for_position(self.position)
  store.acquire_unknown(identity,['chipdip.ru','favorit-el.ru','electro-mpo.ru'],owner='offline326')
  with request_alternatives_scope(self.position),patch('engine.search.whitelist.capture_seller_reference',side_effect=AssertionError('no directory refreeze')):
   for prefix,expected in [('1d1f',3160/3),('bd1e',1124.61),('2888',1236.47)]:
    with self.subTest(prefix=prefix):
     snapshot=self.snapshot(prefix);data=_deterministic_card_data(snapshot);self.assertIsNotNone(data)
     offer=_finish_captured_card(None,snapshot,data);self.assertIsNotNone(offer);self.assertAlmostEqual(offer.price_with_vat,expected);self.assertEqual(quote_review_reason(offer,self.position),'')
     observed=publish_snapshot(store,identity,self.position,snapshot,image_store=CatalogueImageStore(self.cache),offer=offer)
     self.assertEqual(observed.state,'observed');self.assertEqual(observed.raw_amount,data['listed_price_with_vat'])
   reopened=CatalogueStore(self.root/'catalogue.sqlite3');result=project_product(reopened,identity,self.position)
   self.assertEqual(len(result.all_offers),3)
   with catalogue_scope(reopened):
    for offer in result.all_offers:self.assertEqual(catalogue_review_reason(offer,self.position),'');self.assertTrue(offer_is_comparable(offer,self.position))
  self.assertEqual(before,{str(p.relative_to(self.cache)):hashlib.sha256(p.read_bytes()).hexdigest()for p in self.cache.rglob('*')if str(p.relative_to(self.cache))in before})
 def test_actual_photo_closure_separate_from_financial(self):
  for prefix,expected in [('1d1f',False),('bd1e',False),('2888',True)]:
   f=self.snapshot(prefix).facts();source=f.get('rendered_source');context=f['context_capture_evidence'];photo=self.cache/Path(f['screenshot_context']).name
   with self.subTest(prefix=prefix):self.assertEqual(dom.rendered_photo_valid(source,context,str(photo)),expected)
 def test_exact_denominations_not_rounded_equality(self):
  self.assert_replay_denied('bd1e',lambda s:s.replace('3 373.83','3 373.82').replace('3373.83','3373.82'))
 def test_conflicting_order_quantity(self):
  self.assert_replay_denied('bd1e',lambda s:s.replace('data-quantity="3"','data-quantity="6"'))
 def test_conflicting_same_role_manufacturer(self):
  self.assert_replay_denied('1d1f',lambda s:s.replace('itemprop="model">3526210</span>','itemprop="model">3526210</span><span itemprop="model">9999999</span>'))
 def test_single_wrong_article_never_financially_admitted(self):
  ref=self.changed('1d1f',lambda s:s.replace('itemprop="model">3526210</span>','itemprop="model">9999999</span>'))
  if ref is None:return # structural ownership already rejected the mutation
  snapshot=rows.replay_snapshot(ref)
  with request_alternatives_scope(self.position):
   data=_deterministic_card_data(snapshot);offer=_finish_captured_card(None,snapshot,data)
   if offer is not None:self.assertFalse(offer_is_comparable(offer,self.position));self.assertTrue(quote_review_reason(offer,self.position))
 def test_competing_independent_purchase(self):
  self.assert_replay_denied('1d1f',lambda s:s.replace('</h1>','</h1><div itemtype="https://schema.org/Offer"><span>100 руб. / м</span><button>Купить</button></div>'))
 def test_current_conditional_cannot_hide_under_installment_role(self):
  self.assert_replay_denied('1d1f',lambda s:s.replace('Плати частями','Клубная цена при заказе. Плати частями'))
 def test_primary_conditional_not_erased(self):
  self.assert_replay_denied('bd1e',lambda s:re.sub(r'(<div[^>]*data-product-id="[^"]+"[^>]*>)',r'\1<p>Клубная цена по карте</p>',s,count=1))
 def test_present_malformed_witness_no_fallback(self):
  snapshot=self.snapshot('bd1e');facts=snapshot.facts()
  for proof in [None,{},[],{**facts['purchase_row_v2'],'raw_amount':1}]:
   with self.subTest(proof=proof):
    altered={**facts,'purchase_row_v2':proof};payload=json.dumps(altered,ensure_ascii=False,sort_keys=True);bad=CardSnapshot(snapshot.card_id,payload,hashlib.sha256(payload.encode()).hexdigest(),snapshot.deadline)
    self.assertRaises(ValueError,_deterministic_card_data,bad)
 def test_bool_is_not_numeric_witness_or_raw_fact(self):
  snapshot=self.snapshot('bd1e');facts=snapshot.facts()
  for key in ('purchase_quantity','price_denominator','selected_quote','version','raw_amount'):
   altered=copy.deepcopy(facts);altered['purchase_row_v2'][key]=True if key!='selected_quote'else False
   payload=json.dumps(altered,ensure_ascii=False,sort_keys=True);bad=CardSnapshot(snapshot.card_id,payload,hashlib.sha256(payload.encode()).hexdigest(),snapshot.deadline)
   with self.subTest(key=key):self.assertRaises(ValueError,_deterministic_card_data,bad)
  altered=copy.deepcopy(facts);altered['main_facts']['observed_at']=True
  payload=json.dumps(altered,ensure_ascii=False,sort_keys=True);self.assertRaises(ValueError,_deterministic_card_data,CardSnapshot(snapshot.card_id,payload,hashlib.sha256(payload.encode()).hexdigest(),snapshot.deadline))
 def test_retained_raw_context_and_deadline_never_rewritten(self):
  snapshot=self.snapshot('bd1e');facts=snapshot.facts()
  for key,value in [('captured_at',facts['captured_at']+1),('seller_reference_snapshot',None),('text',facts['text']+' altered'),('main_title','Other')]:
   altered={**facts,key:value};payload=json.dumps(altered,ensure_ascii=False,sort_keys=True);bad=CardSnapshot(snapshot.card_id,payload,hashlib.sha256(payload.encode()).hexdigest(),snapshot.deadline)
   with self.subTest(key=key):self.assertRaises(ValueError,_deterministic_card_data,bad)
  self.assertRaises(ValueError,_deterministic_card_data,CardSnapshot(snapshot.card_id,snapshot.payload,snapshot.sha256,123))
 def test_per_use_body_hash_still_required(self):
  snapshot=self.snapshot('bd1e');facts=snapshot.facts();p=self.cache/facts['rendered_source']['ref'];p.write_bytes(p.read_bytes()+b' ')
  self.assertRaises(ValueError,_deterministic_card_data,snapshot)
 def test_per_use_frame_epoch_still_required(self):
  snapshot=self.snapshot('bd1e');facts=snapshot.facts();p=self.cache/facts['rendered_source']['main_facts']['ref'];raw=json.loads(p.read_bytes());raw['observed_at']+=1;p.write_text(json.dumps(raw))
  self.assertRaises(ValueError,_deterministic_card_data,snapshot)


 def structured_snapshot(self,product):
  ref=self.changed('1d1f',lambda html:html+'<script type="application/ld+json">'+json.dumps(product)+'</script>')
  self.assertIsNotNone(ref)
  return rows.replay_snapshot(ref)
 def actual_product(self,**changes):
  original,_=self.original('1d1f');facts=json.loads(original['payload'])
  return dict({'@type':'Product','url':facts['url'],'mpn':'3526210','offers':{'@type':'Offer','price':3160,'priceCurrency':'RUB'}},**changes)
 def assert_native_product_rejected(self,product):
  try:snapshot=self.structured_snapshot(product)
  except ValueError:return # rejected before deterministic financial admission
  with request_alternatives_scope(self.position):
   data=_deterministic_card_data(snapshot);offer=_finish_captured_card(None,snapshot,data)if data else None
   self.assertTrue(offer is None or not offer_is_comparable(offer,self.position))
 def test_primary_structured_mpn_conflict_native(self):
  self.assert_native_product_rejected(self.actual_product(mpn='WRONG999'))
 def test_primary_structured_minimum_native(self):
  product=self.actual_product();product['offers']['eligibleQuantity']={'@type':'QuantitativeValue','minValue':10,'unitCode':'H87'}
  self.assert_native_product_rejected(product)
 def test_primary_structured_exact_roles_aliases_and_geography_native(self):
  product=self.actual_product(model='3526210',sku='8076007569',brand={'@type':'Brand','name':'DKC'},manufacturer={'@type':'Organization','name':'ДКС'})
  product['offers'].update(eligibleRegion='Москва',shippingDetails={'@type':'OfferShippingDetails','shippingDestination':{'@type':'DefinedRegion','addressRegion':'Москва'}})
  snapshot=self.structured_snapshot(product)
  with request_alternatives_scope(self.position):
   offer=_finish_captured_card(None,snapshot,_deterministic_card_data(snapshot));self.assertIsNotNone(offer)
   self.assertTrue(offer_is_comparable(offer,self.position));self.assertEqual(quote_review_reason(offer,self.position),'')
 def test_foreign_structured_identity_and_restrictions_do_not_poison_native(self):
  product=self.actual_product(mpn='WRONG999',url='https://foreign.example/product/other');product['offers'].update(eligibleQuantity={'minValue':10,'unitCode':'H87'},priceCurrency='USD')
  snapshot=self.structured_snapshot(product)
  with request_alternatives_scope(self.position):
   offer=_finish_captured_card(None,snapshot,_deterministic_card_data(snapshot));self.assertIsNotNone(offer)
   self.assertTrue(offer_is_comparable(offer,self.position));self.assertEqual(quote_review_reason(offer,self.position),'')

# These cases ship without captured merchant data or workspace artifact paths.
class PurchaseContractPortableTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.cache=Path(self.temp.name);self.patch=patch.object(dom,'CACHE_DIR',self.cache);self.patch.start()
 def tearDown(self):self.patch.stop();self.temp.cleanup()
 def html(self,quotes='<p><span>100 руб.</span> / м</p>',extra='',quantity=1,metadata=100):
  return ('<html><body><main itemscope itemtype="https://schema.org/Product">'
    '<h1>Tray ABC123</h1><div>Артикул:<br><span itemprop="mpn">ABC123</span></div>'
    '<span itemprop="sku">SELLER789</span>'+quotes+extra+
    f'<input type="number" name="quantity" value="{quantity}" data-price="{metadata}"><button>Купить</button>'
    '</main></body></html>')
 def proof(self,html,url='https://merchant.example/product/tray'):
  owned=dom._neutral_owner(html,url)
  if not owned:return None
  stamp=time.time()-1;frame=dict(version=1,role='main_card_facts',document_url=url,main_title=owned['title'],article=owned['article'],owner_text=owned['text'],purchase_text='',owner_bound=True,owner_truncated=False,observed_at=stamp)
  source=dom.save_rendered_dom(html,frame,url);frame['rendered_source']=source
  folder=self.cache/'main_facts';folder.mkdir(exist_ok=True);path=folder/(uuid.uuid4().hex+'.json');raw=json.dumps(frame,sort_keys=True).encode();path.write_bytes(raw)
  source={**source,'main_facts':dict(ref=str(path.relative_to(self.cache)),sha256=hashlib.sha256(raw).hexdigest(),observed_at=stamp)}
  self.last_source=source;self.last_owned=owned
  return rows.derive(source,url=url,observed_at=stamp,title=owned['title'],article=owned['article'],text=owned['text'])
 def test_portable_domains_same_semantic_quote(self):
  for host in ('merchant.example','different.test'):
   with self.subTest(host=host):
    proof=self.proof(self.html(),f'https://{host}/product/tray');self.assertIsNotNone(proof);self.assertEqual(proof['version'],2)
    decision,reason=rows.decision(proof,dict(unit='м'));self.assertEqual(reason,'');self.assertEqual(decision['price'],100)
 def test_sole_explicit_lot_native_conversion(self):
  proof=self.proof(self.html('<p><span>300 руб.</span> / 3 м</p>',quantity=3,metadata=300));self.assertIsNotNone(proof)
  decision,reason=rows.decision(proof,dict(unit='м'));self.assertEqual(reason,'');self.assertEqual(decision['price'],100);self.assertEqual(decision['quantity'],3)
 def test_exact_equivalent_denominations(self):
  proof=self.proof(self.html('<p><span>100 руб.</span> / м</p><p><span>300 руб.</span> / 3 м</p>',quantity=3));self.assertIsNotNone(proof);self.assertEqual(len(proof['quotations']),2);self.assertEqual(proof['raw_amount'],100)
 def test_conflicting_denominations_and_unknown_units_deny(self):
  for quote in ('299.99 руб. / 3 м','300 руб. / 3 шт','300 руб. / 3 кг','100 руб. / 2 м'):
   with self.subTest(quote=quote):self.assertIsNone(self.proof(self.html('<p><span>100 руб.</span> / м</p><p>'+quote+'</p>')))
 def test_subtotal_cannot_supply_missing_cash(self):
  self.assertIsNone(self.proof(self.html('<p>1 м на сумму 100 руб.</p>')))
 def test_stock_minimum_or_installment_cannot_supply_denominator(self):
  for label in ('В наличии 3 м','Минимальный заказ 3 м','Плати частями 25 руб. x 4'):
   with self.subTest(label=label):self.assertIsNone(self.proof(self.html('<p>100 руб.</p><p>'+label+'</p>')))
 def test_old_price_not_selected_and_primary_conditions_retained(self):
  proof=self.proof(self.html(extra='<del>120 руб.</del><p>Без НДС</p>'));self.assertIsNotNone(proof);self.assertIn('Без НДС',proof['terms_text']);self.assertTrue(any(v['role']=='historical'for v in proof['monetary_roles']))
  from engine.search.catalogue_producer import _interpret
  self.assertEqual(_interpret(proof['price_text']+'\n'+proof['terms_text'],100)[2],'excluded')
  for label in ('Клубная цена по карте','Цена при заказе от 10 м','Оптовая цена','Плати частями: клубная цена 25 руб.'):
   with self.subTest(label=label):self.assertIsNone(self.proof(self.html(extra='<p>'+label+'</p>')))
 def test_independent_offer_wrong_owner_currency_or_metadata_deny(self):
  extras=['<div itemtype="https://schema.org/Offer">100 руб. / м<button>Купить</button></div>',
   '<div itemtype="https://schema.org/Product"><h2>Other product</h2>100 руб. / м</div>',
   '<meta itemprop="priceCurrency" content="USD">','<meta itemprop="price" content="99">']
  for extra in extras:
   with self.subTest(extra=extra):self.assertIsNone(self.proof(self.html(extra=extra)))
 def test_hidden_or_outside_owner_price_is_not_current(self):
  proof=self.proof(self.html(extra='<p hidden>99 руб. / м</p>')+'<p>50 руб. / м</p>');self.assertIsNotNone(proof);self.assertEqual(proof['raw_amount'],100)
 def test_same_role_code_conflict_denied_sku_distinct_allowed(self):
  self.assertIsNotNone(self.proof(self.html()))
  self.assertIsNone(self.proof(self.html(extra='<span itemprop="mpn">OTHER99</span>')))
 def test_structured_range_and_currency_conflicts(self):
  for offers in [dict(**{'@type':'AggregateOffer'},lowPrice=99,highPrice=100),dict(**{'@type':'Offer'},price=100,priceCurrency='USD')]:
   extra='<script type="application/ld+json">'+json.dumps(dict(**{'@type':'Product'},url='https://merchant.example/product/tray',offers=offers))+'</script>'
   with self.subTest(offers=offers):self.assertIsNone(self.proof(self.html()+extra))

 def test_unrelated_structured_product_is_not_primary_offer(self):
  foreign=dict(**{'@type':'Product'},url='https://other.example/product/foreign',sku='OTHER99',offers=dict(**{'@type':'Offer'},price=999,priceCurrency='USD'))
  proof=self.proof(self.html()+'<script type="application/ld+json">'+json.dumps(foreign)+'</script>')
  self.assertIsNotNone(proof);self.assertEqual(proof['raw_amount'],100)
 def test_version_one_proof_and_receipt_are_not_upgraded(self):
  html=('<main itemscope itemtype="https://schema.org/Product"><h1>Tray ABC123</h1>'
    '<div>Артикул:<br><span itemprop="mpn">ABC123</span></div><table>'
    '<tr itemprop="offers" itemscope itemtype="https://schema.org/Offer">'
    '<td>ABC123</td><td><span itemprop="price">100</span> руб. / м</td>'
    '<td><input type="number" name="quantity" value="1">м<button>Купить</button></td></tr></table></main>')
  modern=self.proof(html);self.assertIsNotNone(modern);self.assertEqual(modern['version'],2)
  source=modern['source'];retained=dom.read_source(source,allow_rendered=True);self.assertIsNotNone(retained)
  record,owned=retained
  legacy=rows._derive_v1(source,url=record['url'],observed_at=record['observed_at'],title=owned['title'],article=owned['article'],text=owned['text'])
  self.assertIsNotNone(legacy);self.assertEqual(legacy['version'],1)
  observation=dict(url=record['url'],observed_at=record['observed_at'],title=owned['title'],manufacturer_code=owned['article'],source_text=owned['text'],source_sha256=legacy['source_sha256'],raw_amount=100,currency='RUB',sale_unit='м',lot_quantity=None,package_quantity=None)
  self.assertEqual(rows.verify(legacy,observation),legacy)
  from engine.source_quote import observe_captured_quote
  receipt=observe_captured_quote(owned['text'],actual_url=record['url'],observed_at=record['observed_at'],url=record['url'],title=owned['title'],article=owned['article'],listed=100,normalized=100,sale_unit='м',requested_unit='м',data={},owned_source=source)
  self.assertTrue(receipt);self.assertNotIn('purchase_row_v2',receipt)

 def structured_proof(self,changes=None,offer_changes=None):
  product={'@type':'Product','url':'https://merchant.example/product/tray','mpn':'ABC123','sku':'SELLER789','offers':{'@type':'Offer','price':100,'priceCurrency':'RUB'}}
  product.update(changes or {});product['offers'].update(offer_changes or {})
  return self.proof(self.html()+'<script type="application/ld+json">'+json.dumps(product)+'</script>')
 def test_primary_structured_identity_family(self):
  self.assertIsNotNone(self.structured_proof())
  for fields in [{'mpn':'WRONG999'},{'sku':'ABC123'},{'model':'MODEL8'},{'brand':{'@type':'Brand','name':'OtherMaker'}},{'manufacturer':{'@type':'Organization','name':'OtherMaker'}},{'name':'Other item ABC123'},{'brand':{'@id':'#brand'}},{'isVariantOf':{'@id':'#other-model'}}]:
   with self.subTest(fields=fields):self.assertIsNone(self.structured_proof(fields))
 def test_primary_structured_price_condition_family(self):
  conditions=[{'price':True},{'price':None},{'eligibleQuantity':{'minValue':10,'unitCode':'H87'}},{'eligibleCustomerType':'ClubMember'},{'eligibleTransactionVolume':{'minValue':10000}},
   {'priceSpecification':{'@type':'UnitPriceSpecification','price':100,'priceCurrency':'RUB'}},{'priceSpecification':{'@id':'#tier'}},
   {'businessFunction':'https://schema.org/LeaseOut'},{'validFrom':'2099-01-01'},{'priceValidUntil':'1970-01-01'},
   {'name':'Клубная цена по карте'},{'description':'Окончательная цена уточняется индивидуально'}]
  for fields in conditions:
   with self.subTest(fields=fields):self.assertIsNone(self.structured_proof(offer_changes=fields))
 def test_primary_structured_geography_delivery_and_stock_are_contextual(self):
  proof=self.structured_proof(offer_changes={'eligibleRegion':'Москва','ineligibleRegion':'Other','areaServed':'RU','availability':'https://schema.org/InStock','inventoryLevel':{'value':5},'shippingDetails':{'shippingDestination':{'addressRegion':'Москва'}}})
  self.assertIsNotNone(proof);self.assertTrue(proof['structured_evidence']);self.assertIn('eligibleRegion',proof['structured_evidence'][0]['offer']['contextual_fields'])
 def test_unresolved_primary_offer_cannot_drop_conditions(self):
  product={'@type':'Product','url':'https://merchant.example/product/tray','mpn':'ABC123','offers':{'@id':'#primary-offer'}}
  self.assertIsNone(self.proof(self.html()+'<script type="application/ld+json">'+json.dumps(product)+'</script>'))

 def legacy_table_html(self):
  return ('<main itemscope itemtype="https://schema.org/Product"><h1>Tray ABC123</h1>'
   '<div>Артикул:<br><span itemprop="mpn">ABC123</span></div><table>'
   '<tr itemprop="offers" itemscope itemtype="https://schema.org/Offer">'
   '<td>ABC123</td><td><span itemprop="price">100</span> руб. / м</td>'
   '<td><input type="number" name="quantity" value="1">м<button>Купить</button></td></tr></table></main>')
 def test_legacy_table_primary_constraints_cannot_downgrade(self):
  html=self.legacy_table_html()
  for wrong_mpn in (True,False):
   product={'@type':'Product','url':'/product/tray','name':'Tray ABC123','mpn':'WRONG999'if wrong_mpn else'ABC123','offers':{'@type':'Offer','price':100,'priceCurrency':'RUB'}}
   if not wrong_mpn:product['offers']['eligibleQuantity']={'minValue':10,'unitCode':'H87'}
   with self.subTest(wrong_mpn=wrong_mpn):
    self.assertIsNone(self.proof(html+'<script type="application/ld+json">'+json.dumps({'@graph':[product]})+'</script>'))
    self.assertIsNone(dom.read_source(self.last_source,allow_rendered=True))
    source=self.last_source;owned=self.last_owned;record=dom._read_render_frame(source['main_facts'])
    self.assertIsNone(rows._derive_v1(source,url=record['document_url'],observed_at=record['observed_at'],title=owned['title'],article=owned['article'],text=owned['text']))
    from engine.source_quote import observe_captured_quote
    self.assertEqual(observe_captured_quote(owned['text'],actual_url=record['document_url'],observed_at=record['observed_at'],url=record['document_url'],title=owned['title'],article=owned['article'],listed=100,normalized=100,sale_unit='м',requested_unit='м',data={},owned_source=source),{})
 def test_ordinary_unsupported_v2_layout_keeps_valid_legacy_fallback(self):
  html=self.legacy_table_html().replace('<input type="number" name="quantity" value="1">м<button>Купить</button>','')
  proof=self.proof(html);self.assertIsNotNone(proof);self.assertEqual(proof['version'],1)

 def test_direct_and_graph_primary_offer_conditions_are_not_dropped(self):
  for inside in (False,True):
   offer={'@type':'Offer','price':100,'priceCurrency':'RUB','eligibleQuantity':{'minValue':10,'unitCode':'H87'}}
   if not inside:offer['url']='/product/tray'
   script='<script type="application/ld+json">'+json.dumps({'@graph':[offer]})+'</script>'
   html=self.html().replace('</main>',script+'</main>')if inside else self.html()+script
   with self.subTest(inside=inside):self.assertIsNone(self.proof(html))
  foreign={'@type':'Offer','url':'https://foreign.example/product/other','price':999,'priceCurrency':'USD','eligibleQuantity':{'minValue':10}}
  self.assertIsNotNone(self.proof(self.html()+'<script type="application/ld+json">'+json.dumps({'@graph':[foreign]})+'</script>'))
