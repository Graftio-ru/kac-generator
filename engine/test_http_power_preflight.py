"""Conservative exact power evidence; no transport or application startup."""
import unittest
from engine.models import SpecPosition
from engine.search.http_card import main_product
from engine.search.http_identity_preflight import _explicit_power_conflict


class ExactPowerPreflightTests(unittest.TestCase):
    def position(self, power='5 VA'):
        name = 'Transformer CT30, accuracy class 0.5, ' + power
        return SpecPosition(number=1, name=name, full_name=name,
                            type_mark='CT30 / 35266', unit='шт', qty=1,
                            source_evidence=dict(reader='native_pdf', cells=dict(
                                name=name, model='CT30', article='35266',
                                maker='DKC', unit='шт')))

    def owned(self, power='3 VA', extra=''):
        return main_product('<main><h1>Transformer CT30, accuracy class 0.5, '
                            + power + '</h1>' + extra + '</main>',
                            'https://supplier.test/card', admission_only=True,
                            check_controls=False)

    def test_only_explicit_different_singleton_refuses(self):
        for power in ('3 VA', '3,00 ВА', 'power: 7.5 VA'):
            with self.subTest(power=power):
                self.assertTrue(_explicit_power_conflict(self.position(), self.owned(power)))
        for power in ('5 VA', '5,00 ВА', 'power: 5 VA', ''):
            with self.subTest(power=power):
                self.assertFalse(_explicit_power_conflict(self.position(), self.owned(power)))

    def test_qualifiers_ranges_and_multiple_ratings_preserve_unknown(self):
        for expression in ('не более 5 ВА', 'максимум 5 ВА', '1—5 ВА',
                           '1 to 5 VA', '5 VA or 7 VA', '5 VA±10%',
                           'около 5 ВА', '5 VA; 7 VA', '5000 mVA', '5 В·А'):
            with self.subTest(expression=expression):
                self.assertFalse(_explicit_power_conflict(self.position(expression), self.owned()))
                self.assertFalse(_explicit_power_conflict(self.position('9 VA'), self.owned(expression)))

    def test_unowned_power_ratio_and_unbound_request_are_not_evidence(self):
        for power in ('', '300/5A'):
            frame = self.owned(power, '<aside>3 VA</aside><div class="recommendations">7 VA</div>')
            self.assertFalse(_explicit_power_conflict(self.position(), frame))
        unbound = self.position().model_copy(update={'source_evidence': {}})
        self.assertFalse(_explicit_power_conflict(unbound, self.owned()))

    def test_conflicting_owned_properties_prevent_new_shortcut(self):
        self.assertFalse(_explicit_power_conflict(self.position(), self.owned('3 VA', '<p>Power 5 VA</p>')))


if __name__ == '__main__':
    unittest.main()
