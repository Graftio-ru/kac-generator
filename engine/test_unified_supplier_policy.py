"""Native offline product-policy controls, not supplier or release measurements."""
import ast, copy, hashlib, json, tempfile, time, unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from openpyxl import load_workbook
from engine import app as application
from engine.models import PriceOffer, Requisites, SpecPosition
from engine.region_policy import region_status, region_notice, region_approval
from engine.offer_selection import offer_is_comparable
from engine.schemas import PositionIn, ResearchReportRequest
from engine.search import agent, whitelist
from engine.search.discovery_plan import StagedDiscoveryPlan, query_sweeps
from engine.search.session import research_session
from engine.builders.research_report_xlsx import build_research_report
from engine.test_region_policy import RegionPolicyTests
from engine.test_catalogue_producer import CatalogueProducerTests
from engine.search.card_batch import CardSnapshot
from engine.search.catalogue_producer import publish_snapshot
from engine.search.catalogue_projection import project_product, catalogue_scope
from engine.test_catalogue_native_roles import NativeRoleTests
from engine.search.catalogue import CatalogueStore, identity_for_position
from engine.search.catalogue_images import CatalogueImageStore
from engine.source_quote import observe_captured_quote

ROOT=Path(__file__).resolve().parents[1]
UNKNOWN={'etm.ru','russvet.ru','minimaks.ru','electro-mpo.ru','220.ru','elektromehanika.ru','thermopat.com'}

class UnifiedSupplierPolicyTests(unittest.TestCase):
 def offer(self,url='https://favorit-el.ru/item',**fields):
  return RegionPolicyTests().offer(url=url,**fields)

 def published(self):
  directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup);root=Path(directory.name)
  store=CatalogueStore(root/'catalogue.sqlite3')
  p=NativeRoleTests().position();identity=identity_for_position(p);url='https://favorit-el.ru/product/ZX-96D';stamp=time.time()
  store.acquire_unknown(identity,['favorit-el.ru'],owner='test')
  proof=NativeRoleTests().proof(p);proof.update(source_url=url,canonical_url=url,host='favorit-el.ru',observed_at=stamp)
  title=proof['main_title'];text=title+'\nАртикул: A51417\nПроизводитель: Maker\n100 руб.\nЦена за1шт'
  facts=dict(url=url,requested_url=url,main_title=title,page_article='A51417',captured_at=stamp,text=text,identity_evidence=proof,screenshot_context='',context_capture_evidence={},seller_reference_snapshot=whitelist.capture_seller_reference(url,stamp))
  evidence=observe_captured_quote(text,actual_url=url,observed_at=stamp,url=url,title=title,article='A51417',listed=100,normalized=100,sale_unit='шт',requested_unit='шт',data={})
  offer=PriceOffer(url=url,price_with_vat=100,listed_price_with_vat=100,price_unit='шт',source_quote_evidence=evidence,requisites=whitelist.requisites_for(url),region_status='incompatible',region_evidence='Самовывоз Казань',in_moscow_region=False)
  observation=publish_snapshot(store,identity,p,CardSnapshot.create(facts),image_store=CatalogueImageStore(root),offer=offer)
  projected=project_product(store,identity,p)
  return store,p,observation,projected

 def test_inventory_36_legal29_unchanged_url_only7_and_no_reserve_reads(self):
  hosts=whitelist.whitelist_hosts();self.assertEqual(len(hosts),36);self.assertEqual(len(set(hosts)),36)
  self.assertEqual(len(whitelist._BY_HOST),29);self.assertEqual(set(hosts)-set(whitelist._BY_HOST),UNKNOWN)
  baseline=ROOT.parent/'baseline/Поставщики.md'
  legal=lambda text:[line for line in text.splitlines() if line.startswith('|') and len(line.split('|'))>4 and line.split('|')[3].strip().isdigit()]
  self.assertEqual(legal(baseline.read_text()),legal((ROOT/'Поставщики.md').read_text()))
  self.assertEqual((ROOT/'Поставщики_резерв_Москва.md').read_bytes(),(ROOT.parent/'baseline/Поставщики_резерв_Москва.md').read_bytes())
  native=Path.open
  def guarded(path,*a,**kw):
   self.assertNotEqual(path.name,'Поставщики_резерв_Москва.md');return native(path,*a,**kw)
  with patch.object(Path,'open',guarded),patch.object(whitelist,'_PRIMARY_REFERENCE_REVISION',None):
   self.assertEqual(whitelist.whitelist_hosts(),hosts);self.assertEqual(whitelist.fallback_hosts(),[])
  for host in UNKNOWN:self.assertIsNone(whitelist.requisites_for('https://'+host));self.assertIsNone(whitelist.seller_reference_for('https://'+host))

 def test_normalization_subdomains_lookalikes_and_provenance(self):
  for url in ['https://www.chipdip.ru/x','https://moscow.chipdip.ru/x','https://CHIPDIP.RU:443/x']:
   proof=whitelist.region_approval_for(url);self.assertEqual(proof['host'],'chipdip.ru');self.assertEqual(proof['source_sha256'],hashlib.sha256((ROOT/'Поставщики.md').read_bytes()).hexdigest())
  for url in ['https://evilchipdip.ru/x','https://chipdip.ru.evil.test/x','https://chipdip.ru@evil.test/x','https://evilwww.chipdip.ru.evil.test','https://www.chipdip.rux/','ftp://chipdip.ru/x','https://[bad','https://evil.test\\@chipdip.ru/x','https://evil.test\\foo@chipdip.ru/x','https://evil.test\t@chipdip.ru/x','https://chipdip.ru\n.evil.test/x','https://user@chipdip.ru/x','https://chipdip.ru:evil/x','https://chipdip.ru:443.evil.test/x']:
   self.assertIsNone(whitelist.region_approval_for(url),url);self.assertIsNone(whitelist.requisites_for(url),url);self.assertIsNone(whitelist.seller_reference_for(url),url)
  self.assertEqual(whitelist.source_type('shop.poisk.im'),'aggregator');self.assertEqual(whitelist.source_type('russvet.ru'),'corporate_site')

 def test_effective_approval_keeps_raw_unknown_negative_city_and_ignores_forged_flags(self):
  for host in whitelist.whitelist_hosts():
   for raw in ['unknown','incompatible']:
    offer=dict(url='https://'+host+'/x',region_status=raw,region_evidence='Самовывоз только Казань',in_moscow_region=False,city='Казань')
    before=copy.deepcopy(offer);self.assertEqual(region_status(offer),'confirmed');self.assertEqual(region_notice(offer),'');self.assertEqual(offer,before)
  forged=dict(url='https://unlisted.test/x',region_status='incompatible',region_evidence='Москва исключена',region_approval=whitelist.region_approval_for('https://chipdip.ru'))
  self.assertEqual(region_status(forged),'incompatible');self.assertIsNone(region_approval(forged))

 def test_native_selection_api_preserves_raw_and_other_financial_guards(self):
  store,position,observed,projected=self.published();self.assertEqual(len(projected.offers),1);offer=projected.offers[0]
  offer.requisites.city='Казань'
  with catalogue_scope(store):
   self.assertTrue(offer_is_comparable(offer,position))
   view=application._offer_to_dict(offer,position=position);self.assertEqual(view['region_approval']['host'],'favorit-el.ru');self.assertEqual(view['region_status'],'incompatible');self.assertIs(view['in_moscow_region'],False);self.assertEqual(view['city'],'Казань')
   for fields in [dict(unit_review_required=True),dict(match_review_required=True),dict(price_with_vat=0),dict(refresh_failed=True)]:
    self.assertFalse(offer_is_comparable(offer.model_copy(update=fields),position))
  for url in ['https://unlisted.test/x','https://favorit-el.ru.evil.test/x']:
   bad=offer.model_copy(update={'url':url});self.assertEqual(region_status(bad),'incompatible')
  anonymous=PriceOffer(url='https://etm.ru/x',price_with_vat=100,listed_price_with_vat=100,price_unit='шт')
  self.assertFalse(offer_is_comparable(anonymous));self.assertEqual(region_status(anonymous),'confirmed')

 def test_new_native_job_and_initial_plan_have_one_full_bucket(self):
  with patch.object(application,'RESEARCH_JOBS',{}),patch.object(application,'_RESEARCH_INPUTS',{}),patch.object(application,'_save_research_job') as save,patch.object(application,'_launch_research_job') as launch:
   jid=application._start_research_job([PositionIn(number=1,name='Item',type_mark='ABC123',unit='шт')]);job=application.RESEARCH_JOBS[jid]
   self.assertEqual(job['configured_hosts'],whitelist.whitelist_hosts());self.assertEqual(job['primary_hosts'],job['configured_hosts']);self.assertEqual(job['fallback_hosts'],[]);save.assert_called_once_with(jid);launch.assert_called_once_with(jid)
   planner=StagedDiscoveryPlan(['literal','literal2'],job['primary_hosts'],job['fallback_hosts'])
   self.assertEqual(set(h for t in planner.initial() for h in t.hosts),set(job['configured_hosts']))
   self.assertEqual(planner.tiers,[job['configured_hosts'],[]])
   self.assertEqual(set(h for _,hs in query_sweeps(['literal','semantic'],job['primary_hosts'],[]) for h in hs),set(job['configured_hosts']))

 def test_standalone_literal_semantic_share_full_hosts_deadline_and_no_reserve(self):
  pos=SpecPosition(number=1,name='Item',type_mark='ABC123',unit='шт');calls=[]
  def phase(*args):calls.append(args);return [],0,[]
  with patch.object(agent,'_validated_retained_offers',return_value=[]),patch.object(agent,'build_queries',side_effect=lambda p,semantic=True:['literal','semantic'] if semantic else ['literal']),patch.object(agent,'_search_phase',side_effect=phase),patch.object(agent,'_search_unrepresented_hosts',return_value=[]),patch.object(agent,'_collect_offers',return_value=([],0)),patch.object(agent,'_record_phase_timing'),patch.object(agent,'_log'),research_session(deadline=time.monotonic()+80):
   agent.find_offers_for_position(None,pos)
  self.assertEqual(len(calls),2)
  self.assertEqual(calls[0][2],['literal']);self.assertEqual(calls[1][2],['semantic'])
  self.assertEqual(calls[0][3],whitelist.whitelist_hosts());self.assertEqual(calls[1][3],calls[0][3]);self.assertEqual(calls[0][5],calls[1][5]);self.assertGreater(calls[0][7],70)

 def test_standalone_control_exception_is_identical_without_extra_phase(self):
  from engine.search.session import SearchBudgetExceeded
  control=SearchBudgetExceeded('synthetic deadline')
  with patch.object(agent,'_validated_retained_offers',return_value=[]),patch.object(agent,'build_queries',return_value=['literal']),patch.object(agent,'_search_phase',side_effect=control) as phase,patch.object(agent,'_log'),research_session(deadline=time.monotonic()+80):
   with self.assertRaises(SearchBudgetExceeded) as raised:agent.find_offers_for_position(None,SpecPosition(number=1,name='Item',type_mark='ABC123',unit='шт'))
  self.assertIs(raised.exception,control);phase.assert_called_once()

 def test_native_observation_projection_keep_negative_raw_region_but_admit_listed(self):
  store,position,observed,projected=self.published()
  self.assertEqual(observed.region_status,'incompatible');self.assertIs(observed.in_moscow_region,False)
  self.assertEqual(store.lookup(identity_for_position(position))['coverage']['favorit-el.ru']['status'],'found')
  self.assertEqual(len(projected.offers),1);self.assertEqual(projected.offers[0].region_status,'incompatible');self.assertEqual(region_notice(projected.offers[0]),'')
  self.assertEqual(projected.offers[0].catalogue_vat_status,'unknown')

 def test_report_has_single_list_and_attributed_region_not_card_confirmation(self):
  offer=application._offer_to_dict(self.offer(in_moscow_region=False,region_status='incompatible',region_evidence='Только Казань'))
  req=ResearchReportRequest(positions=[dict(number=1,name='Item',type_mark='FIXTURE1',unit='шт',offers=[offer],candidates=[offer],research_status='partial')])
  wb=load_workbook(BytesIO(build_research_report(req)))
  cells=[c for row in wb['Предложения'].values for c in row if isinstance(c,str)]
  self.assertTrue(any('допущен по согласованному перечню' in c for c in cells));self.assertFalse(any('Регион поставки не подтверждён' in c for c in cells))
  matrix=list(wb['Позиции × сайты'].values);self.assertEqual(len({r[3] for r in matrix[1:]}),36);self.assertTrue(all(r[4]=='Единый согласованный' for r in matrix[1:]))

 def test_background_publication_and_search_position_return_bound_approval(self):
  store,position,observed,projected=self.published();p=PositionIn.model_validate(position.model_dump())
  row=application._catalogue_known_row(p,store=store)
  self.assertEqual(row['offers'][0]['region_approval']['host'],'favorit-el.ru')
  with tempfile.TemporaryDirectory() as temp,patch.object(application,'RESEARCH_JOB_DIR',Path(temp)),patch.object(application,'RESEARCH_JOBS',{}),patch.object(application,'_RESEARCH_INPUTS',{}),patch.object(application,'_RESEARCH_STORES',{}),patch.object(application,'_RESEARCH_PUBLISHERS',{}),patch.object(application,'_save_research_job'),patch.object(application,'_launch_research_job'):
   jid=application._start_research_job([p]);application._publish_catalogue_row(jid,0,row,terminal=False)
   native=application._research_store().public(jid)['rows'][0]
   for field in ['offers','candidates']:
    self.assertEqual(native[field][0]['region_approval']['host'],'favorit-el.ru');self.assertEqual(native[field][0]['region_status'],'incompatible');self.assertIs(native[field][0]['in_moscow_region'],False)
  with patch.object(application,'_catalogue_store',return_value=store):
   response=application.search_position(application.SearchRequest.model_validate(position.model_dump()))
  self.assertEqual(response['offers'][0]['region_approval']['host'],'favorit-el.ru')

 def test_capture_endpoint_metadata_raw_fields_and_minimum_source_guard(self):
  from contextlib import nullcontext
  from engine.search import card_attachments
  from engine.models import PositionResult
  from engine.final_eligibility import prepare_final_results
  store,position,observed,projected=self.published();offer=projected.offers[0]
  with catalogue_scope(store),patch.object(application,'_browser_factory',side_effect=lambda:nullcontext(None)),patch.object(card_attachments,'capture_current',return_value=(None,offer)):
   response=application.capture(application.CaptureRequest(url=offer.url,name=position.name,type_mark=position.type_mark,unit=position.unit,qty=1))
   self.assertEqual(response['region_approval']['host'],'favorit-el.ru');self.assertEqual(response['region_status'],'incompatible');self.assertIs(response['in_moscow_region'],False)
   with self.assertRaisesRegex(ValueError,'1 из 3'):prepare_final_results([projected])

 def test_native_kac_and_tkp_suppress_listed_region_veto_without_stock_claim(self):
  from engine.builders.kac_xlsx import build_kac
  from engine.builders.tkp_pdf import build_tkp
  import fitz
  store,position,observed,projected=self.published()
  with tempfile.TemporaryDirectory() as tmp,catalogue_scope(store):
   root=Path(tmp);path=build_kac({'ЭМ':[projected]},'Policy fixture',root/'kac.xlsx')
   book=load_workbook(path);self.assertNotIn('Регион поставки',book.sheetnames)
   pdf,_=build_tkp([projected],'Policy fixture',root/'tkp.pdf')
   with fitz.open(pdf) as document:text=''.join(page.get_text() for page in document)
   self.assertNotIn('Регион поставки не соответствует',text);self.assertNotIn('Регион поставки не подтверждён',text)

 def test_recovery_control_preserves_identity_and_prevents_semantic_or_extra_collect(self):
  from engine.search.session import SearchBudgetExceeded
  for mark,maker in [('ABC123',False),('THERMOPAT FM 2K',True)]:
   control=SearchBudgetExceeded('synthetic recovery deadline')
   pos=SpecPosition(number=1,name='Item',type_mark=mark,unit='шт')
   target='_search_queries_on_sites' if maker else '_search_unrepresented_hosts'
   with patch.object(agent,'_validated_retained_offers',return_value=[]),patch.object(agent,'build_queries',return_value=['literal']),patch.object(agent,'whitelist_hosts',return_value=['thermopat.com','chipdip.ru']),patch.object(agent,'_search_phase',return_value=([],0,[])) as phase,patch.object(agent,target,side_effect=control),patch.object(agent,'_collect_offers') as collect,patch.object(agent,'_log'),research_session(deadline=time.monotonic()+80):
    with self.assertRaises(SearchBudgetExceeded) as raised:agent.find_offers_for_position(None,pos)
   self.assertIs(raised.exception,control);phase.assert_called_once();collect.assert_not_called()
