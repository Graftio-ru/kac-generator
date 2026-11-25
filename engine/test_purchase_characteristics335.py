"""Validated purchase/photo proof and a distinct strict characteristics locator."""
import copy,hashlib,json,tempfile,time,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from .search import http_card as dom,owned_purchase_row as rows
from .source_quote import _validated_owned_length

class PurchaseCharacteristicsTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.cache=Path(self.temp.name);self.patch=patch.object(dom,'CACHE_DIR',self.cache);self.patch.start()
 def tearDown(self):self.patch.stop();self.temp.cleanup()
 def make(self,*,photo=False,title='Tray ABC123',length='3000 мм',host='supplier.example'):
  url=f'https://{host}/product/tray';stamp=time.time()-5
  html=('<main itemscope itemtype="https://schema.org/Product"><h1>'+title+'</h1><div>Артикул:<span itemprop="model">ABC123</span></div><span itemprop="mpn">ABC123</span><span itemprop="sku">SELLER789</span><p>100 руб. / шт</p><input type="number" name="quantity" value="1" data-price="100"><button>Купить</button><h2>Технические параметры</h2><table><tr><td>Длина</td><td>'+length+'</td></tr></table></main>')
  owned=dom._neutral_owner(html,url);self.assertIsNotNone(owned)
  def frame(stamp):
   f=dict(version=1,role='main_card_facts',requested_url=url,document_url=url,main_title=owned['title'],article=owned['article'],owner_text=owned['text'],purchase_text='',owner_bound=True,owner_truncated=False,observed_at=stamp)
   source=dom.save_rendered_dom(html,f,url);f['rendered_source']=source;folder=self.cache/'main_facts';folder.mkdir(exist_ok=True);p=folder/(uuid.uuid4().hex+'.json');raw=json.dumps(f,sort_keys=True).encode();p.write_bytes(raw)
   return {**source,'main_facts':dict(ref=str(p.relative_to(self.cache)),sha256=hashlib.sha256(raw).hexdigest(),observed_at=stamp)}
  source=frame(stamp);self.context=None;self.photo=None
  if photo:
   from PIL import Image
   self.photo=self.cache/(uuid.uuid4().hex+'.png');Image.new('RGB',(100,100),'white').save(self.photo);raw=self.photo.read_bytes()
   self.context=dict(version=1,role='review_context',filename=self.photo.name,sha256=hashlib.sha256(raw).hexdigest(),url=url,captured_at=stamp+1,width=100,height=100,document_width=100,document_height=100,truncated=False)
   source['photo_binding']=dict(after_main_facts=frame(stamp+2)['main_facts'],context=self.context)
  proof=rows.derive(source,url=url,observed_at=stamp,title=owned['title'],article=owned['article'],text=owned['text']);self.assertIsNotNone(proof);self.assertEqual(proof['version'],2)
  return proof
 def test_five_and_full_six_key_sources_use_same_strict_owned_length(self):
  for photo in (False,True):
   proof=self.make(photo=photo);before=copy.deepcopy(proof);answer,reason=rows.decision(proof,dict(unit='м'))
   self.assertEqual(reason,'');self.assertAlmostEqual(answer['price'],100/3);self.assertEqual(answer['quantity'],3)
   witness=answer['owned_length'];self.assertEqual(set(witness['source']),{'role','ref','record_sha256','body_sha256','main_facts'});self.assertEqual(_validated_owned_length(witness,proof['title']),3)
   self.assertEqual(before,proof);self.assertIsNot(witness['source']['main_facts'],proof['source']['main_facts'])
   if photo:self.assertTrue(dom.rendered_photo_valid(proof['source'],self.context,str(self.photo)))
 def test_unknown_extra_and_proof_identity_amount_or_hash_tamper_deny(self):
  proof=self.make(photo=True)
  def frame(v):v['source']['main_facts']['sha256']='0'*64
  changes=[lambda v:v['source'].update(unknown='not-a-characteristic'),lambda v:v['source'].update(record_sha256='0'*64),lambda v:v['source'].update(body_sha256='0'*64),frame,lambda v:v.update(article='OTHER999'),lambda v:v.update(raw_amount=1),lambda v:v.update(raw_amount=True),lambda v:v.update(observed_at=v['observed_at']+1),lambda v:v['source']['main_facts'].update(observed_at=v['observed_at']+1)]
  for change in changes:
   value=copy.deepcopy(proof);change(value)
   with self.subTest(change=change):self.assertIsNone(rows.decision(value,dict(unit='м'))[0])
 def test_title_field_and_owned_length_conflict_is_not_repaired(self):
  proof=self.make(title='Tray ABC123 L4000');self.assertIsNone(rows.decision(proof,dict(unit='м'))[0])
 def test_invalid_or_ambiguous_length_has_no_numeric_hint_fallback(self):
  for value in ('3000–4000 мм','3000','0 мм','около 3000 мм'):
   with self.subTest(value=value):
    proof=self.make(length=value);self.assertIsNone(rows.decision(proof,dict(unit='м'))[0])
 def test_per_use_body_and_frame_mutation_still_rejects_saved_witness(self):
  proof=self.make(photo=True);answer,reason=rows.decision(proof,dict(unit='м'));self.assertEqual(reason,'');witness=answer['owned_length']
  p=self.cache/proof['source']['ref'];raw=p.read_bytes();p.write_bytes(raw+b' ')
  self.assertIsNone(rows.decision(proof,dict(unit='м'))[0]);self.assertIsNone(_validated_owned_length(witness,proof['title']));p.write_bytes(raw)
  p=self.cache/proof['source']['main_facts']['ref'];p.write_bytes(p.read_bytes()+b' ')
  self.assertIsNone(rows.decision(proof,dict(unit='м'))[0]);self.assertIsNone(_validated_owned_length(witness,proof['title']))
 def test_characteristics_view_never_grants_tampered_photo_authority(self):
  proof=self.make(photo=True);self.assertTrue(dom.rendered_photo_valid(proof['source'],self.context,str(self.photo)))
  changed=copy.deepcopy(proof);changed['source']['photo_binding']['after_main_facts']['sha256']='0'*64
  # A photo-only mutation must remain on the original full proof. The
  # characteristics view is neither a replacement image proof nor a repair.
  before=copy.deepcopy(changed);answer,reason=rows.decision(changed,dict(unit='м'));self.assertEqual(before,changed)
  self.assertFalse(dom.rendered_photo_valid(changed['source'],self.context,str(self.photo)))
  if answer:self.assertNotIn('photo_binding',answer['owned_length']['source'])
