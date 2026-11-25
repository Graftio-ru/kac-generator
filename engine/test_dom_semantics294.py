"""Invocation-local DOM reuse preserves evidence decisions, never source freshness."""
import base64,importlib.util,json,sys,unittest
from pathlib import Path
from unittest.mock import patch
from engine.search import http_card as dom

A=Path('/Users/MmM/WebstormProjects/graftio/sources/kac-generator/atomic-simplification-2026-10-14')
B=A/'EPIC-0001-TASK-0294'
class DomSemanticsTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  name='engine.search._baseline_dom294';spec=importlib.util.spec_from_file_location(name,B/'http_card.before.py');cls.old=importlib.util.module_from_spec(spec);sys.modules[name]=cls.old;spec.loader.exec_module(cls.old)
 def html(self,attrs='',model='ABC123',length='3000 мм',extra=''):
  return '<main><section '+attrs+' itemscope itemtype="https://schema.org/Product"><h1>Tray ABC123</h1><div>Артикул<span itemprop="model">'+model+'</span></div><span itemprop="sku">987654321</span><h2>Технические параметры</h2><table><tr><td>Длина</td><td>'+length+'</td>'+extra+'</tr></table></section></main>'
 def compare(self,html):
  expected=self.old.primary_characteristics(html,title='Tray ABC123',code='ABC123');actual=dom.primary_characteristics(html,title='Tray ABC123',code='ABC123');self.assertEqual(actual,expected);self.assertIsNone(dom._DOM_CALCULATIONS.get());return actual
 def test_mutation_between_calls_does_not_reuse_context(self):
  self.assertEqual(self.compare(self.html())['length']['metres'],3)
  for attrs in ['hidden','style="opacity:0"','style="display:none"','data-related-products="yes"','class="recommendations"']:
   self.assertIsNone(self.compare(self.html(attrs=attrs)))
  self.assertIsNone(self.compare(self.html(model='OTHER99')))
  self.assertEqual(self.compare(self.html(length='4000 мм'))['length']['metres'],4)
  self.assertEqual(self.compare(self.html())['length']['metres'],3)
 def test_unsafe_ambiguous_and_span_decisions_equal(self):
  for html in [self.html(extra='<td>4000 мм</td>'),self.html(extra='<td hidden>3000 мм</td>'),self.html(extra='<td></td>'),self.html().replace('<td>3000','<td rowspan="2">3000'),self.html().replace('<table>','<div>Упаковка</div><table>'),self.html().replace('<table>','<div class="variant"><table>').replace('</table>','</table></div>'),self.html().replace('<table>','<div class="recommendations"><table>').replace('</table>','</table></div>'),self.html().replace('<h1>','<h1 hidden>')]:
   with self.subTest(html=html):self.compare(html)
 def test_owned_sets_are_distinct_and_direct_mutation_is_uncached(self):
  parser=dom.MainParser('<div><span>one</span><span>two</span></div>',False);root=parser.root.children[0];a,b=root.children
  @dom._immutable_dom
  def owned():
   self.assertEqual(dom._owned_value(root,{root,a}),'one')
   self.assertEqual(dom._owned_value(root,{root,b}),'two')
   self.assertEqual(dom._owned_value(root,{root,a}),'one')
  owned();self.assertTrue(dom._semantic_visible(a));root.attrs['hidden']='';self.assertFalse(dom._semantic_visible(a));root.attrs.clear();self.assertTrue(dom._semantic_visible(a))
 def test_error_and_reentrant_context_reset(self):
  marker=RuntimeError('sentinel')
  @dom._immutable_dom
  def outer():
   context=dom._DOM_CALCULATIONS.get();dom.primary_characteristics(self.html(),title='Tray ABC123',code='ABC123');self.assertIs(dom._DOM_CALCULATIONS.get(),context);raise marker
  with self.assertRaises(RuntimeError)as error:outer()
  self.assertIs(error.exception,marker);self.assertIsNone(dom._DOM_CALCULATIONS.get())
 def test_repeated_calculation_reuses_only_local_nodes(self):
  parser=dom.MainParser('<main><div><span>one</span></div></main>',False);node=parser.root.children[0];owned=set(node.descendants())
  @dom._immutable_dom
  def calculate():
   first=dom._owned_value(node,owned);context=dom._DOM_CALCULATIONS.get();size=len(context['owned']);second=dom._owned_value(node,owned)
   self.assertEqual(first,second);self.assertEqual(len(context['owned']),size);self.assertGreater(size,1)
  calculate();self.assertIsNone(dom._DOM_CALCULATIONS.get())
 def test_retained23_owned_fields_and_admission_decisions_equal(self):
  path=A/'EPIC-0001-TASK-0293/data/CACHE_DIR/rendered_cards/5264d8a0afe54119880e542cf1981b5e.json';record=json.loads(path.read_text());html=base64.b64decode(record['body_base64']).decode('utf-8');frame=record['frame']
  old=self.old.primary_characteristics(html,title=frame['main_title'],code='3526210');new=dom.primary_characteristics(html,title=frame['main_title'],code='3526210');self.assertEqual(old,new);self.assertEqual(new['length']['metres'],3)
  self.assertEqual(self.old.main_product(html,record['url'],check_controls=False,admission_only=True),dom.main_product(html,record['url'],check_controls=False,admission_only=True))
