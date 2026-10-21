"""Original-image transport contracts without provider requests."""
import base64
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch, MagicMock
from PIL import Image
from engine import llm


class DeepSeekVisionTests(unittest.TestCase):
    def test_image_request_uses_vision_model_pixels_and_json_but_text_models_stay_unchanged(self):
        with TemporaryDirectory() as directory:
            image=Path(directory)/'wrong-extension.png'
            Image.new('RGB',(12,12),'white').save(image,format='JPEG')
            response=MagicMock(status_code=200)
            response.json.return_value={'model':'deepseek-flash','choices':[{'message':{'content':'{"positions":[]}'}}]}
            with patch.object(llm.settings,'deepseek_api_key','offline-test-key'), \
                 patch.object(llm.settings,'deepseek_model_vision','deepseek-flash'), \
                 patch.object(llm.settings,'deepseek_model_fast','text-fast'), \
                 patch.object(llm.settings,'deepseek_model_smart','text-smart'), \
                 patch.object(llm,'provider_post',return_value=response) as post:
                self.assertEqual({'positions':[]},llm._complete_deepseek('Read','',True,123,[str(image)],30))
                request=post.call_args.kwargs['json']
                self.assertEqual('deepseek-flash',request['model'])
                self.assertEqual({'type':'disabled'},request['thinking'])
                self.assertEqual({'type':'json_object'},request['response_format'])
                self.assertIsInstance(request['messages'][0]['content'],str)
                block=request['messages'][1]['content'][1]
                self.assertEqual('original',block['image_url']['detail'])
                self.assertTrue(block['image_url']['url'].startswith('data:image/jpeg;base64,'))
                self.assertEqual(image.read_bytes(),base64.b64decode(block['image_url']['url'].split(',',1)[1]))
                for smart,model in [(False,'text-fast'),(True,'text-smart')]:
                    llm._complete_deepseek('text','',smart,10,None,30)
                    request=post.call_args.kwargs['json']
                    self.assertEqual(model,request['model'])
                    self.assertEqual('text',request['messages'][1]['content'])
                    self.assertNotIn('thinking',request)

    def test_tiff_pixels_are_encoded_as_supported_png(self):
        with TemporaryDirectory() as directory:
            image=Path(directory)/'scan.tiff'
            Image.new('RGB',(12,12),'white').save(image)
            block=llm._deepseek_image_block(image)
            data=base64.b64decode(block['image_url']['url'].split(',',1)[1])
            self.assertTrue(data.startswith(b'\x89PNG'))

    def test_oversized_dimensions_fail_before_provider_call(self):
        with TemporaryDirectory() as directory:
            image=Path(directory)/'long.png'
            Image.new('RGB',(8193,1),'white').save(image)
            with patch.object(llm,'provider_post') as post:
                with self.assertRaisesRegex(llm.LLMUnavailable,'8192'):
                    llm._deepseek_image_block(image)
            post.assert_not_called()

    def test_no_key_does_not_read_images_or_make_request(self):
        with patch.object(llm.settings,'deepseek_api_key',''),patch.object(llm,'provider_post') as post:
            with self.assertRaisesRegex(llm.LLMUnavailable,'API_KEY'):
                llm._complete_deepseek('read','',False,10,['missing.png'],30)
            post.assert_not_called()

    def test_provider_rejection_is_not_silently_replaced_by_text_result(self):
        response=MagicMock(status_code=400,text='unsupported image model')
        with patch.object(llm.settings,'deepseek_api_key','offline-test-key'), \
             patch.object(llm,'_deepseek_image_block',return_value={}), \
             patch.object(llm,'provider_post',return_value=response):
            with self.assertRaisesRegex(llm.LLMUnavailable,'400'):
                llm._complete_deepseek('read','',False,10,['image.png'],30)

    def test_incomplete_or_invalid_json_is_an_explicit_failure(self):
        for content,reason in [('', 'length'),('{"positions":[]}', 'length'),('broken','stop')]:
            with self.subTest(content=content,reason=reason):
                response=MagicMock(status_code=200)
                response.json.return_value={'choices':[{'finish_reason':reason,'message':{'content':content}}]}
                with patch.object(llm.settings,'deepseek_api_key','offline-test-key'), \
                     patch.object(llm,'provider_post',return_value=response):
                    with self.assertRaises(llm.ModelOutputInvalid):
                        llm._complete_deepseek('read','',False,10,None,30)

    def test_visual_provenance_does_not_clear_actual_source_errors(self):
        from engine.models import SpecPosition
        from engine.source_review import recognition_fields
        p=SpecPosition(number=1,name='Бирка',type_mark='ABC123',unit='шт',qty=1,source_reader='vision')
        self.assertFalse(recognition_fields(p,scanned=True))
        p.review_fields={'type_mark':'Разные чтения'}
        self.assertIn('type_mark',recognition_fields(p,scanned=True))
        p.unit='к2'
        self.assertIn('unit',recognition_fields(p,scanned=True))

if __name__=='__main__':
    unittest.main()
