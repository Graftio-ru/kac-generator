"""Explicit synthetic purchase structure; no dated page snapshot gains provenance."""
import copy,json,re,tempfile,unittest
from pathlib import Path
from .price_capture import qualify_snapshot,capture_price,capture_review_reason,_capture_frame,visible_prices
from .purchase_scope import scope_review,consumer_scope_valid

def fixture_purchase_snapshot(snapshot,fields=None):
 """Describe a deliberately constructed one-product/one-purchase OFFLINE scene."""
 s=copy.deepcopy(snapshot)
 if fields:
  selected=[p for p in s.get('prices',[]) if fields['listed'] in visible_prices(p['text'])]
  s['prices']=selected[:1]  # Explicit new single-price scene, not old-page inference.
 # This is a newly constructed one-panel scene. Historical page structure is
 # deliberately discarded; these descriptors are not real observed provenance.
 purchase_lines=[]
 for p in s.get('prices',[]):purchase_lines.extend((p['text'],p.get('unit_context',p['text'])))
 purchase_lines += [line for line in s.get('text','').splitlines() if re.search(r'^\s*(?:Упаковка|В\s+1\s+упаковке)\s*[:(]?\s*\d',line,re.I)]
 purchase_text='\n'.join(dict.fromkeys(purchase_lines))
 s['offline_synthetic_structure']='one product heading and one explicit purchase panel'
 for i,p in enumerate(s.get('prices',[])):
  p['purchase_scope']=dict(version=1,provenance='rendered_purchase_scope',document_url=s['url'],
   owner_id='offline/product/purchase',basis_owner_id='offline/product/purchase',basis_text=purchase_text,kind='main_purchase',association='unique_simple',main_title=s['title'],
   own_titles=[],own_articles=[],product_links=[],related=False,purchase_controls=['В корзину'],
   identity_owner='offline/product',identity_heading=s['title'],tier_selected=False,tier_key='',tier_text='',
   price_node=f'offline/product/purchase/price:{i}',tier_prices=[x['text'] for x in s['prices']],scope_text=purchase_text)
 for f in s.get('fragments',[]):f.update(purchase_scope_id='offline/product/purchase',purchase_basis_id='offline/product/purchase')
 return s

class PurchaseScopeTests(unittest.TestCase):
 def test_actionless_scope_requires_independently_printed_local_identity(self):
  for kind,association in [('main_purchase','article_panel'),('variant_row','article_row')]:
   s=self.scene();scope=s['prices'][0]['purchase_scope'];scope.update(kind=kind,association=association,purchase_controls=[],own_articles=['ABC123'],identity_owner=scope['owner_id'])
   self.assertEqual(scope_review(s,s['prices'][0],article='ABC123'),'')
   scope['own_articles']=['OTHER456'];self.assertEqual(scope_review(s,s['prices'][0],article='ABC123'),'purchase_scope_conflicting_identity')
   scope['own_articles']=[];self.assertEqual(scope_review(s,s['prices'][0],article='ABC123'),'purchase_scope_missing')
  s=self.scene();s['prices'][0]['purchase_scope']['purchase_controls']=[]
  self.assertEqual(scope_review(s,s['prices'][0],article='ABC123'),'purchase_scope_missing')
 def test_only_explicit_physical_store_channel_is_separate(self):
  from .purchase_scope import price_applicability
  self.assertEqual(price_applicability('120 ₽ в розничных магазинах'),'offline_store')
  for text in ['120 ₽ розничная','120 ₽ за 70 шт.']:
   self.assertEqual(price_applicability(text),'unspecified')
  self.assertEqual(price_applicability('120 ₽ на сайте и в розничных магазинах'),'ambiguous')
  for text in ['120 ₽ оптовая','120 ₽ по клубной карте','120 ₽ при заказе 100 шт.','120 ₽ цена в корзине']:
   self.assertEqual(price_applicability(text),'conditional')
   s=self.scene();scope=s['prices'][0]['purchase_scope'];scope.update(basis_text=text,scope_text=text);s['text']=text
   self.assertEqual(scope_review(s,s['prices'][0],article='ABC123'),'purchase_price_condition_unconfirmed')
  s=self.scene();scope=s['prices'][0]['purchase_scope'];scope['tier_prices']+=['150 ₽ в розничных магазинах','₽'];scope['scope_text']+='\n150 ₽ в розничных магазинах';s['text']+='\n150 ₽ в розничных магазинах'
  self.assertTrue(qualify_snapshot(s,**self.fields())[0])
  scope['basis_text']='150 ₽ в розничных магазинах';self.assertEqual(scope_review(s,s['prices'][0],article='ABC123'),'purchase_price_offline_store')
 def test_full_assembled_production_dom_snapshot_parses_in_javascript(self):
  import shutil,subprocess
  from .price_capture import DOM_SNAPSHOT
  node=shutil.which('node');self.assertIsNotNone(node)
  result=subprocess.run([node,'-e',"let s='';process.stdin.on('data',d=>s+=d);process.stdin.on('end',()=>{new Function('return ('+s+')');console.log('complete production DOM parsed')})"],input=DOM_SNAPSHOT,text=True,capture_output=True,timeout=10)
  self.assertEqual(result.returncode,0,result.stderr);self.assertIn('complete production DOM parsed',result.stdout)
 def scene(self):
  s=dict(url='https://seller.example/product/ABC123',title='Item ABC123',text='Item ABC123\nАртикул: ABC123\n120 ₽ / уп\nУпаковка 12 м',
   prices=[dict(text='120 ₽ / уп',x=30,y=280,width=180,height=30)],
   fragments=[dict(text='Артикул: ABC123',x=30,y=240,width=220,height=25),dict(text='120 ₽ / уп',x=30,y=280,width=180,height=30),dict(text='Упаковка 12 м',x=30,y=320,width=220,height=25)],heading=dict(x=30,y=200,width=600,height=30))
  return fixture_purchase_snapshot(s)
 def test_malformed_scope_inputs_are_review_without_exceptions(self):
  s=self.scene();p=s['prices'][0];p['purchase_scope']['own_articles']=['ABC123']
  for article in [[],{},True,123,None]:self.assertEqual(scope_review(s,p,article=article),'purchase_scope_missing')
  for snapshot,price in [(None,p),(False,p),(s,None),(s,[]),({},p)]:self.assertEqual(scope_review(snapshot,price),'purchase_scope_missing')
  for proof in [None,[],True]:self.assertFalse(consumer_scope_valid(proof))
  p['purchase_scope']['version']=True;self.assertEqual(scope_review(s,p),'purchase_scope_missing')
 def test_dated_saved_pages_cannot_receive_purchase_authority_retroactively(self):
  for name in ['duplicate-article-video-heading.json','duplicate-article-marketing-paragraph.json','purchase-frame-screenshot-refusal.json']:
   data=json.loads((Path(__file__).parent/'testdata/price-frame'/name).read_text());proof,reason=qualify_snapshot(data['snapshot'],**data['fields']);self.assertFalse(proof);self.assertIn('purchase_scope_missing',reason)
 def fields(self):return dict(url='https://seller.example/product/ABC123',listed=120,normalized=10,sale_unit='уп',requested_unit='м',title='Item ABC123',article='ABC123',expected_mark='ABC123')
 def test_legacy_page_h1_never_donates_purchase_authority(self):
  s=self.scene();del s['prices'][0]['purchase_scope'];self.assertIn('purchase_scope_missing',qualify_snapshot(s,**self.fields())[1])
 def test_conflicting_sku_and_url_veto_even_with_current_backlink(self):
  for change in [dict(own_articles=['OTHER456']),dict(own_titles=['Analog OTHER456']),dict(product_links=[self.fields()['url'],'https://seller.example/product/OTHER456']),dict(product_links=[self.fields()['url']+'?variant=OTHER456']),dict(related=True),dict(association='unconfirmed_panel')]:
   s=self.scene();s['prices'][0]['purchase_scope'].update(change);self.assertFalse(qualify_snapshot(s,**self.fields())[0],change)
 def test_same_sku_tier_ambiguity_selected_scope_is_local(self):
  s=self.scene();scope=s['prices'][0]['purchase_scope'];scope['tier_prices']=['120 ₽ / уп','150 ₽ / уп'];scope['scope_text']+='\n150 ₽ / уп';scope['basis_text']+='\n150 ₽ / уп';s['text']+='\n150 ₽ / уп'
  self.assertIn('purchase_tier_ambiguous',qualify_snapshot(s,**self.fields())[1])
  scope.update(tier_selected=True,tier_key='offline/selected',tier_text='120 ₽ / уп');self.assertTrue(qualify_snapshot(s,**self.fields())[0])
  scope['tier_text']='150 ₽ / уп';self.assertFalse(qualify_snapshot(s,**self.fields())[0])
  scope['tier_text']='120 ₽ / уп\n150 ₽ / уп';self.assertFalse(qualify_snapshot(s,**self.fields())[0])
 def test_equal_price_different_visible_tier_conditions_remain_ambiguous(self):
  s=self.scene();scope=s['prices'][0]['purchase_scope'];scope['tier_prices']=['Розничная 120 ₽ / уп','Юридические лица 120 ₽ / уп'];scope['scope_text']+='\n'+'\n'.join(scope['tier_prices']);s['text']+='\n'+'\n'.join(scope['tier_prices'])
  self.assertIn('purchase_tier_ambiguous',qualify_snapshot(s,**self.fields())[1])
 def test_different_panels_do_not_merge_equal_price_different_tier_context(self):
  s=self.scene();first=s['prices'][0];second=copy.deepcopy(first);second['purchase_scope']['owner_id']='offline/product/other';second['purchase_scope']['basis_owner_id']='offline/product/other';second['purchase_scope']['price_node']='offline/product/other/price';second['purchase_scope']['scope_text']+='\nКорпоративная';second['purchase_scope']['basis_text']+='\nКорпоративная';s['text']+='\nКорпоративная';s['prices'].append(second)
  self.assertIn('purchase_tier_ambiguous',qualify_snapshot(s,**self.fields())[1])
  first['purchase_scope'].update(tier_selected=True,tier_key='chosen',tier_text='120 ₽ / уп');self.assertTrue(qualify_snapshot(s,**self.fields())[0])
 def test_selected_pack_row_cannot_borrow_other_price_row_quantity(self):
  s=self.scene();scope=s['prices'][0]['purchase_scope'];scope.update(basis_owner_id=scope['owner_id']+'/selected-row',basis_text='120 ₽ / уп',tier_selected=True,tier_key='selected-row',tier_text='120 ₽ / уп')
  scope['scope_text']+='\n240 ₽ / уп';s['text']+='\n240 ₽ / уп';scope['tier_prices'].append('240 ₽ / уп')
  self.assertIn('количество в упаковке не доказано',qualify_snapshot(s,**self.fields())[1])
 def test_adjacent_unit_or_basis_not_borrowed(self):
  s=self.scene();s['prices'][0]['purchase_scope']['scope_text']='120 ₽ / уп';s['prices'][0]['purchase_scope']['basis_text']='120 ₽ / уп';self.assertFalse(qualify_snapshot(s,**self.fields())[0])
  s=self.scene();s['fragments'][-1]['purchase_scope_id']='offline/another';p,r=qualify_snapshot(s,**self.fields());self.assertTrue(p,r);self.assertFalse(_capture_frame(s,p)[0])
 def test_readable_duplicate_chosen_without_other_sku_or_tier_merge(self):
  s=self.scene();late=copy.deepcopy(s['prices'][0]);s['prices'][0]['y']=2400;late['purchase_scope']['price_node']='offline/product/purchase/later';s['prices'].append(late)
  p,r=qualify_snapshot(s,**self.fields());self.assertTrue(p,r);self.assertEqual(p['price_rect']['y'],280)
  late['purchase_scope']['own_articles']=['OTHER'];p,r=qualify_snapshot(s,**self.fields());self.assertTrue(p,r);self.assertEqual(p['price_rect']['y'],2400)
 def test_actual_producer_binds_scope_and_client_copy_cannot_change_it(self):
  from .test_capture_visibility import Page
  from .capture_visibility import verify_attestation
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'price.png';proof,reason=capture_price(Page(self.scene()),path,**self.fields());self.assertTrue(proof,reason);self.assertTrue(consumer_scope_valid(proof));self.assertTrue(verify_attestation(path,proof))
   for change in [dict(purchase_scope={}),dict(basis_scope_ids=['other'])]:
    bad=copy.deepcopy(proof);bad.update(change);self.assertFalse(consumer_scope_valid(bad));self.assertFalse(verify_attestation(path,bad))
 def test_occluded_duplicate_can_use_later_readable_same_authority(self):
  from .test_capture_visibility import Page
  from .capture_visibility import VISIBILITY_SCRIPT
  s=self.scene();second=copy.deepcopy(s['prices'][0]);second['x']=260;second['purchase_scope']['price_node']='offline/product/purchase/second';s['prices'].append(second)
  class Selective(Page):
   def evaluate(self,script,arg=None):
    if script==VISIBILITY_SCRIPT and any(f['role']=='price' and f['x']==30 for f in arg):return {'verified':False,'reason':'occluded'}
    return super().evaluate(script,arg)
  with tempfile.TemporaryDirectory() as folder:
   proof,reason=capture_price(Selective(s),Path(folder)/'price.png',**self.fields());self.assertTrue(proof,reason);self.assertEqual(proof['price_rect']['x'],260)
