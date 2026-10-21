"""Missing locality is unknown; explicit source locality survives native consumers."""
import tempfile,unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
from engine.models import Requisites,PriceOffer,SpecPosition,PositionResult
from engine.requisites import supplier,dadata
from engine.builders.kac_xlsx import build_kac
from openpyxl import load_workbook
class SupplierCityTests(unittest.TestCase):
 def test_model_roundtrip_empty_and_explicit(self):
  self.assertEqual(Requisites().city,'')
  for city in ['', 'Москва', 'Тверь']:
   self.assertEqual(Requisites.model_validate(Requisites(city=city).model_dump()).city,city)
 def test_deterministic_terms_do_not_invent_locality(self):
  a=supplier._electro_mpo_terms_seller('Продавец: ООО Продавец, ИНН 7707083893')
  b=supplier._etm_published_requisites('Полное наименование\nООО Продавец\nИНН\n7707083893\nКПП\n770701001\nАдрес юридического лица\nМосква, улица 1')
  self.assertIsNotNone(a);self.assertIsNotNone(b);self.assertEqual(a.city,'');self.assertEqual(b.city,'');self.assertIn('Москва',b.address)
 def test_site_extraction_absent_and_explicit_city(self):
  browser=MagicMock();page=browser.page.return_value.__enter__.return_value;page.inner_text.return_value='ООО Продавец ИНН 7707083893';page.url='https://supplier.example/about'
  for data,city in [({},''),({'city':'Москва'},'Москва'),({'city':'Тверь'},'Тверь')]:
   with patch.dict(supplier._cache,{},clear=True),patch.object(supplier,'retain_contextual_seller_terms',return_value=None),patch.object(supplier,'complete_json',return_value={'name':'ООО Продавец','inn':'7707083893',**data}):
    self.assertEqual(supplier.fetch_requisites_from_site(browser,'https://supplier.example/item').city,city)
 def test_dadata_stub_absent_explicit_and_no_token(self):
  with patch.object(dadata.settings,'dadata_token',''):
   self.assertEqual(dadata.fetch_requisites('7707083893').city,'')
  for address in [{},{'city':'Москва'},{'city':'Тверь'},{'region':'Тверская'}]:
   response=MagicMock();response.json.return_value={'suggestions':[{'value':'ООО Продавец','data':{'address':{'data':address,'value':'Legal address fixture'}}}]}
   with patch.object(dadata.settings,'dadata_token','SYNTHETIC_NOT_A_CREDENTIAL'),patch.object(dadata.httpx,'post',return_value=response) as request:
    result=dadata.fetch_requisites('7707083893');self.assertEqual(result.city,'');self.assertEqual(result.address,'Legal address fixture');request.assert_called_once()
 def test_real_kac_city_cells_blank_unknown_preserve_explicit(self):
  offers=[PriceOffer(price_with_vat=100+i,requisites=Requisites(name=f'Seller{i}',city=city),region_status='unknown') for i,city in enumerate(['','Москва','Тверь'])]
  with tempfile.TemporaryDirectory() as tmp:
   path=build_kac({'ЭМ':[PositionResult(position=SpecPosition(number=1,name='Item',unit='шт',qty=1),offers=offers)]},'Local synthetic city control',Path(tmp)/'kac.xlsx')
   sheet=load_workbook(path)['ЭМ (ЭЗС)'];self.assertEqual([sheet.cell(row=r,column=15).value for r in [9,10,11]],[None,'Москва','Тверь'])

 def test_api_offer_city_conversion_and_actual_kac(self):
  from engine.schemas import PositionIn
  for fields,city in [({},None),({'city':'Москва'},'Москва'),({'city':'Тверь'},'Тверь')]:
   result=PositionIn.model_validate({'number':1,'name':'Item','unit':'шт','qty':1,'offers':[{'price_with_vat':100,**fields}]}).to_result()
   self.assertEqual(result.offers[0].requisites.city,city or '')
   with tempfile.TemporaryDirectory() as tmp:
    ws=load_workbook(build_kac({'ЭМ':[result]},'Synthetic API city control',Path(tmp)/'kac.xlsx'))['ЭМ (ЭЗС)']
    self.assertEqual(ws.cell(row=9,column=15).value,city)
 def test_whitelist_identity_without_warehouse_does_not_invent_city(self):
  from engine.search import whitelist
  with patch.object(whitelist,'_primary_record',return_value={'name':'Seller','inn':'7707083893','kpp':'770701001'}):
   result=whitelist.requisites_for('https://synthetic.example/item')
  self.assertEqual(result.city,'');self.assertEqual(result.inn,'7707083893')
