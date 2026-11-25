"""Typed primary length ownership, immutable locators and strict conversion."""
import copy,hashlib,json,tempfile,time,unittest,uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from engine.search import http_card as dom
from engine.source_quote import selected_price_context,owned_product_length,_validated_owned_length
from engine.search.purchase_preview import derive_unit_basis
from engine.search.catalogue_projection import _facts

class OwnedLengthTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.cache=patch.object(dom,'CACHE_DIR',self.root);self.cache.start();dom._CHAR_INDEX.clear()
  self.title='Лоток L3000 сталь 1мм ABC123 (987654321)';self.url='https://supplier.test/product/ABC123';self.stamp=time.time()
 def tearDown(self):self.cache.stop();self.tmp.cleanup()
 def html(self,model=None,rows=None):
  model=model if model is not None else '<div><span>Артикул</span><span itemprop="model">ABC123</span></div>'
  rows=rows if rows is not None else '<tr><td>Длина</td><td>3000 мм</td></tr>'
  return '<main><section itemscope itemtype="https://schema.org/Product"><h1>'+self.title+'</h1><div>Номенклатурный номер<span itemprop="sku">987654321</span></div>'+model+'<p>100 ₽</p><p>1 шт.</p><p>на сумму 100 ₽</p><h2>Технические параметры</h2><table>'+rows+'</table></section></main>'
 def store(self,html=None):
  html=html or self.html();owned=dom.main_product(html,self.url,check_controls=False,admission_only=True)
  frame=dict(version=1,role='main_card_facts',document_url=self.url,requested_url=self.url,main_title=self.title,article='ABC123',owner_text=owned['text'],purchase_text='',owner_bound=True,owner_truncated=False,observed_at=self.stamp)
  base=dom.save_rendered_dom(html,frame,self.url);frame['rendered_source']=base;folder=self.root/'main_facts';folder.mkdir(exist_ok=True);path=folder/(uuid.uuid4().hex+'.json');path.write_text(json.dumps(frame))
  source={**base,'main_facts':dict(ref='main_facts/'+path.name,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),observed_at=self.stamp)}
  text=owned['text'];obs=dict(url=self.url,observed_at=self.stamp,title=self.title,manufacturer_code='ABC123',source_text=text,source_sha256=hashlib.sha256(text.encode()).hexdigest(),raw_amount=100,currency='RUB',state='observed',vat='unknown',purchase_terms='',sale_unit='шт',lot_quantity=1,package_quantity=None)
  cash=dict(version=1,source='product_card_text',url=self.url,observed_at=self.stamp,title=self.title,listed_price=100,price_text=selected_price_context(text,100)[0]);obs['unit_basis']=derive_unit_basis(obs,cash);return obs,source
 def test_typed_model_and_sku_stay_separate(self):
  obs,source=self.store();retained=dom.read_primary_characteristics_source(source,expected_code='ABC123');self.assertIsNotNone(retained)
  self.assertEqual(retained[1]['article'],'987654321');self.assertEqual(retained[1]['characteristics']['model'],'ABC123');self.assertEqual(retained[1]['characteristics']['sku'],['987654321']);self.assertIsNone(dom.read_source(source,allow_rendered=True))
 def test_native_preview_conversion_and_zero_warm_metadata_reads(self):
  obs,_=self.store();w,reason=owned_product_length(obs);self.assertEqual(reason,'');self.assertEqual(w['length']['metres'],3)
  again,reason=owned_product_length(obs);self.assertEqual(again,w);index=dom._CHAR_INDEX[str(self.root.resolve())];self.assertEqual(index['last_reads'],0);self.assertEqual(index['last_bytes'],0)
  facts,reason=_facts(obs,SimpleNamespace(unit='м',name='Лоток',type_mark='ABC123'));self.assertEqual(reason,'');self.assertAlmostEqual(facts['price'],100/3)
 def test_models_need_one_owned_printed_label(self):
  valid='<div><span>Артикул</span><span itemprop="model">ABC123</span></div>'
  for model in [valid+valid,valid+'<span itemprop="model">OTHER99</span>','<span itemprop="model">ABC123</span>','<meta itemprop="model" content="ABC123">','<div class="variant">'+valid+'</div>',valid+'<span itemprop="mpn">OTHER99</span>']:
   with self.subTest(model=model):self.assertIsNone(dom.primary_characteristics(self.html(model=model),title=self.title,code='ABC123'))
 def test_printed_manufacturer_conflict_is_not_overridden_by_model(self):
  obs,source=self.store(self.html(model='<div>Артикул<span itemprop="model">ABC123</span></div><div>Артикул производителя: OTHER99</div>'))
  self.assertIsNone(dom.read_primary_characteristics_source(source,expected_code='ABC123'))
 def test_length_requires_primary_technical_row(self):
  for rows in ['<tr><td>Длина упаковки</td><td>3000 мм</td></tr>','<tr><td>Длина</td><td>3000–4000 мм</td></tr>','<tr><td>Длина</td><td>3000</td></tr>','<tr><td>Длина</td><td>0 мм</td></tr>','<tr class="variant"><td>Длина</td><td>3000 мм</td></tr>','<tr><td>Длина</td><td>3000 мм</td></tr><tr><td>Длина</td><td>4000 мм</td></tr>']:
   with self.subTest(rows=rows):self.assertIsNone(dom.primary_characteristics(self.html(rows=rows),title=self.title,code='ABC123'))
 def test_plain_package_and_variant_captions_end_technical_scope(self):
  for caption in ['Упаковка','Размеры упаковки','Варианты товара']:
   html=self.html().replace('<table>','<div>'+caption+'</div><table>');self.assertIsNone(dom.primary_characteristics(html,title=self.title,code='ABC123'))
 def test_length_row_has_exactly_one_owned_value_cell(self):
  for extra in ['<td>4000 мм</td>','<td>3000 мм</td>','<th>Вариант</th>','<td hidden>4000 мм</td>','<td><input></td>','<td><span></span></td>','<td data-value="4000"></td>','<td aria-label="Вариант"></td>','<td title="4000 мм"></td>','<td colspan="2"></td>','<td rowspan="2"></td>']:
   rows='<tr><td>Длина</td><td>3000 мм</td>'+extra+'</tr>'
   with self.subTest(extra=extra):
    self.assertIsNone(dom.primary_characteristics(self.html(rows=rows),title=self.title,code='ABC123'))
    obs,source=self.store(self.html(rows=rows));self.assertIsNone(dom.read_primary_characteristics_source(source,expected_code='ABC123'));self.assertIsNone(owned_product_length(obs)[0])
  for extra in ['<td></td>','<td class="empty"> \n </td>']:
   rows='<tr><td>Длина</td><td>3000 мм</td>'+extra+'</tr>'
   with self.subTest(empty=extra):self.assertEqual(dom.primary_characteristics(self.html(rows=rows),title=self.title,code='ABC123')['length']['metres'],3)
 def test_length_cells_cannot_span_alternate_values(self):
  for tag in ['td','th']:
   for attr in ['rowspan','colspan']:
    for cell in [0,1]:
     cells=['<'+tag+'>Длина</'+tag+'>','<td>3000 мм</td>'];cells[cell]=cells[cell].replace('>',' '+attr+'="2">',1)
     rows='<tr>'+''.join(cells)+'</tr><tr><td>4000 мм</td></tr>'
     with self.subTest(tag=tag,attr=attr,cell=cell):self.assertIsNone(dom.primary_characteristics(self.html(rows=rows),title=self.title,code='ABC123'))
 def test_recommendation_or_external_model_cannot_supply_owner(self):
  model='<div class="recommendations"><div>Артикул<span itemprop="model">ABC123</span></div></div>'
  self.assertIsNone(dom.primary_characteristics(self.html(model=model),title=self.title,code='ABC123'))
  html=self.html(model='')+'<div>Артикул<span itemprop="model">ABC123</span></div>';self.assertIsNone(dom.primary_characteristics(html,title=self.title,code='ABC123'))
 def test_changed_bound_artifacts_fail_on_every_use(self):
  obs,source=self.store();self.assertIsNotNone(owned_product_length(obs)[0]);p=self.root/source['ref'];p.write_bytes(p.read_bytes()+b' ');self.assertIsNone(owned_product_length(obs)[0])
 def test_changed_frame_hash_url_and_epoch_fail(self):
  obs,source=self.store()
  for key,value in [('observed_at',self.stamp+1),('url','https://supplier.test/other'),('title','Other title'),('source_sha256','0'*64),('manufacturer_code','OTHER99')]:
   other=copy.deepcopy(obs);other[key]=value;self.assertIsNone(owned_product_length(other)[0])
  path=self.root/source['main_facts']['ref'];path.write_bytes(path.read_bytes()+b' ');self.assertIsNone(owned_product_length(obs)[0])
 def test_ambiguous_locator_never_picks_first(self):
  obs,_=self.store();self.store();w,reason=owned_product_length(obs);self.assertIsNone(w);self.assertIn('ambiguous',reason)
 def test_entry_limit_counts_invalid_names_before_sorting(self):
  obs,_=self.store();folder=self.root/'main_facts';(folder/'invalid-a').touch();(folder/'invalid-b').touch()
  with patch.object(dom,'_CHAR_MAX_FILES',2):w,reason=owned_product_length(obs)
  self.assertIsNone(w);self.assertIn('index_bound',reason)
 def test_byte_limit_cannot_accept_partial_index_or_cached_prefix(self):
  obs,_=self.store();path=next((self.root/'main_facts').glob('*.json'))
  with patch.object(dom,'_CHAR_MAX_BYTES',path.stat().st_size-1):self.assertIn('index_bound',owned_product_length(obs)[1])
  dom._CHAR_INDEX.clear();self.assertIsNotNone(owned_product_length(obs)[0]);self.store()
  used=dom._CHAR_INDEX[str(self.root.resolve())]['bytes']
  with patch.object(dom,'_CHAR_MAX_BYTES',used):self.assertIn('index_bound',owned_product_length(obs)[1])
 def test_title_conflict_or_range_is_not_erased(self):
  for title in ['Лоток L4000 ABC123','Лоток L3000–4000 ABC123','Лоток около L3000 ABC123']:
   self.title=title;obs,_=self.store();self.assertIsNone(owned_product_length(obs)[0])
 def test_lifetime_entry_limit_survives_folder_churn(self):
  with patch.object(dom,'_CHAR_MAX_FILES',2):
   for count in range(3):
    self.stamp+=1;obs,source=self.store();w,reason=owned_product_length(obs)
    if count<2:self.assertIsNotNone(w)
    else:self.assertIsNone(w);self.assertIn('index_bound',reason)
    (self.root/source['main_facts']['ref']).unlink()
 def test_same_root_frame_reference_replacement_cannot_rebind_old_observation(self):
  obs,source=self.store();self.assertIsNotNone(owned_product_length(obs)[0]);first=self.root/source['main_facts']['ref']
  self.stamp+=1;_,replacement=self.store();second=self.root/replacement['main_facts']['ref'];first.write_bytes(second.read_bytes());second.unlink()
  self.assertIsNone(owned_product_length(obs)[0]);dom._CHAR_INDEX.clear();self.assertIsNone(owned_product_length(obs)[0])
 def test_price_lot_package_subtotal_and_cash_gates_remain(self):
  obs,_=self.store();position=SimpleNamespace(unit='м',name='Лоток',type_mark='ABC123')
  for key,value in [('sale_unit','8 шт'),('lot_quantity',8),('package_quantity',2),('currency','USD'),('raw_amount',200)]:
   other=copy.deepcopy(obs);other[key]=value;self.assertIsNone(_facts(other,position)[0])
  other=copy.deepcopy(obs);other['unit_basis']['subtotal']=999;self.assertIsNone(_facts(other,position)[0])
