import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from engine.llm import LLMAccountError, LLMUnavailable
from engine.parsers import spec_pdf
from engine.vision_views import page_bands


def row(number=1, **changes):
    return dict(number=number, name='Item', type_mark='AB-12', maker='Maker',
                unit='шт', qty=7, review_fields={}, **changes)


class CloudFragmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / 'page.png')
        Image.new('RGB', (1000, 800), 'white').save(self.path)
        for name, value in [('cloudru_spec_reader', 'vision'), ('cloudru_vision_verify', True)]:
            patcher = patch.object(spec_pdf.settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_full_width_overlap_and_lifetime(self):
        # Horizontal markers prove each band keeps both outermost columns and
        # overlaps its neighbour; page borders must survive all three views.
        image = Image.new('RGB', (1000, 800), 'white')
        image.paste((255, 0, 0), (0, 0, 10, 800))
        image.paste((0, 255, 0), (990, 0, 1000, 800))
        image.paste((0, 0, 255), (0, 290, 1000, 310))
        image.save(self.path)
        with page_bands(self.path) as views:
            self.assertEqual(len(views), 3)
            bands = [Image.open(path) for path in views]
            try:
                self.assertEqual([band.size for band in bands], [(2000, 640)] * 3)
                self.assertEqual(bands[0].getpixel((0, 0)), (255, 0, 0))
                self.assertEqual(bands[2].getpixel((1999, 639)), (0, 255, 0))
                # Middle source y=300 appears in both first and second band.
                self.assertEqual(bands[0].getpixel((0, 600)), bands[1].getpixel((0, 120)))
            finally:
                for band in bands:
                    band.close()
        self.assertTrue(all(not Path(path).exists() for path in views))

    def test_disabled_verification_never_calls_native_ocr_or_second_model(self):
        with patch.object(spec_pdf.settings, 'cloudru_vision_verify', False), \
             patch.object(spec_pdf, 'ocr_available') as native, \
             patch.object(spec_pdf, '_structure_positions', return_value=[row()]) as model:
            result = spec_pdf._cloudru_positions([self.path])
        native.assert_not_called()
        self.assertEqual(model.call_count, 1)
        self.assertIn('выключено', result[0]['review_fields']['source'])

    def test_different_readings_keep_values_and_specific_warning(self):
        primary = row()
        second = {**row(), 'qty': 9, 'type_mark': 'AB-1Z'}
        def read(*, images):
            if images == [self.path]:
                return [primary]
            self.assertEqual(len(images), 3)
            self.assertTrue(all(Path(path).exists() for path in images))
            return [second]
        with patch.object(spec_pdf, 'ocr_available') as native, \
             patch.object(spec_pdf, '_structure_positions', side_effect=read) as model:
            result = spec_pdf._cloudru_positions([self.path])[0]
        native.assert_not_called()
        self.assertEqual(model.call_count, 2)
        self.assertEqual((result['qty'], result['type_mark']), (7, 'AB-12'))
        self.assertIn('увеличенные фрагменты', result['review_fields']['qty'])
        self.assertIn('AB-1Z', result['review_fields']['type_mark'])

    def test_agreement_never_auto_accepts_source(self):
        with patch.object(spec_pdf, '_structure_positions', side_effect=[[row()], [row()]]):
            result = spec_pdf._cloudru_positions([self.path])[0]
        self.assertIn('не подтверждает', result['review_fields']['source'])
        self.assertNotIn('qty', result['review_fields'])

    def test_secondary_failure_preserves_primary_without_retry_or_sensitive_error(self):
        for failure in (LLMUnavailable('sensitive'), LLMUnavailable('malformed')):
            with self.subTest(failure=failure), \
                 patch.object(spec_pdf, '_structure_positions', side_effect=[[row()], failure]) as model:
                result = spec_pdf._cloudru_positions([self.path])[0]
            self.assertEqual(model.call_count, 2)
            self.assertEqual(result['qty'], 7)
            self.assertIn('не завершена', result['review_fields']['source'])
            self.assertNotIn('sensitive', result['review_issue'])

    def test_account_rejection_stops_before_next_page(self):
        with patch.object(spec_pdf, '_structure_positions', side_effect=[
                [row()], LLMAccountError(402, 'Cloud.ru')]) as model:
            with self.assertRaises(LLMAccountError):
                spec_pdf._cloudru_positions([self.path, self.path])
        self.assertEqual(model.call_count, 2)

    def test_empty_secondary_is_failed_verification_not_agreement(self):
        with patch.object(spec_pdf, '_structure_positions', side_effect=[[row()], []]):
            result = spec_pdf._cloudru_positions([self.path])[0]
        self.assertIn('не завершена', result['review_fields']['source'])
