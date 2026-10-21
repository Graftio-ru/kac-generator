"""Known row coverage keeps actual native identity without supplier revalidation."""
import asyncio,json,sqlite3,tempfile,unittest,zipfile
from pathlib import Path
from io import BytesIO
from unittest.mock import patch
from openpyxl import load_workbook
from engine import app
from engine.schemas import PositionIn,ResearchReportRequest
from engine.search.catalogue import CatalogueStore

class CatalogueKnownIdentityTests(unittest.TestCase):
 def test_actual_card_store_native23_known_coverage_and_export_no_new_calls(self):
  root=Path(__file__).resolve().parents[2]
  source=root/'sources/kac-generator/catalogue-live-2026-10-13/card-proof-1/catalogue.sqlite3'
  native=json.loads((root/'sources/kac-generator/native-document-notes-2026-10-13/root-native-visual-rework/input.json').read_text())
  p=next(PositionIn.model_validate(v) for v in native['positions'] if v['number']==23)
  with tempfile.TemporaryDirectory() as directory:
   path=Path(directory)/'catalogue.sqlite3'
   with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as original,sqlite3.connect(path) as owned:original.backup(owned)
   store=CatalogueStore(path)
   with patch.object(app,'_catalogue_store',return_value=store),patch.object(app,'_browser_factory',side_effect=AssertionError('No browser')),patch('engine.search.base_db.lookup',side_effect=AssertionError('No legacyprice')),patch('engine.llm.LLMRequestGuard',side_effect=AssertionError('No model')):
    row=app._research_row(p);self.assertEqual(row['found'],1);self.assertEqual(row['comparable_sources'],1);self.assertEqual(row['offers'][0]['price_with_vat'],1000);self.assertEqual(row['offers'][0]['normalized_price_unit'],'м');self.assertEqual(row['source_evidence']['cells'],p.source_evidence['cells']);self.assertEqual(row['full_name'],p.full_name)
    async def data(response):return b''.join([part async for part in response.body_iterator])
    book=load_workbook(BytesIO(asyncio.run(data(app.research_report(ResearchReportRequest(positions=[row]))))))
    matrix=book['Позиции × сайты'];values=list(matrix.values);priced=[r for r in values[1:] if r[6]==1000];self.assertTrue(priced);self.assertEqual(book['Предложения']['G2'].value,1000)
    from engine.builders.research_report_xlsx import _checked_date
    date=_checked_date(row['offers'][0]['catalogue_observed_at']);self.assertEqual(book['Предложения']['Y2'].value,date)
    self.assertEqual(priced[0][20],date);self.assertNotIn('Наблюдение не относится к серверному снимку каталога',str(list(book['Предложения'].values)))
    packed=asyncio.run(data(app.research_package(ResearchReportRequest(positions=[row]))))
    with zipfile.ZipFile(BytesIO(packed)) as archive:
     name=next(n for n in archive.namelist() if n.endswith('.xlsx'));inside=load_workbook(BytesIO(archive.read(name)));self.assertEqual(inside['Предложения']['G2'].value,1000);self.assertEqual(inside['Предложения']['Y2'].value,date);self.assertNotIn('Наблюдение не относится к серверному снимку каталога',str(list(inside['Предложения'].values)))
 def test_alias_maker_mixed_quantity_and_wrong_binding_preserve_normal_guard(self):
  from engine import test_catalogue_maker_aliases as fixtures,config
  f=fixtures.CatalogueMakerAliasTests();f.setUp();self.addCleanup(f.doCleanups);f.store.acquire_unknown(f.identity,f.resources,owner='fixture');f.observe()
  with patch.object(app,'_catalogue_store',return_value=f.store),patch.object(config,'CACHE_DIR',f.root),patch.object(app,'_browser_factory',side_effect=AssertionError('No supplier')):
   for maker in ('DKC','ДКС','dkc'):
    for qty in (1,150):
     p=PositionIn.model_validate(f.position(maker,qty).model_dump());row=app._research_row(p);self.assertEqual(row['found'],1);self.assertEqual(row['comparable_sources'],1);self.assertEqual(row['qty'],qty);self.assertEqual(row['source_evidence']['cells']['maker'],maker)
   p=PositionIn.model_validate(f.position('IEK').model_dump());self.assertIsNone(app._catalogue_known_row(p))
   wrong=app._catalogue_known_row(p,store=f.store,product=f.store.lookup(f.identity));self.assertEqual(wrong['found'],0);self.assertEqual(wrong['comparable_sources'],0)
