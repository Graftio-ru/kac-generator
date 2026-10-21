"""Bounded static main-product evidence; JSON-LD locates, never invents quotes."""
from __future__ import annotations
from html.parser import HTMLParser
from dataclasses import dataclass,field
from pathlib import Path
import base64,hashlib,json,re,time,uuid,threading,copy,math
from collections import OrderedDict
_PARSED=OrderedDict()
_PARSED_LOCK=threading.Lock()
from urllib.parse import urljoin,urlsplit
from ..config import CACHE_DIR
from .product_identity import canonical_card_url,card_host
from .session import bounded_timeout

@dataclass(eq=False)
class Node:
 tag:str
 attrs:dict
 parent:object=None
 children:list=field(default_factory=list)
 text:list=field(default_factory=list)
 start:int=0
 end:int=0
 def descendants(self):
  yield self
  for child in self.children:yield from child.descendants()
 def value(self):
  if excluded(self):return ''
  result=[]
  for part in self.text:
   if isinstance(part,Node):
    content=part.value();result.append(('\n'+content+'\n') if part.tag in {'div','p','h1','h2','dt','dd','li','tr','section','article','main','br'} else content)
   else:result.append(part)
  return ''.join(result)
 def path(self):
  if self.parent is None:return []
  return self.parent.path()+[self.parent.children.index(self)]


def excluded(node):
 attrs=node.attrs;classes=attrs.get('class','').lower().split();style=attrs.get('style','').replace(' ','').lower()
 return node.tag in {'script','style','nav','footer','aside','noscript','template','del','s'} or 'hidden' in attrs or attrs.get('aria-hidden')=='true' or attrs.get('role')=='dialog' or attrs.get('aria-modal')=='true' or any(c in {'hidden','d-none','sr-only','old-price','old_price','cart-modal','recommendations','related-products'} for c in classes) or 'display:none' in style or 'visibility:hidden' in style

class MainParser(HTMLParser):
 def __init__(self,text,check_controls=True):
  super().__init__(convert_charrefs=True);self.check_controls=check_controls;self.source=text;self.lines=text.splitlines(keepends=True);self.offsets=[0]
  for line in self.lines:self.offsets.append(self.offsets[-1]+len(line))
  self.root=Node('root',{});self.stack=[self.root];self.count=0
  self.feed(text);self.close()
  for node in self.stack:node.end=len(text)
 def source_offset(self):line,column=self.getpos();return self.offsets[line-1]+column
 def handle_starttag(self,tag,attrs):
  self.count+=1
  if self.check_controls and self.count%256==0:bounded_timeout(1)
  if self.count>20000 or len(self.stack)>128:raise ValueError('HTTP structure exceeds bounds')
  node=Node(tag,{k:v or '' for k,v in attrs},self.stack[-1],start=self.source_offset());self.stack[-1].children.append(node);self.stack[-1].text.append(node)
  if tag not in {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}:self.stack.append(node)
  else:node.end=node.start+len(self.get_starttag_text())
 def handle_startendtag(self,tag,attrs):
  self.handle_starttag(tag,attrs)
  if self.stack[-1].tag==tag:self.stack.pop().end=self.source_offset()+len(self.get_starttag_text())
 def handle_endtag(self,tag):
  for index in range(len(self.stack)-1,0,-1):
   if self.stack[index].tag==tag:
    for node in self.stack[index:]:node.end=self.source_offset()+len(tag)+3
    del self.stack[index:];break
 def handle_data(self,data):self.stack[-1].text.append(data)


def itemtype(node,kind):return bool(re.search(r'(?:^|/|\s)'+re.escape(kind)+r'(?:\s|$)',node.attrs.get('itemtype',''),re.I))
def prop(node,key):return key in node.attrs.get('itemprop','').split()
def visible(node):
 while node is not None:
  if excluded(node):return False
  node=node.parent
 return True

def _one(nodes):return nodes[0] if len(nodes)==1 else None

def _owned_text(value):
 # Generic visible section boundary; recommendations never supply main price.
 return re.split(r'\n\s*(?:Рекомендуем|Рекомендации|Похожие товары|Сопутствующие товары|С этим товаром|Аналогичные товары|Related products|Recommended products)(?=\s|[:：]|$)',value,maxsplit=1,flags=re.I)[0].strip()

_RELATED_SECTION=re.compile(r'(?:Рекомендуем|Рекомендации|Похожие товары|Сопутствующие товары|С этим товаром|Аналогичные товары|Related products|Recommended products)\s*[:：]?',re.I)

def _section_heading(node,parent):
 level=int(node.tag[1]) if re.fullmatch(r'h[1-6]',node.tag) else None
 control=node.attrs.get('role') in {'tab','button'} or parent.attrs.get('role')=='tablist'
 return level, bool(level and not control and _RELATED_SECTION.fullmatch(_semantic_text(node).strip()))

def _owned_nodes(root):
 """Visible primary sections; captions never delimit later technical facts."""
 result=[]
 def visit(node):
  if not _semantic_visible(node):return
  result.append(node);boundary=None
  for child in node.text:
   if not isinstance(child,Node) or not _semantic_visible(child):continue
   level,related=_section_heading(child,node)
   if related:boundary=level;continue
   if boundary is not None:
    if level and level<=boundary:boundary=None
    else:continue
   visit(child)
 visit(root)
 return result

def _owned_value(node,owned):
 if node not in owned:return ''
 parts=[];boundary=None
 for part in node.text:
  if isinstance(part,Node):
   if not _semantic_visible(part):continue
   level,related=_section_heading(part,node)
   if related:boundary=level;continue
   if boundary is not None:
    if level and level<=boundary:boundary=None
    else:continue
   value=_owned_value(part,owned);parts.append(('\n'+value+'\n') if part.tag in {'div','p','section','article','h1','h2','h3','h4','h5','h6','br','li','dt','dd','tr','td','th'} else value)
  elif boundary is None:parts.append(part)
 return ''.join(parts)


def _metadata_sibling_identity(parser,product,actual_url,*,with_owner=False):
 # Rejection-only join: metadata cannot supply a price or a body-wide owner.
 norm=lambda value:re.sub(r'\s+',' ',value).strip()
 def shown(node):
  while node is not None:
   style=re.sub(r'\s+','',node.attrs.get('style','')).lower()
   opacity=re.search(r'(?:^|;)opacity:([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:!important)?(?:;|$)',style)
   if not visible(node) or 'data-related-products' in node.attrs or 'data-recommendations' in node.attrs or node.tag=='dialog' or opacity and float(opacity[1])==0:return False
   node=node.parent
  return True
 def visible_text(node):
  if not shown(node):return ''
  parts=[]
  for part in node.text:
   if isinstance(part,Node):
    content=visible_text(part);parts.append(('\n'+content+'\n') if part.tag in {'div','p','h1','h2','dt','dd','li','tr','section','article','main','br'} else content)
   else:parts.append(part)
  return ''.join(parts)
 all_nodes=list(parser.root.descendants())
 if len([n for n in all_nodes if itemtype(n,'Product')])!=1 or not shown(product):return None
 headings=[n for n in all_nodes if n.tag=='h1']
 if len(headings)!=1 or not shown(headings[0]):return None
 heading=headings[0]
 if any(n.tag=='h1' for n in product.descendants()):return None
 def properties(owner,key):
  values=[]
  for node in owner.descendants():
   if node is owner or not prop(node,key) or not shown(node):continue
   parent=node.parent
   while parent is not None and 'itemscope' not in parent.attrs:parent=parent.parent
   if parent is owner:values.append(norm(node.attrs.get('content') or node.attrs.get('href') or node.value()))
  return set(filter(None,values))
 names=properties(product,'name');mpns=properties(product,'mpn');urls=properties(product,'url')
 if names!={norm(visible_text(heading))} or len(mpns)!=1:return None
 if len(urls)>1 or any(canonical_card_url(urljoin(actual_url,url))!=canonical_card_url(actual_url) for url in urls):return None
 common=heading.parent
 while common is not None and product not in common.descendants():common=common.parent
 if common is None or common.tag in {'body','html','root'} or not shown(common):return None
 card=heading
 while card.parent is not common:card=card.parent
 if card is product or any(itemtype(n,'Product') for n in card.descendants()):return None
 owned=[n for n in _owned_nodes(card) if shown(n)]
 def rows(pattern):
  found=[]
  for node in owned:
   if node.tag in {'script','style','template','input','meta'} or len(node.children)>4:continue
   value=norm(visible_text(node))
   match=re.fullmatch(pattern,value,re.I) if len(value)<=256 else None
   if match:found.append((node,match[1]))
  return [(n,v) for n,v in found if not any(other is not n and other in n.descendants() for other,_ in found)]
 articles=rows(r'(?:Артикул производителя|Код производителя|Manufacturer code|MPN)\s*[:：]?\s*([A-Za-zА-Яа-я0-9._/-]{3,256})')
 codes={v.casefold() for _,v in articles}
 mpn=next(iter(mpns))
 if codes!={mpn.casefold()}:return None
 brands=[n for n in product.descendants() if prop(n,'brand') and 'itemscope' in n.attrs and shown(n)]
 brand_names=set().union(*(properties(n,'name') for n in brands)) if brands else set()
 printed={v for _,v in rows(r'(?:Производитель|Manufacturer|Brand)\s*[:：]?\s*(.{1,128})')}
 if len(brand_names)!=1 or len(printed)!=1 or {v.casefold() for v in brand_names}!={v.casefold() for v in printed}:return None
 # Explicit semantic references may bind a technical region outside the
 # purchase branch. Keep the full common owner so intervening contradictions
 # cannot disappear. An unlinked paragraph/metadata description is no authority.
 purchase_card=card
 if 'itemref' in product.attrs:
  ids=product.attrs['itemref'].split()
  if not ids or len(ids)>16 or len(set(ids))!=len(ids):return None
  refs=[]
  for identifier in ids:
   matches=[n for n in all_nodes if n.attrs.get('id')==identifier]
   if len(matches)!=1 or not shown(matches[0]) or not prop(matches[0],'description'):return None
   refs.append(matches[0])
  closure=card
  for ref in refs:
   while closure is not None and ref not in closure.descendants():closure=closure.parent
  if closure is None or closure.tag in {'body','html','root'} or not shown(closure):return None
  card=closure
 text=_owned_value(card,set(_owned_nodes(card))).strip()
 if len(text)>200000:return None
 result=dict(title=visible_text(heading).strip(),article=mpn,text=text,identity_basis='manufacturer_code',code_observations=[dict(role='manufacturer_code',label='itemprop:mpn+printed manufacturer article',value=mpn)])
 if with_owner:result.update(_owner=card,_purchase_owner=purchase_card,manufacturer=next(iter(brand_names)))
 return result


def _semantic_visible(node):
 while node is not None:
  style=re.sub(r'\s+','',node.attrs.get('style','')).lower()
  opacity=re.search(r'(?:^|;)opacity:([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:!important)?(?:;|$)',style)
  if excluded(node) or 'data-related-products' in node.attrs or 'data-recommendations' in node.attrs or opacity and float(opacity[1])==0:return False
  node=node.parent
 return True

def _semantic_text(node):
 if not _semantic_visible(node) or re.fullmatch(r'(?:Снять фильтр|Remove filter)',node.attrs.get('title',''),re.I):return ''
 result=[]
 for part in node.text:
  if isinstance(part,Node):
   value=_semantic_text(part);result.append(('\n'+value+'\n') if part.tag in {'div','p','h1','h2','dt','dd','li','tr','section','article','main','br'} else value)
  else:result.append(part)
 return ''.join(result)

def _factory_features(owner):
 # Typed two-cell technical rows only. Their values never supply a kit/count
 # witness; the inference separately checks their compatibility.
 rows=[]
 for node in owner.descendants():
  if not _semantic_visible(node) or node.tag not in {'tr','div','li'}:continue
  if node.tag=='li':
   labels=[n for n in node.children if n.tag=='span']
   if len(labels)==1:
    label=re.sub(r'\s+',' ',_semantic_text(labels[0])).strip();full=re.sub(r'\s+',' ',_semantic_text(node)).strip()
    if label and full.startswith(label) and len(full)<=384:rows.append(dict(label=label.rstrip(':：'),value=full[len(label):].strip(),label_path=labels[0].path(),value_path=node.path(),direct_value=True))
   continue
  cells=[n for n in node.children if n.tag in ({'td','th'} if node.tag=='tr' else {'div','span','a'})]
  if len(cells)!=2:continue
  values=[re.sub(r'\s+',' ',_semantic_text(n)).strip() for n in cells]
  if all(values) and len(values[0])<=128 and len(values[1])<=256:rows.append(dict(label=values[0],value=values[1],label_path=cells[0].path(),value_path=cells[1].path(),direct_value=False))
 return rows

def _factory_prose_blocks(owner):
 """Retain untyped prose, separately from declared fields/navigation/stock.

 No keyword decides whether a free-form product assertion matters. Unknown
 blocks must be checked against the bound technical description by the caller.
 """
 nodes=list(owner.descendants());features=_factory_features(owner)
 neutral_labels=r'(?:Артикул производителя|Код производителя|Производитель|Manufacturer|Brand|Модельный ряд|Модель/исполнение|Единица измерения|(?:Вес|Длина|Ширина|Высота|Объем|Объём)(?: упаковки)?(?:, (?:кг|мм|м))?|Тип товара|Цвет|Изменение направления|Отклонение \(угол\)|Материал|Серия производителя|Нержавеющая сталь травлёная|Вид/марка материала|НАТО монтажные отверстия|Защитное покрытие поверхности|Внутр\. радиус|Высота кабельного лотка, мм|Перфорация в основании для монтажа|Исполнение для больших расстояний \(усиленный\)|Ширина кабельного лотка|Толщина материала|Подходит для обеспеч\. целостности цепи \(огнестойкость\)|Перфорированная боковая стенка|С крышкой|Модель изгиба|Тип позиции|Кол-во в транспортной упаковке|Штрих-код EAN|Код|Класс|Наименование|Код класса|Именование класса)'
 neutral_nodes=set();neutral_direct=set()
 for f in features:
  label=f['label'].rstrip(':：').strip();value=f['value']
  # Only closed scalar/identifier/engineering values can be neutral fields.
  number=re.fullmatch(r'([+-]?\d+(?:[.,]\d+)?)(?:\s*(мм|м|кг|уп|шт|°)\.?)?',value,re.I)
  unit=re.search(r',\s*(мм|м|кг)$',label,re.I)
  scalar=bool(number and math.isfinite(float(number[1].replace(',','.'))) and (not unit or not number[2] or unit[1].casefold()==number[2].casefold()))
  identifier=bool(re.fullmatch(r'[A-Za-zА-Яа-я0-9._/-]{1,128}',value)) and bool(re.search(r'\d',value)) and label in {'Код','Код производителя','Артикул производителя','Код класса','Код ОКПД 2','ТН ВЭД','Класс','Штрих-код EAN'}
  enum=bool(re.fullmatch(r'(?:Да|Нет|упак|Сталь с цинк-ламельным покрытием|сталь|Сталь оцинкованная|Серый|Цинк-ламельное покрытие|Плавный изгиб|Smooth bend \(\*\)|Вертикально ниспадающий \(внешний\)|Вертикальный внешний|Угол вертикальный внешний 90 градусов|Системные аксессуары)',value,re.I))
  title=next((n.value().strip() for n in nodes if n.tag=='h1'),'')
  named=label=='Наименование' and len(value)>=30 and title.casefold().startswith(value.casefold())
  rowlabel=bool(re.fullmatch(neutral_labels,label,re.I))
  rowlabel=rowlabel or bool(re.fullmatch(r'(?:Серия|Тип|В упаковке|Объ[её]м, м3|Толщина материала, мм|Ширина кабельного лотка, мм|Исполнение изгиба|Тип продукции|Тип аксессуара|Код ОКПД 2|ТН ВЭД|Климатическое исполнение|Вид/ марка материала|Ширина|Высота)',label,re.I))
  maker=label in {'Производитель','Manufacturer','Brand'} and bool(re.search(r'(?<!\w)'+re.escape(value)+r'(?!\w)',title,re.I))
  composition=label=='Модель/исполнение' and bool(re.fullmatch(r'С (?:соединит\. )?разъемом(?:/соединителем)? в комплекте',value,re.I))
  climate=label=='Климатическое исполнение' and bool(re.fullmatch(r'(?:У|ХЛ|УХЛ|ОМ|Т)\d(?:, (?:У|ХЛ|УХЛ|ОМ|Т)\d)*',value))
  series=label in {'Серия','Модельный ряд','Серия производителя'} and bool(re.fullmatch(r'(?:Листовые лотки «)?[A-Za-z]{1,8}\d{1,8}(?:[ -][A-Za-z]{1,32})?»?',value))
  commercial=label=='Тип позиции' and value=='Заказной ассортимент '+next((f['value'] for f in features if f['label']=='Производитель'),'')
  category=label=='Именование класса' and value=='Конструкции и детали конструкций из черных металлов прочие, не включенные в другие группировки' and any(f['label']=='Код класса' and re.fullmatch(r'\d{2}\.\d{2}\.\d{2}\.\d{3}',f['value']) for f in features)
  product_type=label=='Тип товара' and value=='Угол (поворот) для листового кабельного лотка '+next((f['value'] for f in features if f['label']=='Производитель'),'')
  if rowlabel and (scalar or identifier or enum or named or maker or series or composition or climate or commercial or category or product_type):
   neutral_nodes.add(tuple(f['label_path']))
   (neutral_direct if f['direct_value'] else neutral_nodes).add(tuple(f['value_path']))
 # Semantic stock/delivery tabs are distinct from product-composition prose.
 # Match their declared control label and the exact controlled pane key.
 stock_keys={n.attrs.get('data-name') for n in nodes if n.attrs.get('data-name') and re.fullmatch(r'(?:Наличие|Наличие в магазинах|Доставка по России)',_semantic_text(n).strip(),re.I)}
 labels={'Заказной ассортимент','Код товара:','Наличие','Доставка по России','Описание','Характеристики','Сопутствующие товары','Наличие в магазинах','Видео','Аналоги','Документы','Отзывы','Основные характеристики','Дополнительные характеристики','Сведения РАЭК','Общероссийский классификатор продукции ОКПД2','Сертификат дилера','О бренде','Гарантия производителя','Документация','Россия — родина бренда','Россия — страна производства','в розничных магазинах','Загружаем цену и доступные действия','Упаковка','Мастер','Поделиться:','Характеристики по стандарту ETIM','Пользовательские характеристики','Написать отзыв','Нашли ошибку в описании?','Отказное письмо','Сертификат соответствия'}
 brand_roots=[]
 for heading in nodes:
  if _semantic_text(heading).strip()!='О бренде':continue
  group=heading.parent
  while group is not None and group is not owner:
   children=list(group.descendants())
   if len(_semantic_text(group))>2000 or any(n.tag=='h1' or n.tag=='input' for n in children):break
   if any(re.search(r'/(?:brands?|manufacturers?)/',n.attrs.get('href',''),re.I) for n in children):brand_roots.append(group);break
   group=group.parent
 blocks=[]
 for n in nodes:
  if not _semantic_visible(n):continue
  direct=re.sub(r'\s+',' ',' '.join(v for v in n.text if isinstance(v,str))).strip()
  if not direct:continue
  ancestors=[];parent=n
  while parent is not None and parent is not owner.parent:ancestors.append(parent);parent=parent.parent
  if any(a.tag in {'script','style'} or re.fullmatch(r'(?:Снять фильтр|Remove filter)',a.attrs.get('title',''),re.I) for a in ancestors):continue
  stock_role=any(a.attrs.get('data-name') in stock_keys for a in ancestors)
  if stock_role:
   stock_enum={'Склады поставщика','Склад интернет-магазина','Есть в наличии','Нет в наличии','Под заказ','Склады','На складах поставщиков','Торгово-выставочные залы и электромаркеты','Наличие товара по области'}
   stock_scalar=re.fullmatch(r'(?:данные на:|Остатки обновлены:)?\s*\d{2}\.\d{2}\.\d{4}(?: \d{2}:\d{2}:\d{2})?|\d+(?:[.,]\d+)? ед\.',direct,re.I)
   geography=re.fullmatch(r'(?:В |г\. )?[А-ЯЁ][а-яё]+(?:, Распределительный центр| \( \))?|Весь [А-ЯЁ][а-яё]+ федеральный округ',direct)
   contact=n.tag=='a' and (re.fullmatch(r'\+?[\d ()-]{7,32}',direct) or re.fullmatch(r'[А-ЯЁ][а-яё]+, (?:ул\.|пр\.|пр-кт|проспект|мкрн|\d+ мкрн)[^.!?]{1,150}\d[^!?]{0,32}',direct))
   contact=contact or n.tag=='a' and bool(re.fullmatch(r'[А-ЯЁ][а-яё]+, [А-ЯЁ][а-яё]+(?: [А-ЯЁ][а-яё]+){0,4} (?:тракт|проспект|шоссе|улица), \d+[а-яё]?',direct))
   logistics=re.fullmatch(r'Уважаемый клиент! Товары между торговыми залами не перемещаются\. Пополнение с распределительного и региональных складов БЕСПЛАТНО!',direct)
   if direct in stock_enum or stock_scalar or geography or contact or logistics:continue
  nav_role=any(a.tag in {'a','button'} or 'href' in a.attrs for a in ancestors)
  if nav_role and (direct in {'Фото','Чертежи','Все характеристики','Скачать в PDF','Задать вопрос','Горячий цинк','Цинк-ламель','Сендзимир','Высота','Материал','Ширина','В корзине'} or re.fullmatch(r'\d+(?:[.,]\d+)?\s*(?:мм|м|кг)?',direct)):continue
  if direct in {'Высота','Материал','Ширина'} and n.parent is not None and any(c.tag=='a' for c in n.parent.descendants()):continue
  if direct=='Тип:' and n.parent is not None and any(c.tag=='select' for c in n.parent.children):continue
  if direct=='?' and n.tag in {'span','button','a'}:continue
  if any(tuple(a.path()) in neutral_nodes for a in ancestors) or tuple(n.path()) in neutral_direct:continue
  if any(a in brand_roots for a in ancestors):
   primary_title=next((node.value().strip() for node in nodes if node.tag=='h1'),'')
   maker=direct.isascii() and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,64}',direct)) and direct in primary_title.split()
   company=re.fullmatch(r'О компании [A-Za-z0-9_-]{1,64}(?: \([А-ЯЁ]{1,16}\))? [–-] один из лидеров по производству кабеленесущих систем: кабель-\.\.\.',direct)
   if maker or company or direct=='Все товары бренда':continue
  if n.tag=='button' and re.fullmatch(r'Этот товар в [A-Za-zА-Яа-я0-9 ._-]{1,64}',direct):continue
  if direct in labels or re.fullmatch(r'[\d\s.,°₽+×x/−–-]+',direct):continue
  if any(a.attrs.get('data-prod-id') or a.attrs.get('data-product-id') for a in ancestors if a is not owner) and re.fullmatch(r'(?:В корзину|В корзине|Купить|Add to cart|Buy)',direct,re.I):continue
  blocks.append(direct)
 return blocks

def _factory_owned_candidate(parser,product,actual_url):
 """Neutral current purchase facts joined to active technical evidence.

 The rejection-only join does not itself admit cash. AggregateOffer amounts
 are ignored; only independently visible selected purchase facts are used.
 """
 frame=_metadata_sibling_identity(parser,product,actual_url,with_owner=True)
 if not frame:return None
 from .factory_reference import lookup_reference,FactoryEvidenceError
 try:reference=lookup_reference(frame['manufacturer'],frame['article'])
 except (FactoryEvidenceError,OSError,ValueError):return None
 if not reference:return None
 owner=frame.pop('_owner');purchase_card=frame.pop('_purchase_owner');owned=[n for n in owner.descendants() if _semantic_visible(n)]
 from ..source_quote import owned_purchase_facts
 purchase=owned_purchase_facts(frame['text'])
 from ..price_capture import _unit
 if purchase['reason'] or _unit(purchase['sale_unit'])!='уп':return None
 inputs=[n for n in owned if n.tag=='input' and n.attrs.get('type')=='number' and ('data-count-per-pack' in n.attrs or 'data-pack-size' in n.attrs)]
 if len(inputs)!=1:return None
 control=inputs[0]
 if {control.attrs.get('data-count-per-pack'),control.attrs.get('data-pack-size')}!={'1'}:return None
 # The two packaging fields must belong to this product's purchase control,
 # independently of min/value/MOQ. A transport table cannot qualify it.
 product_id=purchase_card.attrs.get('data-prod-id') or purchase_card.attrs.get('data-product-id')
 parent=control.parent;purchase_owner=None
 while parent is not None:
  descendants=list(parent.descendants())
  buttons=[n for n in descendants if _semantic_visible(n) and n.tag in {'button','span','a'} and re.fullmatch(r'(?:В корзину|Купить|Add to cart|Buy)',n.value().strip(),re.I)]
  if buttons and product_id and all((n.attrs.get('data-prod-id') or n.attrs.get('data-product-id'))==product_id for n in buttons):purchase_owner=parent;break
  if parent is owner:break
  parent=parent.parent
 if purchase_owner is None:return None
 frame['factory_features']=_factory_features(owner)
 frame['factory_prose_blocks']=_factory_prose_blocks(owner)
 frame['factory_owner_text']=_semantic_text(owner).strip()
 if len(frame['factory_owner_text'])>200000:return None
 frame.update(scope_path=owner.path(),scope_sha256=hashlib.sha256(parser.source[owner.start:owner.end].encode()).hexdigest(),listed_price=purchase['raw_amount'],sale_unit=purchase['sale_unit'],factory_reference=reference,pack_counts={'count_per_pack':control.attrs['data-count-per-pack'],'pack_size':control.attrs['data-pack-size']},purchase_scope_sha256=hashlib.sha256(parser.source[purchase_owner.start:purchase_owner.end].encode()).hexdigest(),merchant_requisites={},merchant_assertions=[])
 return frame

def main_product(text,actual_url,*,check_controls=True,admission_only=False):
 """Positive scope only; unknown layouts and multiple products go to render."""
 parser=MainParser(text,check_controls);nodes=[n for n in parser.root.descendants() if _semantic_visible(n)]
 products=[n for n in nodes if itemtype(n,'Product')]
 # Structured product lists/Offer ranges are ambiguous even with a main h1.
 structured=[]
 for n in parser.root.descendants():
  if n.tag=='script' and n.attrs.get('type','').lower()=='application/ld+json':
   try:value=json.loads(''.join(part for part in n.text if isinstance(part,str)))
   except (ValueError,TypeError,RecursionError):return None
   visits=[0]
   def walk(value,depth=0):
    visits[0]+=1
    if depth>64 or visits[0]>10000:return False
    if isinstance(value,dict):
     kinds=value.get('@type',[]);kinds=[kinds] if isinstance(kinds,str) else kinds
     if not isinstance(kinds,list) or any(not isinstance(k,str) for k in kinds):return False
     if 'Product' in kinds:structured.append(value)
     for child in value.values():
      if not walk(child,depth+1):return False
    elif isinstance(value,list):
     for child in value:
      if not walk(child,depth+1):return False
    return True
   if not walk(value):return None
 if card_host(actual_url)=='favorit-el.ru' and any(n.attrs.get('id')=='catalogElementD' for n in parser.root.descendants()):
  return _favorit_product(parser,nodes,structured,actual_url,admission_only=admission_only)
 if len(structured)>1:
  # Other Product records are excluded only by an explicit different URL.
  selected=[]
  for product in structured:
   linked=product.get('url')
   if not isinstance(linked,str) or not linked:return None
   canonical=canonical_card_url(urljoin(actual_url,linked))
   if not canonical:return None
   if canonical==canonical_card_url(actual_url):selected.append(product)
  if len(selected)!=1:return None
  structured=selected
 scope=_one(products)
 if scope is None:
  if products:return None
  if admission_only and any(itemtype(n,'Product') for n in parser.root.descendants()):return None
  scope=_one([n for n in nodes if n.tag=='main'])
 if scope is None:return None
 if scope in products and not any(n.tag=='h1' for n in scope.descendants()):
  if admission_only:return _metadata_sibling_identity(parser,scope,actual_url)
  return _factory_owned_candidate(parser,scope,actual_url)
 own=_owned_nodes(scope);owned=set(own)
 value=lambda node:_owned_value(node,owned).strip()
 text=value(scope)
 observations=[]
 def retain_code(role,label,value):
  if not isinstance(value,str) or not re.fullmatch(r'[A-Za-zА-Яа-я0-9._/-]{3,256}',value):return False
  item={'role':role,'label':label,'value':value}
  if item not in observations:observations.append(item)
  return len(observations)<=32
 for role,pattern in (
  ('manufacturer_code',r'Артикул\s+производителя|Код\s+производителя|Manufacturer\s+(?:part\s+number|code)|MPN'),
  ('vendor_code',r'Номер\s+поставщика|Код\s+поставщика|Supplier\s+(?:number|code)|Vendor\s+(?:number|code)'),
  ('price_list_code',r'Номер\s+по\s+прайс[- ]листу|Код\s+прайс[- ]листа|Price[- ]list\s+(?:number|code)')):
  for match in re.finditer(r'(?:^|\n)\s*('+pattern+r')\s*[:：]?\s*\n?\s*([A-Za-zА-Яа-я0-9._/-]{3,256})(?=\s|$)',text,re.I):
   if not retain_code(role,match[1],match[2]):return None
 # Visible PropertyValue rows bind a role to their own value, never to a
 # following section. Metadata name can corroborate but cannot supply a label.
 for row in own:
  if not itemtype(row,'PropertyValue'):continue
  direct=[n for n in row.children if n in owned and n.tag!='meta' and value(n)]
  labels=[n for n in direct if re.fullmatch(r'Артикул производителя|Код производителя|Manufacturer (?:part number|code)|MPN',value(n),re.I)]
  values=[n for n in direct if prop(n,'value')]
  if labels:
   declared={n.attrs.get('content') for n in row.children if prop(n,'name') and n.tag=='meta' and n.attrs.get('content')}
   if len(labels)!=1 or len(values)!=1 or declared and declared!={value(labels[0])}:return None
   if not retain_code('manufacturer_code',value(labels[0]),value(values[0])):return None
 for n in own:
  if prop(n,'mpn') and n.tag!='meta' and not retain_code('manufacturer_code','itemprop:mpn',value(n)):return None
 manufacturer={o['value'] for o in observations if o['role']=='manufacturer_code'}
 secondary={o['value'] for o in observations if o['role'] in {'vendor_code','price_list_code'}}
 if len(manufacturer)>1:return None
 declared_mpn={n.attrs.get('content') for n in own if n.tag=='meta' and prop(n,'mpn') and n.attrs.get('content')}
 if manufacturer and declared_mpn and declared_mpn!=manufacturer:return None
 title_node=_one([n for n in own if n.tag=='h1'])
 sku_nodes=[n for n in own if prop(n,'sku')]
 sku_values={value(n) for n in sku_nodes if n.tag!='meta' and value(n)}
 legacy=sku_values-secondary
 if not legacy:
  from .product_identity import main_card_article
  labelled=main_card_article(text)
  if labelled and labelled not in secondary:legacy={labelled}
 if title_node is None or len(legacy)>1:return None
 title=value(title_node)
 article=next(iter(manufacturer)) if manufacturer else next(iter(legacy)) if legacy else ''
 basis='manufacturer_code' if manufacturer else 'legacy_article' if article else 'title_only'
 for n in sku_nodes:
  if n.tag!='meta' and value(n) and value(n) not in secondary:
   if not retain_code('unspecified_article','itemprop:sku',value(n)):return None
 if admission_only:return {'title':title,'article':article,'text':text,'identity_basis':basis,'code_observations':observations}
 offers=[n for n in own if itemtype(n,'Offer')]
 if basis=='title_only' and (len(products)!=1 or len(offers)!=1):return None
 offer=_one(offers)
 if any(itemtype(n,'AggregateOffer') for n in own) or len(offers)>1:return None
 if offer is None:
  if not structured:return None
  offer=scope
 for n in offer.descendants():
  if prop(n,'url'):
   linked=urljoin(actual_url,n.attrs.get('href') or n.attrs.get('content') or value(n))
   if canonical_card_url(linked)!=canonical_card_url(actual_url):return None
  if prop(n,'sku') and basis=='legacy_article' and (n.attrs.get('content') or value(n))!=article:return None
 currencies={n.attrs.get('content') or value(n) for n in offer.descendants() if prop(n,'priceCurrency')}
 if currencies and currencies!={'RUB'}:return None
 text=value(scope)
 if len(text)>200000:return None
 from ..source_quote import selected_price_context
 from ..price_capture import visible_prices
 offer_text=value(offer)
 prices=visible_prices(offer_text)
 if not prices:return None
 price_nodes=[n for n in offer.descendants() if prop(n,'price') and n in own and visible(n)]
 if basis=='title_only' and len(price_nodes)!=1:return None
 if len(price_nodes)>1:return None
 if price_nodes:
  literal_price=value(price_nodes[0])
  if price_nodes[0].tag=='meta' and not literal_price:
   # Metadata reconciles, never creates a positive current cash observation.
   literal_price=price_nodes[0].attrs.get('content','')
   if not re.fullmatch(r'\d+(?:[.,]\d{1,2})?',literal_price) or len(prices)!=1:return None
   if re.search(r'рассроч|плат[её]ж|в кредит|installment|per month',offer_text,re.I):return None
  try:amount=float(re.sub(r'[^\d.,]','',literal_price).replace(',','.'))
  except ValueError:return None
  if not math.isfinite(amount) or amount<=0:return None
 else:
  linked=structured[0].get('offers') if structured else None
  if isinstance(linked,list):linked=_one(linked)
  if not isinstance(linked,dict) or type(linked.get('price')) not in (str,int,float):return None
  try:amount=float(linked['price'])
  except ValueError:return None
 if amount not in prices:return None
 if re.search(r'USD|EUR|\$|€|цена\s+от|по\s+запросу|рекомендованн\w*\s+цен',offer_text,re.I):return None
 if structured:
  product=structured[0]
  if product.get('name')!=title:return None
  if basis=='legacy_article' and str(product.get('sku') or '')!=article:return None
  if manufacturer and product.get('mpn') is not None and product['mpn']!=article:return None
  linked=product.get('url')
  if linked is not None and not isinstance(linked,str):return None
  if linked and canonical_card_url(urljoin(actual_url,linked))!=canonical_card_url(actual_url):return None
  linked_offer=product.get('offers')
  if isinstance(linked_offer,list):linked_offer=_one(linked_offer)
  if not isinstance(linked_offer,dict) or linked_offer.get('@type')=='AggregateOffer' or linked_offer.get('priceCurrency')!='RUB':return None
  try:
   if float(linked_offer.get('price'))!=amount:return None
  except (TypeError,ValueError):return None
  if linked_offer.get('url') is not None and not isinstance(linked_offer['url'],str):return None
  if linked_offer.get('url') and canonical_card_url(urljoin(actual_url,linked_offer['url']))!=canonical_card_url(actual_url):return None
 sellers=[n for n in offer.descendants() if prop(n,'seller') and itemtype(n,'Organization')]
 merchant={}
 merchant_assertions=[]
 for seller in sellers[:7]:  # Bound retained seller alternatives independently of page size.
  names=[value(n) for n in seller.descendants() if prop(n,'name') and n.tag!='meta']
  inns=[value(n) for n in seller.descendants() if prop(n,'taxID') and n.tag!='meta']
  if len(names)==1 and len(inns)==1 and names[0]:
   item={'name':names[0],'inn':inns[0]}
   if item not in merchant_assertions:merchant_assertions.append(item)
 if len(sellers)==1:
  seller=sellers[0];names=[value(n) for n in seller.descendants() if prop(n,'name') and n.tag!='meta']
  inns=[value(n) for n in seller.descendants() if prop(n,'taxID') and n.tag!='meta']
  from ..requisites.supplier import valid_inn
  if len(names)==1 and len(inns)==1 and valid_inn(inns[0]):merchant={'name':names[0],'inn':inns[0]}
 return dict(title=title,article=article,identity_basis=basis,code_observations=observations,text=text,scope_path=scope.path(),scope_sha256=hashlib.sha256(parser.source[scope.start:scope.end].encode()).hexdigest(),listed_price=amount,merchant_requisites=merchant,merchant_assertions=merchant_assertions,merchant_assertions_overflow=len(sellers)>6)


def save_response(response,requested_url):
 body=response.content;record=dict(version=1,requested_url=requested_url,url=str(response.url),observed_at=time.time(),status=response.status_code,content_type=response.headers.get('content-type',''),encoding=response.encoding or 'utf-8',body_base64=base64.b64encode(body).decode(),body_sha256=hashlib.sha256(body).hexdigest())
 payload=json.dumps(record,ensure_ascii=False,sort_keys=True);reference='http_cards/'+uuid.uuid4().hex+'.json';path=CACHE_DIR/reference;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(payload,encoding='utf-8')
 return record,{'ref':reference,'record_sha256':hashlib.sha256(payload.encode()).hexdigest(),'body_sha256':record['body_sha256']}


def read_source(evidence,*,allow_rendered=False):
 """Read only a bounded owned capture, never an arbitrary client filesystem path."""
 if type(evidence) is dict and evidence.get('role')=='rendered_primary_dom':return _read_rendered_source(evidence) if allow_rendered is True else None
 try:
  ref=evidence.get('ref','')
  if not re.fullmatch(r'http_cards/[a-f0-9]{32}\.json',ref):return None
  root=CACHE_DIR.resolve();path=CACHE_DIR/ref
  if path.is_symlink() or path.resolve().parent!=root/'http_cards' or path.stat().st_size>3*1024*1024:return None
  payload=path.read_bytes()
  if hashlib.sha256(payload).hexdigest()!=evidence.get('record_sha256'):return None
  digest=evidence['record_sha256']
  with _PARSED_LOCK:
   if digest in _PARSED:
    value=_PARSED[digest];_PARSED.move_to_end(digest)
    if value[0].get('body_sha256')!=evidence.get('body_sha256'):return None
    return copy.deepcopy(value)
  record=json.loads(payload);body=base64.b64decode(record['body_base64'],validate=True)
  if len(body)>2*1024*1024 or hashlib.sha256(body).hexdigest()!=record['body_sha256'] or record['body_sha256']!=evidence.get('body_sha256'):return None
  if type(record.get('version')) is not int or record['version']!=1 or type(record.get('status')) is not int or record['status']!=200:return None
  observed=main_product(body.decode(record.get('encoding') or 'utf-8',errors='replace'),record['url'],check_controls=False)
  record.pop('body_base64',None)
  value=(record,observed)
  with _PARSED_LOCK:
   _PARSED[digest]=value;_PARSED.move_to_end(digest)
   while len(_PARSED)>64:_PARSED.popitem(last=False)
  return copy.deepcopy(value)
 except (OSError,ValueError,TypeError,KeyError,LookupError,RecursionError):return None


# This profile is colocated in the acquisition-intent-attested parser module.
def _favorit_product(parser, nodes, structured, actual_url, *, admission_only=False):
    from ..price_capture import visible_prices, _unit

    url = canonical_card_url(actual_url)
    if not re.fullmatch(r'/catalog/[^/]+\.html', urlsplit(url).path):
        return None
    # A profile is a closed layout contract, not a nearest-div heuristic.
    def safe(n):
        if not visible(n):
            return False
        while n is not None:
            if re.search(r'modal|recommend|related|old[-_]?price|economy|saving|discount',
                         n.attrs.get('class', ''), re.I):
                return False
            n = n.parent
        return True
    all_nodes = list(parser.root.descendants())
    owners = [n for n in all_nodes if n.attrs.get('id') == 'catalogElementD']
    owner = _one(owners)
    heading = _one([n for n in nodes if n.tag == 'h1'])
    product = _one(structured)
    if (owner is None or not safe(owner) or heading is None or not safe(heading) or product is None
            or any(itemtype(n, 'Product') for n in nodes)):
        return None
    canonicals = [n for n in all_nodes if n.tag == 'link'
                  and 'canonical' in n.attrs.get('rel', '').lower().split()]
    if not canonicals or {canonical_card_url(urljoin(url, n.attrs.get('href', ''))) for n in canonicals} != {url}:
        return None
    pid = owner.attrs.get('data-product-id', '')
    if (not re.fullmatch(r'[1-9]\d{0,19}', pid)
            or owner.attrs.get('data-currency') != 'RUB'
            or owner.attrs.get('data-currency-id') != 'RUB'):
        return None
    own = [n for n in owner.descendants() if safe(n)]
    def classes(node):
        return set(node.attrs.get('class', '').split())
    articles = [n for n in own if 'changeArticle' in classes(n)]
    article_node = _one(articles)
    if article_node is None:
        return None
    article = article_node.value().strip()
    if (not re.fullmatch(r'[A-Za-zА-Яа-я0-9._/-]{3,256}', article)
            or article_node.attrs.get('data-first-value') != article
            or not re.match(r'^\s*Артикул\s*:', article_node.parent.value())
            or product.get('sku') != article or product.get('url') != url):
        return None
    title = heading.value().strip()
    name = product.get('name')
    brand = product.get('brand')
    # Short JSON-LD names are accepted only with the EXACT witnessed brand/SKU
    # suffix. This never discards differing model, ratio, class or power text.
    if (not isinstance(name, str) or not name or not isinstance(brand, dict)
            or not isinstance(brand.get('name'), str) or not brand['name']
            or title != name + ' ' + brand['name'] + ' ' + article):
        return None
    offer = product.get('offers')
    if (not isinstance(offer, dict) or offer.get('@type') != 'Offer'
            or offer.get('priceCurrency') != 'RUB' or offer.get('url') != url):
        return None
    try:
        structured_amount = float(offer['price'])
    except (KeyError, ValueError, TypeError):
        return None
    if not math.isfinite(structured_amount) or structured_amount <= 0:
        return None
    # All technical rows are retained for downstream native specification gates.
    rows = []
    for n in own:
        if n.tag == 'tr' or 'propertyTable' in classes(n):
            value = n.value().strip()
            if value:
                rows.append(value)
    article_rows = [n for n in own if n.tag == 'tr' and
                    any(c.value().strip() == 'Артикул' for c in n.children)]
    if len(article_rows) != 1 or article_rows[0].children[-1].value().strip() != article:
        return None
    stock_rows = [n for n in own if 'propertyTable' in classes(n) and
                  any(c.value().strip() == 'В наличии' for c in n.children)]
    if len(stock_rows) != 1:
        return None
    stock = stock_rows[0].children[-1].value().strip()
    # This first bounded layout supports the witnessed explicit order/prepay
    # variant only; no JSON InStock interpretation or invented availability.
    if not re.fullmatch(r'Под заказ\s*\(по предоплате\)', stock, re.I):
        return None
    panels = []
    panel_nodes = []
    for panel_id in ('smallElementTools', 'elementTools'):
        panel = _one([n for n in all_nodes if n.attrs.get('id') == panel_id])
        if panel is None or panel not in own:
            return None
        panel_nodes.append(panel)
        part = [n for n in panel.descendants() if safe(n)]
        cash = _one([n for n in part if 'priceContainer' in classes(n)])
        buys = [n for n in part if 'addCart' in classes(n)]
        qty = _one([n for n in part if n.tag == 'input' and 'qty' in classes(n)])
        state = _one([n for n in part if 'eChangeAvailable' in classes(n)])
        if (cash is None or qty is None or state is None or len(buys) != 1
                or buys[0].attrs.get('data-id') != pid
                or buys[0].attrs.get('data-quantity') != '1'
                or qty.attrs.get('value') != '1' or qty.attrs.get('data-step') != '1'
                or state.value().strip().lower() != 'под заказ'):
            return None
        prices = [n for n in cash.descendants() if 'priceVal' in classes(n) and safe(n)]
        measures = [n for n in cash.descendants() if 'measure' in classes(n) and safe(n)]
        if len(prices) != 1 or len(measures) != 1:
            return None
        literal = prices[0].value().strip()
        if not re.fullmatch(r'\d[\d \u00a0\u202f]*(?:[.,]\d{1,2})?\s*(?:руб\.|₽|RUB)', literal):
            return None
        amounts = visible_prices(literal)
        unit_match = re.fullmatch(r'/\s*(шт\.?)', measures[0].value().strip())
        if len(amounts) != 1 or unit_match is None:
            return None
        amount, unit = amounts[0], unit_match[1]
        if re.sub(r'\s+', ' ', cash.value()).strip() != 'Цена: ' + literal + ' / ' + unit:
            return None
        counter = _one([n for n in panel.descendants() if 'qtyBlock' in classes(n)])
        if counter is None or qty.parent is not counter or counter not in part:
            return None
        # Only this closed, independently bound UI scaffold may be omitted.
        # Unknown text/nodes are purchase evidence, even in both responsive
        # counters: they require fallback rather than silent projection loss.
        if any(isinstance(p, str) and p.strip() for p in counter.text):
            return None
        controls = counter.children
        if ([n.tag for n in controls] != ['svg', 'label', 'a', 'input', 'a', 'label']
                or controls[3] is not qty
                or controls[1].children or controls[5].children
                or controls[1].value().strip() != 'Кол-во:'
                or controls[5].value().strip() != unit
                or classes(controls[2]) != {'minus'} or controls[2].value().strip()
                or controls[2].children
                or classes(controls[4]) != {'plus'} or controls[4].value().strip()
                or controls[4].children
                or controls[0].value().strip()
                or any(n.tag not in {'svg', 'use'} for n in controls[0].descendants())):
            return None
        # Keep every non-price panel statement for admission/identity review,
        # including newly added order conditions. Remove only known old/saving
        # roles and purchase counters (which are independently bound above).
        def panel_text(n):
            if excluded(n) or n is counter or classes(n) & {'priceContainer', 'oldPriceLabel'}:
                return ''
            return '\n'.join(panel_text(p) if isinstance(p, Node) else p for p in n.text)
        terms = panel_text(panel).strip()
        if re.search(r'в\s+наличии|предоплат|без\s+заказа|InStock|отсроч|минимальн|рознич|оптов', terms, re.I):
            return None
        labels = [n.value().strip() for n in qty.parent.children if n.tag == 'label']
        if not labels or _unit(labels[-1]) != _unit(unit):
            return None
        try:
            if float(qty.attrs.get('data-price', '')) != amount:
                return None
        except ValueError:
            return None
        if amount != structured_amount:
            return None
        panels.append((literal, unit, amount, cash.path(), terms))
    if (panels[0][:3] != panels[1][:3]
            or re.sub(r'\s+', ' ', panels[0][4]) != re.sub(r'\s+', ' ', panels[1][4])):
        return None
    # Deduplicate only the independently checked responsive current-price nodes.
    # Old/saving/recommendation/modal amounts never enter this witnessed frame.
    def owner_text(n):
        if excluded(n) or n in panel_nodes:
            return ''
        return '\n'.join(owner_text(p) if isinstance(p, Node) else p for p in n.text)
    witnessed = _owned_text(owner_text(owner))
    if re.search(r'без\s+предоплаты|без\s+заказа|InStock|в\s+наличии\s*[:：]?\s*(?:да|есть|\d)', witnessed, re.I):
        return None
    text = '\n'.join([title, 'Артикул производителя: ' + article, witnessed,
                      'Цена: ' + panels[0][0] + ' / ' + panels[0][1],
                      'Единица измерения: ' + panels[0][1], stock, panels[0][4], panels[1][4]])
    if len(text) > 20000:
        return None
    result = dict(title=title, article=article, text=text,
                  identity_basis='manufacturer_code',
                  code_observations=[dict(role='manufacturer_code', label='Артикул', value=article)])
    if admission_only:
        return result
    result.update(scope_path=owner.path(),
                  scope_sha256=hashlib.sha256(parser.source[owner.start:owner.end].encode()).hexdigest(),
                  listed_price=panels[0][2], merchant_requisites={},
                  layout_profile='favorit_order_prepay_v1',
                  evidence_paths=dict(heading=heading.path(), article=article_node.path(),
                                      current_prices=[p[3] for p in panels]))
    return result

# A browser DOM is an observed DOM, never an HTTP response. The producer's
# coherent JS read serializes it with computed-invisible nodes marked hidden.
_RENDER_FRAME=('document_url','main_title','article','owner_text','purchase_text','owner_bound','owner_truncated','observed_at')
def save_rendered_dom(body,frame,requested_url):
 if type(body) is not str or not body or len(body.encode())>2*1024*1024:raise ValueError('rendered_source_bound')
 if any(k not in frame for k in _RENDER_FRAME) or frame['owner_bound'] is not True or frame['owner_truncated'] is not False:raise ValueError('rendered_source_owner')
 from .product_identity import canonical_card_url
 import math
 stamp=frame['observed_at'];actual=canonical_card_url(frame['document_url']);requested=canonical_card_url(requested_url)
 if not actual or actual!=requested or type(stamp) not in (int,float) or not math.isfinite(stamp) or not 0<stamp<=time.time()+300:raise ValueError('rendered_source_epoch')
 raw_body=body.encode();record=dict(version=1,role='rendered_primary_dom',representation='visibility_annotated_dom',requested_url=requested,url=actual,observed_at=stamp,encoding='utf-8',body_base64=base64.b64encode(raw_body).decode(),body_sha256=hashlib.sha256(raw_body).hexdigest(),frame={k:frame[k] for k in _RENDER_FRAME})
 payload=json.dumps(record,ensure_ascii=False,sort_keys=True,allow_nan=False).encode();ref='rendered_cards/'+uuid.uuid4().hex+'.json';path=CACHE_DIR/ref;path.parent.mkdir(parents=True,exist_ok=True)
 with path.open('xb') as file:file.write(payload)
 return dict(role='rendered_primary_dom',ref=ref,record_sha256=hashlib.sha256(payload).hexdigest(),body_sha256=record['body_sha256'])

def rendered_source(coherent,main_facts,*,after=None,after_facts=None,context=None):
 base=coherent.get('rendered_source')
 if not base:return None
 source={**base,'main_facts':main_facts}
 if context:
  if after and after_facts:
   if after.get('rendered_source',{}).get('body_sha256')!=base['body_sha256']:return None
   source['photo_binding']={'after_main_facts':after_facts,'context':context}
 return source

def _read_render_frame(reference):
 if type(reference) is not dict or set(reference)!={'ref','sha256','observed_at'} or not re.fullmatch(r'main_facts/[a-f0-9]{32}\.json',reference.get('ref','')):raise ValueError('rendered_frame_ref')
 root=CACHE_DIR.resolve();path=CACHE_DIR/reference['ref']
 if path.parent.is_symlink() or path.is_symlink() or path.resolve().parent!=root/'main_facts' or path.stat().st_size>1024*1024:raise ValueError('rendered_frame_path')
 raw=path.read_bytes();frame=json.loads(raw)
 if hashlib.sha256(raw).hexdigest()!=reference['sha256'] or type(frame.get('version')) is not int or frame.get('version')!=1 or frame.get('role')!='main_card_facts' or frame.get('observed_at')!=reference['observed_at']:raise ValueError('rendered_frame_changed')
 return frame

def _read_rendered_source(evidence):
 from .product_identity import canonical_card_url
 import math
 try:
  if type(evidence) is not dict or set(evidence) not in ({'role','ref','record_sha256','body_sha256','main_facts'},{'role','ref','record_sha256','body_sha256','main_facts','photo_binding'}) or evidence['role']!='rendered_primary_dom' or not re.fullmatch(r'rendered_cards/[a-f0-9]{32}\.json',evidence['ref']):return None
  root=CACHE_DIR.resolve();path=CACHE_DIR/evidence['ref']
  if path.parent.is_symlink() or path.is_symlink() or path.resolve().parent!=root/'rendered_cards' or path.stat().st_size>4*1024*1024:return None
  raw=path.read_bytes()
  if hashlib.sha256(raw).hexdigest()!=evidence['record_sha256']:return None
  record=json.loads(raw);body=base64.b64decode(record['body_base64'],validate=True)
  if set(record)!={'version','role','representation','requested_url','url','observed_at','encoding','body_base64','body_sha256','frame'} or type(record['version']) is not int or record['version']!=1 or record['role']!='rendered_primary_dom' or record['representation']!='visibility_annotated_dom' or record['encoding']!='utf-8' or len(body)>2*1024*1024 or hashlib.sha256(body).hexdigest()!=record['body_sha256'] or record['body_sha256']!=evidence['body_sha256']:return None
  stamp=record['observed_at']
  if type(stamp) not in (int,float) or not math.isfinite(stamp) or not 0<stamp<=time.time()+300 or canonical_card_url(record['url'])!=record['url'] or record['url']!=record['requested_url']:return None
  frame=_read_render_frame(evidence['main_facts']);base={k:evidence[k] for k in ('role','ref','record_sha256','body_sha256')}
  if frame.get('rendered_source')!=base or record['frame']!={k:frame.get(k) for k in _RENDER_FRAME} or frame.get('document_url')!=record['url'] or frame['owner_bound'] is not True or frame['owner_truncated'] is not False:return None
  digest='rendered:'+evidence['record_sha256']
  with _PARSED_LOCK:
   cached=_PARSED.get(digest)
   if cached is not None:_PARSED.move_to_end(digest)
  owned=copy.deepcopy(cached[1]) if cached is not None else main_product(body.decode('utf-8'),record['url'],check_controls=False)
  if cached is None and owned is not None:
   with _PARSED_LOCK:
    _PARSED[digest]=(None,copy.deepcopy(owned));_PARSED.move_to_end(digest)
    while len(_PARSED)>64:_PARSED.popitem(last=False)
  norm=lambda value:re.sub(r'\s+',' ',value).strip()
  if not owned or owned['title']!=frame['main_title'] or owned['article']!=frame['article'] or norm(owned['text'])!=norm(frame['owner_text']):return None
  from ..source_quote import owned_purchase_facts
  parsed=owned_purchase_facts(owned['text']);visible=owned_purchase_facts(frame['purchase_text'] or frame['owner_text'])
  if parsed!=visible:return None
  # Preserve the coherent visible source spelling/time, not a later text read.
  owned=dict(owned,text=frame['owner_text']);record.pop('body_base64')
  return record,owned
 except (OSError,ValueError,TypeError,KeyError,LookupError,RecursionError):return None


def rendered_photo_valid(evidence,context,reference):
 """Attachment validity only; missing bytes never erase the financial DOM."""
 try:
  retained=_read_rendered_source(evidence)
  if not retained:return False
  record,owned=retained;frame=_read_render_frame(evidence['main_facts'])
  binding=evidence.get('photo_binding')
  if type(binding) is not dict or set(binding)!={'after_main_facts','context'} or binding['context']!=context:return False
  after=_read_render_frame(binding['after_main_facts'])
  if not _read_rendered_source({**after.get('rendered_source',{}),'main_facts':binding['after_main_facts']}):return False
  stamp=record['observed_at']
  if any(after.get(k)!=frame.get(k) for k in _RENDER_FRAME if k!='observed_at') or after.get('rendered_source',{}).get('body_sha256')!=record['body_sha256'] or not stamp<=context['captured_at']<=after['observed_at'] or after['observed_at']-stamp>30:return False
  from ..review_context import context_valid
  root=CACHE_DIR.resolve();photo=Path(reference);photo=photo if photo.is_absolute() else CACHE_DIR/photo
  return not photo.is_symlink() and photo.resolve().parent==root and photo.stat().st_size<=20*1024*1024 and context_valid(context,str(photo),record['url'],photo.read_bytes())
 except (OSError,ValueError,TypeError,KeyError,LookupError,RecursionError):return False
