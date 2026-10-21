"""Shared complete-product-kit pack evidence; all captures and stores are offline."""
import copy,hashlib,tempfile,unittest,re
from pathlib import Path
from unittest.mock import patch
from engine.source_quote import (strict_unit_decision,complete_kit_pack_context,
 complete_kit_pack_witness,observe_captured_quote,quote_review_reason)
from engine.search.agent import _price_per_requested_unit,_saved_price_unit_is_verified
from engine.search.catalogue_projection import _facts,catalogue_scope,project_product
from engine.search.catalogue import CatalogueStore,identity_for_position
from engine.search.catalogue_producer import publish_snapshot
from engine.search.catalogue_images import CatalogueImageStore
from engine.models import SpecPosition
from engine import test_literal_card_quote as fixture
from engine import test_http_card as http_fixture

class CompleteKitPackTests(unittest.TestCase):
 def context(self,n=2,line=None):
  return '1200 ₽ / упаковку\nЕдиница измерения: упаковка\n'+(line or f'В упаковке: {n} комплекта данного изделия')
 def decide(self,text,n=None):
  return strict_unit_decision(listed=1200,sale_unit='уп',requested_unit='компл',title='Whole product ABC123',unit_text=text,quantity=n,package_quantity=n)
 def test_shared_cross_domain_n1_n2_n3_and_converter_parity(self):
  for title,n,line in [('Electrical bracket ABC123',2,'В одной продаваемой упаковке 2 полных комплекта данного изделия'),('Plumbing repair ABC123',3,'Одна упаковка содержит 3 полных комплекта данного изделия'),('Furniture assembly ABC123',1,'В упаковке: 1 полный комплект данного изделия')]:
   with self.subTest(title=title):
    context=self.context(line=line);decision,reason=self.decide(context,n)
    self.assertEqual(reason,'');self.assertEqual(decision['price'],1200/n);self.assertEqual(decision['basis'],'explicit_complete_kit_pack');self.assertEqual(decision['witness']['quantity'],n)
    data=dict(price_unit='уп',listed_price_with_vat=1200,price_with_vat=1200/n,package_quantity=n,listed_price_covers_multiple_requested_units=True)
    self.assertEqual(_price_per_requested_unit(data,'компл',title,owned_unit_context=context),1200/n)
    self.assertIsNone(_price_per_requested_unit(data,'компл',title))
    for delta in [dict(price_with_vat=1200/n+1),dict(package_quantity=n+1),dict(package_quantity=True)]:
     self.assertIsNone(_price_per_requested_unit(dict(data,**delta),'компл',title,owned_unit_context=context))
 def test_bad_counts_components_conditions_and_transport_not_witnesses(self):
  lines=['В упаковке: '+v+' комплекта данного изделия' for v in ['0','-1','1.5','1–2','1 или 2','True','NaN','Infinity']]
  lines+=['В упаковке: 2 комплекта крепежа данного изделия','В упаковке: 2 полных комплекта крепежа','В упаковке: 2 комплекта данного изделия при заказе','В упаковке: не 2 комплекта','Если заказ: в упаковке 2 комплекта','Транспортная упаковка: 2','Минимальный заказ: 2 упаковки','Количество в упаковке: 4 шт','Состав: 2 пластины и 4 винта']
  for line in lines:
   with self.subTest(line=line):self.assertIsNone(self.decide(self.context(line=line),2)[0])
  for tail in ['\nВ упаковке: 3 комплекта данного изделия','\nкомплекты крепежа','\nкрепежа','\nдля сборки узла','\nнеполный','\nКомплект не включает изделие','\nЕдиница измерения: шт']:
   with self.subTest(tail=tail):self.assertIsNone(self.decide(self.context()+tail,2)[0])
  self.assertIsNone(self.decide('Единица измерения: упаковка\nВ упаковке: 2 комплекта',2)[0])
 def test_selected_occurrence_bounds_no_truncated_or_other_branch_witness(self):
  for text in [self.context()+'\n'+('x'*2100)+'\nВ упаковке: 3 комплекта',self.context()+'\nДругая цена 900 ₽', '1200 ₽ / упаковку\nРозничная цена\n900 ₽\nВ упаковке: 2 комплекта']:
   self.assertEqual(complete_kit_pack_context(text,1200),'')
  for prefix in ['Неполный комплект','Упаковка содержит только комплект крепежа','Комплект крепежа, без изделия']:
   self.assertIsNone(self.decide(complete_kit_pack_context(prefix+'\n'+self.context(),1200),2)[0])
  for count in [True,0,-1,1.5,float('nan'),float('inf'),3]:self.assertIsNone(self.decide(self.context(),count)[0])
  self.assertIsNone(complete_kit_pack_witness(self.context()+'x'*2000))
 def test_catalogue_full_hash_rederivation_and_declared_quantity_conflict(self):
  text='Electrical bracket ABC123\n'+self.context();obs=dict(raw_amount=1200,state='observed',currency='RUB',vat='unknown',title='Electrical bracket ABC123',sale_unit='уп',source_text=text,source_sha256=hashlib.sha256(text.encode()).hexdigest(),purchase_terms='',lot_quantity=None,package_quantity=2,unit_basis=None,manufacturer_code='ABC123')
  before=copy.deepcopy(obs);facts,reason=_facts(obs,dict(unit='компл',name='Electrical bracket',type_mark='ABC123'));self.assertEqual(reason,'');self.assertEqual(facts['price'],600);self.assertEqual(obs,before)
  for changes in [dict(package_quantity=3),dict(package_quantity=True),dict(source_sha256='0'*64),dict(source_text=text.replace('2 комплекта','3 комплекта')),dict(purchase_terms='Цена только при заказе от 10 шт')]:
   with self.subTest(changes=changes):self.assertIsNone(_facts(dict(obs,**changes),dict(unit='компл',name='Electrical bracket',type_mark='ABC123'))[0])
 def approved_reference(self,helper,host):
  # Real approved-reference loader on an owned synthetic fixture, not a seller
  # authority bypass. No seller prose is smuggled into the closed unit proof.
  from engine.search import whitelist
  ref=helper.root/'suppliers.md';ref.write_text('| Supplier | 123 | 7842224734 | https://'+host+' |\n')
  scope=patch.object(whitelist,'SUPPLIERS_FILE',ref);scope.start();self.addCleanup(scope.stop)
 def collect(self,host='electrical.example',name='Electrical bracket',n=2,extra=''):
  helper=fixture.LiteralCardQuoteTests();helper.setUp();self.addCleanup(helper.doCleanups)
  self.approved_reference(helper,host)
  body=http_fixture.html().replace('Cable ABC123',name+' ABC123').replace('Цена за 1 м','Цена за 1 упаковку').replace('</section>',f'<p>Единица измерения: упаковка</p><p>В упаковке: {n} комплекта данного изделия</p>'+extra+'</section>')
  body=re.sub(r'<div itemprop="seller".*?</div>','',body)
  p=SpecPosition(number=1,name=name,type_mark='ABC123',unit='компл',qty=1)
  url='https://'+host+'/item'
  # Replace exactly the native HTTP wire seam; sockets/Client.send remain
  # denied by the enclosing offline boundary. Native parsing/save/producer run.
  import httpx
  from engine.search import agent
  helper.clients=lambda handler:patch.object(agent,'supplier_http_get',side_effect=lambda url,**kw:handler(httpx.Request('GET',url)))
  snapshot,offer,model_calls=helper.collect(body,p,url)
  return helper,p,snapshot,offer,model_calls
 def test_native_http_producer_quote_then_persisted_reopened_catalogue(self):
  for host,name,n in [('electrical.example','Electrical bracket',2),('plumbing.example','Plumbing repair',3),('furniture.example','Furniture assembly',1)]:
   with self.subTest(host=host):
    helper,p,snapshot,offer,model_calls=self.collect(host,name,n)
    self.assertEqual(model_calls,[]);self.assertIsNotNone(offer);self.assertEqual(offer.price_with_vat,120/n)
    self.assertEqual(offer.source_quote_evidence['unit_witness']['quantity'],n)
    self.assertEqual(quote_review_reason(offer,p),'')
    root=helper.root;path=root/'kit.sqlite3';store=CatalogueStore(path);identity=identity_for_position(p);store.acquire_unknown(identity,[host],owner='fixture')
    obs=publish_snapshot(store,identity,p,snapshot,image_store=CatalogueImageStore(root),offer=offer)
    self.assertEqual(obs.raw_amount,120);self.assertEqual(obs.package_quantity,n)
    reopened=CatalogueStore(path);resolved=reopened.resolve_observation(identity.key,obs.observation_id)
    facts,reason=_facts(resolved['observation'],p);self.assertEqual(reason,'');self.assertEqual(facts['price'],120/n)
    projection=project_product(reopened,identity,p)
    self.assertEqual(projection.all_offers[0].price_with_vat,120/n)
    with catalogue_scope(reopened):self.assertEqual(quote_review_reason(projection.all_offers[0],p),'')
    self.assertLess(projection.sources_found,3) # Unit comparison never claims final readiness.
 def test_native_preprice_claims_reject_quote_and_reopened_raw_observation(self):
  from engine.search import agent
  from engine.search.catalogue_producer import publish_facts
  from engine.search.literal_card_quote import current_literal_quote
  import httpx
  for prefix in ['Данный товар не является полным комплектом','Цена за 1 шт','Упаковка содержит только комплект крепежа','Это неполный комплект','Без самого изделия','Без изделия','Только крепления','Unknown owner assertion']:
   with self.subTest(prefix=prefix):
    helper=fixture.LiteralCardQuoteTests();helper.setUp();self.addCleanup(helper.doCleanups)
    self.approved_reference(helper,'independent.example')
    body=http_fixture.html().replace('Cable ABC123','Electrical bracket ABC123').replace('Цена за 1 м','Цена за 1 упаковку').replace('<section itemprop="offers"','<p>'+prefix+'</p><section itemprop="offers"').replace('</section>','<p>Единица измерения: упаковка</p><p>В упаковке: 2 полных комплекта данного изделия</p></section>')
    body=re.sub(r'<div itemprop="seller".*?</div>','',body)
    p=SpecPosition(number=1,name='Electrical bracket',type_mark='ABC123',unit='компл',qty=1);url='https://independent.example/item'
    helper.clients=lambda handler:patch.object(agent,'supplier_http_get',side_effect=lambda url,**kw:handler(httpx.Request('GET',url)))
    snapshot,offer,model_calls=helper.collect(body,p,url,model_quote=dict(price_unit='упаковка',listed_price_with_vat=120,price_with_vat=60,package_quantity=2,listed_price_covers_multiple_requested_units=True))
    self.assertIsNone(current_literal_quote(snapshot.facts()))
    self.assertTrue(model_calls) # Literal producer conservatively leaves fallback unresolved.
    self.assertIsNotNone(offer);self.assertTrue(quote_review_reason(offer,p));self.assertTrue(offer.unit_review_required)
    # Publish the actual retained RAW review offer using the native internal
    # publication stage; no fabricated quote receipt or financial approval.
    path=helper.root/'negative.sqlite3';store=CatalogueStore(path);identity=identity_for_position(p);store.acquire_unknown(identity,['independent.example'],owner='fixture')
    observation=publish_facts(store,identity,p,snapshot.facts(),image_store=CatalogueImageStore(helper.root),offer=offer,verified_owner=True)
    self.assertEqual(observation.raw_amount,120)
    saved=CatalogueStore(path).resolve_observation(identity.key,observation.observation_id)['observation']
    facts,reason=_facts(saved,p);self.assertIsNone(facts);self.assertTrue(reason)
 def test_closed_symmetric_grammar_mixed_orders_and_request_metadata(self):
  from engine.source_quote import complete_kit_identity_metadata
  title='Electrical bracket ABC123';metadata=complete_kit_identity_metadata(title,'ABC123','Electrical bracket','ABC123')
  self.assertEqual(metadata,(title,'ABC123'))
  lines=['1200 ₽','Цена за 1 упаковку','Единица измерения: упаковка','В упаковке: 2 полных комплекта данного изделия',title,'ABC123']
  orders=[lines,lines[::-1],[lines[i] for i in [5,0,3,4,1,2]],[lines[i] for i in [2,1,4,3,0,5]]]
  for order in orders:
   text='\n'.join(order);decision,reason=strict_unit_decision(listed=1200,sale_unit='уп',requested_unit='компл',title=title,unit_text=text,identity_metadata=metadata)
   self.assertEqual(reason,'');self.assertEqual(decision['price'],600)
  for unknown in ['Без самого изделия','Неизвестное утверждение','Только крепления','Комплект крепежа','Цена за 1 шт']:
   for order in orders:
    for index in [0,len(order)//2,len(order)]:
     text='\n'.join(order[:index]+[unknown]+order[index:]);decision,_=strict_unit_decision(listed=1200,sale_unit='уп',requested_unit='компл',title=title,unit_text=text,identity_metadata=metadata)
     self.assertIsNone(decision,(unknown,index))
  for actual in ['Без самого изделия ABC123','Electrical bracket ABC123 без изделия','Unrelated ABC123']:
   self.assertEqual(complete_kit_identity_metadata(actual,'ABC123','Electrical bracket','ABC123'),())
  # Arbitrary owned titles cannot become neutral headers without exact request compatibility.
  text='Без самого изделия ABC123\n'+self.context()
  self.assertIsNone(strict_unit_decision(listed=1200,sale_unit='уп',requested_unit='компл',title='Без самого изделия ABC123',unit_text=text)[0])
 def test_native_receipt_tamper_wrong_identity_and_missing_legacy_witness(self):
  helper,p,snapshot,offer,_=self.collect();self.assertEqual(quote_review_reason(offer,p),'')
  original=offer.model_dump()
  for key,value in [('quantity',3),('unit_witness',{}),('unit_text',self.context(3)),('article','OTHER'),('observed_at',offer.source_quote_evidence['observed_at']-1)]:
   bad=offer.model_copy(deep=True);bad.source_quote_evidence[key]=value;self.assertTrue(quote_review_reason(bad,p),key)
  bad=offer.model_copy(deep=True);bad.identity_evidence['http_source']['body_sha256']='0'*64;self.assertTrue(quote_review_reason(bad,p))
  self.assertTrue(quote_review_reason(offer,dict(name=p.name,type_mark='OTHER',unit='компл')))
  self.assertEqual(offer.model_dump(),original)
  self.assertFalse(_saved_price_unit_is_verified('компл','уп',120,60,'Whole kit ABC123',offer.url,self.context()))
 def test_hidden_related_and_component_source_never_supply_missing_relation(self):
  from engine.search.literal_card_quote import current_literal_quote
  helper,p,snapshot,offer,_=self.collect()
  facts=snapshot.facts();from engine.search import http_card,product_identity
  import httpx,time
  for extra in ['<aside>В упаковке: 2 комплекта данного изделия</aside>','<p hidden>В упаковке: 2 комплекта данного изделия</p>','<p>В упаковке: 2 комплекта крепежа данного изделия</p>']:
   body=http_fixture.html().replace('Цена за 1 м','Цена за 1 упаковку').replace('</section>','<p>Единица измерения: упаковка</p>'+extra+'</section>')
   rec,ref=http_card.save_response(httpx.Response(200,text=body,request=httpx.Request('GET',facts['url'])),facts['url']);owned=http_card.main_product(body,facts['url']);self.assertIsNotNone(owned)
   proof=product_identity.http_identity(rec,owned,ref,'ABC123','Cable','electrical.example')
   current=dict(facts,text=owned['text'],main_title=owned['title'],expected_name='Cable',captured_at=rec['observed_at'],http_source=ref,identity_evidence=proof)
   self.assertIsNone(current_literal_quote(current))
