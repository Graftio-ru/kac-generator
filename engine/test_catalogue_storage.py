import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from pydantic import SecretStr
from botocore.stub import Stubber
from unittest.mock import patch
import os

from .search.catalogue_storage import create_image_store, ScreenshotStorageConfigurationError


class CatalogueStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_unconfigured_never_constructs_sdk_or_falls_back_to_llm_key(self):
        settings = SimpleNamespace(cloudru_api_key='fake-llm-key')
        store = create_image_store(self.root, settings=settings,
            client_factory=lambda **kwargs: self.fail('SDK must not be constructed'))
        self.assertIsNone(store.client)

    def test_partial_credentials_fail_closed_without_transport(self):
        with self.assertRaises(ScreenshotStorageConfigurationError):
            create_image_store(self.root, settings=SimpleNamespace(s3_bucket='shots'),
                client_factory=lambda **kwargs: self.fail('SDK must not be constructed'))

    def test_explicit_config_is_reused_and_bounded(self):
        calls = []
        sentinel = object()
        settings = SimpleNamespace(s3_bucket='shots', s3_access_key_id=SecretStr('fake-id'),
            s3_secret_access_key=SecretStr('fake-secret'), s3_endpoint_url='https://s3.cloud.ru')
        def client(**kwargs):
            calls.append(kwargs)
            return sentinel
        store = create_image_store(self.root, settings=settings, client_factory=client)
        self.assertIs(store.client, sentinel)
        self.assertEqual(calls[0]['aws_access_key_id'], 'fake-id')
        self.assertEqual(calls[0]['config']['proxies'], {})
        self.assertEqual(calls[0]['config']['retries']['total_max_attempts'], 3)
        self.assertEqual(store.bucket, 'shots')

    def test_insecure_or_authenticated_endpoint_is_refused(self):
        for endpoint in ('http://s3.test', 'https://user:password@s3.test', 'https://s3.test?secret=x'):
            settings = SimpleNamespace(s3_bucket='shots', s3_access_key_id='fake-id',
                s3_secret_access_key='fake-secret', s3_endpoint_url=endpoint)
            with self.assertRaises(ScreenshotStorageConfigurationError):
                create_image_store(self.root, settings=settings,
                    client_factory=lambda **kwargs: self.fail('SDK must not be constructed'))

    def test_real_sdk_accepts_conditional_upload_without_live_transport(self):
        settings = SimpleNamespace(s3_bucket='shots', s3_access_key_id='fake-id',
            s3_secret_access_key='fake-secret', s3_endpoint_url='https://s3.cloud.ru')
        store = create_image_store(self.root, settings=settings)
        self.addCleanup(store.client.close)
        self.assertEqual(store.client.meta.config.proxies, {})
        with Stubber(store.client) as stub:
            stub.add_response('put_object', {'ResponseMetadata': {'HTTPStatusCode': 200}})
            result = store.retain(b'fixture-png', observation_id='sdk-test',
                url='https://shop.test/card?id=17', observed_at=1000, captured_at=1001)
            self.assertEqual(result['status'], 'stored')
            stub.assert_no_pending_responses()

    def test_sdk_never_reads_ambient_profile_paths(self):
        import botocore.configloader
        original = botocore.configloader.load_config
        paths = []
        def isolated(path):
            paths.append(path)
            self.assertEqual(path, os.devnull)
            return original(path)
        settings = SimpleNamespace(s3_bucket='shots', s3_access_key_id='fake-id',
            s3_secret_access_key='fake-secret', s3_endpoint_url='https://s3.cloud.ru')
        with patch.dict(os.environ, {'AWS_PROFILE': 'must-not-load',
            'AWS_CONFIG_FILE': '/forbidden/aws-config', 'AWS_SHARED_CREDENTIALS_FILE': '/forbidden/aws-credentials'}), \
             patch('botocore.configloader.load_config', side_effect=isolated):
            store = create_image_store(self.root, settings=settings)
            store.client.close()
        self.assertTrue(paths)
