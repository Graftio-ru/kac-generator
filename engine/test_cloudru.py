"""Offline Cloud.ru contracts: no live API requests."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image
from engine import cloudru, llm
from engine.parsers import spec_pdf
from engine.telemetry import capture_metrics


def response(content='{"positions":[]}', status=200, finish='stop'):
    result = MagicMock(status_code=status)
    result.json.return_value = {'choices': [{'finish_reason': finish,
                                            'message': {'content': content}}],
                                'usage': {'prompt_tokens': 12, 'completion_tokens': 5}}
    return result


class CloudruTests(unittest.TestCase):
    def setUp(self):
        local = patch.object(spec_pdf, 'ocr_available', return_value=False)
        local.start()
        self.addCleanup(local.stop)
        for name, value in [('llm_provider', 'cloudru'), ('cloudru_spec_reader', 'ocr'), ('cloudru_api_key', 'offline-key'),
                            ('cloudru_base_url', 'https://foundation-models.api.cloud.ru/v1')]:
            patcher = patch.object(llm.settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_text_dispatch_and_independent_model_selection(self):
        with patch.object(cloudru, 'provider_post', return_value=response()) as post, \
             patch.object(llm.settings, 'cloudru_model_fast', 'fast'), \
             patch.object(llm.settings, 'cloudru_model_smart', 'smart'), \
             patch.object(cloudru, 'record_llm_usage') as usage:
            for smart, model in [(False, 'fast'), (True, 'smart')]:
                self.assertEqual(llm.complete_json('Read', smart=smart), {'positions': []})
                request = post.call_args
                self.assertEqual(request.args[0], 'https://foundation-models.api.cloud.ru/v1/chat/completions')
                self.assertEqual(request.kwargs['headers']['Authorization'], 'Bearer offline-key')
                self.assertEqual(request.kwargs['json']['model'], model)
                self.assertEqual(request.kwargs['json']['response_format'], {'type': 'json_object'})
                usage.assert_called_with('cloudru', model, {'prompt_tokens': 12, 'completion_tokens': 5})

    def test_ocr_uses_original_page_and_model_specific_prompt(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'wrong-extension.png'
            Image.new('RGB', (12, 12), 'white').save(path, format='JPEG')
            with patch.object(cloudru, 'provider_post', return_value=response('| Position |')) as post:
                self.assertEqual(cloudru.recognize_page(path), '| Position |')
                body = post.call_args.kwargs['json']
                self.assertEqual(body['model'], 'deepseek-ai/DeepSeek-OCR-2')
                self.assertNotIn('response_format', body)
                self.assertEqual(len(body['messages']), 1)
                content = body['messages'][0]['content']
                self.assertEqual(content[0]['text'], '<image><|grounding|>Convert the document to markdown.')
                self.assertTrue(content[1]['image_url']['url'].startswith('data:image/jpeg;base64,'))
                self.assertNotIn('detail', content[1]['image_url'])

    def test_missing_key_does_not_read_image_or_call_api(self):
        with patch.object(llm.settings, 'cloudru_api_key', ''), patch.object(cloudru, 'provider_post') as post:
            with self.assertRaises(llm.LLMUnavailable):
                cloudru.recognize_page('missing.png')
            post.assert_not_called()

    def test_account_rejection_stops_job_and_is_sanitized(self):
        for status in (401, 402, 403):
            with self.subTest(status=status), llm.llm_request_scope(llm.LLMRequestGuard()), \
                 patch.object(cloudru, 'provider_post', return_value=response(status=status)) as post:
                for _ in range(2):
                    with self.assertRaises(llm.LLMAccountError) as caught:
                        llm.complete_json('Read')
                    self.assertEqual(caught.exception.provider, 'Cloud.ru')
                    self.assertNotIn('offline-key', str(caught.exception))
                self.assertEqual(post.call_count, 1)

    def test_invalid_or_truncated_result_is_failure(self):
        for content, finish in [('broken', 'stop'), ('', 'stop'), ('{}', 'length')]:
            with self.subTest(content=content), patch.object(cloudru, 'provider_post', return_value=response(content, finish=finish)):
                with self.assertRaises(llm.ModelOutputInvalid):
                    llm.complete_json('Read')

    def test_images_use_separate_vision_model(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'scan.png'
            Image.new('RGB', (12, 12), 'white').save(path)
            with patch.object(cloudru, 'provider_post', return_value=response()) as post:
                llm.complete_json('Read', images=[str(path)])
            payload = post.call_args.kwargs['json']
            self.assertEqual(payload['model'], 'qwen/qwen3-vl-30b-a3b-instruct')
            self.assertEqual(payload['messages'][1]['content'][1]['type'], 'image_url')
            self.assertEqual(payload['temperature'], 0.7)
            self.assertEqual(payload['top_p'], 0.8)

    def test_sampling_is_specific_to_qwen_vision(self):
        with patch.object(cloudru, 'provider_post', return_value=response()) as post:
            llm.complete_json('Read')
        self.assertEqual(post.call_args.kwargs['json']['temperature'], 0)
        self.assertNotIn('top_p', post.call_args.kwargs['json'])
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'scan.png'
            Image.new('RGB', (12, 12), 'white').save(path)
            with patch.object(llm.settings, 'cloudru_model_vision', 'other/vision'), \
                 patch.object(cloudru, 'provider_post', return_value=response()) as post:
                llm.complete_json('Read', images=[str(path)])
            self.assertEqual(post.call_args.kwargs['json']['temperature'], 0)
            self.assertNotIn('top_p', post.call_args.kwargs['json'])

    def test_scan_requests_constrain_output_and_reject_root_metadata(self):
        with patch.object(spec_pdf, 'complete_json', return_value={'positions': []}) as complete:
            spec_pdf._structure_positions(images=['offline.png'])
        schema = complete.call_args.kwargs['response_schema']
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(set(schema['properties']), {'positions'})
        self.assertFalse(schema['properties']['positions']['items']['additionalProperties'])
        with patch.object(spec_pdf, 'complete_json', return_value={
                'positions': [], 'review_issue': 'unrelated root metadata'}):
            with self.assertRaisesRegex(llm.ModelOutputInvalid, 'invalid_rows'):
                spec_pdf._structure_positions(images=['offline.png'])
        with patch.object(spec_pdf, 'complete_json', return_value={'positions': []}) as complete:
            spec_pdf._structure_positions(text='offline text')
        self.assertNotIn('response_schema', complete.call_args.kwargs)

    def test_cloudru_transmits_strict_schema_without_fallback(self):
        schema = spec_pdf._position_response_schema()
        with patch.object(cloudru, 'provider_post', return_value=response()) as post:
            llm.complete_json('Read', response_schema=schema)
        result = post.call_args.kwargs['json']['response_format']
        self.assertEqual(result['type'], 'json_schema')
        self.assertTrue(result['json_schema']['strict'])
        self.assertEqual(result['json_schema']['schema'], schema)
        with patch.object(cloudru, 'provider_post', return_value=response(status=422)) as post:
            with self.assertRaises(llm.LLMUnavailable):
                llm.complete_json('Read', response_schema=schema)
            self.assertEqual(post.call_count, 1)

    def test_scanned_pdf_ocr_then_structure_and_preserves_review(self):
        with patch.object(spec_pdf, 'extract_raw_text', return_value=('', True)), \
             patch.object(spec_pdf, '_cloudru_pdf_pages', return_value=['a.png', 'b.png']), \
             patch.object(spec_pdf, 'ocr_available', return_value=False) as local, \
             patch.object(cloudru, 'recognize_page', side_effect=['page A', 'page B']) as ocr, \
             patch.object(spec_pdf, '_structure_positions', return_value=[{
                 'name': 'Cable', 'review_issue': 'Uncertain unit', 'review_fields': {'unit': 'Uncertain'}}]) as structure:
            raw = spec_pdf._raw_positions_from_pdf('scan.pdf')
            self.assertEqual(ocr.call_count, 2)
            self.assertIn('page A', structure.call_args.kwargs['text'])
            self.assertIn('page B', structure.call_args.kwargs['text'])
            self.assertEqual(raw[0]['review_fields']['unit'], 'Uncertain')
            self.assertIn('OCR', raw[0]['review_fields']['source'])
            self.assertIn('Uncertain unit', raw[0]['review_issue'])
            local.assert_called_once()

    def test_ocr_failure_prevents_text_call_and_fallback(self):
        with patch.object(cloudru, 'recognize_page', side_effect=llm.LLMUnavailable('failed')), \
             patch.object(spec_pdf, '_structure_positions') as structure:
            with self.assertRaises(llm.LLMUnavailable):
                spec_pdf._raw_positions_from_image('scan.png')
            structure.assert_not_called()

    def test_empty_positions_and_oversized_ocr_are_explicit_failures(self):
        with patch.object(cloudru, 'recognize_page', return_value='page'), \
             patch.object(spec_pdf, '_structure_positions', return_value=[]):
            with self.assertRaises(llm.ModelOutputInvalid):
                spec_pdf._cloudru_positions(['scan.png'])
        with patch.object(cloudru, 'recognize_page', return_value='a' * 120001), \
             patch.object(spec_pdf, '_structure_positions') as structure:
            with self.assertRaises(llm.LLMUnavailable):
                spec_pdf._cloudru_positions(['scan.png'])
            structure.assert_not_called()

    def test_ocr_and_text_usage_are_separate(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'scan.png'
            Image.new('RGB', (12, 12), 'white').save(path)
            with capture_metrics() as metrics, patch.object(cloudru, 'provider_post', side_effect=[
                    response('page'), response()]):
                cloudru.recognize_page(path)
                llm.complete_json('Read')
            usage = metrics.summary()['llm_usage']
            self.assertEqual(set(usage), {'cloudru:deepseek-ai/DeepSeek-OCR-2',
                                         'cloudru:deepseek-ai/DeepSeek-V4-Flash'})
            self.assertEqual(usage['cloudru:deepseek-ai/DeepSeek-OCR-2']['input_tokens'], 12)

    def test_malformed_envelope_and_timeout_are_safe_failures(self):
        for data in ({}, {'choices': []}, None):
            malformed = response()
            malformed.json.return_value = data
            with self.subTest(data=data), patch.object(cloudru, 'provider_post', return_value=malformed):
                with self.assertRaises(llm.LLMUnavailable):
                    llm.complete_json('Read')
        with patch.object(cloudru, 'provider_post', side_effect=cloudru.httpx.ReadTimeout('secret-server-detail')):
            with self.assertRaises(llm.LLMUnavailable) as caught:
                llm.complete_json('Read')
            self.assertNotIn('secret-server-detail', str(caught.exception))

    def test_health_uses_cloudru_key_independently(self):
        from engine.app import health
        with patch.object(llm.settings, 'cloudru_api_key', ''), \
             patch.object(llm.settings, 'anthropic_api_key', 'other-key'), \
             patch.object(llm.settings, 'deepseek_api_key', 'other-key'):
            self.assertFalse(health()['llm_key'])
        self.assertTrue(health()['llm_key'])

    def test_scanned_pdf_page_limit_fails_before_paid_calls(self):
        import fitz
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'large.pdf'
            with fitz.open() as document:
                for _ in range(13):
                    document.new_page()
                document.save(path)
            with patch.object(cloudru, 'recognize_page') as ocr:
                with self.assertRaisesRegex(llm.LLMUnavailable, '12'):
                    spec_pdf._raw_positions_from_pdf(path)
                ocr.assert_not_called()

    def test_all_invalid_structured_rows_never_succeed_as_empty_spec(self):
        invalid_rows = [[None], [{'number': 1, 'name': 'Heading'}]]
        invalid_rows.extend([[{'number': number, 'name': 'Cable', 'unit': 'м'}]
                             for number in ('?', 1.5, True, False)])
        for rows in invalid_rows:
            with self.subTest(rows=rows), patch.object(cloudru, 'recognize_page', return_value='page'), \
                 patch.object(spec_pdf, '_structure_positions', return_value=rows):
                with self.assertRaises(llm.ModelOutputInvalid):
                    spec_pdf.parse_spec('offline.png')

    def test_cloudru_compares_independent_local_cells_without_extra_llm(self):
        text = """Позиция Кол-во Единица
y=0.010 | x=0.08:Позиция | x=0.8:Кол-во | x=0.74:Единица
y=0.100 | x=0.08:1 | x=0.45:MT-96D | x=0.74:шт | x=0.8:43
y=0.120 | x=0.08:2 | x=0.74:шт | x=0.8:1
y=0.140 | x=0.08:3 | x=0.74:шт | x=0.8:2"""
        rows = [{'number': 1, 'name': 'Control', 'type_mark': 'M1-96D',
                 'unit': 'шт', 'qty': 4.3, 'review_fields': {'qty': 'model uncertain'}}]
        with patch.object(cloudru, 'recognize_page', return_value='cloud OCR') as paid, \
             patch.object(spec_pdf, '_structure_positions', return_value=rows) as structure, \
             patch.object(spec_pdf, 'ocr_available', return_value=True), \
             patch.object(spec_pdf, 'ocr_image', return_value=text) as local:
            result = spec_pdf._cloudru_positions(['scan.png'])[0]
        self.assertEqual(paid.call_count, 1)
        self.assertEqual(structure.call_count, 1)
        local.assert_called_once_with('scan.png')
        self.assertEqual(result['qty'], 4.3)
        self.assertEqual(result['type_mark'], 'M1-96D')
        self.assertIn('43', result['review_fields']['qty'])
        self.assertIn('model uncertain', result['review_fields']['qty'])
        self.assertIn('MT-96D', result['review_fields']['type_mark'])
        self.assertIn('source', result['review_fields'])

    def test_optional_local_ocr_failure_keeps_provisional_rows_and_source_warning(self):
        for failure in (OSError('sensitive path'), RuntimeError('backend detail'), None):
            rows = [{'number': 1, 'name': 'Cable', 'qty': 7, 'unit': 'м'}]
            with self.subTest(failure=failure), \
                 patch.object(cloudru, 'recognize_page', return_value='cloud OCR') as paid, \
                 patch.object(spec_pdf, '_structure_positions', return_value=rows), \
                 patch.object(spec_pdf, 'ocr_available', return_value=True), \
                 patch.object(spec_pdf, 'ocr_image', side_effect=failure, return_value=''):
                row = spec_pdf._cloudru_positions(['scan.png'])[0]
            self.assertEqual(row['qty'], 7)
            self.assertIn('Независимый локальный OCR', row['review_fields']['source'])
            self.assertNotIn('sensitive', row['review_issue'])
            self.assertNotIn('backend detail', row['review_issue'])
            self.assertEqual(paid.call_count, 1)

    def test_thinking_disabled_only_for_selected_fast_deepseek(self):
        with patch.object(cloudru, 'provider_post', return_value=response()) as post, \
             patch.object(llm.settings, 'cloudru_model_fast', 'deepseek-ai/DeepSeek-V4-Flash'):
            llm.complete_json('Extract')
            self.assertEqual(post.call_args.kwargs['json']['thinking'], {'type': 'disabled'})
            llm.complete_json('Match', smart=True)
            self.assertNotIn('thinking', post.call_args.kwargs['json'])
        with patch.object(cloudru, 'provider_post', return_value=response()) as post, \
             patch.object(llm.settings, 'cloudru_model_fast', 'another-model'):
            llm.complete_json('Extract')
            self.assertNotIn('thinking', post.call_args.kwargs['json'])
