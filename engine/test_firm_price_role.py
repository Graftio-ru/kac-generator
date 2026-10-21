"""Firm currency role is independently reprojected through ordinary business gates."""
import copy,json,time,hashlib,unittest
from pathlib import Path
from unittest.mock import patch
from engine import source_quote as source
from engine.offer_selection import offer_is_comparable,source_count,select_top_offers
from engine.search.source_checks import producer_receipt,matching_quote_receipt,source_checks
from engine.search import base_db,agent,session
from engine import browser,llm
from engine.schemas import PositionIn,OfferIn
from engine.models import SpecPosition

class FirmPriceTests(unittest.TestCase):
 def produce(self,text):
  from engine.test_source_quote import SourceQuoteTests
  page,pos,offer=SourceQuoteTests().produce(price=100)
  page.text=page.title+'\nАртикул: 3526410\n'+text
  offer.source_quote_evidence=source.observe_quote(page,url=offer.url,title=page.title,article='3526410',listed=100,normalized=100,sale_unit='м',requested_unit='м',data={})
  return pos,offer
 def test_starting_currency_whitespace_and_v1_reprojection(self):
  for literal in ('от100₽','Цена от 100 руб.','от 100,00 RUB','от\n100\n₽','От\u00a0100,00\u00a0рублей','Цена:\nот\n100 RUB'):
   with self.subTest(literal=literal):
    pos,offer=self.produce(literal+'\nЦена за1м');self.assertTrue(offer.source_quote_evidence)
    self.assertIn('начальная',source.quote_review_reason(offer,pos));self.assertFalse(offer_is_comparable(offer,pos));self.assertEqual(source_count([offer],pos),0);self.assertEqual(select_top_offers([offer],3,pos),[])
    receipt=producer_receipt(offer,pos,job_id='a123456789ab',host='supplier.example',started_at=time.time()-1,validated_at=time.time())
    self.assertFalse(matching_quote_receipt(receipt,[offer],pos));self.assertEqual(offer.source_quote_evidence['version'],1);self.assertEqual(offer.price_with_vat,100)
 def test_indicative_prefix_before_split_currency_is_preserved(self):
  for label in ('Минимальная цена','Ориентировочная цена','Приблизительная цена','Индикативная цена'):
   pos,offer=self.produce(label+'\n100\nRUB\nЦена за1м');self.assertFalse(offer_is_comparable(offer,pos),label)
 def test_owner_disclosures_retained_and_conditional_not_quantity(self):
  for terms in ('Стоимость продукции рассчитывается индивидуально, исходя из объема заказа.','Чтобы узнать окончательную цену, необходимо оставить заявку.','Точную цену уточняйте у менеджера.','Ориентировочная цена','Цена по запросу','Минимальная цена 100 руб.'):
   pos,offer=self.produce('100 ₽\nЦена за1м\n'+terms)
   self.assertTrue(offer.source_quote_evidence['terms_text'],terms);self.assertFalse(offer_is_comparable(offer,pos),terms)
  for terms in ('Минимальная сумма заказа 5000 руб.','Доставка от 100 руб.','Стоимость доставки рассчитывается индивидуально','Изображения служат только для ознакомления','Сопутствующие товары\nЦена от100₽','Похожие товары\nТочную цену уточняйте у менеджера'):
   pos,offer=self.produce('100 ₽\nЦена за1м\n'+terms)
   self.assertTrue(offer_is_comparable(offer,pos),source.quote_review_reason(offer,pos));self.assertNotIn('ознакомления',offer.source_quote_evidence['terms_text'])
  self.assertEqual(source.firm_price_role_reason('100 ₽','от10м /м'),'');self.assertEqual(source.firm_price_role_reason('100 RUB','от5шт /шт'),'')
  pos,offer=self.produce('100 ₽\nЦена за1м от 5 шт');self.assertFalse(offer_is_comparable(offer,pos)) # Existing quantity conditions stay authoritative.
 def test_same_owner_wrapped_clauses_producer_consumer_and_boundaries(self):
  for terms in ('Стоимость продукции\nрассчитывается индивидуально','Чтобы узнать окончательную цену,\nнеобходимо оставить заявку.','Цена\nпо запросу'):
   with self.subTest(terms=terms):
    self.assertTrue(source.firm_price_role_reason('100 ₽','Цена за1м',terms))
    pos,offer=self.produce('100 ₽\nЦена за1м\n'+terms)
    self.assertTrue(offer.source_quote_evidence['terms_text']);self.assertFalse(offer_is_comparable(offer,pos))
    # Existing v1 data may retain wrapping in unit or terms fields.
    offer.source_quote_evidence['terms_text']=terms;self.assertFalse(offer_is_comparable(offer,pos));offer.source_quote_evidence['terms_text']='';offer.source_quote_evidence['unit_text']='Цена за1м\n'+terms;self.assertFalse(offer_is_comparable(offer,pos))
  for terms in ('Стоимость доставки\nрассчитывается индивидуально','Минимальная сумма заказа\nрассчитывается индивидуально','Стоимость продукции\nДоставка\nрассчитывается индивидуально','Цена\nДоставка и оплата\nпо запросу'):
   self.assertEqual(source.firm_price_role_reason('100 ₽','Цена за1м',terms),'',terms)
   pos,offer=self.produce('100 ₽\nЦена за1м\n'+terms);self.assertTrue(offer_is_comparable(offer,pos),source.quote_review_reason(offer,pos))
 def test_old_price_role_cannot_contaminate_current_price(self):
  pos,offer=self.produce('Старая цена\nот 200 ₽\nТекущая цена\n100 ₽\nЦена за1м')
  self.assertTrue(offer_is_comparable(offer,pos),source.quote_review_reason(offer,pos));self.assertNotIn('200',offer.source_quote_evidence['price_text'])
  pos,offer=self.produce('Ориентировочная цена 200 ₽\nТекущая цена\n100 ₽\nЦена за1м');self.assertTrue(offer_is_comparable(offer,pos),source.quote_review_reason(offer,pos))
  pos,offer=self.produce('100 ₽\nЦена за1м\nДоставка и оплата\nСтоимость доставки рассчитывается индивидуально');self.assertTrue(offer_is_comparable(offer,pos))
 def test_immutable_actual_cable_refusal_dkc_survives_and_receipt_rolls_back(self):
  path=Path(__file__).resolve().parents[2]/'sources/kac-generator/current-quote-main-2026-10-13/live/public.json';before=hashlib.sha256(path.read_bytes()).hexdigest();job=json.loads(path.read_text());rows=job['rows']
  row=next(r for r in rows if any(o['url'].startswith('https://cable.ru/') for o in r.get('candidates',[])))
  pos=PositionIn.model_validate(row);offers={o.url:o for o in pos.model_copy(update={'offers':[OfferIn.model_validate(o) for o in row['candidates']]}).to_result().offers}
  cable=next(o for url,o in offers.items() if url.startswith('https://cable.ru/'));dkc=next(o for url,o in offers.items() if 'dkc.market' in url and o.source_quote_evidence.get('price_text'))
  self.assertIn('начальная',source.quote_review_reason(cable,pos));self.assertFalse(offer_is_comparable(cable,pos));self.assertTrue(offer_is_comparable(dkc,pos),source.quote_review_reason(dkc,pos))
  self.assertNotIn(cable,select_top_offers(list(offers.values()),3,pos));self.assertEqual(cable.source_quote_evidence['terms_text'],'')
  receipt=producer_receipt(cable,pos,job_id=job['job_id'],host='cable.ru',started_at=time.time()-1,validated_at=time.time());self.assertFalse(matching_quote_receipt(receipt,[cable],pos))
  projected=source_checks(['cable.ru'],{'cable.ru':{'current_quote_receipt':receipt,'queries':1,'cards':1}},row,terminal=True);self.assertNotEqual(projected[0].get('coverage_obligation'),'current_quote_confirmed')
  from engine import app
  stored=next(c for c in row['source_checks'] if c['host']=='cable.ru')
  self.assertFalse(app._research_pair_finished(stored,position=row,job_id=job['job_id'],offers=row['candidates']))
  fixed=next(c for c in row['source_checks'] if c['host']=='dkc.market')
  self.assertTrue(app._research_pair_finished(fixed,position=row,job_id=job['job_id'],offers=row['candidates']))
  from engine.final_eligibility import prepare_final_results
  from engine.models import PositionResult
  with patch('engine.search.base_db.apply_refresh',side_effect=lambda p,r:r),self.assertRaisesRegex(ValueError,'1 из 3'):
   prepare_final_results([PositionResult(position=pos.to_result().position,offers=[cable,dkc],all_offers=[cable,dkc])])
  self.assertEqual(source_count([cable,dkc],pos),1)

  self.assertEqual(before,hashlib.sha256(path.read_bytes()).hexdigest())

class HttpFirmPriceTests(unittest.TestCase):
 def setUp(self):
  from engine.test_http_card import HttpCardTests
  HttpCardTests.setUp(self)
 def test_actual_http_owned_source_reparse_refuses_starting_amount(self):
  from engine import test_http_card as fixture
  from engine.search import http_card,product_identity
  from engine.models import PriceOffer,Requisites
  body=fixture.html().replace('<span itemprop="price">120</span>','от <span itemprop="price">120</span>')
  response=fixture.HttpCardTests.response(self,body);record,ref=http_card.save_response(response,fixture.URL)
  observed=http_card.main_product(body,fixture.URL);self.assertIsNotNone(observed)
  identity=product_identity.http_identity(record,observed,ref,'ABC123','Cable','supplier.example')
  facts=source.observe_captured_quote(observed['text'],actual_url=record['url'],observed_at=record['observed_at'],url=record['requested_url'],title=observed['title'],article=observed['article'],listed=120,normalized=120,sale_unit='м',requested_unit='м',data={})
  offer=PriceOffer(price_with_vat=120,listed_price_with_vat=120,url=fixture.URL,identity_evidence=identity,source_quote_evidence=facts,price_unit='м',normalized_price_unit='м',requisites=Requisites(name='Supplier',inn='7842224734'))
  pos=dict(type_mark='ABC123',name='Cable',unit='м');self.assertTrue(facts);self.assertIn('начальная',source.quote_review_reason(offer,pos));self.assertFalse(offer_is_comparable(offer,pos));self.assertEqual(offer.price_with_vat,120)
  body=fixture.html();record,ref=http_card.save_response(fixture.HttpCardTests.response(self,body),fixture.URL);observed=http_card.main_product(body,fixture.URL)
  identity=product_identity.http_identity(record,observed,ref,'ABC123','Cable','supplier.example')
  facts=source.observe_captured_quote(observed['text'],actual_url=record['url'],observed_at=record['observed_at'],url=record['requested_url'],title=observed['title'],article=observed['article'],listed=120,normalized=120,sale_unit='м',requested_unit='м',data={})
  facts['terms_text']='Изображения служат только для ознакомления'
  offer=offer.model_copy(update={'identity_evidence':identity,'source_quote_evidence':facts})
  self.assertTrue(offer_is_comparable(offer,pos),source.quote_review_reason(offer,pos))
  offer.source_quote_evidence['terms_text']='Стоимость продукции рассчитывается индивидуально';self.assertFalse(offer_is_comparable(offer,pos))

