"""Native document notes do not authorize source/row/quote uncertainty."""
import copy,hashlib,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from pydantic_settings import BaseSettings
_init=BaseSettings.__init__
def safe(obj,*a,**kw):kw['_env_file']=None;return _init(obj,*a,**kw)
with patch.object(BaseSettings,'__init__',safe):
 from engine.parsers.spec_pdf import parse_spec
 from engine.source_notes import *
 from engine.source_review import recognition_fields,review_pending
 from engine.schemas import PositionIn,ResearchReportRequest,ResearchReportPositionIn
 from engine.models import SpecPosition,PositionResult
 from engine.equipment_output_policy import is_graftio_equipment

ROOT=Path(__file__).resolve().parents[2]
class SourceNoteTests(unittest.TestCase):
 def fixture(self):
  old=json.loads((ROOT/'sources/kac-generator/full-local-spec-2026-10-12/native-offline/input.json').read_text())['positions']
  path=ROOT/'sources/kac-generator/full-local-spec-2026-10-12/spec.pdf'
  self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),'66f1ac632f39f8514fa8c7d05c3a2ec3945a211b7c19f1f010934591d7d5fb4b')
  outcomes=[]
  with patch('engine.parsers.spec_pdf._structure_positions',side_effect=AssertionError('No model')):
   current=parse_spec(path,page_outcomes=outcomes)
  return old,current,outcomes
 def test_fresh_native_96_672_fields_ten_own_thirteen_notes_no_false_row_flags(self):
  old,current,outcomes=self.fixture();self.assertEqual(len(current),96)
  fields=['number','name','type_mark','unit','qty','is_unique','discipline']
  self.assertEqual([[p[k] for k in fields] for p in old],[[getattr(p,k) for k in fields] for p in current])
  self.assertEqual(next(p.qty for p in current if p.number==22),2117)
  self.assertEqual(sum(is_graftio_equipment(p) for p in current),10)
  notes=document_notes(current);self.assertEqual(len(notes),13)
  before={json.dumps(n,sort_keys=True) for p in old for n in p['source_evidence'].get('unassigned_text',[])}
  after={json.dumps({k:n[k] for k in ('page','table','row','text','bbox')},sort_keys=True) for n in notes if n.get('original_cell','name')=='name'}
  self.assertEqual(before,after)
  self.assertEqual(sum(n.get('original_cell')=='number' for n in notes),4);self.assertTrue(all(o['status']=='done' for o in outcomes))
  self.assertEqual(sum(len(o['document_notes']) for o in outcomes),13)
  self.assertFalse(any('source' in recognition_fields(p) for p in current))
 def test_unnumbered_material_only_cells_never_become_notes_or_silent_omission(self):
  from engine.test_native_pdf import NativePdfTests
  from engine.parsers.spec_pdf import _native_table_positions
  for field,index,value in [('model',4,'M1'),('article',5,'SKU1'),('unit',2,'pcs'),('maker',6,'Vendor'),('qty',3,'3'),('number',0,'0')]:
   with self.subTest(field=field):
    fixture=NativePdfTests();unknown=['']*7;unknown[index]=value
    try:
     path=fixture.pdf(['Position','Description','Unit','Qty','Model','SKU','Maker'],[['1','Cable','m','10','MODEL','SKU','Vendor'],unknown])
     outcomes=[];self.assertIsNone(_native_table_positions(path,outcomes));self.assertEqual(outcomes,[])
    finally:fixture.doCleanups()
 def test_number_text_only_retained_unknown_but_material_or_numeric_rows_not_waived(self):
  from engine.test_native_pdf import NativePdfTests
  from engine.parsers.spec_pdf import _native_table_positions
  headers=['Position','Description','Unit','Qty','Model','SKU','Maker']
  fixture=NativePdfTests()
  try:
   path=fixture.pdf(headers,[['1','Cable','m','10','MODEL','SKU','Vendor'],['Unknown text','','','','','','']])
   outcomes=[];rows=_native_table_positions(path,outcomes)
   self.assertEqual(len(rows),1);note=outcomes[0]['document_notes'][0]
   self.assertEqual(note['code'],NUMBER_TEXT_CODE);self.assertEqual(note['original_cell'],'number');self.assertEqual(note['text'],'Unknown text');self.assertEqual(outcomes[0]['status'],'done')
  finally:fixture.doCleanups()
  for index,value in [(1,'Name'),(2,'pcs'),(3,'5'),(4,'MODEL'),(5,'SKU'),(6,'Vendor')]:
   fixture=NativePdfTests();row=['Unknown text','','','','','',''];row[index]=value
   try:self.assertIsNone(_native_table_positions(fixture.pdf(headers,[['1','Cable','m','10','MODEL','SKU','Vendor'],row])))
   finally:fixture.doCleanups()
  fixture=NativePdfTests()
  try:
   rows=_native_table_positions(fixture.pdf(headers,[['1','Known','','','','','']]))
   self.assertIn('qty',rows[0]['review_fields']);self.assertIn('unit',rows[0]['review_fields']);self.assertEqual(rows[0]['_source_evidence'].get('document_notes',[]),[])
  finally:fixture.doCleanups()
 def test_old_draft_source_flags_are_not_cleared_or_grandfathered(self):
  old,_,_=self.fixture()
  flagged=[p for p in old if p.get('review_fields',{}).get('source')]
  self.assertEqual(len(flagged),68)
  for row in flagged:
   p=PositionIn(**{**row,'needs_review':True})
   self.assertTrue(review_pending(p));self.assertEqual(document_notes([p]),[])
 def test_typed_note_only_malformed_and_untrusted_legacy_not_reclassified(self):
  note=native_name_only_note({'page':1,'table':0,'row':2,'text':'Unknown category','bbox':[1,2,30,40]})
  self.assertTrue(valid_document_note(note))
  for key,value in [('page',True),('table',-1),('bbox',[1,2,float('nan'),4]),('bbox',[1,2,1,4]),('text',None),('producer','ocr'),('scope','row')]:
   bad={**note,key:value};self.assertFalse(valid_document_note(bad))
  p=SpecPosition(number=1,name='Known',unit='m',qty=1,review_fields={'qty':'Bad quantity'},source_evidence={'reader':'native_pdf','document_notes':[note]})
  self.assertEqual(recognition_fields(p),{'qty':'Bad quantity'});self.assertEqual(len(document_notes([p,p])),1)
 def test_real_exports_retain_all_notes_without_becoming_quote_or_row_warnings(self):
  from engine.builders.kac_xlsx import build_kac
  from engine.builders.tkp_pdf import build_tkp
  from engine.builders.research_report_xlsx import build_research_report
  from openpyxl import load_workbook
  import fitz
  _,positions,_=self.fixture();results=[PositionResult(position=p) for p in positions]
  with tempfile.TemporaryDirectory() as d:
   d=Path(d);wb=load_workbook(build_kac({'ЭМ':results},'Structural fixture; no price claim',d/'kac.xlsx'))
   self.assertEqual(wb['Предупреждения к документу'].max_row,14)
   pdf,_=build_tkp(results,'Structural fixture; no price claim',d/'tkp.pdf')
   with fitz.open(pdf) as doc:text=''.join(p.get_text() for p in doc)
   self.assertIn('Предупреждения к документу',text)
   for note in document_notes(positions):self.assertIn(' '.join(note['text'].split()),' '.join(text.split()))
   request=ResearchReportRequest(positions=[ResearchReportPositionIn(**p.model_dump()) for p in positions])
   with patch('engine.search.base_db.refresh_receipt_snapshot',return_value={}):wb=load_workbook(io.BytesIO(build_research_report(request)))
   self.assertEqual(wb['Предупреждения к документу'].max_row,14)
   self.assertTrue(all(not p.review_fields.get('source') for p in positions));self.assertTrue(all(not r.offers for r in results))
