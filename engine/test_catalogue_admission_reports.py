"""Native catalogue admission stops through persisted CONTROL downloads."""
from contextlib import ExitStack
from hashlib import sha256
from io import BytesIO
import sqlite3
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from . import app
from .runner_control import DispatchError, RunnerControl
from .runner_executor import DirectControl, NativeResearchRunner, RunnerExecutionError, _saved_catalogue_candidates
from .runner_protocol import CATALOGUE_ADMISSION_REPORT
from .search import acquisition_scheduler, base_db, catalogue_projection
from .search.catalogue import CatalogueStoreError, identity_for_position
from .search.session import SearchBudgetExceeded
from .telemetry import record_stage
from .test_runner_finalization403 import RunnerFinalization403Tests as FinalizationFixture

BOOK = 'supplier-research-summary.xlsx'
BUNDLE = 'supplier-research-package.zip'
SITE = 'https://one.test/card'


class CatalogueAdmissionReportsTests(unittest.TestCase):
    """Keep genuine catalogue, publication, lease and CONTROL transactions in the test."""

    def exercise(self, fault=None, *, full=False):
        """Make a real revision conflict after settling an earlier price and test its delivery."""
        fixture = FinalizationFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        store = fixture.fixture.store
        saved = fixture.fixture.position
        earlier = saved.model_copy(update={'number':8,'name':'Предыдущая позиция','type_mark':'XYZ456'})
        target = saved.model_copy(update={'number':9,'name':'Проверка допуска','type_mark':'UVW789'})
        expected_identity = identity_for_position(target)
        earlier_identity = identity_for_position(earlier)
        from .search.whitelist import whitelist_hosts
        seed = store.acquire_unknown(expected_identity,whitelist_hosts(),owner='old-owner')
        self.assertTrue(store.finish_acquisition(seed))
        positions = [saved,earlier,target]
        if full:
            positions += [saved.model_copy(update={'number':i,'is_unique':True}) for i in range(10,103)]
        control = fixture.control
        job = control.enqueue({'positions':[pos.model_dump(mode='json') for pos in positions],
                               'acquisition_mode':'missing_data'},'catalogue-admission-test','server-basic:fixture')
        claim = control.claim('fixture-runner',fixture.seal)
        client = DirectControl(control)
        runner = NativeResearchRunner(fixture.root/'runner',client,app,clock=lambda:fixture.now[0],
                                      heartbeat_seconds=10000)
        revoke = store.finish_acquisition
        real_unknown, real_missing = store.acquire_unknown, store.acquire_missing
        real_run, real_save, real_action = app._run_research_job, app._save_research_job, client.action
        actions, earlier_lease, revisions = [], [], []

        def acquire_unknown(identity, resources, **kwargs):
            """Save the earlier product's actual issued lease."""
            grant = real_unknown(identity,resources,**kwargs)
            if identity.key==earlier_identity.key:
                self.assertTrue(grant.allowed)
                earlier_lease.append(grant)
            return grant

        def acquire_missing(identity, resources, **kwargs):
            """Force the tested causal ordering without a sleep or a mock CAS result."""
            self.assertEqual(identity.key,expected_identity.key)
            if fault=='generic':
                raise CatalogueStoreError('fixture generic failure')
            before=kwargs['expected_revision']
            self.assertEqual(store.read_snapshot([])['revision'],before)
            self.assertTrue(store.renew_acquisition(earlier_lease[0],owner=job).allowed)
            after=store.read_snapshot([])['revision']
            self.assertEqual(after,before+1)
            grant=real_missing(identity,resources,**kwargs)
            self.assertEqual((grant.allowed,grant.reason),(False,'snapshot_changed'))
            revisions.append(before)
            return grant

        def needs_work(position, unused_store, product):
            """Read the established priced row and admit the insufficient target."""
            return position.number == target.number

        def fail_cleanup(admission, **kwargs):
            """Model a cleanup error or a refused revocation after the actual no-write refusal."""
            if revisions and fault=='cleanup':
                raise CatalogueStoreError('fixture cleanup failure')
            if revisions and fault=='lost-catalogue-owner':
                return False
            return revoke(admission, **kwargs)

        def lose_native_save(job_id, **kwargs):
            """Inject a lost native lifecycle acknowledgment after SQLite commits."""
            state=app.RESEARCH_JOBS[job_id]
            if state.get('error_code')!='catalogue_store_error':
                return real_save(job_id,**kwargs)
            native_store=app._research_store()

            def lose(boundary):
                """Signal an unknown acknowledgment without undoing the transaction."""
                if boundary=='after_commit':
                    raise TimeoutError('fixture unknown native write')

            with patch.object(native_store,'fault',side_effect=lose):
                return real_save(job_id,**kwargs)

        def native_with_saved_price(job_id):
            """Save real navigation metrics, then prohibit every store read after native exit."""
            with app._catalogue_operation_metrics(job_id,0,lambda:None):
                record_stage('navigation',1.25)
                record_stage('navigation',.75,ok=False)
            real_run(job_id)
            native = app._research_store().public(job)
            self.assertEqual(native['error_code'],'catalogue_store_error')
            if full:
                # The native store exposes the same observation both selected and as a candidate.
                row = native['rows'][0]
                selected = {offer['catalogue_ref']['observation_id'] for offer in row['offers'] if offer['url']==SITE}
                candidates = {offer['catalogue_ref']['observation_id'] for offer in row['candidates'] if offer['url']==SITE}
                self.assertEqual(len(selected & candidates),1)
                approved, = [offer for offer in row['offers'] if offer['url']==SITE]
                self.assertIn('Подтверждено сметчиком',approved['price_unit_evidence'])
            self.assertIs(catalogue_projection.current_catalogue_store(),store)
            stack.enter_context(patch.object(app,'_catalogue_store',side_effect=AssertionError('live catalogue')))
            for method in ('read_snapshot','lookup','resolve_observation','_connection'):
                stack.enter_context(patch.object(store,method,side_effect=AssertionError('catalogue reread')))
            if fault=='lost-lease':
                fixture.now[0]=claim['deadline']

        def action(job_id, name, *args, **kwargs):
            """Capture RPCs and insert cancellation or unknown outcomes at their exact fences."""
            actions.append(name)
            if (fault=='late-cancel' and name=='complete') or (fault=='cancel' and name=='checkpoint'):
                control.cancel(job_id)
            result=real_action(job_id,name,*args,**kwargs)
            if fault=='lost-checkpoint-ack' and name=='checkpoint':
                raise TimeoutError('fixture unknown checkpoint acknowledgment')
            if fault=='lost-complete-ack' and name=='complete':
                raise TimeoutError('fixture unknown complete acknowledgment')
            return result

        with ExitStack() as stack:
            stack.enter_context(patch.object(base_db,'refresh_receipt_snapshot',side_effect=AssertionError('legacy refresh')))
            stack.enter_context(patch.object(app,'_catalogue_image_store',side_effect=AssertionError('S3')))
            stack.enter_context(patch.object(app,'_catalogue_export_snapshot',side_effect=AssertionError('catalogue reprojection')))
            stack.enter_context(patch.object(acquisition_scheduler,'run_products',side_effect=AssertionError('acquisition replay')))
            stack.enter_context(patch.object(app,'_catalogue_missing_required',side_effect=needs_work))
            stack.enter_context(patch.object(store,'acquire_unknown',side_effect=acquire_unknown))
            stack.enter_context(patch.object(store,'acquire_missing',side_effect=acquire_missing))
            stack.enter_context(patch.object(store,'finish_acquisition',side_effect=fail_cleanup))
            stack.enter_context(patch.object(app,'_run_research_job',side_effect=native_with_saved_price))
            stack.enter_context(patch.object(client,'action',side_effect=action))
            if fault=='lost-native-ack':
                stack.enter_context(patch.object(app,'_save_research_job',side_effect=lose_native_save))
            errors={'lost-lease':RunnerExecutionError,'late-cancel':DispatchError,'lost-checkpoint-ack':TimeoutError,
                    'lost-complete-ack':TimeoutError,'lost-native-ack':TimeoutError}
            if fault in errors:
                with self.assertRaises(errors[fault]):
                    runner.execute(claim,fixture.seal)
            else:
                self.assertEqual(runner.execute(claim,fixture.seal)['state'],'error')
        cold=RunnerControl(control.root,fixture.seal,clock=lambda:fixture.now[0])
        with patch.object(app,'_remote_control',return_value=True), patch.object(app,'_runner_store',return_value=cold), TestClient(app.app) as web:
            response=web.get(f'/research/{job}')
            self.assertEqual(response.status_code,200)
            state=response.json()
            downloads=state['downloads']
            self.assertEqual(len(revisions),0 if fault=='generic' else 2)
            if fault in {None,'lost-complete-ack'}:
                self.assertEqual((state['status'],state['partial'],state['business_complete']),('error',True,False))
                self.assertEqual(state['finalization_reason'],CATALOGUE_ADMISSION_REPORT)
                self.assertEqual(state['catalogue_admission_stop'],{
                    'code':'snapshot_changed','job_id':job,'position_index':2,'position_number':9,
                    'expected_revision':revisions[0],'retry_expected_revision':revisions[1],
                    'prior_generation':1,'cleanup_confirmed':True,
                })
                self.assertLess(actions.index('checkpoint'),actions.index('complete'))
                self.check_books(web,downloads,len(positions))
            else:
                self.assertEqual(downloads,[])
                self.assertFalse(state.get('partial') and state['status']=='done')
                if fault in {'cleanup','lost-catalogue-owner','generic'}:
                    if fault == 'generic':
                        self.assertNotIn('catalogue_admission_stop',state)
                    else:
                        self.assertEqual(state['catalogue_admission_stop'], {
                            'code':'snapshot_changed','job_id':job,'position_index':2,'position_number':9,
                            'expected_revision':revisions[0],'retry_expected_revision':revisions[1],
                            'prior_generation':1,'cleanup_confirmed':False,
                        })
                    self.assertNotIn('checkpoint',actions)
                    self.assertTrue(state['report_unavailable'])
                if fault in {'lost-lease','lost-native-ack','lost-checkpoint-ack'}:
                    self.assertNotIn('complete',actions)
            self.assertLessEqual(actions.count('complete'),1)
        if fault in errors:
            with sqlite3.connect(runner.path) as db:
                self.assertEqual(db.execute('SELECT state FROM executions WHERE job=?',(job,)).fetchone()[0],
                                 'uncertain')
            with self.assertRaisesRegex(RunnerExecutionError,'runner_execution_already_attempted'):
                runner.execute(claim,fixture.seal)

    def check_books(self,web,downloads,position_count):
        """Read the standalone and nested Excel after cold GET, keeping normalization unconfirmed."""
        self.assertEqual({entry['name'] for entry in downloads},{BOOK,BUNDLE})
        blobs={}
        for entry in downloads:
            reply=web.get(entry['url'])
            self.assertEqual(reply.status_code,200)
            self.assertEqual((len(reply.content),sha256(reply.content).hexdigest()),
                             (entry['bytes'],entry['sha256']))
            blobs[entry['name']]=reply.content
        with ZipFile(BytesIO(blobs[BUNDLE])) as bundle:
            self.assertIsNone(bundle.testzip())
            embedded=bundle.read(BOOK)
        for data in (blobs[BOOK],embedded):
            book=load_workbook(BytesIO(data),read_only=True,data_only=True)
            try:
                positions = list(book['Позиции'].values)
                self.assertEqual(len(positions)-1,position_count)
                self.assertEqual(positions[1][3],'шт')
                sheet=book['Состояние сбора']
                self.assertEqual(sheet['A2'].value,'Сбор прерван')
                self.assertIn('Каталог изменился',sheet['B2'].value)
                self.assertIn((7,2,2,1),list(book['Время открытия карточек'].values))
                offers = list(book['Предложения'].values)
                self.assertEqual((offers[0][7],offers[0][11]),('Ед. цены','Ед. продажи на сайте'))
                matching = [row for row in offers[1:] if row[8]==SITE]
                self.assertEqual(len(matching),1)
                row, = matching
                self.assertEqual((row[0],row[6],row[7],row[11],row[12],row[16]),
                                 (7,None,'Не подтверждена','уп',100,'Нет'))
                self.assertEqual(offers[0][19],'Основание пересчёта')
                self.assertEqual(row[19],'Основание не перепроверено')
                self.assertIn('пересчёт не подтверждён',row[20])
                self.assertNotEqual(row[24],'Дата не сохранена')
            finally:
                book.close()

    def test_full96_saved_reports_are_visible_without_catalogue_reads(self):
        """Publish both guarded 96-row reports after two real no-write CAS conflicts."""
        self.exercise(full=True)

    def test_recheck_projects_sufficient_snapshot_without_reallocation(self):
        """Use the real sufficiency policy and return its exact detached adequate view."""
        fixture = FinalizationFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        catalogue, position = fixture.fixture.store, fixture.fixture.position
        identity = identity_for_position(position)
        fresh = catalogue.read_snapshot([identity])
        product = fresh['products'][identity.key]
        incomplete = dict(product, heads=product['heads'][:1])
        self.assertTrue(app._catalogue_missing_required(position,catalogue,incomplete))
        self.assertFalse(app._catalogue_missing_required(position,catalogue,product))
        keeper, runner_stop = Mock(), Mock(return_value=False)
        with patch.object(catalogue,'acquire_missing',side_effect=AssertionError('unexpected CAS')):
            admission, snapshot, revision = app._recheck_catalogue_missing(
                position,catalogue,identity,incomplete,fresh['revision']-1,tuple(product['coverage']),
                job_id='abc123abc123',lease_keeper=keeper,check_runner_stop=runner_stop,
            )
        self.assertIsNone(admission)
        self.assertEqual(snapshot,product)
        self.assertEqual(revision,fresh['revision'])
        self.assertEqual(keeper.check_control.call_count,2)
        self.assertEqual(runner_stop.call_count,2)

    def test_recheck_admits_incomplete_target_at_fresh_revision(self):
        """After a real unrelated catalogue write, the same incomplete product receives one new grant."""
        fixture = FinalizationFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        catalogue = fixture.fixture.store
        position = fixture.fixture.position.model_copy(update={'number':8,'type_mark':'PQR789'})
        identity = identity_for_position(position)
        hosts = ('one.test',)
        previous = catalogue.acquire_unknown(identity,hosts,owner='previous-owner')
        self.assertTrue(catalogue.finish_acquisition(previous))
        before = catalogue.read_snapshot([identity])
        other = fixture.fixture.position.model_copy(update={'number':9,'type_mark':'XYZ987'})
        self.assertTrue(catalogue.acquire_unknown(identity_for_position(other),hosts,owner='other-owner').allowed)
        owner = 'abc123abc123'
        with patch.object(catalogue,'acquire_missing',wraps=catalogue.acquire_missing) as acquire:
            refusal = catalogue.acquire_missing(identity,hosts,owner=owner,
                                                expected_revision=before['revision'],mode='missing_data')
            self.assertEqual((refusal.allowed,refusal.reason),(False,'snapshot_changed'))
            admission, _, revision = app._recheck_catalogue_missing(
                position,catalogue,identity,before['products'][identity.key],before['revision'],hosts,
                job_id=owner,lease_keeper=Mock(),check_runner_stop=None,
            )
        self.assertTrue(admission.allowed)
        self.assertEqual((admission.product_id,admission.generation),(previous.product_id,2))
        self.assertEqual([call.kwargs['expected_revision'] for call in acquire.call_args_list],
                         [before['revision'],revision])
        self.assertEqual(catalogue.read_snapshot([])['revision'],revision+1)

    def test_recheck_rejects_identity_and_authority_changes(self):
        """Disappearance, ambiguity and a changed target binding cannot authorize any CAS."""
        fixture = FinalizationFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        catalogue, position = fixture.fixture.store, fixture.fixture.position
        identity = identity_for_position(position)
        view = catalogue.read_snapshot([identity])
        original = view['products'][identity.key]
        changes = {
            'missing':None,'ambiguous':dict(original,identity_ambiguous=True),
            'identity':dict(original,identity=dict(original['identity'],code='changed')),
            'product':dict(original,product_id='a'*64),
            'owner':dict(original,acquisition_owner='other'),
            'generation':dict(original,acquisition=dict(original['acquisition'],generation=2)),
            'scope':dict(original,coverage={}),
        }
        for case, change in changes.items():
            with self.subTest(case=case):
                fresh = dict(view,products={identity.key:change})
                with patch.object(catalogue,'read_snapshot',return_value=fresh), \
                        patch.object(catalogue,'acquire_missing') as acquire:
                    with self.assertRaises(CatalogueStoreError):
                        app._recheck_catalogue_missing(
                            position,catalogue,identity,original,view['revision']-1,tuple(original['coverage']),
                            job_id='abc123abc123',lease_keeper=Mock(),check_runner_stop=None,
                        )
                acquire.assert_not_called()

    def test_recheck_fences_both_reads_and_writes(self):
        """Reject live stop and keeper failure on either side of the fresh read."""
        fixture = FinalizationFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        catalogue, position = fixture.fixture.store, fixture.fixture.position
        identity = identity_for_position(position)
        fresh = catalogue.read_snapshot([identity])
        product = fresh['products'][identity.key]
        for gate in ('runner','keeper'):
            for after_read in (False,True):
                with self.subTest(gate=gate,after_read=after_read):
                    keeper, runner_stop = Mock(), Mock(return_value=False)
                    if gate=='runner':
                        runner_stop.side_effect = [False,True] if after_read else [True]
                        failure = SearchBudgetExceeded
                    else:
                        stop = CatalogueStoreError('fixture latched keeper failure')
                        keeper.check_control.side_effect = [None,stop] if after_read else [stop]
                        failure = CatalogueStoreError
                    with patch.object(catalogue,'read_snapshot',return_value=fresh) as read, \
                            patch.object(app,'_catalogue_missing_required',return_value=True), \
                            patch.object(catalogue,'acquire_missing') as acquire:
                        with self.assertRaises(failure):
                            app._recheck_catalogue_missing(
                                position,catalogue,identity,product,fresh['revision']-1,tuple(product['coverage']),
                                job_id='abc123abc123',lease_keeper=keeper,check_runner_stop=runner_stop,
                            )
                    self.assertEqual(read.call_count,int(after_read))
                    acquire.assert_not_called()

    def test_saved_quote_identity_is_independent_of_url(self):
        """Collapse one observation across lists while retaining different observations at the same URL."""
        product = 'a'*64
        first = {'catalogue_ref':{'product_id':product,'observation_id':'b'*32},
                 'url':SITE,'price_unit':'уп','listed_price_with_vat':100}
        another = dict(first,catalogue_ref={'product_id':product,'observation_id':'c'*32},listed_price_with_vat=110)
        row = {'catalogue_product_id':product,'candidates':[first,another],'offers':[dict(first)]}
        quotes = _saved_catalogue_candidates(row)
        self.assertEqual([(item['catalogue_ref']['observation_id'],item['url'],item['listed_price_with_vat'])
                          for item in quotes],[('b'*32,SITE,100),('c'*32,SITE,110)])

    def test_catalogue_errors_and_unconfirmed_cleanup_never_authorize_reports(self):
        """Protect generic failures, cleanup exceptions and unsuccessful revocation."""
        for fault in ('generic','cleanup','lost-catalogue-owner'):
            with self.subTest(fault=fault):
                self.exercise(fault)
                self.doCleanups()

    def test_lost_fence_and_native_or_control_commit_ack_do_not_replay(self):
        """Keep uncertainty terminal for automatic execution across each write boundary."""
        for fault in ('lost-lease','lost-native-ack','lost-checkpoint-ack','lost-complete-ack'):
            with self.subTest(fault=fault):
                self.exercise(fault)
                self.doCleanups()

    def test_control_requires_two_refusals_and_confirmed_cleanup(self):
        """A matching persisted checkpoint alone cannot authorize absent or invalid retry evidence."""
        fixture = FinalizationFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        job, claim, _ = fixture.setup_job()
        control = fixture.control
        token, fence = claim['lease_token'], claim['fence']
        control.begin(job,token,fence)
        stop = {'code':'snapshot_changed','job_id':job,'position_index':0,'position_number':7,
                'expected_revision':8,'retry_expected_revision':9,'prior_generation':1,'cleanup_confirmed':False}
        raw = b'fixture bytes'
        entry = dict(name=BOOK,sha256=sha256(raw).hexdigest(),bytes=len(raw),
                     content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        control.put_artifact(job,token,fence,entry,raw)
        missing_retry = dict(stop,cleanup_confirmed=True)
        del missing_retry['retry_expected_revision']
        for sequence, receipt in enumerate((stop,missing_retry,
                                            dict(stop,cleanup_confirmed=True,retry_expected_revision=8)),1):
            snapshot = dict(job_id=job,status='error',partial=True,business_complete=False,
                            finalization_reason=CATALOGUE_ADMISSION_REPORT,catalogue_admission_stop=receipt)
            self.assertTrue(control.checkpoint(job,token,fence,sequence,snapshot)['persisted'])
            with self.assertRaisesRegex(DispatchError,'runner_catalogue_report_checkpoint_invalid'):
                control.complete(job,token,fence,[entry],snapshot,'error')
        self.assertEqual(control.public(job)['downloads'],[])

    def test_cancel_before_build_and_inside_final_manifest_commit(self):
        """Require cancellation to win both checkpoint and atomic completion races."""
        for fault in ('cancel','late-cancel'):
            with self.subTest(fault=fault):
                self.exercise(fault)
                self.doCleanups()
