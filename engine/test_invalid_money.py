"""Offline retained monetary-candidate regression; no merchant re-delivery."""
import hashlib,json,math,tempfile,shutil,unittest
from pathlib import Path
from unittest.mock import patch
from engine import price_capture
from engine.search import agent,http_card
from engine.search.card_batch import CardSnapshot
from engine.test_card_batch import snapshot
from engine.search.failure_policy import ResearchControlError
FIXTURES=Path(__file__).resolve().parents[2]/'fixtures'
METRICS={}
class InvalidMoneyTests(unittest.TestCase):
 def test_retained_native_literal_extractor_does_not_crash(self):
  p=FIXTURES/'captured_inputs/a63f073c873c4742a507067ba166d433.json';raw=p.read_bytes()
  self.assertEqual(hashlib.sha256(raw).hexdigest(),'33d6ddf8387e78d8d6ddf13a8d2261998c7065bb269a3948d7abd32167176fbb')
  stored=json.loads(raw);facts=json.loads(stored['payload']);self.assertEqual(hashlib.sha256(stored['payload'].encode()).hexdigest(),stored['sha256'])
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);shutil.copytree(FIXTURES/'http_cards',root/'http_cards')
   with patch.object(http_card,'CACHE_DIR',root):
    retained=http_card.read_source(facts['http_source']);self.assertIsNotNone(retained)
    self.assertEqual(retained[1]['text'],facts['text'])
    result=agent._literal_card_fields(CardSnapshot(stored['id'],stored['payload'],stored['sha256'],None))
  values=price_capture.visible_prices(facts['text']);self.assertNotIn(None,values)
  self.assertEqual(list(dict.fromkeys(values)),[490.0,701.23,690.1])
  METRICS['retained']={'native_literal_fields':result,'positive_candidates':values,'source_sha256':hashlib.sha256(raw).hexdigest(),'no_new_quote_or_delivery':True}
 def test_zero_negative_nonfinite_and_signed_amounts_not_candidates(self):
  for value in [None,True,False,0,-1,float('inf'),float('-inf'),float('nan'),'nan','inf']:
   self.assertIsNone(price_capture._number(value))
  for text in ['0.00 ₽','-5 ₽','−5 ₽','- 5,00 RUB','-1 234,50 руб.','NaN RUB','Infinity ₽','9'*400+' ₽']:
   with self.subTest(text=text[:30]):self.assertEqual(price_capture.visible_prices(text),[])
  self.assertEqual(price_capture.visible_prices('Цена 1 234,50 ₽; 12 м; артикул 345; +25 RUB; 0 ₽; -9 ₽'),[1234.5,25.0])
 def test_valid_literal_and_zero_only_native_behavior(self):
  valid=snapshot('Cable ABC123\nАртикул: ABC123\n120 ₽\nЦена за 1 м')
  mixed=snapshot('Cable ABC123\nАртикул: ABC123\n120 ₽\nЦена за 1 м\nМинимальная сумма заказа 0,00 ₽')
  self.assertEqual(agent._literal_card_fields(valid),agent._literal_card_fields(mixed))
  self.assertEqual(agent._literal_card_fields(valid)['price_with_vat'],120)
  for literal in ['0 ₽','-5 ₽','−5 ₽']:
   result=agent._literal_card_fields(snapshot('Cable ABC123\nАртикул: ABC123\n'+literal+'\nЦена за 1 м'))
   self.assertNotIn('price_with_vat',result)
 def test_nonproduct_roles_and_control_errors_unchanged(self):
  for text in ['Cable ABC123\nКлубная цена\n100 ₽\nЦена за 1 м','Cable ABC123\nРозничная цена\n100 ₽\nЦена за 1 м']:
   self.assertNotIn('price_with_vat',agent._literal_card_fields(snapshot(text)))
  error=ResearchControlError('fixed test control')
  with patch('engine.price_capture.visible_prices',side_effect=error):
   with self.assertRaises(ResearchControlError) as caught:agent._literal_card_fields(snapshot())
  self.assertIs(caught.exception,error)
