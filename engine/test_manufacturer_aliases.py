import unittest
from .search.manufacturer_aliases import REGISTRY,manufacturer_equal,manufacturer_in_title

class ManufacturerAliasTests(unittest.TestCase):
    def test_primary_evidence_exact_names_only(self):
        self.assertTrue(manufacturer_equal('DKC',' ДКС '))
        self.assertTrue(manufacturer_equal('дкс','dkc'))
        self.assertTrue(manufacturer_in_title('DKC','Лоток ДКС 3526210'))
        self.assertTrue(all(v.evidence_type=='manufacturer_primary_publication' and v.sources for v in REGISTRY))
        for value in ('IEK','ДКС fake','DKC GROUP','DКC','DKC123','Cinch'):
            self.assertFalse(manufacturer_equal('DKC',value),value)
        self.assertFalse(manufacturer_equal('IEK','ИЭК'))
        self.assertFalse(manufacturer_in_title('DKC','Лоток DKC123'))
