"""Offline DOM late-preview wake controls; no financial admission or live timing."""
import json,time,unittest
from unittest.mock import patch
from engine import test_owned_preview_readiness as original
from engine.search import agent

class PreviewChangeReadinessTests(original.OwnedPreviewReadinessTests):
 def test_frozen_html_late_owned_preview_wakes_native_gate_promptly(self):
  mutation="""() => {const e=document.querySelector('.ordering-tocart-info'),saved=e.innerHTML;e.replaceChildren();setTimeout(()=>e.innerHTML=saved,60)}"""
  r,elapsed=self.ready(self.fixture(),wait=1,mutate=mutation)
  self.assertEqual(r['kind'],'complete');self.verify_basis(r)
  self.assertGreater(elapsed,.04);self.assertLess(elapsed,.55)
 def test_identical_invalid_snapshot_timestamp_and_attribute_churn_do_not_reprove(self):
  mutation="""() => {const e=document.querySelector('.ordering-tocart-info');let i=0;const timer=setInterval(()=>{e.dataset.tick=String(++i);if(i===60)clearInterval(timer)},3)}"""
  with patch.object(agent,'_owned_readiness_preview',wraps=agent._owned_readiness_preview) as proof:
   r,elapsed=self.ready(self.small(total='на сумму 101 ₽'),wait=.35,mutate=mutation)
  self.assertNotEqual(r['kind'],'complete');self.assertNotIn('readiness_unit_basis',r)
  self.assertEqual(proof.call_count,1);self.assertGreater(elapsed,.28)
 def test_distinct_invalid_changes_are_bounded_and_late_valid_remains_eligible(self):
  mutation="""() => {const e=document.querySelector('.ordering-tocart-info');let i=0;const timer=setInterval(()=>{e.innerHTML='<div>1 шт.</div><div>на сумму '+(200+(++i))+' ₽</div>';if(i===55){clearInterval(timer);e.innerHTML='<div>1 шт.</div><div>на сумму 100 ₽</div>'}},4)}"""
  with patch.object(agent,'_owned_readiness_preview',wraps=agent._owned_readiness_preview) as proof:
   r,elapsed=self.ready(self.small(total='на сумму 101 ₽'),wait=.6,mutate=mutation)
  self.assertEqual(r['kind'],'complete');self.verify_basis(r)
  self.assertLessEqual(proof.call_count,33);self.assertGreater(proof.call_count,1)
  self.assertGreater(elapsed,.4)
 def test_late_title_cash_and_explicit_unit_wake_without_js_unit_acceptance(self):
  mutation="""() => {setTimeout(()=>document.querySelector('h1').textContent='Item ABC123',40);setTimeout(()=>document.querySelector('.ordering-mainoffer-price-w').textContent='100 ₽',80);setTimeout(()=>document.querySelector('.ordering-tocart-info').innerHTML='<div>1 шт.</div><div>на сумму 100 ₽</div>',120)}"""
  r,elapsed=self.ready(self.small(title='',cash='',quantity='',total=''),wait=1,mutate=mutation)
  self.assertEqual(r['kind'],'complete');self.verify_basis(r);self.assertGreater(elapsed,.09);self.assertLess(elapsed,.55)
  # Neutral observer alone may wake, but never certifies preview as complete.
  self.document(self.small())
  value=self.page.evaluate('('+agent._MAIN_FACTS_READINESS_JS+')({limit:0,previous_preview_key:"different"})')
  self.assertEqual(value['kind'],'partial');self.assertTrue(value['preview_changed'])
  self.assertNotIn('readiness_unit_basis',value)
 def test_late_hidden_and_decoy_invalid_preview_keep_full_remaining_wait(self):
  cases=[self.small().replace('class="ordering-tocart-info"','style="opacity:0" class="ordering-tocart-info"'),self.small().replace('itemprop="offers"','itemprop="unrelated"')]
  for html in cases:
   with self.subTest(html=html):
    with patch.object(agent,'_owned_readiness_preview',wraps=agent._owned_readiness_preview) as proof:
     r,elapsed=self.ready(html,wait=.3,mutate="() => setTimeout(()=>document.querySelector('.ordering-tocart-info').dataset.tick='changed',50)")
    self.assertNotEqual(r['kind'],'complete');self.assertNotIn('readiness_unit_basis',r)
    self.assertEqual(proof.call_count,1);self.assertGreater(elapsed,.24)
