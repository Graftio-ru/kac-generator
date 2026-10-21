"""A transient hit mismatch never certifies the interceptor or a failed image."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import price_capture
from .capture_visibility import VISIBILITY_SCRIPT, attestation_path
from .search.failure_policy import ResearchControlError
from .test_capture_visibility import Page
from . import test_capture_visibility as fixtures


class CaptureTransientVisibilityTests(unittest.TestCase):
    def scenario(self, permanent=False, cancel=False, no_prior=False):
        snapshot = fixtures.VisibilityTests().snapshot()['snapshot']
        clock = [1000.0]

        class TransientPage(Page):
            checks = 0
            frame_waits = 0
            def evaluate(self, script, arg=None):
                if script == price_capture.VISIBILITY_FRAME_SCRIPT:
                    self.frame_waits += 1
                    clock[0] += 1
                    if cancel:
                        raise ResearchControlError('cancelled', 'cancelled')
                    return True
                result = super().evaluate(script, arg)
                if script == VISIBILITY_SCRIPT:
                    self.checks += 1
                    if self.checks == (1 if no_prior else 2) or (permanent and self.checks > 2):
                        return {'verified': False, 'reason': 'occluded', 'fields': [
                            {'role': 'heading', 'uncovered': False, 'reason': 'occluded',
                             'witness': {'branch': 'topmost_hit_mismatch'}}]}
                return result
            def wait_for_timeout(self, milliseconds):
                clock[0] += milliseconds / 1000

        return TransientPage(snapshot), clock

    def test_transient_hit_waits_then_requalifies_before_only_fresh_screenshot(self):
        page, clock = self.scenario()
        with tempfile.TemporaryDirectory() as folder, patch.object(price_capture.time, 'monotonic', lambda: clock[0]):
            path = Path(folder) / 'price.png'
            proof, reason = fixtures.VisibilityTests().capture(page, path)
            self.assertTrue(proof, reason)
            self.assertEqual(page.frame_waits, 1)
            self.assertGreaterEqual(page.checks, 5)
            self.assertEqual(page.shots, 1)
            self.assertTrue(attestation_path(path).exists())
            self.assertTrue(proof['visibility_before']['verified'])
            self.assertTrue(proof['visibility_after']['verified'])

    def test_persistent_unknown_hit_never_attests_and_keeps_original_deadline(self):
        page, clock = self.scenario(permanent=True)
        with tempfile.TemporaryDirectory() as folder, patch.object(price_capture.time, 'monotonic', lambda: clock[0]):
            path = Path(folder) / 'price.png'
            proof, reason = fixtures.VisibilityTests().capture(page, path)
            self.assertFalse(proof); self.assertIn('видимость обязательного поля не подтверждена', reason)
            self.assertLessEqual(page.frame_waits, 8)
            self.assertEqual(page.shots, 0); self.assertFalse(attestation_path(path).exists())
            self.assertEqual(page.scroll, {'x': 0, 'y': 0})

    def test_cancel_during_recheck_restores_owned_page_and_never_attests(self):
        page, clock = self.scenario(cancel=True)
        with tempfile.TemporaryDirectory() as folder, patch.object(price_capture.time, 'monotonic', lambda: clock[0]):
            path = Path(folder) / 'price.png'
            with self.assertRaises(ResearchControlError):
                fixtures.VisibilityTests().capture(page, path)
            self.assertEqual(page.shots, 0); self.assertFalse(attestation_path(path).exists())
            self.assertEqual(page.scroll, {'x': 0, 'y': 0})

    def test_initial_unknown_hit_rechecks_without_certifying_interceptor(self):
        page, clock = self.scenario(no_prior=True)
        with tempfile.TemporaryDirectory() as folder, patch.object(price_capture.time, 'monotonic', lambda: clock[0]):
            path = Path(folder) / 'price.png'
            proof, reason = fixtures.VisibilityTests().capture(page, path)
            self.assertTrue(proof, reason)
            self.assertEqual(page.frame_waits, 1); self.assertEqual(page.shots, 1)
            self.assertGreaterEqual(page.checks, 4)
            self.assertTrue(attestation_path(path).exists())
