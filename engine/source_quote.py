"""Product/price/denomination business checks, independent of image attachments."""
import hashlib,math,re,time

def retired_attachment_reason(reason):
 """Only recognizable retired checker outcomes, never an unknown transport stop."""
 text=str(reason or '').strip()
 if re.search(r'неизвест|unknown|timeout|connection|network|transport|http|tls|dns|капч|captcha|измен[её]н|друг(?:ой|ая|ие)\s+(?:товар|артикул|карточк)|количеств|состав|реквизит|продав|поставщик',text,re.I):return False
 codes=r'\b(?:price_capture_deadline|pixel_reader_unqualified|pixel_mode_invalid|pixel_unreadable_or_ambiguous|pixel_unit_location_mismatch|pixel_identity_mismatch|pixel_price_mismatch|font_visibility_unconfirmed)\b'
 phrases=r'^(?:Шрифт снимка не подтвержд[её]н|Видимость обязательных полей снимка не подтверждена|Цена/единица продажи не подтверждены читаемым снимком основной карточки)(?:$|[;:,. ])'
 return bool(re.search(codes,text,re.I) or re.search(phrases,text,re.I))

def verified_owned_text(text,source,*,url,observed_at):
 """Return full text only after reopening the exact immutable native owner."""
 from .search.http_card import read_source
 from .search.product_identity import canonical_card_url
 retained=read_source(source,allow_rendered=True)
 if not retained or not retained[1]:raise ValueError('owned_source_unconfirmed')
 record,owned=retained
 if (type(text) is not str or text!=owned['text']
     or canonical_card_url(url)!=canonical_card_url(record['url'])
     or type(observed_at) not in (int,float) or observed_at!=record['observed_at']):raise ValueError('owned_source_binding_changed')
 return owned['text']

def primary_text(text):
 return re.split(r'(?im)^\s*(?:Рекомендуем|Рекомендации|Похожие товары|С этим товаром|Сопутствующие товары|Вы смотрели)',str(text))[0]

def observe_identity(page,url,mark,name,host):
 from .search.product_identity import rendered_identity,main_card_article
 from .search.failure_policy import ResearchControlError
 try:
  titles=page.locator('h1')
  if titles.count()!=1:return {}
  title=titles.first.inner_text().strip();text=primary_text(page.inner_text('body'))
  return rendered_identity(url,page.url,title,main_card_article(text),mark,name,host)
 except ResearchControlError:raise
 except Exception:return {}

def _price_clauses(text):
 """Join only bounded continuations of a pricing clause, never across owner topics."""
 lines=str(text).splitlines();result=[]
 boundary=re.compile(r'^\s*(?:(?:старая|прошлая|новая|текущая|розничная|клубная|оптовая|ориентировочная|минимальная)\s+цена|(?:цена\s+)?за\s*\d*|(?:от\s*)?\d[^\n]*(?:₽|руб|RUB)|доставк|перевозк|оплата|контакты|о\s+компании|минимальн\w*\s+(?:сумм|заказ)|упаковк|количеств|рекоменд|похож|сопутствующ)',re.I)
 for index,line in enumerate(lines):
  clause=line.strip()
  if re.search(r'цен[ауы]|стоимость',clause,re.I):
   for following in lines[index+1:index+3]:
    following=following.strip()
    if not following or boundary.search(following) or len(clause)+len(following)>320 or re.search(r'[.!?]\s*$',clause):break
    clause+=' '+following
  result.append(clause)
 return result

def firm_price_role_reason(price_text,unit_text='',terms_text=''):
 """Selected currency roles and product-price disclosures, never order quantities."""
 amount=r'(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?'
 currency=r'(?:₽|руб(?:\.|лей|ля|ль)?|RUB)(?![^\W\d_])'
 selected_lines=[line for line in str(price_text).splitlines() if not re.search(r'доставк|перевозк|стоимость\s+заказа|сумма\s+заказа',line,re.I)]
 if re.search(r'(?<![^\W\d_])от\s*'+amount+r'\s*'+currency,'\n'.join(selected_lines),re.I):return 'Указана начальная цена, окончательная цена не подтверждена'
 for line in _price_clauses(str(price_text)+'\n'+str(unit_text)+'\n'+str(terms_text)):
  # A logistical/quantity notice cannot redefine the selected product amount.
  if re.search(r'доставк|перевозк|минимальн\w*\s+(?:сумм|заказ)|от\s*\d+\s*(?:м(?:етр)?|шт)',line,re.I) and not re.search(r'(?:цен[ауы]|стоимость)\s+(?:товар|продукц)',line,re.I):continue
  indicative=re.search(r'(?:ориентировочн\w*|приблизительн\w*|индикативн\w*|минимальн\w*)\s+(?:цен[ауы]|стоимость)|(?:цен[ауы]|стоимость)\s*[:—-]?\s*(?:ориентировочн\w*|приблизительн\w*|индикативн\w*|минимальн\w*|по\s+запросу)',line,re.I)
  individual=re.search(r'(?:цен[ауы]|стоимость)(?:[^.!?\n]{0,100})(?:рассчитыва\w*|определя\w*|уточня\w*)\s+индивидуально',line,re.I)
  final_request=re.search(r'(?:окончательн\w*|точн\w*|финальн\w*)\s+(?:цен[ауы]|стоимость)[^.!?\n]{0,160}(?:уточн|менеджер|заявк|запрос|расч[её]т)',line,re.I)
  if indicative or individual or final_request:return 'Цена предварительная или требует индивидуального расчёта'
 return ''

def _pricing_terms(text,listed):
 """Pricing disclosures only within the selected amount's textual quote region."""
 pattern=re.compile(r'(?<![\d.,])((?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?)\s*(?:₽|руб(?:\.|лей|ля|ль)?|RUB)(?![^\W\d_])',re.I)
 matches=list(pattern.finditer(text));regions=[];previous_end=0
 for index,match in enumerate(matches):
  amount=float(re.sub(r'\s','',match[1]).replace(',','.'))
  if math.isclose(amount,listed,rel_tol=1e-8,abs_tol=.005):
   start=max(previous_end,text.rfind('\n',previous_end,match.start())+1)
   tail=text[match.end():matches[index+1].start() if index+1<len(matches) else len(text)]
   if index+1<len(matches):tail=re.split(r'(?im)^\s*(?:ориентировочная|минимальная|приблизительная|индикативная)\s+цена\s*[:—-]?(?:\s|$)',tail)[0]
   tail=re.split(r'(?im)^\s*(?:(?:старая|прошлая|новая|текущая|розничная|клубная|оптовая)\s+цена|доставка(?:\s+и\s+оплата)?|оплата|контакты|о\s+компании)\s*[:—-]?(?:\s|$)',tail)[0]
   regions.append(text[start:match.end()]+tail)
  previous_end=match.end()
 text='\n'.join(regions)
 return _pricing_disclosures(text)

def _pricing_disclosures(text):
 """Canonical business terms, including legacy v1 notices without monetary data."""
 return '\n'.join(line.strip() for line in _price_clauses(text) if
  re.search(r'(?:цен[ауы]|стоимость)',line,re.I) and
  (firm_price_role_reason('',terms_text=line) or re.search(r'клуб|по\s+карт[еы]|при\s+(?:покупк|заказ)|оптов',line,re.I))
  or re.search(r'^\s*только\s+для\s+(?:участник|член|владельц)',line,re.I))[:1000]

def labelled_sale_units(text):
 """Explicit source label/value rows, including DOM line wrapping."""
 pattern=r'(?im)^\s*(?:Единица\s+измерения|Ед\.?\s*изм\.?|Sale\s+unit|Unit\s+of\s+sale)\s*[:：]?\s*(?:\n\s*)?((?:\d+(?:[.,]\d+)?\s*)?(?:штука|штуки|шт\.?|метр|м|компл(?:ект)?|упак(?:овка)?))\s*(?=\n|$)'
 return list(dict.fromkeys(m.group(1).strip() for m in re.finditer(pattern,str(text))))

def _nonproduct_money_role(text,matches,index):
 """Local monetary clause only; a following labelled charge owns its own amount."""
 match=matches[index];previous=matches[index-1].end() if index else 0
 following=matches[index+1].start() if index+1<len(matches) else len(text)
 role=r'(?:доставк\w*|перевозк\w*|транспортировк\w*|курьер\w*|shipping|delivery|freight|postage|(?:сумма|стоимость|итого)\s+(?:заказ\w*|корзин\w*)|order\s+(?:total|subtotal|amount)|subtotal|checkout\s+total|к\s+оплате|итого|total)'
 boundary=r'(?i)^\s*(?:(?:цена|стоимость)\s+(?:товара|продукции)|product\s+price|current\s+price|sale\s+unit|unit\s+of\s+sale|единица\s+измерения|manufacturer|add\s+to\s+cart)\b'
 before=text[previous:match.start()].splitlines()
 # Only the closest bounded label/continuation may qualify a monetary role.
 prefix=[]
 for line in reversed(before[-3:]):
  if re.search(boundary,line):break
  if line.strip():prefix.insert(0,line.strip())
  if ':' in line:break
 prefix=' '.join(prefix)
 tail=text[match.end():following]
 suffix=[]
 for n,line in enumerate(tail.splitlines()[:3]):
  if re.search(boundary,line):break
  # A colon-labelled following row is a new branch, not this amount's suffix.
  if n>0 and (':' in line or index+1<len(matches) and re.fullmatch(r'\s*(?:'+role+r')\s*',line,re.I)):break
  if line.strip():suffix.append(line.strip())
 return bool(re.search(role,prefix,re.I) or re.search(role,' '.join(suffix),re.I))

def owned_purchase_facts(text):
 """Neutral purchase-panel parser: no expected units, schema amount or model."""
 from .price_capture import _unit
 if type(text) is not str or len(text)>20000:raise ValueError('invalid_purchase_text')
 pattern=re.compile(r'(?<![\d.,])((?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?)[ \t\r\n\u00a0\u202f]*(₽|руб(?:\.|лей|ля|ль)?|RUB)(?![^\W\d_])',re.I)
 matches=list(pattern.finditer(text));choices=[]
 for i,m in enumerate(matches):
  amount=float(re.sub(r'\s','',m.group(1)).replace(',','.'))
  if not math.isfinite(amount) or amount<=0:continue
  if _nonproduct_money_role(text,matches,i):continue
  context=selected_price_context(text,amount)[0]
  if re.search(r'рознич|retail|старая\s+цена|прошлая\s+цена|old\s+price|до\s+скидки',context,re.I):continue
  choices.append((amount,context))
 unique={v[0] for v in choices}
 raw=next(iter(unique)) if len(choices)==1 else None
 units=labelled_sale_units(text);normalized={_unit(v) for v in units}
 sale=units[0] if len(normalized)==1 and normalized!={''} else ''
 price_text=choices[0][1] if raw is not None else ''
 unit_text='Единица измерения: '+sale if sale else ''
 reason=('purchase_nonproduct_cash' if raw is None and matches and not choices else 'purchase_price_ambiguous' if raw is None and matches else 'purchase_price_missing' if raw is None else
         'purchase_unit_unconfirmed' if not sale else firm_price_role_reason(price_text,unit_text,text))
 return dict(raw_amount=raw,currency='RUB' if matches else 'unknown',sale_unit=sale,price_text=price_text,unit_text=unit_text,reason=reason)

def literal_same_unit_confirmed(unit_text,wanted):
 """Literal denomination in an already selected cash context, never a counter."""
 from .price_capture import _sale_unit_token
 token=_sale_unit_token(wanted)
 return bool(token and re.search(r'(?:/[ \t\u00a0\u202f]*|\bза(?=\s|\d|$)\s*(?:1\s*)?|единица[^\n:]*:\s*)'+token+r'\b',unit_text,re.I))


def selected_price_context(text,listed):
 """A currency-labelled amount owns only its immediate textual role prefix."""
 pattern=re.compile(r'(?<![\d.,])((?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?)[ \t\r\n\u00a0\u202f]*(?:₽|руб(?:\.|лей|ля|ль)?|RUB)(?![^\W\d_])',re.I)
 result=[];units=[];previous_end=0
 matches=list(pattern.finditer(text))
 for index,match in enumerate(matches):
  amount=float(re.sub(r'\s','',match.group(1)).replace(',','.'))
  if math.isclose(amount,listed,rel_tol=1e-8,abs_tol=.005):
   start=text.rfind('\n',previous_end,match.start())+1
   start=max(start,previous_end)
   prefix=text[start:match.start()]
   if not prefix.strip():
    before=text[previous_end:match.start()].rstrip()
    preamble=[]
    for preceding in reversed(before.splitlines()):
     if not preceding.strip():continue
     role=re.fullmatch(r'\s*(?:(?:старая|прошлая|новая|текущая|розничная|клубная|оптовая|ориентировочная|минимальная|приблизительная|индикативная)\s+цена|цена\s+(?:до\s+скидки|по\s+карте)|(?:retail|old|current)\s+price)\s*[:—-]?\s*',preceding,re.I)
     denomination=re.fullmatch(r'\s*(?:цена\s+)?за\s*(?:(?:\d+(?:[.,]\d+)?\s*)?(?:м|метр(?:а|ов)?|шт\.?|штук[аи]?|упаковку|комплект))\s*[:—-]?\s*',preceding,re.I)
     starting=re.fullmatch(r'\s*(?:(?:цена|стоимость)\s*[:—-]?\s*)?от\s*',preceding,re.I)
     if not (role or denomination or starting):break
     preamble.insert(0,preceding)
    if preamble:prefix='\n'.join(preamble)+'\n'
   tail=text[match.end():matches[index+1].start() if index+1<len(matches) else len(text)]
   # A price suffix belongs to that branch, not to the other current amount.
   suffix=tail.strip().splitlines()[0] if tail.strip() else ''
   same_line=bool(tail.split('\n',1)[0].strip())
   alternate=suffix if (re.match(r'^(?:в\s+розничн\w*\s+магазин\w*|retail\s+stores?)(?:\s|$)',suffix,re.I) or same_line and re.match(r'^(?:retail|old\s+price|до\s+скидки)(?:\s|$)',suffix,re.I)) else ''
   result.append(prefix+match.group(0)+(' '+alternate if alternate else ''))
   # A following quote heading starts another price scope before its amount.
   tail=re.split(r'(?im)\b(?:(?:старая|прошлая|новая|текущая|розничная|клубная|оптовая|ориентировочная|минимальная|приблизительная|индикативная)\s+цена|цена\s+(?:до\s+скидки|по\s+карте))',tail)[0]
   if re.search(r'\bза\s*',prefix,re.I):tail=re.split(r'(?im)^\s*(?:цена\s+)?за\s*',tail)[0]
   units.extend(line.strip() for line in (prefix+tail).splitlines() if re.search(r'(?:/|\bза(?=\s|\d|$)\s*|единиц|упаковк)',line,re.I) and re.search(r'(?:шт|м|метр|компл|упак)',line,re.I))
  previous_end=match.end()
 units.extend('Единица измерения: '+v for v in labelled_sale_units(text))
 return '\n'.join(result)[:1500],'\n'.join(dict.fromkeys(units))[:2000]

def complete_kit_pack_context(text, listed):
 """Full bounded selected purchase occurrence, never a truncated unit excerpt.

 Only one literal monetary occurrence is supported by this new capability.
 Other price branches retain the existing conservative fallback.
 """
 if type(text) is not str or len(text) >= 2000:return ''
 text=primary_text(text)
 matches=list(re.finditer(r'(?<![\d.,])((?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?)\s*(?:₽|руб(?:\.|лей|ля|ль)?|RUB)(?![^\W\d_])',text,re.I))
 if len(matches)!=1:return ''
 match=matches[0]
 if not math.isclose(float(re.sub(r'\s','',match[1]).replace(',','.')),listed,rel_tol=1e-8,abs_tol=.005):return ''
 # Preserve the ENTIRE bounded main-product context. Every claim before and
 # after this cash occurrence is checked; no title/metadata is silently erased.
 return text

def complete_kit_identity_metadata(title, article, expected_name, expected_mark):
 """Only a full exact request-compatible owned header/code is neutral metadata.

 Actual-title equality alone is insufficient. Descriptive suffixes, arbitrary
 source prose or a header with no exact requested counterpart remain unknown.
 """
 values=(title,article,expected_name,expected_mark)
 if any(type(v) is not str or not v.strip() or len(v)>512 or '\n' in v or '\r' in v for v in values):return ()
 normalize=lambda v:re.sub(r'\s+',' ',v).strip()
 if normalize(title)!=normalize(expected_name+' '+expected_mark) or article!=expected_mark:return ()
 return tuple(dict.fromkeys((normalize(title),article)))

def complete_kit_pack_witness(context, *, listed=None, identity_metadata=()):
 """Closed symmetric selected purchase proof, not a natural-language classifier.

 Only typed price/denomination/unit/relation lines and separately request-bound
 exact identity metadata are supported. Unknown prose on EITHER side refuses.
 """
 if type(context) is not str or not context or len(context)>=2000:return None
 if type(identity_metadata) is not tuple or any(type(v) is not str or len(v)>512 for v in identity_metadata):return None
 relation=re.compile(r'(?:в\s+(?:(?:одной|1)\s+)?(?:(?:продаваемой|продажной)\s+)?упаковке\s*[:：]?|(?:одна\s+)?упаковка\s+содержит)\s*([1-9]\d{0,5})\s+(?:(?:полный|полных|полные)\s+)?комплект(?:а|ов|ы)?(?:\s+(?:данного\s+изделия|этого\s+товара))?\s*[.]?',re.I)
 pack=r'(?:1\s+)?уп(?:ак(?:овк[ау])?)?\.?'
 denom=r'(?:/|за\s+)\s*'+pack
 amount=r'((?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?)'
 currency=r'(?:₽|руб(?:\.|лей|ля|ль)?|RUB)'
 cash=re.compile(r'(?:Цена\s*[:：]?\s*)?(?:'+denom+r'\s*[:：]?\s*)?'+amount+r'\s*'+currency+r'(?:\s*'+denom+r')?\s*',re.I)
 values=[];excerpt=[];cash_values=[];denominations=0
 for raw in context.splitlines():
  line=re.sub(r'\s+',' ',raw).strip()
  if not line:continue
  if line in identity_metadata:continue
  match=relation.fullmatch(line)
  if match:values.append(int(match[1]));excerpt.append(raw.strip());continue
  if re.fullmatch(r'(?:Единица\s+измерения|Sale\s+unit)\s*[:：]?\s*уп(?:ак(?:овка)?)?\.?',line,re.I):continue
  if re.fullmatch(r'(?:Цена\s+)?'+denom+r'\s*[:：]?',line,re.I):denominations+=1;continue
  match=cash.fullmatch(line)
  if match:
   cash_values.append(float(re.sub(r'\s','',match[1]).replace(',','.')))
   if re.search(denom,line,re.I):denominations+=1
   continue
  return None
 if not values or len(set(values))!=1 or not denominations or len(cash_values)!=1:return None
 if listed is not None and cash_values[0]!=listed:return None
 return dict(version=1,kind='explicit_complete_kit_pack',quantity=values[0],
             context_sha256=hashlib.sha256(context.encode()).hexdigest(),
             identity_metadata=list(identity_metadata),excerpt='\n'.join(excerpt))

def selected_price_text(text,listed):
 return selected_price_context(text,listed)[0]

def observe_quote(page,*,url,title,article,listed,normalized,sale_unit,requested_unit,data):
 """Retain current textual facts; image/file/geometry is never consulted."""
 from .price_capture import visible_prices,_unit,_sale_lot
 from .search.failure_policy import ResearchControlError
 try:text=primary_text(page.inner_text('body'))
 except ResearchControlError:raise
 except Exception:return {}
 return observe_captured_quote(text,actual_url=page.url,observed_at=time.time(),url=url,title=title,article=article,listed=listed,normalized=normalized,sale_unit=sale_unit,requested_unit=requested_unit,data=data)

def observe_captured_quote(text,*,actual_url,observed_at,url,title,article,listed,normalized,sale_unit,requested_unit,data,expected_name='',expected_mark='',owned_source=None):
 from .price_capture import visible_prices,_unit,_sale_lot
 if owned_source is None and isinstance(data.get('factory_unit_proof'),dict):owned_source=data['factory_unit_proof'].get('supplier_source')
 try:text=verified_owned_text(text,owned_source,url=actual_url,observed_at=observed_at) if owned_source is not None else primary_text(text)
 except (ValueError,TypeError,KeyError,OSError):return {}
 from urllib.parse import urlparse
 requested=urlparse(url);actual=urlparse(actual_url)
 if requested.scheme not in {'http','https'} or actual.scheme not in {'http','https'} or not actual.hostname or actual.hostname.removeprefix('www.')!=str(requested.hostname or '').removeprefix('www.'):return {}
 try:
  if not all(type(v) in (int,float) and math.isfinite(v) and v>0 for v in (listed,normalized)):return {}
  # A source amount must actually occur in the main-card text, not a model guess.
  if not any(math.isclose(v,listed,rel_tol=1e-8,abs_tol=.005) for v in visible_prices(text)):return {}
  sale,wanted=_unit(sale_unit),_unit(requested_unit);lot=_sale_lot(sale_unit)
  quantity=lot[0] if lot and lot[1]==wanted else 1 if sale==wanted else data.get('package_quantity')
  if quantity is None and {sale,wanted}=={'шт','м'}:
   from .search.agent import _product_length_metres
   length=_product_length_metres(title);quantity=length if sale=='шт' else 1/length if length else None
  if type(quantity) not in (int,float) or not math.isfinite(quantity) or quantity<=0:return {}
  price_text,unit_text=selected_price_context(text,listed)
  if owned_source is not None:
   from .search.owned_purchase_row import derive
   row=derive(owned_source,url=actual_url,observed_at=observed_at,title=title,article=article,text=text)
   if row:
    if row['raw_amount']!=listed or _unit(row['sale_unit'])!=sale:return {}
    price_text,unit_text=row['price_text'],row['unit_text']
  witness=None
  factory_proof=data.get('factory_unit_proof')
  if sale=='уп' and wanted=='компл' and factory_proof is not None:
   from .search.factory_reference import verify_proof,FactoryEvidenceError
   try:derived=verify_proof(factory_proof,dict(name=expected_name,type_mark=expected_mark,unit=requested_unit))
   except (FactoryEvidenceError,OSError,ValueError,KeyError):return {}
   if quantity!=1 or listed!=derived['listed_price'] or normalized!=listed:return {}
  elif sale=='уп' and wanted=='компл':
   unit_text=complete_kit_pack_context(text,listed)
   decision,reason=strict_unit_decision(listed=listed,sale_unit=sale_unit,requested_unit=requested_unit,title=title,unit_text=unit_text,quantity=quantity,package_quantity=quantity,identity_metadata=complete_kit_identity_metadata(title,article,expected_name,expected_mark))
   if reason or not math.isclose(normalized,decision['price'],rel_tol=1e-8,abs_tol=.005):return {}
   witness=decision['witness']
  result= {'version':1,'source':'product_card_text','url':actual_url,'observed_at':observed_at,'title':title,'article':article,'listed_price':listed,'normalized_price':normalized,'sale_unit':sale_unit,'requested_unit':requested_unit,'quantity':quantity,'unit_text':unit_text,'price_text':price_text,'terms_text':_pricing_terms(text,listed)}
  if factory_proof is not None:result['factory_unit_proof']=factory_proof
  if witness:result['unit_witness']=witness
  return result
 except (ValueError,TypeError,OverflowError):return {}

def quote_review_reason(offer,position=None):
 field=lambda k,d=None:offer.get(k,d) if isinstance(offer,dict) else getattr(offer,k,d)
 if field('catalogue_ref') or field('catalogue_observed_at') or field('catalogue_source_sha256'):
  from .search.catalogue_projection import catalogue_review_reason
  return catalogue_review_reason(offer,position)
 pos=lambda k,d='':position.get(k,d) if isinstance(position,dict) else getattr(position,k,d)
 from .search.agent import _saved_price_unit_is_verified,_price_per_requested_unit
 from .price_capture import _unit,_sale_lot
 from .search.product_identity import valid_identity,canonical_card_url
 requested=pos('unit') if position is not None else field('normalized_price_unit','') or field('price_unit','')
 reason=field('unit_review_reason') or ''
 retired_only=retired_attachment_reason(reason)
 if field('unit_review_required') and not retired_only:return reason or 'Единица продажи/комплектация не подтверждены'
 evidence=field('source_quote_evidence',{}) or {}
 if evidence and (type(evidence) is not dict or type(evidence.get('version')) is not int or evidence.get('version')!=1 or evidence.get('source')!='product_card_text'):return 'Сведения об источнике цены не подтверждены'
 if pos('type_mark') and (evidence or field('listed_price_with_vat') is not None) and not field('from_letter'):
  if not valid_identity(field('identity_evidence',{}) or {},expected_mark=pos('type_mark'),expected_name=pos('name')):return 'Товар/артикул не подтверждены'
 if not evidence:
  # Historical metadata contributes only stored business facts, not image approval.
  old=field('price_capture_evidence',{}) or {}
  if old:
   evidence={'source':'historical_card_facts','url':old.get('url'),'title':old.get('title'),'article':old.get('article'),'listed_price':old.get('listed_price'),'normalized_price':old.get('normalized_price'),'sale_unit':old.get('sale_unit'),'requested_unit':old.get('requested_unit'),'quantity':old.get('quantity'),'unit_text':old.get('unit_text',''),'price_text':old.get('price_text','')}
  else:
   if field('listed_price_with_vat') is not None and not field('from_letter'):return 'Цена/единица карточки не подтверждены сохранёнными сведениями'
   try:
    return '' if _saved_price_unit_is_verified(requested,field('price_unit',''),field('listed_price_with_vat'),field('price_with_vat'),field('product_title',''),field('url',''),field('price_unit_evidence','')) else 'Единица продажи/комплектация не подтверждены'
   except (ValueError,TypeError):return 'Единица продажи/комплектация не подтверждены'
 try:
  if evidence.get('source')=='product_card_text':
   stamp=evidence.get('observed_at')
   if type(stamp) not in (int,float) or not math.isfinite(stamp) or not 0<stamp<=time.time()+300:return 'Дата наблюдения цены не подтверждена'
  if canonical_card_url(evidence.get('url'))!=canonical_card_url(field('url')):return 'Наблюдение цены относится к другой карточке'
  listed,normalized,quantity=[evidence.get(k) for k in ('listed_price','normalized_price','quantity')]
  if not all(type(v) in (int,float) and math.isfinite(v) and v>0 for v in (listed,normalized,quantity)):return 'Цена/количество не подтверждены'
  if not math.isclose(listed/quantity,normalized,rel_tol=1e-8,abs_tol=.005):return 'Цена не соответствует единице/количеству'
  if not math.isclose(listed,field('listed_price_with_vat'),rel_tol=1e-8,abs_tol=.005) or not math.isclose(normalized,field('price_with_vat'),rel_tol=1e-8,abs_tol=.005):return 'Сохранённая цена изменена относительно источника'
  if _unit(evidence.get('requested_unit'))!=_unit(requested) or evidence.get('sale_unit')!=field('price_unit'):return 'Единица цены не соответствует спецификации'
  identity=field('identity_evidence',{}) or {}
  if identity and (evidence.get('title')!=identity.get('main_title') or evidence.get('article')!=identity.get('main_article')):return 'Сведения о цене относятся к другому товару/артикулу'
  if pos('type_mark') and not valid_identity(identity,expected_mark=pos('type_mark'),expected_name=pos('name')):return 'Товар/артикул не подтверждены'
  if identity.get('provenance')=='http_main_product' or identity.get('rendered_source'):
   from .search.http_card import read_source
   source=read_source(identity.get('http_source') or identity.get('rendered_source') or {},allow_rendered=identity.get('provenance')=='rendered_main_card' and bool(identity.get('rendered_source')))
   if not source or not source[1]:return 'Источник основной карточки не подтверждён' if identity.get('rendered_source') else 'HTTP-источник цены не подтверждён'
   record,observed=source
   if listed!=observed['listed_price']:return 'Цена отличается от текущего предложения основной карточки' if identity.get('rendered_source') else 'Цена отличается от текущего предложения HTTP-карточки'
   bound=observe_captured_quote(observed['text'],actual_url=record['url'],observed_at=record['observed_at'],url=record['requested_url'],title=observed['title'],article=observed['article'],listed=listed,normalized=normalized,sale_unit=evidence.get('sale_unit'),requested_unit=requested,data={'package_quantity':quantity,**({'factory_unit_proof':evidence['factory_unit_proof']} if 'factory_unit_proof' in evidence else {})},expected_name=pos('name'),expected_mark=pos('type_mark'),owned_source=identity.get('http_source') or identity.get('rendered_source'))
   if not bound or any(evidence.get(key)!=bound.get(key) for key in ('url','observed_at','title','article','price_text','unit_text','quantity','unit_witness','factory_unit_proof')) or _pricing_disclosures(evidence.get('terms_text',''))!=_pricing_disclosures(bound.get('terms_text','')):return 'Сведения о цене/единице не принадлежат сохранённой основной карточке' if identity.get('rendered_source') else 'Сведения о цене/единице не принадлежат сохранённой HTTP-карточке'
  unit_text=evidence.get('unit_text','');price_text=evidence.get('price_text','')
  from .price_capture import visible_prices
  if not any(math.isclose(v,listed,rel_tol=1e-8,abs_tol=.005) for v in visible_prices(price_text)):return 'Исходная цена не подтверждена текстом карточки'
  if re.search(r'старая\s+цена|прошлая\s+цена|до\s+скидк',price_text,re.I):return 'Выбрана прежняя цена вместо текущей'
  if re.search(r'в\s+розничн\w*\s+магазин\w*|retail\s+stores?',price_text,re.I):return 'Выбрана цена отдельного розничного канала вместо текущего предложения'
  role_reason=firm_price_role_reason(price_text,unit_text,evidence.get('terms_text',''))
  if role_reason:return role_reason
  if re.search(r'клуб|по\s+карт[еы]|при\s+(?:покупк|заказ)|оптов|от\s+\d+\s*(?:шт|м)',unit_text+' '+price_text+' '+evidence.get('terms_text',''),re.I):return 'Цена зависит от условий покупки'
  decision, reason = strict_unit_decision(listed=listed, sale_unit=field('price_unit',''),
      requested_unit=requested, title=evidence.get('title',''), unit_text=unit_text,
      quantity=quantity, package_quantity=quantity,
      identity_metadata=complete_kit_identity_metadata(evidence.get('title',''),evidence.get('article',''),pos('name'),pos('type_mark')),factory_proof=evidence.get('factory_unit_proof'),request_position=position)
  if reason:return reason
  if decision['basis']=='explicit_complete_kit_pack':
   if (identity.get('provenance')!='http_main_product' or
       evidence.get('source')!='product_card_text' or evidence.get('unit_witness')!=decision['witness']):
    return 'Связь упаковки с комплектами не подтверждена текущим источником'
  # Preserve the existing saved-conversion compatibility gate (including xcom).
  sale,wanted=_unit(field('price_unit')),_unit(requested)
  if not _sale_lot(field('price_unit','')) and sale!=wanted and sale!='уп':
   if not _saved_price_unit_is_verified(requested,field('price_unit'),listed,normalized,evidence.get('title',''),field('url'),unit_text):return 'Пересчёт единицы/комплектации не подтверждён'
  return ''
 except (TypeError,ValueError,OverflowError):return 'Цена/единица/комплектация не подтверждены'


# Shared pure price/unit policy. Source ownership/identity/date/price-role checks
# remain at each consumer; these helpers never grant financial admission.
def _sale_unit(value: object) -> str:
    unit = str(value or "").strip().casefold().strip(". ")
    if unit in {"м", "метр", "метра", "метры", "м.п", "пог.м"}:
        return "м"
    if unit in {"шт", "штука", "штуки", "штук"}:
        return "шт"
    if unit in {"уп", "упак", "упаковка", "упаковки"}:
        return "уп"
    if unit in {'компл', 'комплект', 'комплекта', 'комплекты'}:
        return 'компл'
    if unit in {'кг', 'килограмм', 'килограмма', 'килограммы'}:
        return 'кг'
    if unit in {'л', 'литр', 'литра', 'литры'}:
        return 'л'
    return unit

def _explicit_piece_pack_size(title: str) -> int | None:
    sizes = {int(n) for n in re.findall(
        r"(?:уп(?:ак(?:овка|овке)?)?\.?\s*[:(]?\s*)(\d+)\s*шт\b",
        title, re.IGNORECASE)}
    sizes.update(int(n) for n in re.findall(
        r"(\d+)\s*шт\.?\s*(?:/|в)\s*уп", title, re.IGNORECASE))
    return sizes.pop() if len(sizes) == 1 else (0 if sizes else None)

def _product_length_metres(title: str) -> float | None:
    """One exact primary-title length field, with positively closed contexts.

    Unknown prose around a field is review, not an exact numeric prefix.
    L designations, explicit labelled fields, and the established stud role are
    separate grammars; none consumes ranges/operators or qualified quantities.
    """
    owned = primary_text(str(title or ''))
    text = owned.replace('х', 'x').replace('Х', 'X').replace('×', 'x')
    stud = bool(re.match(r'^\s*шпилька\b', text, re.I))
    marker = r'(?<!\w)(?:l(?=\s*(?:=|\d))|длина(?=\s*[:=]?\s*\d)'
    if stud:
        marker += r'|[mм](?=\s*\d{1,2}\s*x\s*\d)'
    markers = list(re.finditer(marker + ')', text, re.I))
    if not markers:
        return None
    scalar = r'\d+(?:[.,]\d+)?'
    # An independent, complete trailing field may follow a length. The closed
    # material/brand/article grammar covers retained titles, not arbitrary prose.
    model_tail = r'[A-Z]{2,}\d[A-Z0-9._-]*'
    material_tail = r'(?:сталь(?:\s*'+scalar+r'\s*мм)?|алюминий|пластик)'
    tail = re.compile(r'\s*[,;)]?\s*(?:'+material_tail+r'(?:\s+[A-Z]{2,})?(?:\s+\d{3,})?|INOX(?:\s*-\s*'+model_tail+r')?(?:\s+\d{3,})?)?\s*[,;(]?\s*')
    def safe_prefix(prefix, role, previous):
        if previous:
            # Every earlier field and its full continuation was already parsed.
            return True
        boundary = bool(re.search(r'[,;:(\n]\s*$', prefix))
        context = re.sub(r'[\s,;:(]+$', '', prefix)
        if not context:return True
        context += ' '
        # Exact model/article/dimension tokens provide an explicit field edge.
        if re.search(r'(?:\b\d{3,}|\b\d{1,4}(?:x\d{1,4}){1,2}|\b[A-Z]{2,}\d[A-Z0-9._-]*)\s+$', context):
            return True
        if re.search(r'\b(?:ширина|высота|глубина|толщина)\s*[:=]?\s*'+scalar+r'\s*(?:мм|см|м)\s+$', context, re.I):
            return True
        # These are the two established primary product-role conventions.
        return bool(((role == 'l' or boundary) and re.fullmatch(r'\s*лоток\s+', context, re.I))
                    or (role == 'stud' and re.fullmatch(r'\s*шпилька\s+', context, re.I)))
    lengths = []
    for index, match in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        field = text[match.start():end]
        if re.match(r'[mм]', match.group(), re.I):
            role = 'stud'
            parsed = re.fullmatch(r'[mм]\s*\d{1,2}\s*x\s*(\d{3,4})(.*)', field, re.I)
            if not parsed:return None
            amount, suffix = float(parsed[1]) / 1000, parsed[2]
        else:
            role = 'l' if match.group().casefold() == 'l' else 'label'
            parsed = re.fullmatch(r'(?:l|длина)\s*[:=]?\s*('+scalar+r')\s*(мм|см|м)?(.*)', field, re.I)
            if not parsed or (role == 'label' and not parsed[2]):return None
            unit = (parsed[2] or 'мм').casefold()
            amount = float(parsed[1].replace(',', '.')) * {'мм': .001, 'см': .01, 'м': 1}[unit]
            suffix = parsed[3]
        if (not safe_prefix(text[:match.start()], role, bool(lengths))
                or not tail.fullmatch(suffix) or not math.isfinite(amount) or amount <= 0):
            return None
        lengths.append(amount)
    return lengths[0] if all(math.isclose(v, lengths[0], rel_tol=1e-12, abs_tol=1e-12) for v in lengths) else None

def _piece_is_complete_set(title: str) -> bool:
    """One sold piece is one specified kit only when the card names its contents."""
    normalized = title.casefold().replace("ё", "е")
    return bool(re.search(r"в (?:комплекте|компл\.) с[^,]{0,90}(?:крепеж|метиз|соединительн|монтажн)",
                          normalized))

def _price_per_requested_unit(data: dict, requested_unit: str = "",
                              product_title: str = "", *, owned_unit_context: str = "", identity_metadata: tuple = (), factory_proof=None, request_position=None) -> float | None:
    """Verify the LLM's unit conversion; reject unsupported metre/piece changes."""
    sale_unit = _sale_unit(data.get("price_unit"))
    requested = _sale_unit(requested_unit)
    # Explicit numeric piece price denominators are not title pack sizes.
    lot = re.fullmatch(r'(\d+)\s*шт\.?', sale_unit)
    if lot:
        try:
            count = int(lot[1]); listed = float(data.get('listed_price_with_vat'))
            normalized = float(data.get('price_with_vat'))
            quantity = float(data.get('package_quantity'))
        except (TypeError, ValueError):
            return None
        if (requested != 'шт' or count <= 0 or not math.isfinite(listed) or listed <= 0
                or data.get('listed_price_covers_multiple_requested_units') is not True
                or quantity != count or not math.isclose(normalized, listed/count, rel_tol=1e-8, abs_tol=.005)):
            return None
        return listed/count
    pack = _explicit_piece_pack_size(product_title)
    if requested == "шт" and sale_unit == "шт" and pack is not None and pack != 1:
        return None
    if requested == "шт" and sale_unit == "уп" and pack is not None and pack != 1:
        if pack == 0:
            return None
        try:
            if not math.isclose(float(data.get("package_quantity")), pack):
                return None
        except (TypeError, ValueError):
            return None
    if requested and not sale_unit:
        return None
    try:
        price = float(data.get("price_with_vat"))
    except (TypeError, ValueError):
        # The extraction may omit its computed field while still returning the
        # card's listed price and selling unit. Reuse that price only when no
        # unit conversion is needed; the package rule below still applies.
        if not sale_unit or sale_unit != requested:
            return None
        try:
            price = float(data.get("listed_price_with_vat"))
        except (TypeError, ValueError):
            return None
    if not math.isfinite(price) or price <= 0:
        return None

    # A requested pack or complete set is already the selling unit. The
    # package quantity describes its contents, not a divisor for its price.
    if sale_unit == requested and requested in {"уп", "компл"}:
        try:
            raw_listed = data.get("listed_price_with_vat")
            listed = float(price if raw_listed is None else raw_listed)
        except (TypeError, ValueError):
            return None
        return listed if math.isfinite(listed) and listed > 0 else None

    if sale_unit == "шт" and requested == "компл" and _piece_is_complete_set(product_title):
        if data.get("listed_price_covers_multiple_requested_units") is True:
            return None
        return price

    # A price per piece is not a price per pack. The card must explicitly
    # quote the requested pack; multiplying by a presumed pack size is unsafe.
    if requested == "уп" and sale_unit != "уп":
        return None

    # Pack contents may be components of one requested kit (e.g. four
    # mounting plates), not several complete kits. Do not divide a pack
    # price into kits without an explicit selling-unit equivalence.
    if requested == "компл" and sale_unit == "уп":
        decision, reason = strict_unit_decision(listed=data.get('listed_price_with_vat'),
            sale_unit=sale_unit, requested_unit=requested, title=product_title,
            unit_text=owned_unit_context, quantity=data.get('package_quantity'),
            package_quantity=data.get('package_quantity'),identity_metadata=identity_metadata,factory_proof=factory_proof,request_position=request_position)
        if reason or not math.isclose(price, decision['price'], rel_tol=1e-8, abs_tol=.005):
            return None
        return decision['price']

    if sale_unit in {"м", "шт"} and requested in {"м", "шт"} and sale_unit != requested:
        length = _product_length_metres(product_title)
        try:
            listed = float(data.get("listed_price_with_vat"))
        except (TypeError, ValueError):
            return None
        if length is None or not math.isfinite(listed) or listed <= 0:
            return None
        converted = listed * length if sale_unit == "м" else listed / length
        if not math.isclose(price, converted, rel_tol=0.02, abs_tol=0.02):
            return None
        return converted

    if sale_unit and requested and sale_unit != requested:
        # A package price is not a piece price unless the card explicitly gives
        # the number of requested units in that package.
        if data.get("listed_price_covers_multiple_requested_units") is not True:
            return None
        try:
            listed = float(data.get("listed_price_with_vat"))
            package_quantity = float(data.get("package_quantity"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(listed) or not math.isfinite(package_quantity) or listed <= 0 or package_quantity <= 0:
            return None
        return listed / package_quantity

    if data.get("listed_price_covers_multiple_requested_units") is True:
        # A matching sale unit plus a claimed multi-unit total is ambiguous:
        # the number may be a per-unit quote with an order multiple (as with
        # cable sold in rolls). A site-specific extractor must resolve it.
        return None
    return price


def strict_unit_decision(*, listed, sale_unit, requested_unit, title, unit_text,
                         quantity=None, package_quantity=None, lot_quantity=None, identity_metadata=(),factory_proof=None,request_position=None):
    """Derive one source-bound denominator; no source or financial admission.

    The caller first establishes ownership of title/unit_text and checks price
    roles. All returned strings are evidence already supplied by that caller.
    A declared quantity is checked, never trusted as its own conversion proof.
    """
    from .price_capture import _sale_lot, _sale_unit_token
    def positive(v):
        return type(v) in (int, float) and math.isfinite(v) and v > 0
    if not positive(listed):
        return None, 'Цена/количество не подтверждены'
    sale, wanted = _sale_unit(sale_unit), _sale_unit(requested_unit)
    lot = _sale_lot(sale_unit)
    token = _sale_unit_token(wanted)
    if lot:
        if lot[1] != wanted or (lot_quantity is not None and lot_quantity != lot[0]):
            return None, 'Количество единиц в цене не подтверждено'
        values = {float(n.replace(',', '.')) for n in re.findall(r'(?:/|\bза(?=\s|\d|$)\s*)\s*(\d+(?:[.,]\d+)?)\s*'+token+r'\b', unit_text, re.I)}
        divisor, basis = lot[0], 'price_lot'
        if values != {divisor}:
            return None, 'Количество единиц не подтверждено текстом карточки'
    elif sale == wanted:
        if not literal_same_unit_confirmed(unit_text, wanted):
            return None, 'Единица продажи не подтверждена текстом карточки'
        if lot_quantity not in (None, 1):
            return None, 'Количество единиц в цене противоречиво'
        divisor, basis = 1, 'same_unit'
    elif sale == 'уп' and wanted == 'компл' and factory_proof is not None:
        from .search.factory_reference import verify_proof,FactoryEvidenceError
        try:proof=verify_proof(factory_proof,request_position)
        except (FactoryEvidenceError,OSError,ValueError,KeyError):return None,'Заводское доказательство комплектации не подтверждено'
        if listed!=proof['listed_price'] or package_quantity is not None and (not positive(package_quantity) or package_quantity!=1):return None,'Цена/количество заводской упаковки противоречивы'
        divisor,basis=1,'derived_factory_singleton_kit'
    elif sale == 'уп' and wanted == 'компл':
        witness = complete_kit_pack_witness(unit_text, listed=listed, identity_metadata=identity_metadata)
        if not witness or (package_quantity is not None and
                (not positive(package_quantity) or package_quantity != witness['quantity'])):
            return None, 'Состав полной товарной комплектации упаковки не подтверждён'
        divisor, basis = witness['quantity'], 'explicit_complete_kit_pack'
    elif sale == 'уп':
        if wanted in {'уп', 'компл'}:
            return None, 'Пересчёт упаковки не подтверждён'
        values = {float(n.replace(',', '.')) for n in re.findall(r'(?:упаковк[аеаи]?|в\s+1\s+упаковке)\s*[:(]?\s*(\d+(?:[.,]\d+)?)\s*'+token+r'\b', unit_text+' '+title, re.I)}
        divisor, basis = package_quantity if package_quantity is not None else quantity, 'pack_contents'
        if not positive(divisor) or values != {divisor}:
            return None, 'Состав упаковки не подтверждён'
    elif {sale, wanted} == {'шт', 'м'}:
        length = _product_length_metres(title)
        if length is None:
            return None, 'Пересчёт длины не подтверждён'
        divisor, basis = (length if sale == 'шт' else 1 / length), 'product_length'
    elif sale == 'шт' and wanted == 'компл' and _piece_is_complete_set(title):
        divisor, basis = 1, 'complete_set'
    else:
        return None, 'Пересчёт единицы не подтверждён'
    if not positive(divisor) or (quantity is not None and
            (not positive(quantity) or not math.isclose(divisor, quantity, rel_tol=1e-9, abs_tol=0))):
        return None, 'Количество единиц в цене противоречиво'
    price = listed / divisor
    if not positive(price):
        return None, 'Цена/количество не подтверждены'
    decision = dict(price=price, quantity=divisor, basis=basis, basis_text=unit_text)
    if basis == 'explicit_complete_kit_pack':decision['witness'] = witness
    return decision, ''
