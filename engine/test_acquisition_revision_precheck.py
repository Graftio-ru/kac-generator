"""Cheap precheck must retain authoritative input/CAS checks at commit."""
import json,unittest,uuid
from unittest.mock import patch
from engine import app,test_catalogue_acquisition_resume as fixtures
from engine.research_store import Invalid,Conflict

class RevisionPrecheckTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ResumeTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.store=app._research_store();self.publisher=app._research_publisher(self.f.job)

    def next(self):
        return self.f.controller.start_query(self.f.controller.value['queries'][0]['id'])

    def test_checkpoint_does_not_copy_context_but_verifies_input_at_commit(self):
        with patch.object(self.store,'acquisition_context',side_effect=AssertionError('No full-spec context for revision only')),patch.object(self.store,'_acquisition_input',wraps=self.store._acquisition_input) as verify:
            self.f.checkpoint(self.next())
        self.assertGreaterEqual(verify.call_count,1)
        frontier=self.store.load_acquisition_frontier(self.f.job,0)['frontier']
        self.assertEqual(frontier['queries'][0]['state'],'running')

    def test_cancel_between_precheck_and_commit_is_refused(self):
        original=self.store.acquisition_revision
        previous=self.store.load_acquisition_frontier(self.f.job,0)['frontier']
        def race(job):
            revision=original(job)
            self.publisher.lifecycle(uuid.uuid4().hex,cancel_requested=True)
            return revision
        with patch.object(self.store,'acquisition_revision',side_effect=race),self.assertRaises(Conflict):
            self.f.checkpoint(self.next())
        current=json.loads(self.store.db.execute('SELECT payload FROM rows WHERE job=? AND idx=0',(self.f.job,)).fetchone()[0])['_catalogue_acquisition_frontier']
        self.assertEqual(current,previous)

    def test_corrupt_input_cannot_be_admitted_by_header_only_precheck(self):
        previous=self.store.load_acquisition_frontier(self.f.job,0)['frontier']
        # Owned disposable fixture only: simulate corruption outside supported API.
        self.store.db.execute('DROP TRIGGER immutable_input_update')
        self.store.db.execute('UPDATE inputs SET payload=? WHERE job=?',('{}',self.f.job))
        self.assertIsInstance(self.store.acquisition_revision(self.f.job),int)
        with self.assertRaises(Invalid):self.f.checkpoint(self.next())
        current=json.loads(self.store.db.execute('SELECT payload FROM rows WHERE job=? AND idx=0',(self.f.job,)).fetchone()[0])['_catalogue_acquisition_frontier']
        self.assertEqual(current,previous)

if __name__=='__main__':unittest.main()
