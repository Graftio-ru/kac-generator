"""Exercise manifest admission and the exact on-disk Docker context."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from deploy.stage_public_context import (
    DOCKERIGNORE,
    REQUIRED_FILES,
    ROOT_RESOURCES,
    RUNTIME_SOURCE_FILES,
    is_runtime_source,
    stage,
)


class StagePublicContextTests(unittest.TestCase):
    def setUp(self):
        """Prepare a small packet and give each test its own fresh output directory."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.packet = Path(temp.name) / 'source-packet'
        self.output = Path(temp.name) / 'context'
        self.files = {
            name: name.encode() for name in REQUIRED_FILES | ROOT_RESOURCES | {
                'engine/run_spec_research.py', 'web/static/research-status.js',
                'deploy/compose.yaml',
            }
        }
        self.files['deploy/.dockerignore'] = DOCKERIGNORE

    def write_packet(self):
        """Write members with a matching manifest and return its trusted digest."""
        files = {}
        for name, data in self.files.items():
            member = self.packet / 'autosmetchik' / name
            member.parent.mkdir(parents=True, exist_ok=True)
            member.write_bytes(data)
            files[name] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        raw = json.dumps({'runtime_source_files': files}).encode()
        (self.packet / 'source-manifest.json').write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def test_context_contains_only_manifest_members_and_build_metadata(self):
        """A working tree file outside the accepted inventory cannot join the context."""
        manifest_sha = self.write_packet()
        (self.packet / 'autosmetchik' / 'engine' / 'unlisted.py').write_text('unlisted')
        self.assertEqual(stage(self.packet, manifest_sha, self.output), len(self.files))
        staged = {
            file.relative_to(self.output / 'public').as_posix(): file.read_bytes()
            for file in (self.output / 'public').rglob('*') if file.is_file()
        }
        self.assertEqual(staged, self.files)
        self.assertEqual({p.name for p in self.output.iterdir()}, {
            'public', 'source-binding.json', '.dockerignore', 'Dockerfile',
        })
        for file in ('.dockerignore', 'Dockerfile'):
            self.assertEqual((self.output / file).read_bytes(), self.files['deploy/' + file])

    def test_rejects_pinned_nonruntime_members_without_creating_context(self):
        """Valid hashes do not authorize test source, test data or local files."""
        for name in (
            'engine/test_supplier_audit.py', 'web/test-research-poll.cjs',
            'web/static/supplier.spec.js', 'engine/search/queue_test.py',
            'engine/testdata/atomic-service/spec.pdf', 'engine/fixtures/inputs.py',
            'web/tests/fixtures.js', 'web/TESTS/fixtures.js', 'web/test.js',
            'engine/search/conftest.py', 'engine/search/fixtures.py', 'engine/benchmark_spec.py',
            'engine/import_factory_reference.py', 'engine/.env.example', 'docs/STATUS.md',
        ):
            with self.subTest(name=name):
                self.files[name] = b'pinned but forbidden'
                with self.assertRaisesRegex(ValueError, 'nonruntime_manifest_member'):
                    stage(self.packet, self.write_packet(), self.output)
                self.assertFalse(self.output.exists())
                del self.files[name]

    def test_rejects_changed_member_without_creating_context(self):
        """A runtime-looking member cannot bypass its accepted content pin."""
        manifest_sha = self.write_packet()
        (self.packet / 'autosmetchik' / 'engine' / 'app.py').write_text('modified')
        with self.assertRaisesRegex(ValueError, 'source_member_hash_mismatch'):
            stage(self.packet, manifest_sha, self.output)
        self.assertFalse(self.output.exists())

    def test_each_pinned_draft_is_rejected_before_creating_context(self):
        """Reject the review counterexamples alone and together, even with accepted pins."""
        engine_draft = 'engine/scratch/replay.py'
        web_draft = 'web/drafts/offline-preview.html'
        valid_files = self.files
        for drafts in ((engine_draft,), (web_draft,), (engine_draft, web_draft),
                       ('engine/search/replay.py',), ('web/static/offline-preview.html',)):
            with self.subTest(drafts=drafts):
                self.files = valid_files | dict.fromkeys(drafts, b'ordinary draft')
                for draft in drafts:
                    self.assertFalse(is_runtime_source(draft))
                with self.assertRaisesRegex(ValueError, f'nonruntime_manifest_member: {drafts[0]!r}'):
                    stage(self.packet, self.write_packet(), self.output)
                self.assertFalse(self.output.exists())

    def test_stages_complete_inventory_with_repository_bytes(self):
        """Every approved member is present on disk and survives staging byte-for-byte."""
        repository = Path(__file__).resolve().parents[1]
        self.files = {name: (repository / name).read_bytes() for name in RUNTIME_SOURCE_FILES}
        self.assertEqual(stage(self.packet, self.write_packet(), self.output), len(self.files))
        staged = {
            file.relative_to(self.output / 'public').as_posix(): file.read_bytes()
            for file in (self.output / 'public').rglob('*') if file.is_file()
        }
        self.assertEqual(staged, self.files)

    def test_rejects_manifest_pinned_to_a_different_dockerignore(self):
        """A valid release pin cannot switch Docker to a looser context policy."""
        self.files['deploy/.dockerignore'] = DOCKERIGNORE + b'!public/web/drafts/offline-preview.html\n'
        with self.assertRaisesRegex(ValueError, 'runtime_dockerignore_mismatch'):
            stage(self.packet, self.write_packet(), self.output)
        self.assertFalse(self.output.exists())
