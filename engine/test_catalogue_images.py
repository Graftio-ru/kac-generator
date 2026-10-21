import hashlib
import tempfile
import unittest
from concurrent.futures import CancelledError
from pathlib import Path

from .search.catalogue_images import CatalogueImageStore


class Client:
    def __init__(self, failure=False):
        self.calls = []
        self.failure = failure

    def put_object(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure:
            raise RuntimeError("secret SDK diagnostic")
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


class CatalogueImagesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.arguments = dict(observation_id="quote-1", url="https://shop.test/card?id=17",
                              observed_at=1000, captured_at=1001)

    def test_retains_snapshot_and_s3_binding(self):
        client = Client()
        result = CatalogueImageStore(self.root, client=client, bucket="shots").retain(b"native-png", **self.arguments)
        self.assertEqual(result["status"], "stored")
        self.assertEqual(Path(result["local_path"]).read_bytes(), b"native-png")
        self.assertEqual(result["sha256"], hashlib.sha256(b"native-png").hexdigest())
        self.assertEqual(result["observed_at"], 1000)
        self.assertEqual(result["url"], self.arguments["url"])
        self.assertEqual(client.calls[0]["IfNoneMatch"], "*")
        self.assertEqual(client.calls[0]["Key"], result["key"])

    def test_new_observation_never_overwrites_previous_image(self):
        store = CatalogueImageStore(self.root, client=Client(), bucket="shots")
        first = store.retain(b"before", **self.arguments)
        second = store.retain(b"after", **dict(self.arguments, observation_id="quote-2", observed_at=2000, captured_at=2001))
        self.assertNotEqual(first["key"], second["key"])
        self.assertEqual(Path(first["local_path"]).read_bytes(), b"before")
        self.assertEqual(first["observed_at"], 1000)

    def test_upload_failure_keeps_local_image_without_secret(self):
        result = CatalogueImageStore(self.root, client=Client(True), bucket="shots").retain(b"native-png", **self.arguments)
        self.assertEqual(result["status"], "upload_failed")
        self.assertEqual(result["error"], "s3_upload_failed")
        self.assertEqual(result["key"], "")
        self.assertTrue(Path(result["local_path"]).is_file())
        self.assertNotIn("secret", str(result))

    def test_unconfigured_is_explicit_local_only(self):
        result = CatalogueImageStore(self.root).retain(b"native-png", **self.arguments)
        self.assertEqual(result["status"], "local")
        self.assertEqual(result["error"], "s3_not_configured")

    def test_bad_input_never_uploads(self):
        client = Client()
        store = CatalogueImageStore(self.root, client=client, bucket="shots", max_bytes=3)
        for data, override in ((b"", {}), (b"large", {}), (b"ok", {"observed_at": False}),
                               (b"ok", {"captured_at": float("nan")}), (b"ok", {"url": "file:///tmp/card"})):
            with self.assertRaises(ValueError):
                store.retain(data, **dict(self.arguments, **override))
        self.assertEqual(client.calls, [])

    def test_missing_acknowledgement_is_failure(self):
        class Missing(Client):
            def put_object(self, **kwargs):
                return {}
        result = CatalogueImageStore(self.root, client=Missing(), bucket="shots").retain(b"png", **self.arguments)
        self.assertEqual(result["status"], "upload_failed")

    def test_cancellation_propagates(self):
        class Cancelled(Client):
            def put_object(self, **kwargs):
                raise CancelledError()
        with self.assertRaises(CancelledError):
            CatalogueImageStore(self.root, client=Cancelled(), bucket="shots").retain(b"png", **self.arguments)

    def test_observation_binding_cannot_change(self):
        store = CatalogueImageStore(self.root)
        store.retain(b"png", **self.arguments)
        for override in ({"url": "https://shop.test/other"}, {"observed_at": 2000}, {"captured_at": 2000}):
            with self.assertRaises(ValueError):
                store.retain(b"png", **dict(self.arguments, **override))

    def test_conditional_retry_verifies_existing_object(self):
        class Exists(Exception):
            response = {"ResponseMetadata": {"HTTPStatusCode": 412}}
        class Retry(Client):
            def put_object(self, **kwargs):
                self.metadata = kwargs["Metadata"]
                self.length = len(kwargs["Body"])
                raise Exists()
            def head_object(self, **kwargs):
                return {"ResponseMetadata": {"HTTPStatusCode": 200}, "Metadata": self.metadata, "ContentLength": self.length}
        result = CatalogueImageStore(self.root, client=Retry(), bucket="shots").retain(b"png", **self.arguments)
        self.assertEqual(result["status"], "stored")

    def test_symlink_root_is_refused(self):
        target = self.root / "target"
        target.mkdir()
        link = self.root / "linked"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            CatalogueImageStore(link).retain(b"png", **self.arguments)


if __name__ == "__main__":
    unittest.main()
