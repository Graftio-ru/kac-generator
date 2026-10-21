"""Pure rejection-only primary-product gate. Never admits financial facts."""
import re

VERSION='http-primary-contradiction-v1'
_CONFLICTS={
 'Артикул производителя основного товара противоречит спецификации',
 'Мощность ВА основного товара противоречит спецификации',
 'Класс точности основного товара противоречит спецификации',
 'Электрические параметры основного товара противоречат запрошенному исполнению',
 'Интерфейс основного товара явно отсутствует или не поддерживается',
}

def _explicit_power_conflict(position,owned):
 # Negative identity evidence is independent of article/price confirmation.
 # The caller supplies the already scoped main H1, never a document/URL title.
 from .product_identity import _native_constraints
 from decimal import Decimal
 from ..source_quote import primary_text
 binding=_native_constraints(position)
 if not binding:return False
 def ratings(text):
  return {Decimal(v.replace(',','.')) for v in re.findall(r'(?<![\w.,-])(\d+(?:[.,]\d+)?)\s*(?:ВА|VA)(?!\w)',text,re.I)}
 def values(text):
  # Accept a complete rating field, never an arbitrary numeric suffix. Product
  # identifiers and complete accuracy-class fields delimit supported clauses.
  text=re.sub(r'(?<=\d),(?=\d)', '.',text)
  clauses=re.split(r'[,;]|(?<!\w)(?:класс(?:\s+точности)?|accuracy\s+class)\s*[:：-]?\s*\d+(?:\.\d+)?\s*[sS]?(?![\w.])',text,flags=re.I)
  found=set()
  for clause in clauses:
   if not re.search(r'(?:ВА|VA)(?!\w)',clause,re.I):continue
   exact=re.fullmatch(r'\s*(?:(?:мощность|(?:rated\s+)?power)\s*[:：=-]?\s*)?(\d+(?:\.\d+)?)\s*(?:ВА|VA)(?:\s*\(\s*(?:Комплект|pack)\s+\d+\s*(?:шт|pcs?)\.?\s*\))?\s*',clause,re.I)
   if not exact:return set()
   found.add(Decimal(exact[1]))
  return found
 wanted=values(binding['requested']);seen=values(owned['title'])
 # More than one rating cannot prove which main-product variant is offered.
 if len(wanted)!=1 or len(seen)!=1:return False
 if ratings(binding['model'])-wanted:return False
 if ratings(primary_text(owned['text']))-seen:return False
 return wanted!=seen

def primary_contradiction(position,owned):
 if type(owned) is not dict or not owned.get('title') or not owned.get('text'):return None
 if _explicit_power_conflict(position,owned):
  return dict(code='http_primary_identity_conflict',reason='Мощность ВА основного товара противоречит спецификации')
 from .product_identity import native_source_spec_reason,native_model_spec_reason,native_comparison_mark,native_request_role
 from .agent import _conflicting_full_model_code,_conflicting_model_code_in_title,_conflicting_network_cable_spec
 title=owned['title'];text=owned['text']
 # Only independently labelled manufacturer codes carry an article obligation.
 article=owned.get('article','') if owned.get('identity_basis')=='manufacturer_code' else ''
 for reason in (native_source_spec_reason(position,title,text,article),native_model_spec_reason(position,title,text)):
  if reason in _CONFLICTS:return dict(code='http_primary_identity_conflict',reason=reason)
 mark=native_comparison_mark(position)
 if any((_conflicting_full_model_code(mark,title),_conflicting_model_code_in_title(mark,title),_conflicting_network_cable_spec(position.name,mark,title))):
  return dict(code='http_primary_model_conflict',reason='Основной товар HTTP-карточки имеет другое явно указанное исполнение')
 if native_request_role(position)!='unknown':
  from .catalogue import identity_for_position
  from .manufacturer_aliases import manufacturer_equal
  from ..source_quote import primary_text
  expected=identity_for_position(position).manufacturer
  labels=re.findall(r'(?im)^\s*(?:Производитель|Бренд|Торговая\s+марка|Manufacturer|Brand)(?!\s+(?:code|article|number|код|артикул|номер)\b)(?=\s|[:：]|$)\s*[:：]?\s*\n?([^\n]{1,512})',primary_text(text))
  if expected and labels and any(not manufacturer_equal(expected,label.strip()) for label in labels):
   return dict(code='http_primary_manufacturer_conflict',reason='Производитель основного товара HTTP-карточки противоречит спецификации')
 return None
