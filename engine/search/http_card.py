"""Bounded static main-product evidence; JSON-LD locates, never invents quotes."""
from __future__ import annotations
from html.parser import HTMLParser
from dataclasses import dataclass,field
from pathlib import Path
import base64,hashlib,json,re,time,uuid,threading,copy,math
from collections import OrderedDict
from contextvars import ContextVar
from functools import wraps
_PARSED=OrderedDict()
_PARSED_LOCK=threading.Lock()
from urllib.parse import urljoin,urlsplit
from ..config import CACHE_DIR
from .product_identity import canonical_card_url,card_host
from .session import bounded_timeout

# Only string-input entrypoints below create these calculation scopes. Their
# freshly parsed trees and owned sets are immutable until the invocation exits.
# No source/evidence result or node survives into another invocation.
_DOM_CALCULATIONS=ContextVar('http_card_dom_calculations',default=None)

def _immutable_dom(function):
 @wraps(function)
 def invoke(*args,**kwargs):
  token=_DOM_CALCULATIONS.set(dict(visible={},text={},owned={},owned_sets={},text_bytes=0))
  try:return function(*args,**kwargs)
  finally:_DOM_CALCULATIONS.reset(token)
 return invoke

def _dom_memo(kind):
 def decorate(function):
  @wraps(function)
  def calculate(node,*args):
   context=_DOM_CALCULATIONS.get()
   if context is None:return function(node,*args)
   key=node
   if kind=='owned':
    owned=args[0];context['owned_sets'][id(owned)]=owned
    key=(node,id(owned))
   values=context[kind]
   if key in values:return values[key]
   value=function(node,*args)
   # Cap retained text only; exhaustion skips reuse without changing decisions.
   size=len(value)*4 if isinstance(value,str) else 0
   if context['text_bytes']+size<=8*1024*1024:
    values[key]=value;context['text_bytes']+=size
   return value
  return calculate
 return decorate

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

@_dom_memo('owned')
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


def _metadata_sibling_identity(parser,product,actual_url,*,with_owner=False,validated_owner=None):
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
 nested=any(n.tag=='h1' for n in product.descendants())
 if nested and (validated_owner is None or validated_owner.get('_owner') is not product or validated_owner.get('_heading') is not heading):return None
 def properties(owner,key):
  values=[]
  for node in owner.descendants():
   if node is owner or not prop(node,key) or not shown(node):continue
   parent=node.parent
   while parent is not None and 'itemscope' not in parent.attrs:parent=parent.parent
   if parent is owner:values.append(norm(node.attrs.get('content') or node.attrs.get('href') or node.value()))
  return set(filter(None,values))
 names=properties(product,'name');mpns=properties(product,'mpn');urls=properties(product,'url')
 if names!={norm(visible_text(heading))} or len(mpns)>1:return None
 correlated=not mpns
 if correlated and (not with_owner or validated_owner is None):return None
 if len(urls)>1 or any(canonical_card_url(urljoin(actual_url,url))!=canonical_card_url(actual_url) for url in urls):return None
 common=heading.parent
 while common is not None and product not in common.descendants():common=common.parent
 if common is None or common.tag in {'body','html','root'} or not shown(common):return None
 card=product if nested else heading
 if not nested:
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
 models=properties(product,'model')
 generic_rows=rows(r'(?:Артикул(?!\s+производителя)|Модель|Article|Model)\s*[:：]?\s*([A-Za-zА-Яа-я0-9._/-]{3,256})')
 generic_codes={v for _,v in generic_rows}
 if len(models)>1 or len(generic_codes)>1:return None
 if correlated:
  # A generic owned code remains unbound until the approved catalogue joins it.
  if len(generic_codes)!=1:return None
  mpn=next(iter(generic_codes))
  if models and models!={mpn} or codes and codes!={mpn.casefold()}:return None
 else:
  mpn=next(iter(mpns))
  if codes!={mpn.casefold()} or models and models!={mpn} or generic_codes and generic_codes!={mpn}:return None
 from .manufacturer_aliases import manufacturer_equal,REGISTRY
 brand_names=set();brand_role_nodes=[]
 for node in product.descendants():
  if not prop(node,'brand') or not shown(node):continue
  parent=node.parent
  while parent is not None and 'itemscope' not in parent.attrs:parent=parent.parent
  if parent is not product:continue
  if 'itemscope' in node.attrs:
   values=properties(node,'name')
   if not values:return None
   declared=norm(node.attrs.get('content')or'')
   if declared:values.add(declared)
  else:
   # A visible scalar property is not a license to use its ancestor's text.
   if node.tag in {'meta','link'}:return None
   value=norm(visible_text(node));declared=norm(node.attrs.get('content')or'')
   if not value or len(value)>128:return None
   values={value}
   if declared:values.add(declared)
  brand_names.update(values);brand_role_nodes.append(node)
 brand_rows=rows(r'(?:Бренд|Производитель|Manufacturer|Brand)\s*[:：]?\s*(.{1,128})')
 printed={v for _,v in brand_rows}
 if not brand_names or not printed:return None
 all_manufacturers=brand_names|printed
 first=sorted(brand_names)[0]
 canonical={entry.canonical for entry in REGISTRY if all(manufacturer_equal(entry.canonical,value)for value in all_manufacturers)}
 if len(canonical)>1 or not canonical and not all(manufacturer_equal(first,value)and manufacturer_equal(value,first)for value in all_manufacturers):return None
 manufacturer=next(iter(canonical))if canonical else first
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
 if with_owner:result.update(_owner=card,_purchase_owner=purchase_card,manufacturer=manufacturer,_manufacturer_rows=brand_rows,_manufacturer_nodes=brand_role_nodes,_manufacturer_values=all_manufacturers,_catalogue_candidate=correlated,_catalogue_rows=[n for n,_ in generic_rows] if correlated else [])
 return result


@_dom_memo('visible')
def _semantic_visible(node):
 while node is not None:
  style=re.sub(r'\s+','',node.attrs.get('style','')).lower()
  opacity=re.search(r'(?:^|;)opacity:([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:!important)?(?:;|$)',style)
  if excluded(node) or 'data-related-products' in node.attrs or 'data-recommendations' in node.attrs or opacity and float(opacity[1])==0:return False
  node=node.parent
 return True

@_dom_memo('text')
def _semantic_text(node):
 if not _semantic_visible(node) or re.fullmatch(r'(?:Снять фильтр|Remove filter)',node.attrs.get('title',''),re.I):return ''
 result=[]
 for part in node.text:
  if isinstance(part,Node):
   value=_semantic_text(part);result.append(('\n'+value+'\n') if part.tag in {'div','p','h1','h2','dt','dd','li','tr','section','article','main','br'} else value)
  else:result.append(part)
 return ''.join(result)

def _factory_features(owner,*,caption_cells=False):
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
  if caption_cells and not re.search(r'(?:label|param[-_]name|property[-_]name)',cells[0].attrs.get('class',''),re.I):continue
  if caption_cells and any(any(v.tag in {'div','section','article','table','ul','button','input'}for v in cell.descendants()if v is not cell)for cell in cells):continue
  values=[re.sub(r'\s+',' ',_semantic_text(n)).strip() for n in cells]
  if all(values) and len(values[0])<=128 and len(values[1])<=256:rows.append(dict(label=values[0],value=values[1],label_path=cells[0].path(),value_path=cells[1].path(),direct_value=False))
 return rows

def _http_product_claims(parser,owner,purchase):
 """Coverage of the complete owned HTTP DOM, by node identity (never text lookup)."""
 nodes=[n for n in owner.descendants()if _semantic_visible(n)];features=[]
 critical=re.compile(r'упаковк|фасовк|комплект|pack|quantity|количеств',re.I)
 # Closed caption/value structure is evidence of a property, not immunity.
 for feature in _factory_features(owner):
  left=next((n for n in nodes if n.path()==feature['label_path']),None)
  right=next((n for n in nodes if n.path()==feature['value_path']),None)
  if left is None or right is None:continue
  caption=bool(re.search(r'(?:label|param[-_]name|property[-_]name)',left.attrs.get('class',''),re.I))
  simple=not any(v.tag in {'div','section','article','table','ul','button','input'}and _semantic_text(v).strip()for cell in(left,right)for v in cell.descendants()if v is not cell)
  if (caption and simple)or (critical.search(feature['label'])and simple):features.append(feature)
 field_nodes={}
 for i,f in enumerate(features):
  for key in ('label_path','value_path'):
   root=next(n for n in nodes if n.path()==f[key])
   for n in root.descendants():field_nodes.setdefault(id(n),[]).append(i)
 bound_control_paths=[]
 if purchase:
  bound_control_paths=[tuple(q[k])for q in purchase.get('quotations',[])for k in ('price_path','denominator_path')]+[tuple(v['role_path'])for v in purchase.get('monetary_roles',[])]+[tuple(purchase['purchase_control_path'])]
  for q in purchase.get('quotations',[]):
   price=next((n for n in nodes if n.path()==q['price_path']),None)
   if price is not None and price.parent is not None and re.fullmatch(r'[\d\s.,]+(?:₽|RUB|руб\.?)',' '.join(_semantic_text(price.parent).split()),re.I):bound_control_paths.append(tuple(price.parent.path()))
 entries=[]
 negative=re.compile(r'без\s+(?:гаек|винтов|болтов|крепеж|крепёж)|только\s+(?:винты|гайки|болты|часть)|(?:прода[её]тся|приобретается|заказывается)\s+отдельно|часть\s+комплекта|неполный\s+комплект|перепаков|расфасов|не\s+вход',re.I)
 for n in nodes:
  text=' '.join(' '.join(v for v in n.text if isinstance(v,str)).split())
  if not text or n.tag in {'script','style','meta','noscript'}:continue
  ancestors=list(_ancestors(n));ancestors=ancestors[:ancestors.index(owner)+1]if owner in ancestors else ancestors
  role='primary_unknown';context=n;refs=[]
  if negative.search(text):role='primary_negative'
  elif id(n)in field_nodes:role='primary_feature';refs=field_nodes[id(n)]
  elif n.tag=='h1'and prop(n,'name'):role='primary_title'
  else:
   # UI authority is structural AND content bounded; all acquisition/count
   # assertions are promoted back to primary before these classifications.
   claim=bool(re.search(r'комплект|упаковк|фасовк|в\s+уп\b|\bуп\s*=|шт\s*в|толщин|высот|CS\s*45|CD\s*45|H\d|га[её]к|винт|болт|креп[её]ж',text,re.I))
   offers=next((a for a in ancestors if prop(a,'offers')),None)
   logistics=next((a for a in ancestors if re.search(r'(?:delivery|city_shops|item_tabs_av)',a.attrs.get('class','')+' '+a.attrs.get('id',''),re.I)),None)
   media=next((a for a in ancestors if re.search(r'(?:image|video)',a.attrs.get('class','')+' '+a.attrs.get('id',''),re.I)and any(v.tag in {'img','video','iframe'}or(v.tag=='a'and re.search(r'video|\.pdf',v.attrs.get('href',''),re.I))for v in a.descendants())),None)
   related=next((a for a in ancestors if re.search(r'(?:suggest|recommend)',a.attrs.get('class',''),re.I)and any(v.tag=='a'and v.attrs.get('href')for v in a.descendants())),None)
   avail=next((a for a in ancestors if re.search(r'(?:avail|stock)',a.attrs.get('class',''),re.I)and re.search(r'склад|налич|срок',_semantic_text(a),re.I)),None)
   nav=next((a for a in ancestors if a.tag in {'a','button'}or a.attrs.get('role')=='tab'or re.search(r'\btab(?:\s|_)|scrollto',a.attrs.get('class',''))),None)
   money_control=bool(re.fullmatch(r'[\d\s.,₽×+−–—/-]+|упак\.?|на сумму|Добавить в корзину|В корзине|В корзину|Купить|Плати частями ×|от|\d+ платежа',text,re.I))
   logistics_control=bool(re.fullmatch(r'Доставка в регион|(?:Склад|Магазин) «[^<>]{1,80}»|Курьер|ПВЗ [А-Яа-яA-Za-z0-9 ]{1,40}|\d{1,2} [а-я]{3,12}|бесплатно|ориентировочно, дата доставки зависит от даты оплаты или подтверждения заказа|для посылок массой до \d+ кг|[А-ЯЁ][а-яё]+, (?:ул\.|пр\.|пр-кт) [А-Яа-яЁё0-9 .,-]{1,100}|нет в наличии|[\d\s.,₽]+',text,re.I))
   geography_control=bool(re.fullmatch(r'[А-ЯЁ][а-яё-]{1,40}',text)and any('geolink'in a.attrs.get('class','')for a in ancestors))
   stock_control=bool(re.fullmatch(r'со склада г\.[А-Яа-яЁё .-]+,?|\d+(?:[.,]\d+)? упак\.?|срок \d+(?:-\d+)? дней',text,re.I))
   media_control=bool(re.fullmatch(r'Изображения служат только для ознакомления,|см\. техническую документацию|Видео',text))
   if media is not None and re.search(r'hosting',n.attrs.get('class','')):
    from urllib.parse import urlsplit
    media_control=any(text.casefold()==(urlsplit(v.attrs.get('href','')).hostname or'').removeprefix('www.').split('.')[0]for v in media.parent.descendants()if v.tag=='a')
   related_target=False
   if related is not None and n.tag=='a'and n.attrs.get('href'):
    for group in ancestors:
     if group is related:break
     same=[v for v in group.descendants()if v.tag=='a'and v.attrs.get('href')==n.attrs.get('href')]
     if len(same)>=2 and any(any(c.tag=='img'for c in v.descendants())for v in same):related_target=True;break
   control_path=next((a for a in ancestors if tuple(a.path())in bound_control_paths),None)
   if control_path is None and purchase and text in {'+','−','-'}and n.tag=='button':
    control_path=next((a for a in ancestors[:3]if any(v.path()==purchase['input_path']for v in a.descendants())),None)
   if not claim and offers is not None and money_control and control_path is not None:role='purchase_control';context=control_path
   elif not claim and logistics is not None and(logistics_control or geography_control):role='logistics';context=logistics
   elif not claim and avail is not None and stock_control:role='stock';context=avail
   elif not claim and media is not None and media_control:role='media';context=media
   elif not claim and related is not None and (text=='Мы рекомендуем'or related_target):role='related_navigation';context=related
   elif not claim and nav is not None and (re.fullmatch(r'Описание|Отзывы|Все параметры|Все характеристики|Фото|Видео|Чертежи|Технические параметры|Скачать в PDF',text,re.I)):role='navigation';context=nav
   elif n.tag in {'h2','h3'}and text=='Видео'and n.parent is not None and any(re.search(r'video',v.attrs.get('href',''))for v in n.parent.descendants()):role='media_heading'
   elif n.tag in {'h2','h3'}and re.fullmatch(r'Технические параметры|Разрешительная документация|Сроки доставки|Цена и наличие в магазинах',text):role='section_heading'
   elif re.fullmatch(r'Товар не подлежит обязательной сертификации и декларированию\.',text):role='unrequested_certification_reference'
  fragment=parser.source[n.start:n.end]
  entries.append(dict(path=n.path(),text=text,text_sha256=hashlib.sha256(text.encode()).hexdigest(),fragment_sha256=hashlib.sha256(fragment.encode()).hexdigest(),role=role,feature_indices=refs,context_path=context.path(),context_sha256=hashlib.sha256(parser.source[context.start:context.end].encode()).hexdigest()))
 if len(entries)>8192:raise ValueError('http_claim_coverage_limit')
 ledger=dict(version=1,role='http_product_claim_coverage',owner_path=owner.path(),owner_sha256=hashlib.sha256(parser.source[owner.start:owner.end].encode()).hexdigest(),entries=entries)
 return features,ledger


def _factory_field_roles(owner,features,purchase=None,*,inline_identity=False):
 """Exact source leaf roles, not label-based immunity for arbitrary fields."""
 nodes={tuple(n.path()):n for n in owner.descendants() if _semantic_visible(n)}
 entries=[]
 for feature in features:
  paths=[tuple(feature[k]) for k in ('label_path','value_path')]
  left,right=[nodes.get(p) for p in paths];role='technical';binding={}
  if left is not None and right is not None and ((not left.children and not right.children)or inline_identity):
   value=re.sub(r'\s+',' ',_semantic_text(right)).strip()
   if _semantic_text(left).strip().rstrip(':：').casefold() in {'номенклатурный номер','sku','код товара','внутренний код'} and (prop(right,'sku')or(inline_identity and any(prop(v,'sku')for v in right.descendants()))) and re.fullmatch(r'[A-Za-zА-Яа-я0-9._/-]{3,128}',value):
    observed=[n for n in nodes.values() if prop(n,'sku')]
    if len(observed)==1 and (observed[0] is right or(inline_identity and observed[0]in list(right.descendants()))):role='seller_sku';binding={'value':value,'path':right.path()}
   elif _semantic_text(left).strip().rstrip(':：').casefold() in {'остаток','количество на складе','stock','stock quantity'} and prop(right,'inventoryLevel') and re.fullmatch(r'\d+(?:[.,]\d+)?(?:\s*(?:уп|упак|шт)\.?)?',value,re.I):
    role='stock';binding={'value':value,'path':right.path()}
   elif all(n.tag=='a' and n.attrs.get('href') for n in (left,right)) and {_semantic_text(left).strip(),value}<={'Описание','Отзывы','Характеристики','Description','Reviews','Specifications'}:
    role='navigation';binding={'label':_semantic_text(left).strip(),'value':value,'paths':[left.path(),right.path()]}
  if purchase is not None and role=='technical':
   from decimal import Decimal
   for quote in purchase['quotations']:
    root=nodes.get(tuple(quote['price_path']))
    if root is None or left is None or right is None:continue
    descendants=list(root.descendants())
    if left not in descendants or right not in descendants:continue
    full=re.sub(r'\s+',' ',_semantic_text(root)).strip()
    pure=re.fullmatch(r'([\d \u00a0\u202f]+(?:[.,]\d{1,2})?)\s*(?:₽|руб\.?|RUB)(?:\s*/\s*(\d+(?:[.,]\d+)?)?\s*(уп|упак|упаковка|упаковки)\.?)?',full,re.I)
    if pure and Decimal(re.sub(r'\s','',pure[1]).replace(',','.'))==Decimal(quote['amount']) and (not pure[3] or Decimal((pure[2]or'1').replace(',','.'))==Decimal(quote['denominator'])):
     role='current_price';binding={'quote':quote,'path':root.path(),'text':full};break
  entries.append({'feature':feature,'role':role,'binding':binding})
 return {'version':1,'role':'source_bound_field_roles','owner_path':owner.path(),'entries':entries}

def _factory_prose_blocks(owner,*,typed_leaf_paths=(),http_caption=False):
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
  if not n.children and tuple(n.path()) in typed_leaf_paths:continue
  direct=re.sub(r'\s+',' ',' '.join(v for v in n.text if isinstance(v,str))).strip()
  if not direct:continue
  ancestors=[];parent=n
  while parent is not None and parent is not owner.parent:ancestors.append(parent);parent=parent.parent
  if any(a.tag in {'script','style'} or re.fullmatch(r'(?:Снять фильтр|Remove filter)',a.attrs.get('title',''),re.I) for a in ancestors):continue
  if http_caption and any(re.search(r'image[-_]warning',a.attrs.get('class',''))for a in ancestors)and direct in {'Изображения служат только для ознакомления,','см. техническую документацию'}:continue
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

def _factory_owned_candidate(parser,product,actual_url,*,validated_purchase=None,validated_owner=None,caption_cells=False):
 """Neutral current purchase facts joined to active technical evidence.

 The rejection-only join does not itself admit cash. AggregateOffer amounts
 are ignored; only independently visible selected purchase facts are used.
 """
 frame=_metadata_sibling_identity(parser,product,actual_url,with_owner=True,validated_owner=validated_owner)
 if not frame:return None
 from .factory_reference import lookup_reference,read_reference,FactoryEvidenceError
 from .manufacturer_aliases import manufacturer_equal
 try:
  reference=lookup_reference(frame['manufacturer'],frame['article'])
  if not reference:return None
  correlated=frame.pop('_catalogue_candidate',False)
  if correlated:
   technical=read_reference(reference)
   if technical['article']!=frame['article'] or not manufacturer_equal(technical['manufacturer'],frame['manufacturer']):return None
   frame['identity_basis']='catalogue_correlated_manufacturer_code'
   frame['catalogue_correlated_identity']=dict(version=1,role='catalogue_correlated_manufacturer_code',article=frame['article'],manufacturer=frame['manufacturer'],reference=reference,review=technical['review'],supplier_scope_sha256=validated_owner['scope_sha256'],supplier_text_sha256=hashlib.sha256(validated_owner['text'].encode()).hexdigest())
 except (FactoryEvidenceError,OSError,ValueError,KeyError,TypeError):return None
 catalogue_rows=frame.pop('_catalogue_rows',[])
 manufacturer_rows=frame.pop('_manufacturer_rows',[])
 manufacturer_nodes=frame.pop('_manufacturer_nodes',[])
 manufacturer_values=frame.pop('_manufacturer_values',set())
 owner=frame.pop('_owner');purchase_card=frame.pop('_purchase_owner');owned=[n for n in owner.descendants() if _semantic_visible(n)]
 from ..source_quote import owned_purchase_facts
 if validated_purchase is None:
  purchase=owned_purchase_facts(frame['text'])
 else:
  # Only the source reader supplies this already rederived, typed witness.
  # Structural factory ownership must be identical to its financial owner.
  if validated_owner is None or any(frame.get(k)!=validated_owner.get(k) for k in ('title','article')) or frame['text']!=validated_owner.get('text','').strip():return None
  if owner.path()!=validated_owner.get('scope_path') or hashlib.sha256(parser.source[owner.start:owner.end].encode()).hexdigest()!=validated_owner.get('scope_sha256'):return None
  if correlated:
   frame['code_observations']=validated_owner['code_observations']
  if validated_purchase.get('price_denominator')!=1:return None
  purchase={'reason':'','raw_amount':float(validated_purchase['raw_amount']),'sale_unit':validated_purchase['sale_unit']}
 from ..price_capture import _unit
 if purchase['reason'] or _unit(purchase['sale_unit'])!='уп':return None
 declared=[n for n in owned if 'data-count-per-pack' in n.attrs or 'data-pack-size' in n.attrs]
 package_candidate=None;pack_counts=None
 if not declared:
  if validated_purchase is None:return None
  # Existing typed quotation already binds the SAME owner, input and action.
  regions=[n for n in owned if n.path()==validated_purchase['row_path']]
  fields=[n for n in owned if n.path()==validated_purchase['input_path']]
  actions=[n for n in owned if n.path()==validated_purchase['purchase_control_path']]
  if len(regions)!=1 or len(fields)!=1 or len(actions)!=1 or fields[0]not in regions[0].descendants()or actions[0]not in regions[0].descendants():return None
  purchase_owner=regions[0]
  package_candidate=dict(version=1,role='factory_package_inference_candidate',purchase_contract=validated_purchase)
 else:
  inputs=[n for n in owned if n.tag=='input' and n.attrs.get('type')=='number' and ('data-count-per-pack' in n.attrs or 'data-pack-size' in n.attrs)]
  if len(inputs)!=1 or len(declared)!=1:return None
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
  pack_counts={'count_per_pack':control.attrs['data-count-per-pack'],'pack_size':control.attrs['data-pack-size']}
 typed_leaf_paths=set()
 for root in manufacturer_nodes:
  for node in root.descendants():
   if not node.children and _semantic_visible(node) and re.sub(r'\s+',' ',_semantic_text(node)).strip() in manufacturer_values:typed_leaf_paths.add(tuple(node.path()))
 for row,_ in manufacturer_rows:
  for node in row.descendants():
   if not node.children and _semantic_visible(node) and re.sub(r'\s+',' ',_semantic_text(node)).strip() in (manufacturer_values|{'Бренд','Производитель','Manufacturer','Brand'}):typed_leaf_paths.add(tuple(node.path()))
 if correlated:
  for row in catalogue_rows:
   for node in row.descendants():
    if not node.children and _semantic_visible(node) and re.sub(r'\s+',' ',_semantic_text(node)).strip() in {frame['article'],'Артикул','Модель','Article','Model'}:typed_leaf_paths.add(tuple(node.path()))
 if validated_purchase is not None:
  # Only complete leaf literals at the already rederived quotation paths.
  # An ancestor containing a price never gains prose-suppression authority.
  from decimal import Decimal
  for quote in validated_purchase['quotations']:
   for node in owned:
    if node.children or node.path() not in (quote['price_path'],quote['denominator_path']):continue
    text=re.sub(r'\s+',' ',_semantic_text(node)).strip()
    pure=re.fullmatch(r'([\d \u00a0\u202f]+(?:[.,]\d{1,2})?)\s*(?:₽|руб\.?|RUB)(?:\s*/\s*(\d+(?:[.,]\d+)?)?\s*(уп|упак|упаковка|упаковки)\.?)?',text,re.I)
    if pure and Decimal(re.sub(r'\s','',pure[1]).replace(',','.'))==Decimal(quote['amount']) and (not pure[3] or Decimal((pure[2]or'1').replace(',','.'))==Decimal(quote['denominator'])):typed_leaf_paths.add(tuple(node.path()))
 frame['factory_features']=_factory_features(owner)
 if caption_cells:frame['factory_features'],frame['factory_node_ledger']=_http_product_claims(parser,owner,validated_purchase)
 frame['factory_field_roles']=_factory_field_roles(owner,frame['factory_features'],validated_purchase,inline_identity=caption_cells)
 for entry in frame['factory_field_roles']['entries']:
  if entry['role']!='technical':
   for key in ('label_path','value_path'):
    path=tuple(entry['feature'][key]);node=next((n for n in owned if tuple(n.path())==path),None)
    if node is not None and not node.children:typed_leaf_paths.add(path)
 frame['factory_prose_blocks']=_factory_prose_blocks(owner,typed_leaf_paths=typed_leaf_paths,http_caption=caption_cells)
 frame['factory_owner_text']=_semantic_text(owner).strip()
 if len(frame['factory_owner_text'])>200000:return None
 frame.update(scope_path=owner.path(),scope_sha256=hashlib.sha256(parser.source[owner.start:owner.end].encode()).hexdigest(),listed_price=purchase['raw_amount'],sale_unit=purchase['sale_unit'],factory_reference=reference,purchase_scope_sha256=hashlib.sha256(parser.source[purchase_owner.start:purchase_owner.end].encode()).hexdigest(),merchant_requisites={},merchant_assertions=[])
 if pack_counts is not None:frame['pack_counts']=pack_counts
 if package_candidate is not None:frame['factory_package_candidate']=package_candidate
 return frame

@_immutable_dom
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


def http_primary_source(reference):
 """Bind existing immutable HTTP bytes to the structural purchase reader."""
 try:
  ref=reference.get('ref','')
  if not re.fullmatch(r'http_cards/[a-f0-9]{32}\.json',ref):return None
  path=CACHE_DIR/ref
  if path.is_symlink()or path.parent.is_symlink()or path.resolve().parent!=CACHE_DIR.resolve()/'http_cards'or path.stat().st_size>3*1024*1024:return None
  raw=path.read_bytes();record=json.loads(raw)
  if hashlib.sha256(raw).hexdigest()!=reference.get('record_sha256')or record.get('body_sha256')!=reference.get('body_sha256')or type(record.get('version'))is not int or record['version']!=1 or type(record.get('status'))is not int or record['status']!=200:return None
  return dict(role='http_primary_dom',ref=ref,record_sha256=reference['record_sha256'],body_sha256=reference['body_sha256'],observed_at=record['observed_at'])
 except (OSError,ValueError,TypeError,KeyError):return None

def _validated_http_owner(evidence):
 try:
  if type(evidence)is not dict or set(evidence)!={'role','ref','record_sha256','body_sha256','observed_at'}or evidence.get('role')!='http_primary_dom':return None
  canonical=http_primary_source(evidence)
  if canonical!=evidence:return None
  raw=(CACHE_DIR/evidence['ref']).read_bytes();record=json.loads(raw);body=base64.b64decode(record['body_base64'],validate=True)
  if len(body)>2*1024*1024 or hashlib.sha256(body).hexdigest()!=evidence['body_sha256']:return None
  owned=_neutral_owner(body.decode(record.get('encoding')or'utf-8'),record['url'])
  if not owned:return None
  record.pop('body_base64');record['role']='http_primary_dom'
  return record,owned
 except (OSError,ValueError,TypeError,KeyError,RecursionError):return None

def read_source(evidence,*,allow_rendered=False):
 """Read only a bounded owned capture, never an arbitrary client filesystem path."""
 if type(evidence)is dict and evidence.get('role')=='http_primary_dom':
  keys={'role','ref','record_sha256','body_sha256','observed_at'};meta={'scope_path','scope_sha256','code_profile','identity_basis','code_observations'}
  if set(evidence)-keys-meta or not keys<=set(evidence):return None
  retained=_read_rendered_source({k:evidence[k]for k in keys})
  if not retained:return None
  observed=retained[1]
  for key in meta & set(evidence):
   if evidence[key]!=('owned_codes_v1'if key=='code_profile'else observed.get(key)):return None
  return retained
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
  if observed is None:
   primary=http_primary_source(evidence)
   return _read_rendered_source(primary)if primary else None
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

# Neutral capture projection: deliberately mirrors _MAIN_FACTS_READINESS_JS.
# Full DOM is retained separately; this text projection is never cash authority.
def _capture_excluded(node):
 while node is not None:
  a=node.attrs;style=re.sub(r'\s+','',a.get('style','')).lower()
  opacity=re.search(r'(?:^|;)opacity:([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:!important)?(?:;|$)',style)
  if (node.tag in {'aside','nav','footer','dialog'} or a.get('role')=='dialog' or 'hidden'in a
      or a.get('aria-hidden')=='true' or 'data-related-products'in a or 'data-recommendations'in a
      or any(re.fullmatch(r'related[-_]products|recommendations|recommended[-_]products|product[-_]recommendations|cart[-_]modal',c,re.I)for c in a.get('class','').split())
      or 'display:none'in style or 'visibility:hidden'in style or 'visibility:collapse'in style
      or opacity and float(opacity[1])<=0):return True
  node=node.parent
 return False

def _capture_text(node,*,complete=False):
 context=_DOM_CALCULATIONS.get()
 values=context.setdefault('capture_projection',{})if context is not None else None
 key=(node,complete)
 if values is not None and key in values:return values[key]
 value=_capture_text_uncached(node,complete=complete)
 if values is not None:
  size=len(value)*4
  if context['text_bytes']+size<=8*1024*1024:values[key]=value;context['text_bytes']+=size
 return value

def _capture_text_uncached(node,*,complete=False):
 if _capture_excluded(node) or node.tag in {'script','style','template'}:return ''
 if not complete and (node.tag in {'del','s'} or any(v in node.attrs.get('class','')for v in ('oldPrice','old-price','economy','saving'))):return ''
 result=[];boundary=None
 for child in node.text:
  if isinstance(child,Node):
   if _capture_excluded(child):continue
   level=int(child.tag[1])if re.fullmatch('h[1-6]',child.tag)else None
   control=child.attrs.get('role')in {'tab','button'}or node.attrs.get('role')=='tablist'
   if not complete and level and not control and _RELATED_SECTION.fullmatch(''.join(_capture_text(c)if isinstance(c,Node)else c for c in child.text).strip()):boundary=level;continue
   if boundary is not None:
    if level and level<=boundary:boundary=None
    else:continue
   result.append(_capture_text(child,complete=complete))
  elif boundary is None:result.append(child)
 value=''.join(result)
 return '\n'+value+'\n'if node.tag in {'div','p','section','article','h1','h2','h3','h4','h5','h6','br','li','dt','dd','tr','td','th'}else value

@_immutable_dom
def _neutral_owner(html,url):
 """Structural, requested-data-free owner. Does not admit a price."""
 parser=MainParser(html,check_controls=False);all_nodes=list(parser.root.descendants())
 headings=[n for n in all_nodes if n.tag=='h1']
 if len(headings)!=1 or _capture_excluded(headings[0]):return None
 heading=headings[0];title=_capture_text(heading).strip()
 products=[n for n in all_nodes if itemtype(n,'Product') and not any(p.tag in {'aside','nav','footer','dialog'}or p.attrs.get('role')=='dialog'or'data-related-products'in p.attrs or'data-recommendations'in p.attrs for p in _ancestors(n))]
 if len(products)>1:return None
 owner=heading
 while owner is not None and not(itemtype(owner,'Product')or owner.tag in {'form','main'}):owner=owner.parent
 if owner is not None and products and owner not in products:return None
 sibling=False
 if owner is None and not products:
  declared=[n for n in all_nodes if n.attrs.get('data-product-id','').strip()and not _capture_excluded(n)]
  if len(declared)!=1:return None
  owner=declared[0];sibling=True
  if any(n.tag=='h1'for n in owner.descendants()):return None
 if owner is None or _capture_excluded(owner):return None
 text=(_capture_text(heading).strip()+'\n'if sibling else '')+_capture_text(owner)
 codes=list(dict.fromkeys(m[1]for m in re.finditer(r'(?:^|\n)\s*(?:Артикул(?: производителя| товара)?|арт\.|Код производителя|Article|SKU|Manufacturer code)\s*[:：]?\s*\n?\s*([A-Za-zА-Яа-я0-9._-]{3,})(?=\s|$)',text,re.I)))
 if len(codes)>1:return None
 article=codes[0]if codes else ''
 if sibling and (not article or article.casefold()not in [v.casefold()for v in re.split(r'[^A-Za-zА-Яа-я0-9._-]+',title)]):return None
 own=[n for n in owner.descendants()if not _capture_excluded(n)]
 typed=[]
 def retain(role,label,value):
  if not re.fullmatch(r'[A-Za-zА-Яа-я0-9._/-]{3,256}',value):return False
  item=dict(role=role,label=label,value=value)
  if item not in typed:typed.append(item)
  return len(typed)<=32
 for n in own:
  if prop(n,'model')or prop(n,'mpn')or prop(n,'sku'):
   value=(n.attrs.get('content')or _capture_text(n).strip())
   role='manufacturer_code'if prop(n,'mpn')else 'model'if prop(n,'model')else 'seller_sku'
   if not retain(role,'itemprop:'+('mpn'if role=='manufacturer_code'else'model'if role=='model'else'sku'),value):return None
 for role in ('manufacturer_code','model','seller_sku'):
  if len({v['value']for v in typed if v['role']==role})>1:return None
 manufacturers={v['value']for v in typed if v['role']=='manufacturer_code'}
 if manufacturers and article and manufacturers!={article}:return None
 # A printed generic article may be a declared model while SKU remains distinct.
 models=[n for n in own if prop(n,'model')]
 if models and article:
  if len(models)!=1 or models[0].tag=='meta' or _capture_text(models[0]).strip()!=article:return None
  parent=models[0].parent;labelled=False
  from ..purchase_scope import labelled_article_value
  while parent is not None and parent is not owner:
   value=re.sub(r'\s+',' ',_capture_text(parent)).strip()
   if len(value)>200:break
   if labelled_article_value(value)==article:labelled=True;break
   parent=parent.parent
  if not labelled:return None
 if sibling:
  # Canonical structured identity is corroboration, never cash authority.
  structured=[]
  for n in all_nodes:
   if n.tag=='script'and n.attrs.get('type','').lower()=='application/ld+json':
    data=json.loads(''.join(v for v in n.text if isinstance(v,str)))
    def walk(v,depth=0):
     if depth>32:raise ValueError('structured_depth')
     if isinstance(v,dict):
      if v.get('@type')=='Product':structured.append(v)
      for c in v.values():walk(c,depth+1)
     elif isinstance(v,list):
      for c in v:walk(c,depth+1)
    walk(data)
  if len(structured)!=1:return None
  product=structured[0];brand=product.get('brand')or{};name=product.get('name');sku=str(product.get('sku')or'')
  if (product.get('url')!=canonical_card_url(url)or sku!=article or not isinstance(name,str)
      or title not in {name,name+' '+str(brand.get('name',''))+' '+sku}):return None
  canonicals=[n.attrs.get('href','')for n in all_nodes if n.tag=='link'and'canonical'in n.attrs.get('rel','').split()]
  if not canonicals or {canonical_card_url(urljoin(url,v))for v in canonicals}!={canonical_card_url(url)}:return None
 return dict(title=title,article=article,text=text,identity_basis='manufacturer_code'if manufacturers else'legacy_article'if article else'title_only',code_observations=typed,
             scope_path=owner.path(),scope_sha256=hashlib.sha256(parser.source[owner.start:owner.end].encode()).hexdigest(),
             _parser=parser,_owner=owner,_nodes=own,_heading=heading,_sibling=sibling,
             complete_visible_text=(_capture_text(heading,complete=True).strip()+'\n'if sibling else '')+_capture_text(owner,complete=True))

def _ancestors(node):
 while node is not None:yield node;node=node.parent

def _validated_rendered_owner(evidence):
 if type(evidence)is dict and evidence.get('role')=='http_primary_dom':return _validated_http_owner(evidence)
 """Private immutable structural reader; deliberately acyclic/no cash admission."""
 try:
  keys={'role','ref','record_sha256','body_sha256','main_facts'}
  if type(evidence)is not dict or set(evidence)not in (keys,keys|{'photo_binding'})or evidence.get('role')!='rendered_primary_dom'or not re.fullmatch(r'rendered_cards/[a-f0-9]{32}\.json',evidence.get('ref','')):return None
  root=CACHE_DIR.resolve();path=CACHE_DIR/evidence['ref']
  if path.parent.is_symlink()or path.is_symlink()or path.resolve().parent!=root/'rendered_cards'or path.stat().st_size>4*1024*1024:return None
  raw=path.read_bytes()
  if hashlib.sha256(raw).hexdigest()!=evidence['record_sha256']:return None
  record=json.loads(raw);body=base64.b64decode(record['body_base64'],validate=True)
  if (set(record)!={'version','role','representation','requested_url','url','observed_at','encoding','body_base64','body_sha256','frame'}or type(record['version'])is not int or record['version']!=1
      or record['role']!='rendered_primary_dom'or record['representation']!='visibility_annotated_dom'or record['encoding']!='utf-8'or len(body)>2*1024*1024
      or hashlib.sha256(body).hexdigest()!=record['body_sha256']or record['body_sha256']!=evidence['body_sha256']):return None
  stamp=record['observed_at']
  if type(stamp)not in (int,float)or not math.isfinite(stamp)or not 0<stamp<=time.time()+300 or canonical_card_url(record['url'])!=record['url']or record['requested_url']!=record['url']:return None
  frame=_read_render_frame(evidence['main_facts']);base={k:evidence[k]for k in ('role','ref','record_sha256','body_sha256')}
  if (frame.get('rendered_source')!=base or record['frame']!={k:frame.get(k)for k in _RENDER_FRAME}or frame.get('document_url')!=record['url']or frame.get('observed_at')!=stamp
      or frame.get('owner_bound')is not True or frame.get('owner_truncated')is not False):return None
  if any(type(frame.get(k))is not str for k in ('document_url','main_title','article','owner_text','purchase_text')):return None
  owned=_neutral_owner(body.decode('utf-8'),record['url'])
  norm=lambda v:re.sub(r'\s+',' ',v).strip()
  if not owned or owned['title']!=frame['main_title']or owned['article']!=frame['article']or norm(owned['text'])!=norm(frame['owner_text']):return None
  # Raw independent projection retained, not replaced with frame.owner_text.
  record.pop('body_base64');return record,owned
 except (OSError,ValueError,TypeError,KeyError,LookupError,RecursionError):return None

def _legacy_structural_owner(evidence,record,owned):
 """Acyclic tree for an already verified legacy owner, not price admission."""
 path=CACHE_DIR/evidence['ref']
 if path.is_symlink()or path.parent.is_symlink()or path.resolve().parent not in {CACHE_DIR.resolve()/'rendered_cards',CACHE_DIR.resolve()/'http_cards'}or path.stat().st_size>4*1024*1024:raise ValueError('legacy_source_boundary')
 with path.open('rb')as stream:raw=stream.read(4*1024*1024+1)
 if len(raw)>4*1024*1024 or hashlib.sha256(raw).hexdigest()!=evidence['record_sha256']:raise ValueError('legacy_source_changed')
 retained=json.loads(raw);body=base64.b64decode(retained['body_base64'],validate=True)
 if hashlib.sha256(body).hexdigest()!=record['body_sha256']:raise ValueError('legacy_body_changed')
 html=body.decode('utf-8');parser=MainParser(html,check_controls=False)
 scopes=[n for n in parser.root.descendants()if n.path()==owned.get('scope_path')]
 if len(scopes)!=1:raise ValueError('legacy_owner_scope')
 owner=scopes[0]
 if hashlib.sha256(html[owner.start:owner.end].encode()).hexdigest()!=owned.get('scope_sha256'):raise ValueError('legacy_owner_changed')
 return dict(owned,_owner=owner,_nodes=_owned_nodes(owner),_parser=parser)

def _read_rendered_source(evidence,*,identity_only=False):
 legacy=_legacy_read_rendered_source(evidence,identity_only=identity_only)
 if legacy:
  if not identity_only:
   try:
    from .owned_purchase_row import _primary_structured_contract
    from decimal import Decimal
    record,owned=legacy
    _primary_structured_contract(record,_legacy_structural_owner(evidence,record,owned),{Decimal(str(owned['listed_price']))})
   except (OSError,ValueError,TypeError,KeyError,RecursionError):return None
  return legacy
 retained=_validated_rendered_owner(evidence)
 if not retained:return None
 record,owned=retained
 if identity_only:return record,{k:v for k,v in owned.items()if not k.startswith('_')}
 from .owned_purchase_row import _parse_validated_owner
 quote=_parse_validated_owner(record,owned)
 if not quote:return None
 enriched={}
 products=[n for n in owned['_parser'].root.descendants() if itemtype(n,'Product')]
 if len(products)==1:
  factory=_factory_owned_candidate(owned['_parser'],products[0],record['url'],validated_purchase=quote,validated_owner=owned,caption_cells=record.get('role')=='http_primary_dom')
  if factory:
   enriched={k:factory[k] for k in ('manufacturer','factory_features','factory_field_roles','factory_prose_blocks','factory_owner_text','factory_reference','purchase_scope_sha256')}
   for key in ('pack_counts','factory_package_candidate','factory_node_ledger'):
    if key in factory:enriched[key]=factory[key]
   if factory.get('identity_basis')=='catalogue_correlated_manufacturer_code':
    enriched.update(identity_basis=factory['identity_basis'],catalogue_correlated_identity=factory['catalogue_correlated_identity'])
 return record,{**{k:v for k,v in owned.items()if not k.startswith('_')},**enriched,'listed_price':float(quote['raw_amount']),'sale_unit':quote['sale_unit'],'purchase_contract':quote,'merchant_requisites':{},'merchant_assertions':[]}


def _legacy_read_rendered_source(evidence,*,identity_only=False):
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
  digest=('identity-rendered:'if identity_only else'rendered:')+evidence['record_sha256']
  with _PARSED_LOCK:
   cached=_PARSED.get(digest)
   if cached is not None:_PARSED.move_to_end(digest)
  owned=copy.deepcopy(cached[1]) if cached is not None else main_product(body.decode('utf-8'),record['url'],check_controls=False,admission_only=identity_only)
  if cached is None and owned is not None:
   with _PARSED_LOCK:
    _PARSED[digest]=(None,copy.deepcopy(owned));_PARSED.move_to_end(digest)
    while len(_PARSED)>64:_PARSED.popitem(last=False)
  norm=lambda value:re.sub(r'\s+',' ',value).strip()
  from ..source_quote import primary_text
  expected_text=primary_text(frame['owner_text'])if identity_only else frame['owner_text']
  actual_text=primary_text(owned['text'])if owned and identity_only else owned['text']if owned else ''
  if not owned or owned['title']!=frame['main_title'] or owned['article']!=frame['article'] or norm(actual_text)!=norm(expected_text):return None
  if identity_only:owned={**owned,'text':actual_text}
  if identity_only:
   record.pop('body_base64');return record,owned
  from ..source_quote import owned_purchase_facts
  parsed=owned_purchase_facts(owned['text']);visible=owned_purchase_facts(frame['purchase_text'] or frame['owner_text'])
  if parsed!=visible:return None
  # Preserve the coherent visible source spelling/time, not a later text read.
  owned=dict(owned,text=frame['owner_text']);record.pop('body_base64')
  return record,owned
 except (OSError,ValueError,TypeError,KeyError,LookupError,RecursionError):return None


_RETAINED_PHOTO_INPUT=ContextVar('native_retained_photo_input',default=None)

from contextlib import contextmanager
@contextmanager
def _retained_photo_scope(capability):
 """Private native import capability; never a caller-supplied root flag."""
 from .retained_document_import import _photo_root
 _photo_root(capability,getattr(capability,'photo',None))
 token=_RETAINED_PHOTO_INPUT.set(capability)
 try:yield
 finally:_RETAINED_PHOTO_INPUT.reset(token)

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
  capability=_RETAINED_PHOTO_INPUT.get()
  if photo.is_absolute() and photo.resolve().parent!=root and capability is not None:
   from .retained_document_import import _photo_root
   root=_photo_root(capability,photo)
  return not photo.is_symlink() and photo.resolve().parent==root and photo.stat().st_size<=20*1024*1024 and context_valid(context,str(photo),record['url'],photo.read_bytes())
 except (OSError,ValueError,TypeError,KeyError,LookupError,RecursionError):return False

def read_identity_source(evidence):
 """Owned immutable identity only; grants no financial/source-price admission."""
 return _read_rendered_source(evidence,identity_only=True)

@_immutable_dom
def primary_characteristics(html, *, title, code):
 """Characteristics ownership only; never reinterpret SKU as a maker article."""
 from ..purchase_scope import labelled_article_value
 parser=MainParser(html,check_controls=False)
 products=[n for n in parser.root.descendants() if itemtype(n,'Product') and _semantic_visible(n)]
 if len(products)!=1:return None
 product=products[0];own=_owned_nodes(product);owned=set(own)
 value=lambda n:re.sub(r'\s+',' ',_owned_value(n,owned)).strip()
 headings=[n for n in own if n.tag=='h1']
 if len(headings)!=1 or value(headings[0])!=title:return None
 def foreign(node):
  while node is not product:
   if node is None:return True
   roles=' '.join(node.attrs.get(k,'') for k in ('class','id','role','aria-label','title'))
   if re.search(r'variant|вариант|related|recommend|рекоменд|packag|shipping|упаков|доставк',roles,re.I):return True
   if node is not product and 'itemscope'in node.attrs:return True
   node=node.parent
  return False
 models=[n for n in own if prop(n,'model')]
 if len(models)!=1 or models[0].tag=='meta' or foreign(models[0]) or value(models[0])!=code:return None
 node=models[0].parent;labelled=False
 while node is not None and node is not product:
  text=value(node)
  if len(text)>200:break
  if labelled_article_value(text)==code:labelled=True;break
  node=node.parent
 if not labelled:return None
 mpns={n.attrs.get('content') or value(n) for n in own if prop(n,'mpn')}
 if mpns and mpns!={code}:return None
 sku={n.attrs.get('content') or value(n) for n in own if prop(n,'sku')}
 if len(sku)>1:return None
 printed={labelled_article_value(value(n)) for n in own if len(value(n))<=200}
 if printed-{code,''}:return None
 technical=False;lengths=[]
 for n in own:
  if re.fullmatch(r'h[2-6]',n.tag):technical=bool(re.fullmatch(r'Технические (?:параметры|характеристики)|Характеристики|(?:Technical )?specifications',value(n),re.I))
  elif re.fullmatch(r'Упаковка|Габариты упаковки|Размеры упаковки|Варианты(?: товара| исполнения)?|Другие варианты|Рекомендации|Похожие товары|Сопутствующие товары|Packaging|Package dimensions|Variants?',value(n),re.I):technical=False
  if n.tag!='tr':continue
  cells=[c for c in n.children if c.tag in {'td','th'}]
  if not cells or value(cells[0]).casefold() not in {'длина','length'}:continue
  if len(cells)<2 or any(c not in owned for c in cells[:2]):return None
  if any(c.attrs.get(a,'1').strip()!='1' for c in cells for a in ('rowspan','colspan')):return None
  for extra in cells[2:]:
   if extra.tag!='td' or extra.children or any(not isinstance(t,str) or t.strip() for t in extra.text):return None
   if set(extra.attrs)-{'class','id','rowspan','colspan'}:return None
  if not technical or foreign(cells[1]):return None
  match=re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*(мм|см|м)',value(cells[1]),re.I)
  if not match:return None
  length=float(match[1].replace(',','.'))*{'мм':.001,'см':.01,'м':1}[match[2].casefold()]
  if not math.isfinite(length) or length<=0:return None
  lengths.append(dict(metres=length,label=value(cells[0]),value=value(cells[1]),node_path=n.path()))
 if len(lengths)!=1:return None
 return dict(model=code,sku=sorted(sku),length=lengths[0])

_CHAR_MAX_FILES=4096
_CHAR_MAX_BYTES=16*1024*1024
_CHAR_INDEX={}
_CHAR_LOCK=threading.Lock()

def locate_primary_characteristics(*,url,observed_at,title,article,source_sha256,primary=False):
 """Exact immutable frame lookup; an incomplete index never proves uniqueness."""
 folder=CACHE_DIR/'main_facts';root=CACHE_DIR.resolve()
 try:
  if folder.is_symlink() or folder.resolve().parent!=root:return None,'characteristics_index_unsafe'
  epoch=folder.stat().st_mtime_ns
  paths=[]
  for visited,path in enumerate(folder.iterdir(),1):
   if visited>_CHAR_MAX_FILES:return None,'characteristics_index_bound'
   if re.fullmatch(r'[a-f0-9]{32}\.json',path.name):paths.append(path)
  paths.sort()
  with _CHAR_LOCK:
   index=_CHAR_INDEX.setdefault(str(root),dict(seen=set(),by_key={},by_primary_key={},bytes=0))
   index.update(last_reads=0,last_bytes=0,metadata_files=len(paths))
   if index['bytes']>_CHAR_MAX_BYTES:return None,'characteristics_index_bound'
   for path in paths:
    if path.name in index['seen']:continue
    if len(index['seen'])>=_CHAR_MAX_FILES:return None,'characteristics_index_bound'
    if path.is_symlink() or not path.is_file():return None,'characteristics_index_unsafe'
    size=path.stat().st_size
    if size>1024*1024 or index['bytes']+size>_CHAR_MAX_BYTES:return None,'characteristics_index_bound'
    with path.open('rb') as file:raw=file.read(1024*1024+1)
    index['bytes']+=len(raw);index['last_bytes']+=len(raw);index['last_reads']+=1
    if len(raw)>1024*1024:return None,'characteristics_index_bound'
    if index['bytes']>_CHAR_MAX_BYTES:return None,'characteristics_index_bound'
    index['seen'].add(path.name)
    try:frame=json.loads(raw)
    except (ValueError,TypeError):continue
    if (frame.get('version')!=1 or frame.get('role')!='main_card_facts' or frame.get('owner_bound')is not True
        or frame.get('owner_truncated')is not False or type(frame.get('owner_text'))is not str
        or type(frame.get('rendered_source'))is not dict):continue
    key=(frame.get('document_url'),frame.get('observed_at'),frame.get('main_title'),frame.get('article'),hashlib.sha256(frame['owner_text'].encode()).hexdigest())
    try:hash(key)
    except TypeError:continue
    evidence={**frame['rendered_source'],'main_facts':dict(ref='main_facts/'+path.name,sha256=hashlib.sha256(raw).hexdigest(),observed_at=frame.get('observed_at'))}
    index['by_key'].setdefault(key,[]).append(evidence)
    from ..source_quote import primary_text
    primary_key=key[:-1]+(hashlib.sha256(primary_text(frame['owner_text']).encode()).hexdigest(),)
    index['by_primary_key'].setdefault(primary_key,[]).append(evidence)
   if folder.stat().st_mtime_ns!=epoch:return None,'characteristics_index_changed'
   matches=index['by_primary_key'if primary else'by_key'].get((url,observed_at,title,article,source_sha256),[])
   if len(matches)!=1:return None,'characteristics_source_ambiguous' if matches else 'characteristics_source_missing'
   return copy.deepcopy(matches[0]),''
 except (OSError,ValueError,TypeError):return None,'characteristics_source_missing'

def read_primary_characteristics_source(evidence,*,expected_code):
 """Typed primary characteristics only. Does not grant price or identity admission."""
 try:
  if type(evidence)is not dict or set(evidence)!={'role','ref','record_sha256','body_sha256','main_facts'} or evidence.get('role')!='rendered_primary_dom' or not re.fullmatch(r'rendered_cards/[a-f0-9]{32}\.json',evidence.get('ref','')):return None
  root=CACHE_DIR.resolve();path=CACHE_DIR/evidence['ref']
  if path.parent.is_symlink() or path.is_symlink() or path.resolve().parent!=root/'rendered_cards' or path.stat().st_size>4*1024*1024:return None
  raw=path.read_bytes()
  if hashlib.sha256(raw).hexdigest()!=evidence['record_sha256']:return None
  record=json.loads(raw);body=base64.b64decode(record['body_base64'],validate=True)
  if (set(record)!={'version','role','representation','requested_url','url','observed_at','encoding','body_base64','body_sha256','frame'}
      or type(record['version'])is not int or record['version']!=1 or record['role']!='rendered_primary_dom' or record['representation']!='visibility_annotated_dom'
      or record['encoding']!='utf-8' or len(body)>2*1024*1024 or hashlib.sha256(body).hexdigest()!=record['body_sha256'] or record['body_sha256']!=evidence['body_sha256']):return None
  stamp=record['observed_at']
  if type(stamp)not in (int,float) or not math.isfinite(stamp) or not 0<stamp<=time.time()+300 or canonical_card_url(record['url'])!=record['url'] or record['url']!=record['requested_url']:return None
  frame=_read_render_frame(evidence['main_facts']);base={k:evidence[k] for k in ('role','ref','record_sha256','body_sha256')}
  if (frame.get('rendered_source')!=base or record['frame']!={k:frame.get(k) for k in _RENDER_FRAME}
      or frame.get('document_url')!=record['url'] or frame.get('requested_url')!=record['url'] or frame.get('observed_at')!=stamp
      or frame.get('article')!=expected_code or frame.get('owner_bound')is not True or frame.get('owner_truncated')is not False):return None
  html=body.decode('utf-8');owned=main_product(html,record['url'],check_controls=False,admission_only=True)
  norm=lambda value:re.sub(r'\s+',' ',value).strip()
  if not owned or owned['title']!=frame['main_title'] or norm(owned['text'])!=norm(frame['owner_text']):
   owned=_neutral_owner(html,record['url'])
   if not owned or owned['title']!=frame['main_title']or norm(owned['text'])!=norm(frame['owner_text']):return None
  manufacturer_codes={v['value'] for v in owned.get('code_observations',[]) if v.get('role')=='manufacturer_code'}
  if manufacturer_codes and manufacturer_codes!={expected_code}:return None
  characteristics=primary_characteristics(html,title=frame['main_title'],code=expected_code)
  if not characteristics:return None
  # Preserve the parser's separately typed legacy article/SKU; never overwrite it.
  record.pop('body_base64');return record,dict(owned,text=frame['owner_text'],characteristics=characteristics)
 except (OSError,ValueError,TypeError,KeyError,LookupError,RecursionError):return None
