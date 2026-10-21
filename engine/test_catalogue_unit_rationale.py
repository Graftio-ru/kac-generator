"""Display only the existing admitted unit decision; retain purchase context."""
import copy,hashlib,importlib.util,json,shutil,tempfile,unittest
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch
from engine import config,schemas
from engine.models import SpecPosition
from engine.schemas import PositionIn,OfferIn,ResearchReportPositionIn,ResearchReportRequest
from engine.search import catalogue_projection as projection,http_card,factory_reference
from engine.search.catalogue import CatalogueStore,identity_for_position
from engine.builders import research_report_xlsx,research_package
ARTIFACT_BASE=None
OUTPUT_ROOT=None
METRICS={}

class UnitRationaleTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  fixture=Path(ARTIFACT_BASE)/'EPIC-0001-TASK-0217/public-input'
  for name in ['CACHE_DIR','BASE_DIR']:shutil.copytree(fixture/name,self.root/name)
  self.stack=ExitStack();self.addCleanup(self.stack.close)
  for module in [config,schemas,http_card,factory_reference,research_package]:self.stack.enter_context(patch.object(module,'CACHE_DIR',self.root/'CACHE_DIR'))
  self.store=CatalogueStore(self.root/'BASE_DIR/catalogue.sqlite3')
  self.position=PositionIn.model_validate(json.loads((fixture/'parsed.json').read_text())['positions'][0]).to_result().position
  self.identity=identity_for_position(self.position)
  self.owned=self.store.resolve_observation(self.identity.key,'23ceea83b5d0492093965bffd3fe649c')['observation']
 def test_actual_owned_complete_set_rationale_reaches_existing_report(self):
  with projection.catalogue_scope(self.store):
   result=projection.project_product(self.store,self.identity,self.position)
   offer=next(o for o in result.offers if o.catalogue_ref['observation_id']=='23ceea83b5d0492093965bffd3fe649c')
   self.assertIn(self.owned['title'],offer.price_unit_evidence)
   self.assertNotIn('Счёт за 15 минут',offer.price_unit_evidence)
   facts,reason=projection._facts(self.owned,self.position);self.assertFalse(reason);self.assertEqual(facts['unit_basis'],'complete_set');self.assertIn('Бережная упаковка',facts['unit_text'])
   self.assertEqual(sorted(o.price_with_vat for o in result.offers),[5431.06,6595,6982.78])
   data=[]
   for o in result.all_offers:
    raw=o.model_dump(exclude={'requisites'});raw.update(org_name=o.requisites.name,inn=o.requisites.inn,screenshot_product=o.screenshot_product or '',screenshot_requisites=o.screenshot_requisites or '');data.append(OfferIn.model_validate(raw))
   p=ResearchReportPositionIn.model_validate(dict(self.position.model_dump(),offers=data))
   content=research_report_xlsx.build_research_report(ResearchReportRequest(object_name='Unit rationale qualification',positions=[p]))
  from openpyxl import load_workbook
  from io import BytesIO
  workbook=load_workbook(BytesIO(content));sheet=workbook['Предложения'];row=next(r for r in range(2,sheet.max_row+1)if sheet.cell(r,9).value==offer.url)
  self.assertIn(self.owned['title'],sheet.cell(row,20).value);self.assertNotIn('Бережная упаковка',sheet.cell(row,20).value)
  if OUTPUT_ROOT:
   Path(OUTPUT_ROOT).mkdir(parents=True,exist_ok=True);(Path(OUTPUT_ROOT)/'unit-rationale-report.xlsx').write_bytes(content)
  METRICS['actual']={'basis':facts['unit_basis'],'rationale':offer.price_unit_evidence,'raw_context':facts['unit_text'],'prices':[o.price_with_vat for o in result.offers],'observation_id':offer.catalogue_ref['observation_id'],'source_sha256':offer.catalogue_source_sha256,'report_row':row}
 def synthetic(self,title,*,text='Цена: 100 руб / шт',sale='шт',amount=100):
  o=copy.deepcopy(self.owned);o.update(title=title,manufacturer_code='XYZ789',source_text=text,source_sha256=hashlib.sha256(text.encode()).hexdigest(),raw_amount=amount,sale_unit=sale)
  return o,SpecPosition(number=1,name=title,type_mark='XYZ789',unit='компл.',qty=3)
 def test_different_complete_title_and_missing_title_do_not_use_context(self):
  o,p=self.synthetic('Опора XYZ789 в комплекте с монтажными болтами')
  facts,reason=projection._facts(o,p);self.assertFalse(reason);self.assertEqual(facts['unit_basis'],'complete_set');self.assertIn(o['title'],facts['unit_rationale']);self.assertEqual(facts['price'],100)
  for title in ['', 'Опора XYZ789 без комплекта крепежа']:
   o['title']=title;self.assertIsNone(projection._facts(o,p)[0])
 def test_contradictory_price_still_refuses(self):
  o,p=self.synthetic('Опора XYZ789 в комплекте с монтажными болтами',text='Цена: 99 руб / шт')
  self.assertIsNone(projection._facts(o,p)[0])
 def test_verified_factory_singleton_exports_actual_assembly_excerpt(self):
  o=self.store.resolve_observation(self.identity.key,'d29814061d8846039072aa85d6eb15ae')['observation'];facts,reason=projection._facts(o,self.position)
  self.assertFalse(reason);self.assertEqual(facts['unit_basis'],'derived_factory_singleton_kit');self.assertIn(o['unit_equivalence']['assembly_excerpt'],facts['unit_rationale']);self.assertEqual(facts['price'],6595);self.assertEqual(facts['quantity'],1)
 def test_explicit_package_count_exports_its_selected_witness(self):
  o,p=self.synthetic('Комплект XYZ789',text='Цена за упаковку: 200 руб\nВ упаковке: 2 комплекта',sale='упак',amount=200);o['package_quantity']=2
  facts,reason=projection._facts(o,p);self.assertFalse(reason);self.assertEqual(facts['unit_basis'],'explicit_complete_kit_pack');self.assertEqual(facts['unit_rationale'],'В упаковке: 2 комплекта');self.assertEqual(facts['quantity'],2);self.assertEqual(facts['price'],100)
 def test_unknown_or_missing_typed_rationale_is_explicit(self):
  for basis in ['', 'future_unknown','derived_factory_singleton_kit','explicit_complete_kit_pack']:
   value=projection._unit_rationale(basis,title='Marketing',sale_unit='шт',requested_unit='компл.',quantity=1)
   self.assertEqual(value,'Основание пересчёта не сохранено')
 def test_review_observation_and_all_original_records_unchanged(self):
  before=self.store.lookup(self.identity)
  with projection.catalogue_scope(self.store):r=projection.project_product(self.store,self.identity,self.position)
  chip=next(o for o in r.all_offers if 'chipdip.ru'in o.url);self.assertTrue(chip.unit_review_required);self.assertEqual(chip.price_unit_evidence,'')
  self.assertEqual(before,self.store.lookup(self.identity))
 def test_all_retained_heads_financial_fields_and_refusals_match_baseline(self):
  path=Path(ARTIFACT_BASE)/'EPIC-0001-TASK-0214/baseline-catalogue_projection.py'
  spec=importlib.util.spec_from_file_location('engine.search.unit_rationale_baseline',path);baseline=importlib.util.module_from_spec(spec);spec.loader.exec_module(baseline)
  heads=self.store.lookup(self.identity)['heads'];count=0
  for head in heads:
   before,old_reason=baseline._facts(head['current'],self.position);after,new_reason=projection._facts(head['current'],self.position)
   self.assertEqual(old_reason,new_reason)
   self.assertEqual(before,{k:after[k]for k in before}if before is not None else after)
   count+=1
  METRICS['financial_baseline_heads_unchanged']=count
