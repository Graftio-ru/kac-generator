"""Native finalization refusal controls, invoked by an isolated offline composition.

The supplied fixture must contain current owned images, three independent sellers,
real durable research and CatalogueStore records. No application config is loaded here.
"""
import copy,time,unittest
from unittest.mock import patch


def run_native_controls(app,store,positions,job_id):
    from engine.search.catalogue import identity_for_position
    from engine.search.session import research_session
    from engine.final_eligibility import require_terminal_research
    research=app._research_store()
    snapshot=store.read_snapshot([identity_for_position(p) for p in positions])
    original=research.materialize(job_id,include_events=False)
    product=next(iter(snapshot['products'].values()))
    product_id=product['product_id']
    def final(value=None):
        return app._catalogue_final_preflight(positions,value or snapshot,list(range(len(positions))),store)
    class NativeTerminalControls(unittest.TestCase):
        def test_native_local_admission_and_truth(self):
            self.assertTrue(final())
            self.assertEqual(original['job']['status'],'error')
            self.assertEqual(product['acquisition']['status'],'failed')
            self.assertIsNone(product['acquisition_completion'])
            self.assertEqual(product['coverage']['blocked.example']['product_presence'],'unknown')
        def test_authoritative_lifecycle_stops(self):
            changes=[dict(cancel_requested=True),dict(first_stop={'code':'account_balance','reason':'global stop'}),
                dict(error_code='account_balance'),dict(error='global stop'),dict(status='cancelled'),
                dict(status='running'),dict(awaiting_supplier=True),dict(awaiting_provider=True),
                dict(awaiting_slice=True),dict(catalogue_awaiting_products=[product_id]),dict(completed=0)]
            for change in changes:
                with self.subTest(change=change):
                    value=copy.deepcopy(original);value['job'].update(change)
                    with patch.object(research,'materialize',return_value=value):
                        with self.assertRaises(app.HTTPException):final()
        def test_live_original_deadline_stop(self):
            with research_session(deadline=time.monotonic()-1):
                with self.assertRaises(Exception):final()
        def test_untyped_failed_is_not_admitted(self):
            value=copy.deepcopy(original);value['job'].pop('local_terminal_reasons')
            with patch.object(research,'materialize',return_value=value):
                with self.assertRaises(app.HTTPException):final()
            with self.assertRaises(Exception):store.validate_final_snapshot(snapshot,[identity_for_position(p) for p in positions])
            with self.assertRaises(ValueError):require_terminal_research(original['job'])
        def test_reason_tampering_is_not_authority(self):
            changes=dict(code='failed',owner='0'*12,generation=999,original_deadline=time.time()+99999,
                observed_at=time.time()+99999,frontier_sha256='0'*64,records_sha256='0'*64,
                coverage_sha256='0'*64,module_sha256='0'*64,error='allowed')
            for key,value in changes.items():
                with self.subTest(field=key):
                    altered=copy.deepcopy(original);altered['job']['local_terminal_reasons'][product_id][key]=value
                    with patch.object(research,'materialize',return_value=altered):
                        with self.assertRaises(app.HTTPException):final()
        def test_original_deadline_cannot_be_shortened_in_reason(self):
            altered=copy.deepcopy(original)
            altered['job']['local_terminal_reasons'][product_id]['original_deadline']-=1
            with patch.object(research,'materialize',return_value=altered):
                with self.assertRaises(app.HTTPException):final()
        def test_snapshot_identity_owner_generation_heads(self):
            changes=[lambda p:p.update(identity_ambiguous=True),lambda p:p.update(acquisition_owner='0'*12),
                lambda p:p['acquisition'].update(generation=999),lambda p:p['acquisition'].update(status='done',error=''),
                lambda p:p['heads'][0]['current'].update(observation_id='0'*32),
                lambda p:p['coverage']['blocked.example'].update(product_presence='observed')]
            for change in changes:
                altered=copy.deepcopy(snapshot);change(next(iter(altered['products'].values())))
                with self.subTest(change=repr(change)):
                    with self.assertRaises(app.HTTPException):final(altered)
        def test_pipeline_rechecks_after_api_for_cancel_deadline_and_global(self):
            from engine import pipeline
            from engine.schemas import BuildRequest
            from engine.llm import LLMRequestGuard
            from engine.search.failure_policy import ResearchInternalFailure
            for mode in ('cancel','deadline','global'):
                def late(job,inputs,factory):
                    if mode=='cancel':
                        altered=copy.deepcopy(original);altered['job']['cancel_requested']=True
                        with patch.object(research,'materialize',return_value=altered):pipeline.run_pipeline(job,inputs,factory)
                    elif mode=='deadline':
                        with research_session(deadline=time.monotonic()-1):pipeline.run_pipeline(job,inputs,factory)
                    else:
                        guard=LLMRequestGuard();guard.reject(ResearchInternalFailure('final_output',ValueError('Synthetic global stop')))
                        with research_session(llm_guard=guard):pipeline.run_pipeline(job,inputs,factory)
                with self.subTest(mode=mode),patch.object(app,'run_pipeline_async',side_effect=late):
                    response=app.build(BuildRequest(object_name='TASK238 late stop',positions=positions,skip_search=True,use_tsn=False))
                    status=app.job_status(response['job_id'])
                    self.assertEqual(status['status'],'error');self.assertFalse(status['outputs'])
        def test_active_owner_thread_refuses(self):
            app._RESEARCH_THREADS.add(job_id)
            try:
                with self.assertRaises(app.HTTPException):final()
            finally:app._RESEARCH_THREADS.discard(job_id)
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(NativeTerminalControls)
    result=unittest.TestResult();suite.run(result)
    return dict(tests=result.testsRun,failures=result.failures,errors=result.errors,
                successful=result.wasSuccessful())
