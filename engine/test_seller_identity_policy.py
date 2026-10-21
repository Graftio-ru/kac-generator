"""Shared identity contract. Run under the offline boundary; no seller-provenance claim."""
import copy,json,unittest
from pathlib import Path
from .offer_selection import seller_key,seller_review_reason,independent_offers,source_count
from .requisites.supplier import canonical_inn,valid_inn,SELLER_DECIMAL_ZEROS
from .models import PriceOffer,Requisites,SpecPosition,PositionResult
from .final_eligibility import prepare_final_results
DATA=Path(__file__).parent/'testdata/seller-policy'
class SellerIdentityPolicyTests(unittest.TestCase):
 def test_shared_matrix_and_raw_preservation(self):
  for c in json.loads((DATA/'matrix.json').read_text()):
   with self.subTest(raw=c['raw']):
    o={'inn':c['raw'],'url':'https://www.alias.test/p'};before=copy.deepcopy(o)
    self.assertEqual(valid_inn(c['raw']),c['valid'])
    self.assertEqual(seller_key(o),('inn',c['key']) if c['raw'] else ('host','alias.test'))
    self.assertEqual(o,before)
    o.update(org_name='Seller',listed_price_with_vat=1)
    self.assertEqual(not bool(seller_review_reason(o,[o])),c['valid'])
 def offers(self,inns,host=None):
  return [PriceOffer(price_with_vat=10+i,url=f'https://{host or str(i)+".test"}/p',requisites=Requisites(inn=inn)) for i,inn in enumerate(inns)]
 def test_real_selection_and_final_boundary(self):
  pool=self.offers(['7610131367','7610 131367','7610-131367'])
  self.assertEqual(source_count(pool),1);self.assertIs(independent_offers(pool)[0],pool[-1])
  row=PositionResult(position=SpecPosition(number=1,name='Legacy manual',unit='шт'),offers=pool,all_offers=pool)
  with self.assertRaisesRegex(ValueError,'1 из 3'):prepare_final_results([row])
  self.assertEqual([o.requisites.inn for o in pool],['7610131367','7610 131367','7610-131367'])
  pool=self.offers(['7610131367','7701677700','7842224734'])
  row=PositionResult(position=row.position,offers=pool,all_offers=pool);prepare_final_results([row]);self.assertEqual(len(row.offers),3)
 def test_legacy_manual_letter_and_host_ambiguity(self):
  for inn in ['abc','123','0000000000']:
   o=self.offers([inn])[0];self.assertEqual(source_count([o]),1)
   o.listed_price_with_vat=1;self.assertTrue(seller_review_reason(o,[o]));o.from_letter=True;self.assertEqual(seller_review_reason(o,[o]),'')
  pool=self.offers(['1','2',''],host='market.test');self.assertEqual(source_count(pool),2)
  pool=self.offers(['7610131367','7610 131367',''],host='market.test');self.assertEqual(source_count(pool),1);self.assertIs(independent_offers(pool)[0],pool[-1])
 def test_automatic_name_and_retained_roles_only(self):
  good={'inn':'7610131367','org_name':'Seller','listed_price_with_vat':1,'url':'https://x.test'}
  self.assertEqual(seller_review_reason(good,[good]),'');good['org_name']=' ';self.assertTrue(seller_review_reason(good,[good]))
  rows=json.loads((DATA/'retained-rows.json').read_text())
  # Identity-only inspection of frozen observations; does not reissue proof or leases.
  self.assertEqual([sum(not bool(seller_review_reason(o,r['candidates'])) for o in r['candidates']) for r in rows],[0,1,1])
  self.assertEqual([len(r['offers']) for r in rows],[0,0,0])

 def test_frozen_decimal_alphabet(self):
  contract=json.loads((DATA/'decimal-contract.json').read_text())
  self.assertEqual(list(SELLER_DECIMAL_ZEROS),contract['supported_decimal_zeros'])
  for zero in contract['supported_decimal_zeros']:
   with self.subTest(zero=zero):
    digits=''.join(chr(zero+d) for d in range(10))
    self.assertEqual(canonical_inn(digits),'0123456789')
    inn=''.join(chr(zero+int(d)) for d in '7610131367')
    self.assertTrue(valid_inn(inn));self.assertEqual(seller_key({'inn':inn}),('inn','7610131367'))
  for zero in contract['unsupported_decimal_zeros']:
   inn=''.join(chr(zero+int(d)) for d in '7610131367')
   self.assertEqual(canonical_inn(inn),'');self.assertFalse(valid_inn(inn))
