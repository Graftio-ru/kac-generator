import unittest
from engine.search.work_plan import build_work_plan
class WorkPlanTests(unittest.TestCase):
 def test_full_obligations_no_caps_and_tails_preserved(self):
  positions=[{'number':i+1,'is_unique':i<10,'review_fields':{'source':'original'} if i<68 else {},'review_accepted':False} for i in range(96)];hosts=[f's{i}.example' for i in range(36)]
  candidates={i:[f'https://s{h}.example/card/{i}-{n}' for n in range(4) for h in range(36)] for i in range(10,96)}
  plan=build_work_plan(positions,hosts[:30],hosts[30:],{},candidates)
  self.assertEqual(plan['logical_pairs_total'],3096);self.assertEqual(len(plan['skipped_numbers']),10);self.assertEqual(len(plan['candidate_order']),86*36*4);self.assertEqual([i['row_index'] for i in plan['candidate_order'][:86]],list(range(10,96)));self.assertIsNone(plan['default_service_card_limit']);self.assertFalse(plan['stop_after_three']);self.assertEqual(len(plan['pairs']),len({(p['number'],p['host']) for p in plan['pairs']}));self.assertTrue(all(p['product_presence']=='unknown' and p['status']=='pending' for p in plan['pairs']))
 def test_confirmed_first_invalidated_and_duplicate_lineage_no_fake_attempt(self):
  mappings={0:{'a.example':{'urls':['https://a.example/known'],'mappings':[{'url':'https://a.example/wrong','state':'invalidated'}]}}}
  candidates={0:['https://a.example/known','https://a.example/backup','https://a.example/wrong','https://b.example/card']}
  plan=build_work_plan([{'number':1}],['a.example'],['b.example'],mappings,candidates)
  self.assertEqual([c['url'] for c in plan['candidate_order']],['https://a.example/known','https://b.example/card','https://a.example/backup']);self.assertEqual(plan['candidate_order'][0]['origin'],'confirmed_identity');self.assertTrue(any(c['refusal_reason']=='identity_invalidated' for c in plan['rows'][0]['candidates']));self.assertEqual(plan['estimates']['logical_candidate_admissions'],3);self.assertTrue(plan['estimates']['wire_work_unknown'])
