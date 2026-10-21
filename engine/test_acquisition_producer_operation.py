"""Server operation-ID seam; no wire or historical observation import."""
import unittest
from unittest.mock import Mock
from .search.catalogue_producer import capture_and_publish,CataloguePublicationError

class ProducerOperationTests(unittest.TestCase):
 def setUp(self):
  from .test_catalogue_producer import CatalogueProducerTests
  self.fixture=CatalogueProducerTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
 def test_saved_operation_is_immutable_observation_and_image_id(self):
  f=self.fixture;snapshot,offer=f.capture();oid='e'*32
  observation=capture_and_publish(f.store,f.identity,f.pos,f.url,browser=object(),image_store=f.images,
    capture=lambda *a,**k:(snapshot,offer),observation_id=oid)
  self.assertEqual(observation.observation_id,oid);self.assertEqual(observation.image.observation_id,oid)
  self.assertEqual(f.store.resolve_observation(f.identity.key,oid)['observation']['raw_amount'],100)
 def test_invalid_operation_prevents_capture(self):
  f=self.fixture;capture=Mock()
  with self.assertRaises(CataloguePublicationError) as raised:capture_and_publish(f.store,f.identity,f.pos,f.url,browser=object(),image_store=f.images,capture=capture,observation_id='not-server-id')
  capture.assert_not_called();self.assertEqual(raised.exception.code,'catalogue_operation_id_invalid')
 def test_default_retains_generated_id(self):
  f=self.fixture;snapshot,offer=f.capture()
  observation=capture_and_publish(f.store,f.identity,f.pos,f.url,browser=object(),image_store=f.images,capture=lambda *a,**k:(snapshot,offer))
  self.assertEqual(len(observation.observation_id),32)
