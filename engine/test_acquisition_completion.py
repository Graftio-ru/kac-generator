"""Server completion is exhausted canonical work, independent of price/photo count."""
import copy,hashlib,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from engine.models import SpecPosition
from engine.search.catalogue import CatalogueStore,identity_for_position,CatalogueConflict,require_catalogue_completion
from engine.search.acquisition_frontier import (new_frontier,FrontierController,NeutralRecords,certify_completion,CompletionUnknown,_hash)
from engine.search.acquisition_source_intent import attest,components,digest
from engine.search.discovery_plan import StagedDiscoveryPlan,completion_plan_tasks
from engine.search.article_query_plan import build_article_query_plan


class AcquisitionCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.p=SpecPosition(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)
        self.identity=identity_for_position(self.p);self.hosts=[f's{i}.test' for i in range(36)];self.primary=self.hosts[:29]
        self.store=CatalogueStore(self.root/'catalogue.sqlite3');self.job='009400000001'
        self.admission=self.store.acquire_unknown(self.identity,self.hosts,owner=self.job)
        self.intent=attest({},components(Path(__file__).parent))
        self.context=dict(positions=[self.p.model_dump()],configured_hosts=self.hosts,primary_hosts=self.primary,input_sha256='a'*64)

    def controller(self, root='records'):
        f=new_frontier(job_id=self.job,input_sha256=self.context['input_sha256'],positions=self.context['positions'],configured_hosts=self.hosts,index=0,product_id=self.identity.key,route_intent_sha256=self.intent['legacy_sha256'],acquisition_generation=self.admission.generation,source_intent=self.intent,indexed=True)
        records=NeutralRecords(self.root/root,f);return FrontierController(f,records),records

    def completed(self, *, subset=False, candidates=None, typed=True, dispositions=None, resolve=False, deferred=False, retry=True, provider_failure=False, foreign_observation=False):
        controller,records=self.controller('records-'+str(time.monotonic_ns()))
        queries=build_article_query_plan(self.p.name,self.p.type_mark,article=self.identity.code if self.identity.kind=='article' else '',manufacturer=self.identity.manufacturer,unit=self.p.unit).queries
        planner=StagedDiscoveryPlan(queries,self.primary,self.hosts[29:],strong_article=self.identity.kind=='article')
        observations={};resolved=set();confirmed=set()
        for phase in ('initial','fallback','literal'):
            specs=completion_plan_tasks(planner,phase,observations,confirmed,resolved)
            if subset:
                specs=specs[:1] if phase=='initial' else []
                if specs:specs=[dict(specs[0],hosts=[self.hosts[0]])]
            if phase!='initial':controller.plan_phase(phase,specs)
            else:
                for spec in specs:controller.add_query(spec['phase'],spec['query'],spec['hosts'],spec['strategy'])
            for task in list(controller.value['queries']):
                if task['state']=='done':continue
                tid=task['id'];controller.start_query(tid);state=controller._task(tid)
                items=candidates if candidates and task['hosts']==specs[0]['hosts'] and phase=='initial' else []
                metadata=[dict(host=h,origin='response',window='below_limit',observed_at=time.time(),empty_code='',success=True,candidate_count=sum(i['host']==h for i in items)) for h in task['hosts'] if h not in resolved]
                if provider_failure and phase=='initial' and task is controller.value['queries'][0]:metadata[0].update(success=False,origin='unknown',diagnostics=[dict(code='network_timeout',reason='Closed unavailable provider')])
                if deferred and phase=='initial' and task is controller.value['queries'][0]:
                    m=metadata[0];m.update(success=False,origin='unknown',deferred_check=dict(host=m['host'],status='incomplete',reason='Supplier check deferred: supplier_cooldown',reason_code='supplier_cooldown',health_state='cooldown',failure_code=None,checked_at=None,deferred_at=time.time(),retry_at=None,product_presence='unknown',queries=0,cards=0,errors=0,reused=False))
                response=records.write_response(state['operation_id'],items,metadata);controller.accept_response(tid,response)
                for i,item in enumerate(items):
                    controller.start_card(tid);card=controller._task(tid)['current_card']
                    complete=dict(version=1,disposition='uncertain',evidence={}) if typed else None
                    state='review';observation_id=None
                    if dispositions:
                        kind=dispositions[i];complete=dict(version=1,disposition=kind,evidence={})
                        if kind=='observed':state='observed';observation_id=card['operation_id'] if not foreign_observation else 'f01a9cdc11101829853be2ce462ea4ca'
                        elif kind=='duplicate':state='no_facts'
                    out=records.write_outcome(card['operation_id'],item['url'],item['host'],state=state,observation_id=observation_id,completion=complete)
                    if resolve and observation_id:resolved.add(item['host']);confirmed.add(item['host'])
                    controller.settle_card(tid,out)
                for m in metadata:
                    observations.setdefault(m['host'],{}).update(dict(group_success=m['success'],group_window='below_limit') if task['phase']=='group' else dict(individual_success=m['success'],individual_window='below_limit',individual_candidates=m['candidate_count']))
            if deferred and retry and phase=='initial':
                _,tid=controller.add_query('individual',specs[0]['query'],[self.hosts[0]],'own_only');task=controller._task(tid)
                # Extra retry after complete initial sweep, as in production.
                controller.start_query(task['id']);state=controller._task(task['id'])
                metadata=[dict(host=self.hosts[0],origin='supplier_own_search',window='below_limit',observed_at=time.time(),empty_code='',success=True,candidate_count=0)]
                response=records.write_response(state['operation_id'],[],metadata);controller.accept_response(task['id'],response)
                observations[self.hosts[0]].update(individual_success=True,individual_window='below_limit',individual_candidates=0)
                planner.admitted.add(('own_only',False,self.hosts[0],task['query'].casefold()))
        controller.plan_phase('complete',[])
        return controller.value,records

    def certificate(self, frontier=None, records=None):
        if frontier is None:frontier,records=self.completed()
        return certify_completion(frontier,self.context,records,self.intent,resolve_hosts=lambda ids:(set(),set()))

    def test_complete_scope_certificate_atomic_idempotent_and_final_independent(self):
        cert=self.certificate();self.assertEqual(cert.payload['configured_hosts'],self.hosts)
        self.assertTrue(self.store.finish_acquisition(self.admission,owner=self.job,completion=cert))
        before=self.store.all_snapshot();self.assertTrue(self.store.finish_acquisition(self.admission,owner=self.job,completion=cert));self.assertEqual(before,self.store.all_snapshot())
        require_catalogue_completion(before,[self.p],{0},configured_hosts=self.hosts,primary_hosts=self.primary,components_sha256=digest(self.intent['components']),runtime_sha256=digest({}))
        from engine.final_eligibility import prepare_final_results
        from engine.models import PositionResult
        with self.assertRaises(ValueError):prepare_final_results([PositionResult(position=self.p)])

    def test_missing_table_read_does_not_migrate_and_status_done_not_authority(self):
        self.store.finish_acquisition(self.admission,owner=self.job)
        snapshot=self.store.read_snapshot([self.identity]);self.assertIsNone(snapshot['products'][self.identity.key]['acquisition_completion'])
        with self.store._connection() as db:self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='acquisition_completions'").fetchone())
        with self.assertRaisesRegex(CatalogueConflict,'completion_unknown'):require_catalogue_completion(snapshot,[self.p],{0},configured_hosts=self.hosts,primary_hosts=self.primary,components_sha256=digest(self.intent['components']),runtime_sha256=digest({}))

    def test_one_of_36_frontier_cannot_mint(self):
        f,r=self.completed(subset=True)
        with self.assertRaises(CompletionUnknown):self.certificate(f,r)

    def test_legacy_and_uncertain_outcomes_never_mint_even_with_other_prices(self):
        items=[dict(url='https://s0.test/product/ABC123',host='s0.test',hint={})]
        for typed in (False,True):
            f,r=self.completed(candidates=items,typed=typed)
            with self.assertRaises(CompletionUnknown):self.certificate(f,r)

    def test_foreign_stale_policy_resources_and_plain_dict_cannot_certify(self):
        cert=self.certificate()
        with self.assertRaises(CatalogueConflict):self.store.finish_acquisition(self.admission,owner=self.job,completion=cert.payload)
        self.store.finish_acquisition(self.admission,owner=self.job,completion=cert)
        snapshot=self.store.read_snapshot([self.identity])
        for hosts,primary,component in [(self.hosts[:-1],self.primary,digest(self.intent['components'])),(self.hosts,self.primary[:-1],digest(self.intent['components'])),(self.hosts,self.primary,'f'*64)]:
            with self.assertRaises(CatalogueConflict):require_catalogue_completion(snapshot,[self.p],{0},configured_hosts=hosts,primary_hosts=primary,components_sha256=component,runtime_sha256=digest({}))
        changed=copy.deepcopy(snapshot);changed['products'][self.identity.key]['acquisition']['generation']+=1
        with self.assertRaises(CatalogueConflict):require_catalogue_completion(changed,[self.p],{0},configured_hosts=self.hosts,primary_hosts=self.primary,components_sha256=digest(self.intent['components']),runtime_sha256=digest({}))

    def test_tamper_and_lost_owner_never_mint_or_create_table(self):
        f,r=self.completed();task=f['queries'][0];path=r._path(task['response']['ref']);path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaises(CompletionUnknown):self.certificate(f,r)
        cert=self.certificate();self.store.cancel_acquisition(self.admission,owner=self.job)
        self.assertFalse(self.store.finish_acquisition(self.admission,owner=self.job,completion=cert))
        with self.store._connection() as db:self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='acquisition_completions'").fetchone())

    def test_atomic_changed_payload_runtime_and_source_intent_are_bound(self):
        cert=self.certificate();self.store.finish_acquisition(self.admission,owner=self.job,completion=cert)
        snapshot=self.store.read_snapshot([self.identity])
        with self.assertRaises(CatalogueConflict):
            require_catalogue_completion(snapshot,[self.p],{0},configured_hosts=self.hosts,primary_hosts=self.primary,components_sha256=digest(self.intent['components']),runtime_sha256='f'*64)
        from engine.search.acquisition_frontier import VerifiedCompletion,_COMPLETION_ISSUER
        changed=copy.deepcopy(cert.payload);changed['card_count']+=1;changed['sha256']=_hash({k:v for k,v in changed.items() if k!='sha256'})
        with self.assertRaises(CatalogueConflict):self.store.finish_acquisition(self.admission,owner=self.job,completion=VerifiedCompletion(changed,_COMPLETION_ISSUER))
        self.assertEqual(snapshot,self.store.read_snapshot([self.identity]))

    def test_final_api_and_pipeline_refuse_missing_completion_without_workers(self):
        from engine import app,pipeline
        from engine.schemas import PositionIn,OfferIn,BuildRequest
        from fastapi import HTTPException
        offers=[OfferIn(price_with_vat=i*10,url=f'https://s{i}.test/card',org_name='Manual test seller',inn=str(i)) for i in (1,2,3)]
        position=PositionIn.model_validate(dict(self.p.model_dump(),offers=[o.model_dump() for o in offers]))
        self.store.finish_acquisition(self.admission,owner=self.job)
        snapshot=self.store.read_snapshot([self.identity]);request=BuildRequest(object_name='Closed completion boundary',skip_search=True,positions=[position])
        with patch.object(app,'_catalogue_export_snapshot',return_value=(request,self.store,snapshot,{0})),patch.object(app,'run_pipeline_async') as spawn:
            with self.assertRaises(HTTPException) as error:app.build(request)
            self.assertEqual(error.exception.status_code,422);self.assertEqual(error.exception.detail['code'],'catalogue_completion_required');spawn.assert_not_called()
        job=pipeline.JobState('009400000099','Closed completion boundary')
        inputs=pipeline.JobInputs('Closed completion boundary',[position.to_result()],skip_search=True,use_tsn=False,catalogue_store=self.store,catalogue_snapshot=snapshot,catalogue_known_indices={0})
        with patch.object(pipeline,'build_tkp') as tkp,patch.object(pipeline,'build_kac') as kac:
            pipeline.run_pipeline(job,inputs,lambda: (_ for _ in ()).throw(AssertionError('No browser')))
            self.assertEqual(job.status,'error');self.assertIn('catalogue_completion_required',job.error);tkp.assert_not_called();kac.assert_not_called()

    def test_final_minimum_3_does_not_inherit_search_target_12(self):
        # Controlled renderer/proof counts, not fabricated image qualification.
        from engine import app,coverage
        from engine.schemas import PositionIn
        from types import SimpleNamespace
        cert=self.certificate();self.store.finish_acquisition(self.admission,owner=self.job,completion=cert)
        product=self.store.lookup(self.identity);position=PositionIn.model_validate(self.p.model_dump())
        result=SimpleNamespace(all_offers=[])
        def annotated(row):return dict(row,required_sources=3,comparable_sources=3,quote_status='sufficient')
        with patch('engine.search.catalogue_projection.project_product',return_value=result),patch.object(app,'_offer_to_dict',return_value={}),patch.object(coverage,'annotate_coverage',side_effect=annotated),patch.object(app.settings,'min_sources',12):
            for count in (2,3):
                with patch('engine.search.selected_proofs.final_offers',return_value=[object() for _ in range(count)]):
                    row=app._catalogue_known_row(position,store=self.store,product=product,hosts=self.hosts)
                    self.assertEqual(row['target'],12);self.assertEqual(row['required_sources'],3)
                    self.assertEqual(row['status'],'partial');self.assertEqual(row['catalogue_completion_state'],'recorded')
                    self.assertEqual(row['quote_status'],'proof_pending' if count==2 else 'sufficient')
                    if count==2:self.assertIn('2 из 3',row['reason'])


    def test_capability_payload_is_detached_and_sql_torn_write_rolls_back_ddl(self):
        from engine.search.catalogue import CatalogueStoreError
        cert=self.certificate();original=cert.payload
        exposed=cert.payload;exposed['configured_hosts'].clear();exposed['generation']=999
        self.assertEqual(cert.payload,original)
        with self.store._connection(True) as db:
            db.execute("CREATE TRIGGER deny_done BEFORE UPDATE OF status ON acquisitions WHEN NEW.status='done' BEGIN SELECT RAISE(ABORT,'torn'); END")
        before=self.store.all_snapshot()
        with self.assertRaises(CatalogueStoreError):self.store.finish_acquisition(self.admission,owner=self.job,completion=cert)
        self.assertEqual(before,self.store.all_snapshot())
        with self.store._connection() as db:self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='acquisition_completions'").fetchone())
        with self.store._connection(True) as db:db.execute('DROP TRIGGER deny_done')
        self.assertTrue(self.store.finish_acquisition(self.admission,owner=self.job,completion=cert))
        self.store.validate_completion_snapshot(self.store.all_snapshot(),[self.identity])

    def test_frozen_generation_rejects_reallocation_and_retains_frozen_heads(self):
        cert=self.certificate();self.store.finish_acquisition(self.admission,owner=self.job,completion=cert)
        frozen=self.store.read_snapshot([self.identity]);before=copy.deepcopy(frozen)
        self.store.validate_completion_snapshot(frozen,[self.identity]);self.assertEqual(frozen,before)
        # Simulate a separately authorized new server allocation, not a client DTO.
        with self.store._connection(True) as db:
            db.execute("UPDATE acquisitions SET generation=generation+1,status='running',owner='009400000002' WHERE product_id=?",(self.identity.key,))
        with self.assertRaisesRegex(CatalogueConflict,'generation_changed'):self.store.validate_completion_snapshot(frozen,[self.identity])
        self.assertEqual(frozen,before)
        self.assertIsNone(self.store.lookup(self.identity)['acquisition_completion'])

    def test_resolved_host_skips_fallback_but_cannot_skip_returned_tail(self):
        item=dict(url='https://s0.test/product/ABC123',host='s0.test',hint={})
        f,r=self.completed(candidates=[item],dispositions=['observed'],resolve=True)
        cert=certify_completion(f,self.context,r,self.intent,resolve_hosts=lambda ids:({'s0.test'},{'s0.test'}))
        self.assertEqual(cert.payload['card_count'],1)
        self.assertFalse(any(t['phase']=='individual' and 's0.test' in t['hosts'] for t in f['queries']))
        for kind in ('uncertain','duplicate'):
            tail=dict(item,url=item['url']+'/tail')
            f,r=self.completed(candidates=[item,tail],dispositions=['observed',kind],resolve=True)
            with self.assertRaises(CompletionUnknown):certify_completion(f,self.context,r,self.intent,resolve_hosts=lambda ids:({'s0.test'},{'s0.test'}))

    def test_duplicate_inherits_only_prior_definitive_same_url(self):
        item=dict(url='https://s0.test/product/ABC123',host='s0.test',hint={})
        for kinds,accepted in ((['observed','duplicate'],True),(['duplicate','observed'],False),(['uncertain','duplicate'],False)):
            f,r=self.completed(candidates=[item,item],dispositions=kinds)
            if accepted:self.assertEqual(self.certificate(f,r).payload['card_count'],2)
            else:
                with self.assertRaises(CompletionUnknown):self.certificate(f,r)

    def test_expired_lease_and_foreign_input_refuse_without_migration(self):
        cert=self.certificate()
        context=copy.deepcopy(self.context);context['input_sha256']='e'*64
        f,r=self.completed()
        with self.assertRaises(CompletionUnknown):certify_completion(f,context,r,self.intent,resolve_hosts=lambda ids:(set(),set()))
        with self.store._connection(True) as db:db.execute('UPDATE acquisitions SET lease_until=1 WHERE product_id=?',(self.identity.key,))
        self.assertFalse(self.store.finish_acquisition(self.admission,owner=self.job,completion=cert))
        with self.store._connection() as db:self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='acquisition_completions'").fetchone())

    def test_verified_retry_obligation_only_and_unresolved_deferred_blocks(self):
        f,r=self.completed(deferred=True,retry=True)
        self.assertGreater(self.certificate(f,r).payload['query_count'],1)
        f,r=self.completed(deferred=True,retry=False)
        with self.assertRaises(CompletionUnknown):self.certificate(f,r)
        f,r=self.completed()
        f['frontier_complete']=False;f['planning_phase']='literal';controller=FrontierController(f,r)
        _,tid=controller.add_query('individual','arbitrary extra query',[self.hosts[0]],'own_only');task=controller._task(tid)
        controller.start_query(task['id']);state=controller._task(task['id'])
        manifest=r.write_response(state['operation_id'],[],[dict(host=self.hosts[0],origin='response',window='below_limit',observed_at=time.time(),empty_code='',success=True,candidate_count=0)])
        controller.accept_response(task['id'],manifest);controller.plan_phase('complete',[])
        with self.assertRaises(CompletionUnknown):self.certificate(controller.value,r)


    def test_exact_http_primary_source_witness_not_free_text_or_foreign_source(self):
        import base64,json
        from engine import app
        from engine.search import http_card
        from engine.search.http_identity_preflight import primary_contradiction
        body=b'<main><h1>Cable XYZ999</h1><p>Manufacturer article: XYZ999</p></main>'
        url='https://s0.test/product/ABC123';owned=http_card.main_product(body.decode(),url,admission_only=True)
        decision=primary_contradiction(self.p,owned);self.assertIsNotNone(decision)
        record=dict(version=1,requested_url=url,url=url,observed_at=time.time(),status=200,encoding='utf-8',body_base64=base64.b64encode(body).decode(),body_sha256=hashlib.sha256(body).hexdigest())
        raw=json.dumps(record).encode();ref='http_cards/'+('a'*32)+'.json';path=self.root/ref;path.parent.mkdir();path.write_bytes(raw)
        source=dict(ref=ref,record_sha256=hashlib.sha256(raw).hexdigest(),body_sha256=record['body_sha256'])
        evidence=dict(role='http_primary_identity',code=decision['code'],source=source)
        with patch.object(app,'CACHE_DIR',self.root),patch.object(http_card,'CACHE_DIR',self.root):
            self.assertTrue(app._catalogue_verify_identity_completion(evidence,url,self.p))
            self.assertFalse(app._catalogue_verify_identity_completion(evidence,url+'/foreign',self.p))
            self.assertFalse(app._catalogue_verify_identity_completion(dict(evidence,code='arbitrary_reason'),url,self.p))
            changed=copy.deepcopy(evidence);changed['source']['record_sha256']='f'*64
            self.assertFalse(app._catalogue_verify_identity_completion(changed,url,self.p))



    def test_provider_failure_and_forged_free_text_rejection_do_not_complete(self):
        f,r=self.completed(provider_failure=True)
        with self.assertRaises(CompletionUnknown):self.certificate(f,r)
        item=dict(url='https://s0.test/product/ABC123',host='s0.test',hint={})
        f,r=self.completed(candidates=[item],dispositions=['deterministic_rejection'])
        with self.assertRaises(CompletionUnknown):self.certificate(f,r)

    def test_legacy_manual_unique_letter_and_native_own_rules_stay_separate(self):
        from engine.models import PriceOffer,PositionResult
        from engine.final_eligibility import prepare_final_results
        from engine.equipment_output_policy import is_graftio_equipment
        unique=self.p.model_copy(update={'is_unique':True})
        own=self.p.model_copy(update={'source_evidence':dict(reader='native_pdf',cells=dict(number='1',name='Cable',model='ABC123',article='',maker='Графтио'))})
        self.assertTrue(is_graftio_equipment(own))
        require_catalogue_completion(dict(products={}),[unique,own],{0,1},configured_hosts=self.hosts,primary_hosts=self.primary,components_sha256=digest(self.intent['components']),runtime_sha256=digest({}))
        # External manual rows (not catalogue known) keep existing rules.
        require_catalogue_completion(dict(products={}),[self.p],set(),configured_hosts=self.hosts,primary_hosts=self.primary,components_sha256=digest(self.intent['components']),runtime_sha256=digest({}))
        letter=PositionResult(position=unique,offers=[PriceOffer(price_with_vat=10,from_letter=True)])
        self.assertTrue(prepare_final_results([letter]))
        with self.assertRaises(ValueError):prepare_final_results([PositionResult(position=unique,offers=[])])



    def test_partial_research_export_without_receipt_stays_available(self):
        import io,zipfile
        from engine import app
        from engine.schemas import ResearchReportRequest,ResearchReportPositionIn
        from engine.builders import research_report_xlsx
        self.store.finish_acquisition(self.admission,owner=self.job)
        request=ResearchReportRequest(object_name='Partial closed research',positions=[ResearchReportPositionIn.model_validate(self.p.model_dump())])
        real=research_report_xlsx.build_research_report;outputs=[]
        def capture(value):
            data=real(value);outputs.append(data);return data
        with patch.object(app,'_catalogue_store',return_value=self.store),patch.object(research_report_xlsx,'build_research_report',side_effect=capture),patch.object(app,'_catalogue_completion_preflight',side_effect=AssertionError('Final guard must not block partial research')):
            response=app.research_report(request)
        self.assertIn('spreadsheetml',response.media_type)
        with zipfile.ZipFile(io.BytesIO(outputs[0])) as archive:
            self.assertIsNone(archive.testzip());text=''.join(archive.read(n).decode() for n in archive.namelist() if n.endswith('.xml'))
            self.assertIn('ABC123',text)



    def test_real_sql_foreign_operation_cannot_mint_but_owned_prefix_can(self):
        from engine.search.catalogue import SourceObservation,ImageEvidence
        from engine.search.catalogue_projection import catalogue_scope,project_product
        from engine.offer_selection import offer_is_comparable
        item=dict(url='https://s0.test/product/ABC123',host='s0.test',hint={})
        for foreign in (True,False):
            frontier,records=self.completed(candidates=[item],dispositions=['observed'],foreign_observation=foreign,resolve=True)
            card=next(FrontierController(frontier,records).completed_cards())
            outcome=records._read(card['outcome']);oid=outcome['observation_id'];stamp=time.time()
            observation=SourceObservation(oid,self.identity,item['host'],item['url'],item['url'],stamp,'supplier_rendered','Cable ABC123','ABC123','Cable ABC123; 150 RUB/m',hashlib.sha256(b'Cable ABC123; 150 RUB/m').hexdigest(),150,'RUB','included','м',1,None,'','in_stock','observed','Seller','7842224734',ImageEvidence(oid,item['url'],stamp,'missing',error='fixture_image_missing'),'catalogue-native-v1')
            self.store.append_observation(self.identity.key,observation,acquisition=self.admission,coverage_status='found')
            callbacks=[]
            def resolve(entries):
                callbacks.append(entries);observed=[]
                for entry in entries:
                    record=self.store.resolve_observation(self.identity.key,entry['observation_id'])
                    self.assertIsNotNone(record);o=record['observation']
                    self.assertEqual(o['requested_url'],entry['url']);self.assertEqual(o['supplier_host'],entry['host']);observed.append(o)
                latest={o['url']:o for o in observed}
                product=dict(self.store.lookup(self.identity),heads=[dict(host=o['supplier_host'],url=o['url'],current=o,last_good=None) for o in latest.values()])
                with catalogue_scope(self.store,snapshot=product):
                    result=project_product(self.store,self.identity,self.p,snapshot=product)
                    ready={o.url.split('/')[2] for o in result.all_offers if offer_is_comparable(o,self.p)}
                return {o['supplier_host'] for o in observed},ready
            if foreign:
                self.assertNotEqual(oid,card['operation_id'])
                before=self.store.all_snapshot()
                with self.assertRaisesRegex(CompletionUnknown,'foreign_operation'):certify_completion(frontier,self.context,records,self.intent,resolve_hosts=resolve)
                self.assertEqual(callbacks,[]);self.assertEqual(before,self.store.all_snapshot())
                self.assertEqual(before['products'][self.identity.key]['acquisition']['status'],'running')
                with self.store._connection() as db:self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='acquisition_completions'").fetchone())
            else:
                # Replay settled persisted prefix after observation publication;
                # no recapture or new observation is needed for the SAME operation.
                self.assertEqual(oid,card['operation_id'])
                cert=certify_completion(frontier,self.context,records,self.intent,resolve_hosts=resolve)
                self.assertTrue(callbacks);self.assertEqual(cert.payload['card_count'],1)
                self.assertTrue(self.store.finish_acquisition(self.admission,owner=self.job,completion=cert))
                self.assertIsNotNone(self.store.lookup(self.identity)['acquisition_completion'])



class CompletionDiagnosticTests(unittest.TestCase):
    def run_job(self,*,failure=None,before_refusal=None,duplicates=False,empty=False,seed=False):
        from engine import app,test_catalogue_unknown as fixtures
        from engine.schemas import PositionIn
        self.app=app;self.fixture=fixtures.CatalogueUnknownTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.fixture.hosts[:]=['s0.test'];self.capture_calls=[]
        position=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)
        positions=[position,position.model_copy(update={'number':2,'qty':2})] if duplicates else [position]
        real_projection=app._catalogue_known_row;real_publish=app._publish_catalogue_row;self.committed=[]
        def projection(*a,**kw):
            row=real_projection(*a,**kw);row['diagnostics'].append(dict(code='unrelated_note',reason='Сохранённая отдельная диагностика'))
            if seed:
                jid=next(j for j,v in app._RESEARCH_INPUTS.items() if v is positions)
                row['diagnostics'].append(dict(code='completion_missing_tail',reason=app._COMPLETION_REFUSAL_REASON,failure_source='acquisition_completion',failure_scope='product',failure_cause='completion_unknown',completion_binding=dict(job_id=jid,owner=jid,product_id=row['catalogue_product_id'],generation=row['catalogue_acquisition']['generation'])))
            return row
        def publish(jid,index,row,**kw):
            result=real_publish(jid,index,row,**kw)
            self.committed.append(app._research_store().public(jid,include_events=False)['rows'][index]);return result
        real_certify=certify_completion
        def certification(*a,**kw):
            self.fixture.current_job_id=next(j for j,v in app._RESEARCH_INPUTS.items() if v is positions)
            self.fixture.current_job=app.RESEARCH_JOBS[self.fixture.current_job_id]
            if before_refusal:before_refusal(app,self.fixture)
            if failure is not None:raise failure
            return real_certify(*a,**kw)
        def capture(*a,**kw):self.capture_calls.append(a[3]);return None
        def empty_search(q,hosts,**kw):
            from engine.search.search_engine import DiscoveryURLs
            return DiscoveryURLs([],origin='response',window='below_limit',host_observations={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=0,diagnostics=[]) for h in hosts})
        with patch.object(app,'_catalogue_known_row',side_effect=projection),patch.object(app,'_publish_catalogue_row',side_effect=publish),patch('engine.search.acquisition_frontier.certify_completion',side_effect=certification):
            job,public=self.fixture.job(positions,capture=capture,search=empty_search if empty else None)
        return job,public

    def test_native_refusal_committed_final_reprojection_and_cache_cleared_readback(self):
        import json
        job,public=self.run_job(duplicates=True);app=self.app;jid=job['job_id']
        for row in public['rows']:
            tagged=[d for d in row['diagnostics'] if d.get('failure_source')=='acquisition_completion']
            self.assertEqual([d['code'] for d in tagged],['completion_uncertain_card'])
            self.assertEqual(tagged[0]['completion_binding']['job_id'],jid)
            self.assertTrue(any(d['code']=='unrelated_note' for d in row['diagnostics']))
            self.assertEqual((row['catalogue_completion_state'],row['offers']),('completion_unknown',[]))
        self.assertTrue(any(any(d.get('code')=='completion_uncertain_card' for d in row.get('diagnostics',[])) for row in self.committed))
        self.assertEqual(job['status'],'done')
        product=self.fixture.store.lookup(identity_for_position(SpecPosition(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)))
        self.assertIsNone(product['acquisition_completion']);self.assertEqual((product['acquisition']['status'],product['acquisition']['error']),('done',''))
        root=app.RESEARCH_JOB_DIR.resolve();app.RESEARCH_JOBS.pop(jid);app._RESEARCH_INPUTS.pop(jid)
        app._RESEARCH_STORES.pop(root).db.close()
        for cache in (app._RESEARCH_PUBLISHERS,app._RESEARCH_LIFECYCLES,app._RESEARCH_LIFECYCLE_RETRIES):cache.pop((root,jid),None)
        with patch.object(app,'_launch_research_job',side_effect=AssertionError('Readback cannot launch work')):readback=app.research_status(jid)
        self.assertEqual([r['diagnostics'] for r in readback['rows']],[r['diagnostics'] for r in public['rows']])
        self.assertNotIn('token',json.dumps([r['diagnostics'] for r in readback['rows']]))


    def test_unknown_tainted_oversized_and_nonstring_native_causes_are_safe(self):
        import json
        from engine import app
        class Tainted(str):pass
        class Hostile(CompletionUnknown):
            def __str__(self):raise AssertionError('Never stringify private error')
        for value in sorted(app._COMPLETION_REFUSAL_CODES):self.assertEqual(app._catalogue_completion_reason(CompletionUnknown(value)),value)
        for value in ('SECRET https://private.invalid/key','x'*100000,Tainted('completion_uncertain_card'),{'raw':'SECRET'}):
            job,public=self.run_job(failure=Hostile(value))
            tagged=[d for d in public['rows'][0]['diagnostics'] if d.get('failure_source')=='acquisition_completion']
            self.assertEqual([d['code'] for d in tagged],['completion_evidence_invalid']);self.assertNotIn('SECRET',json.dumps(tagged));self.assertNotIn('private.invalid',json.dumps(tagged))
            self.assertEqual(job['status'],'done')

    def test_owner_product_job_generation_binding_and_unrelated_diagnostics(self):
        job,public=self.run_job();app=self.app;store=self.fixture.store;identity=identity_for_position(SpecPosition(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1));product=store.lookup(identity);previous=public['rows'][0]
        def carried(jid=job['job_id'],current=product,requested=identity):
            row={'diagnostics':[dict(code='target_note',reason='Target separate note')]}
            app._catalogue_completion_diagnostics(row,previous,job_id=jid,store=store,identity=requested,product=current,carry=True)
            self.assertTrue(any(d['code']=='unrelated_note' for d in row['diagnostics']));self.assertTrue(any(d['code']=='target_note' for d in row['diagnostics']))
            return [d for d in row['diagnostics'] if d.get('failure_source')=='acquisition_completion']
        self.assertEqual(len(carried()),1);self.assertEqual(carried(jid='ffffffffffff'),[])
        fresh={'diagnostics':[]}
        with patch.object(store,'lookup',side_effect=AssertionError('Fresh clear requires no extra catalogue read')):
            app._catalogue_completion_diagnostics(fresh,previous,job_id=job['job_id'],store=store,identity=identity,product=product)
        self.assertFalse(any(d.get('failure_source')=='acquisition_completion' for d in fresh['diagnostics']))
        other=SpecPosition(number=3,name='Other',type_mark='DEF456',unit='м',qty=1);other_identity=identity_for_position(other);store.acquire_unknown(other_identity,['s0.test'],owner=job['job_id'])
        self.assertEqual(carried(current=store.lookup(other_identity),requested=other_identity),[])
        with store._connection(True) as db:db.execute('UPDATE acquisitions SET generation=generation+1 WHERE product_id=?',(product['product_id'],))
        self.assertEqual(carried(current=store.lookup(identity)),[]);self.assertEqual(carried(current=product),[])
        with store._connection(True) as db:db.execute("UPDATE acquisitions SET owner='different-owner' WHERE product_id=?",(product['product_id'],))
        self.assertEqual(carried(current=store.lookup(identity)),[])

    def test_fresh_attempt_and_success_clear_only_owned_tag(self):
        job,public=self.run_job(empty=True,seed=True)
        self.assertEqual(job['status'],'done');self.assertTrue(self.committed)
        for row in self.committed+public['rows']:
            self.assertFalse(any(d.get('failure_source')=='acquisition_completion' for d in row['diagnostics']))
            self.assertTrue(any(d['code']=='unrelated_note' for d in row['diagnostics']))
        self.assertEqual(public['rows'][0]['catalogue_completion_state'],'recorded')
        product=self.fixture.store.lookup(identity_for_position(SpecPosition(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)));self.assertIsNotNone(product['acquisition_completion'])

    def test_real_cancellation_outranks_completion_refusal_without_more_work(self):
        counts=[]
        def cancelled(app,f):f.current_job['cancel_requested']=True;counts.append((len(f.calls),len(self.capture_calls)))
        job,public=self.run_job(before_refusal=cancelled)
        self.assertEqual(job['status'],'cancelled');self.assertEqual(counts,[(len(self.fixture.calls),len(self.capture_calls))]);self.assertFalse(any(d.get('failure_source')=='acquisition_completion' for row in public['rows'] for d in row.get('diagnostics',[])))

    def test_real_global_stop_outranks_completion_refusal(self):
        from engine.search.failure_policy import SearchServiceUnavailable
        from engine.llm import _REQUEST_GUARD
        failure=SearchServiceUnavailable('account_access');counts=[]
        def global_stop(app,f):_REQUEST_GUARD.get().reject(failure);counts.append((len(f.calls),len(self.capture_calls)))
        job,public=self.run_job(before_refusal=global_stop)
        self.assertEqual(job['status'],'error');self.assertEqual(job['first_stop']['code'],failure.diagnostic()['code']);self.assertEqual(counts,[(len(self.fixture.calls),len(self.capture_calls))]);self.assertFalse(any(d.get('failure_source')=='acquisition_completion' for row in public['rows'] for d in row.get('diagnostics',[])))

    def test_real_owner_loss_outranks_completion_refusal(self):
        counts=[]
        def lost(app,f):
            with f.store._connection(True) as db:db.execute("UPDATE acquisitions SET owner='different-owner'")
            counts.append((len(f.calls),len(self.capture_calls)))
        job,public=self.run_job(before_refusal=lost)
        self.assertEqual(job['status'],'error');self.assertEqual(counts,[(len(self.fixture.calls),len(self.capture_calls))]);self.assertFalse(any(d.get('failure_source')=='acquisition_completion' for row in public['rows'] for d in row.get('diagnostics',[])))

    def test_inherited_deadline_outranks_completion_refusal(self):
        from engine.search.session import research_session
        original=time.monotonic;offset=[0]
        def expire(app,f):offset[0]=120
        with patch('time.monotonic',side_effect=lambda:original()+offset[0]),research_session(deadline=original()+60):job,public=self.run_job(before_refusal=expire)
        self.assertEqual(job['status'],'error');self.assertFalse(any(d.get('failure_source')=='acquisition_completion' for row in public['rows'] for d in row.get('diagnostics',[])))

if __name__=='__main__':unittest.main()
