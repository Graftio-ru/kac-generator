"""Independent identity lifecycle: no quote or deadline shortcuts."""
import tempfile,time,unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
from engine.search.product_identity import capture_current_identity
from engine.search.failure_policy import ResearchControlError

class IdentityProjectionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.path=Path(self.tmp.name)/'identity.png';self.url='https://seller.example/product/ABC123'
  self.page=MagicMock();self.page.url=self.url;self.page._kac_document_url=self.url;self.page._kac_response_status=200
  self.raw=dict(url=self.url,title='Cable ABC123',text='Cable ABC123\nАртикул: ABC123')
  self.page.evaluate.return_value=self.raw;self.page.inner_text.return_value=self.raw['text'];self.page.title.return_value='Cable'
  self.page.screenshot.side_effect=lambda **kw:Path(kw['path']).write_bytes(b'local fake pixels')
 def capture(self):
  self.refused=[]
  return capture_current_identity(self.page,self.path,self.url,'ABC123','Cable','seller.example','',self.refused.append)
 def test_success_observes_three_current_reads_and_safe_phase_timings(self):
  proof=self.capture();self.assertTrue(proof);self.assertEqual(3,self.page.evaluate.call_count)
  d=self.page._kac_identity_diagnostics;self.assertEqual('captured',d['outcome']);self.assertEqual(2000,d['budget_ms'])
  self.assertEqual({'identity_dom','identity_protection','identity_screenshot'},{r['phase'] for r in d['observations']})
  self.assertNotIn('text',str(d));self.assertNotIn(self.url,str(d))
 def test_slow_verified_second_read_cannot_capture_after_deadline(self):
  clock=[0.0]
  def evaluate(*a):clock[0]+=1.1;return self.raw
  self.page.evaluate.side_effect=evaluate
  with patch('engine.search.product_identity.time.monotonic',side_effect=lambda:clock[0]):
   self.assertFalse(self.capture())
  self.assertEqual(['identity_stability_timeout'],self.refused);self.page.screenshot.assert_not_called()
 def test_changed_postshot_article_refuses(self):
  self.page.evaluate.side_effect=[self.raw,self.raw,{**self.raw,'text':'Артикул: OTHER456'}]
  self.assertFalse(self.capture());self.assertEqual(1,self.page.screenshot.call_count)
  self.assertIn('identity_post_capture_changed',self.refused)
 def test_redirect_and_protection_remain_refused(self):
  self.page.url='https://seller.example/product/OTHER456';self.assertFalse(self.capture());self.page.screenshot.assert_not_called()
  self.page.url=self.url;self.page.inner_text.return_value='Checking your browser verify you are human'
  self.assertFalse(self.capture());self.assertIn('captcha',self.refused);self.page.screenshot.assert_not_called()
 def test_control_cancellation_propagates(self):
  self.page.evaluate.side_effect=ResearchControlError('cancelled')
  with self.assertRaises(ResearchControlError):self.capture()
  self.page.screenshot.assert_not_called()
