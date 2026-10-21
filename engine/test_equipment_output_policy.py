"""Missing own prices stay literal omissions through real final output builders."""
import io,tempfile,unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch
import fitz
from openpyxl import load_workbook
from engine.models import SpecPosition,PositionResult,PriceOffer,TsnRow
from engine.schemas import PositionIn,BuildRequest,ResearchReportRequest,ResearchReportPositionIn
from engine.equipment_output_policy import *
from engine.final_eligibility import prepare_final_results
from engine import config
from engine.search import base_db

class EquipmentOutputTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  for target,name,value in [(base_db,'BASE_FILE',self.root/'base.json'),(base_db,'CACHE_DIR',self.root),(config,'CACHE_DIR',self.root)]:
   p=patch.object(target,name,value);p.start();self.addCleanup(p.stop)
  self.pos=SpecPosition(number=1,name='Own equipment',type_mark='MODEL',qty=2,unit='шт',is_unique=True,source_evidence={'reader':'native_pdf','cells':{'number':'1','name':'Own equipment','maker':'Графтио','model':'MODEL'}})
 def test_exact_row_manufacturer_only_not_generic_unique(self):
  self.assertTrue(is_graftio_equipment(self.pos))
  for cells in [{'maker':'Other'},{'maker':'Графтио другой'},{'number':'2'},{'name':'Other'},{'model':'DIFFERENT'}]:
   p=self.pos.model_copy(deep=True);p.source_evidence['cells'].update(cells);self.assertFalse(is_graftio_equipment(p))
  p=self.pos.model_copy(update={'source_evidence':{}});self.assertFalse(is_graftio_equipment(p))
  with self.assertRaises(ValueError):prepare_final_results([PositionResult(position=p)])
 def test_exact_native_ten_rows_and_mixed_known_subtotal(self):
  import json
  path=Path(__file__).resolve().parents[2]/'sources/kac-generator/full-local-spec-2026-10-12/native-offline/input.json'
  rows=json.loads(path.read_text())['positions']
  own=[p for p in rows if is_graftio_equipment(SpecPosition(**p))]
  self.assertEqual(len(own),10)
  priced=PositionResult(position=self.pos,offers=[PriceOffer(price_with_vat=123,from_letter=True)])
  missing=PositionResult(position=self.pos.model_copy(update={'number':2,'source_evidence':{'reader':'native_pdf','cells':{'number':'2','name':'Own equipment','maker':'Графтио','model':'MODEL'}}}))
  prepare_final_results([priced,missing]);summary=financial_summary([priced,missing])
  self.assertEqual(summary['known_subtotal'],246);self.assertIsNone(summary['full_total']);self.assertEqual(summary['missing_positions'],[2])
 def test_empty_zero_refused_and_valid_letter(self):
  for offers in [[],[PriceOffer(price_with_vat=0,from_letter=True)],[PriceOffer(price_with_vat=10,from_letter=True,refresh_failed=True)]]:
   r=PositionResult(position=self.pos,offers=offers);prepare_final_results([r]);self.assertEqual(r.offers,[]);self.assertEqual(financial_summary([r])['full_total'],None)
  r=PositionResult(position=self.pos,offers=[PriceOffer(price_with_vat=123,from_letter=True)])
  prepare_final_results([r]);self.assertEqual(r.offers[0].price_with_vat,123);self.assertEqual(financial_summary([r])['full_total'],246)
  with self.assertRaises(ValueError):prepare_final_results([PositionResult(position=SpecPosition(number=2,name='Ordinary'))])
 def test_real_outputs_preserve_blank_numeric_price_row_mark_and_incomplete_total(self):
  from engine.builders.kac_xlsx import build_kac
  from engine.builders.tkp_pdf import build_tkp
  from engine.builders.tsn_xlsx import build_tsn
  from engine.builders.research_report_xlsx import build_research_report
  r=PositionResult(position=self.pos);prepare_final_results([r])
  wb=load_workbook(build_kac({'ЭМ':[r]},'Object',self.root/'kac.xlsx'));sheet=wb['ЭМ (ЭЗС)']
  self.assertEqual(sheet['A9'].value,1);self.assertIn('MODEL',sheet['D9'].value);self.assertIn(MISSING_PRICE,sheet['D9'].value);self.assertIsNone(sheet['G9'].value);self.assertIsNone(sheet['H9'].value)
  self.assertIsNone(wb['Полнота стоимости']['B2'].value)
  pdf,_=build_tkp([r],'Object',self.root/'tkp.pdf')
  with fitz.open(pdf) as doc:text=''.join(p.get_text() for p in doc)
  self.assertIn(MISSING_PRICE,text);self.assertIn('MODEL',text);self.assertIsNone(r.tkp_page)
  tsn,_=build_tsn([TsnRow(number=1,name='Own MODEL',unit='шт',qty=2,tsn_base_price=100,coefficient=2,decision=MISSING_PRICE)],'Object',self.root/'tsn.xlsx')
  ws=load_workbook(tsn).active;self.assertIsNone(ws['H5'].value);self.assertIsNone(ws['I5'].value);self.assertEqual(ws['J5'].value,MISSING_PRICE)
  request=ResearchReportRequest(positions=[ResearchReportPositionIn(**self.pos.model_dump())])
  wb=load_workbook(io.BytesIO(build_research_report(request)));self.assertIn(MISSING_PRICE,wb['Позиции']['I2'].value);self.assertEqual(wb['Позиции']['C2'].value,'MODEL')
 def test_actual_build_policy_keeps_native_review_and_other_unpriced_gates(self):
  from engine import app
  p=PositionIn(**self.pos.model_dump());req=BuildRequest(object_name='Object',positions=[p],skip_search=True,use_tsn=False)
  with patch.object(app,'run_pipeline_async') as launch:
   response=app.build(req);self.assertIn('job_id',response);launch.assert_called_once()
   app.JOBS.pop(response['job_id'],None)
  for p in [p.model_copy(update={'needs_review':True,'review_fields':{'source':'unresolved'}}),PositionIn(number=2,name='Other',is_unique=True)]:
   with patch.object(app,'run_pipeline_async') as launch:
    with self.assertRaises(app.HTTPException):app.build(req.model_copy(update={'positions':[p]}))
    launch.assert_not_called()
 def test_actual_research_no_search_and_pipeline_incomplete_publication(self):
  from engine import app,pipeline
  from engine.search import agent,card_attachments
  row=app._research_row_impl(PositionIn(**self.pos.model_dump()))
  self.assertEqual(row['status'],'skipped');self.assertEqual(row['reason'],MISSING_PRICE)
  p=patch.object(pipeline,'OUTPUT_DIR',self.root);p.start();self.addCleanup(p.stop)
  job=pipeline.JobState('owned','Object')
  with patch.object(agent,'research_position',side_effect=AssertionError('No supplier search')),patch.object(card_attachments,'attach_result',return_value=(None,[])):
   pipeline.run_pipeline(job,pipeline.JobInputs('Object',[PositionResult(position=self.pos)],use_tsn=True),lambda:nullcontext(None))
  self.assertEqual(job.status,'done',job.error);self.assertIn('полная стоимость не определена',job.log[-1]);self.assertEqual(len(job.outputs),3)
  tsn=load_workbook(job.outputs['tsn']).active;self.assertEqual(tsn['J5'].value,MISSING_PRICE);self.assertIn('MODEL',tsn['B5'].value)
