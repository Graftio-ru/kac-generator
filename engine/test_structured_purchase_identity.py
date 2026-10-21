import copy,unittest
from .purchase_scope import scope_observed_article,scope_review
from . import test_purchase_scope as fixtures
class StructuredIdentityTests(unittest.TestCase):
 def scene(self):
  s=fixtures.PurchaseScopeTests().scene();p=s['prices'][0];scope=p['purchase_scope'];owner=scope['owner_id'];scope['own_articles']=['ABC123'];scope.update(identity_facts_version=1,identity_scope_id=owner,identity_facts=[dict(kind='labelled_article',value='ABC123',text='Артикул: ABC123',node=owner+'/label',owner_id=owner)],ancillary_titles=[],quote_role='current');return s,p
 def test_current_local_article_does_not_need_flattened_navigation_text(self):
  s,p=self.scene();self.assertEqual(scope_observed_article(p['purchase_scope']),'ABC123');self.assertEqual(scope_review(s,p),'')
 def test_malformed_and_cross_owner_facts_refuse(self):
  for change in [dict(value='OTHER456'),dict(node='foreign/label'),dict(text='empty'),dict(kind='metadata')]:
   s,p=self.scene();p['purchase_scope']['identity_facts'][0].update(change);self.assertTrue(scope_review(s,p),change)
 def test_conflicting_current_article_still_vetoes(self):
  s,p=self.scene();self.assertEqual(scope_review(s,p,article='OTHER456'),'purchase_scope_conflicting_identity')
 def test_only_current_quote_role_can_be_admitted(self):
  for role in ['saving','historical']:
   s,p=self.scene();p['purchase_scope']['quote_role']=role;self.assertEqual(scope_review(s,p),'purchase_price_not_current')
 def test_fake_ancillary_suffix_and_wrong_article_refuse(self):
  for title,article in [('Другой товар '+fixtures.PurchaseScopeTests().scene()['title'],'ABC123'),('Характеристики '+fixtures.PurchaseScopeTests().scene()['title'],'OTHER456')]:
   s,p=self.scene();scope=p['purchase_scope'];scope['ancillary_titles']=[dict(node=scope['owner_id']+'/h2',text=title,role='product_properties',article=article)];self.assertEqual(scope_review(s,p),'purchase_scope_conflicting_identity')
 def test_plain_row_primary_fact_cannot_be_foreign(self):
  s,p=self.scene();f=p['purchase_scope']['identity_facts'][0];f.update(kind='printed_row_article',name_codes=['ABC123'],primary=dict(kind='labelled_article',value='ABC123',owner_id='foreign'))
  self.assertFalse(scope_observed_article(p['purchase_scope']))
 def test_no_price_identity_uses_explicit_mode(self):
  import inspect
  from .search.product_identity import capture_current_identity
  source=inspect.getsource(capture_current_identity);self.assertIn("'identity_only':True",source);self.assertNotIn('DOM_SNAPSHOT.replace',source)

class PrintedNameCompatibilityTests(unittest.TestCase):
 def test_actual_name_prefix_is_independent_of_sku(self):
  from .purchase_scope import printed_name_compatible
  main='Счетчик электроэнергии CE308 S31.543.OAA.SYUVJLFZ SPDS 3ф 5-10А Энергомера'
  self.assertTrue(printed_name_compatible(main,'Счетчик CE308 S31.543.OAA.SYUVJLFZ SPDS 3ф 5-10А класс точн.0.5S/0.5 Энергомера'))
  for row in ['Other ABC123','Item Alternative ABC123','ABC123','123 Item ABC123']:
   self.assertFalse(printed_name_compatible('Item ABC123',row),row)
 def test_lexical_tokens_can_reorder_only_after_same_type_anchor(self):
  from .purchase_scope import printed_name_compatible
  self.assertTrue(printed_name_compatible('Cable outdoor shielded ABC123','Cable shielded outdoor ABC123'))
  self.assertFalse(printed_name_compatible('Cable outdoor shielded ABC123','Shielded Cable outdoor ABC123'))

class StructuredFactRoleTests(unittest.TestCase):
 def test_complete_article_labels_are_not_values(self):
  from .purchase_scope import labelled_article_value
  for label in ['Артикул производителя','Артикул товара','Код производителя','Артикул:']:
   self.assertEqual(labelled_article_value(label),'')
  for value in ['Артикул производителя\n\nABC123','Артикул:ABC123','Код производителя ABC123']:
   self.assertEqual(labelled_article_value(value),'ABC123')
 def test_claimed_label_word_and_unlabelled_article_refuse(self):
  for text,value in [('Артикул производителя','производителя'),('ABC123','ABC123')]:
   s,p=StructuredIdentityTests().scene();scope=p['purchase_scope'];scope['own_articles']=[value];scope['identity_facts'][0].update(text=text,value=value)
   self.assertEqual(scope_observed_article(scope),'');self.assertTrue(scope_review(s,p))
 def scene(self):
  s,p=StructuredIdentityTests().scene();o=p['purchase_scope']['owner_id'];property_node=o+'/properties/table'
  reference=dict(role='manufacturer',label='Производитель',text='Example',label_node=property_node+'/tr/td:1',value_node=property_node+'/tr/td:2',node=property_node+'/tr/td:2/a',href='https://shop.example/brand/example')
  p['purchase_scope']['ancillary_titles']=[dict(node=o+'/properties/h2',text='Полные технические характеристики '+s['title'],role='product_properties',article='ABC123',property_node=property_node,reference_links=[reference])]
  return s,p,reference
 def test_label_bound_manufacturer_reference_is_not_product_identity(self):
  s,p,r=self.scene();self.assertEqual(scope_review(s,p),'')
 def test_reference_label_role_and_value_node_cannot_be_changed(self):
  for change in [dict(label='Другой товар'),dict(role='product'),dict(text='Other OTHER456'),dict(node='foreign/a'),dict(value_node='foreign/td'),dict(href='javascript:void(0)')]:
   s,p,r=self.scene();r.update(change);self.assertTrue(scope_review(s,p),change)
 def test_document_role_never_includes_product_or_article_labels(self):
  from .purchase_scope import property_reference_role
  self.assertEqual(property_reference_role('Сертификат'),'document')
  for label in ['Артикул','Другой товар','Аналог','Название товара','Сертификат ABC123']:
   self.assertEqual(property_reference_role(label),'')
