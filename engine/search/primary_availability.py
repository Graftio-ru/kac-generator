"""Availability of the owned primary purchase region, not other branch stores."""
import hashlib,math,re
from .purchase_preview import _CASH,_QTY,_TOTAL,_number

_BOUNDARY=re.compile(r'^\s*(?:описание|отзывы|технические\s+параметры|рекоменд\w*|похожие|посмотреть\s+аналоги|сопутств\w*|'
 r'цена\s+и\s+наличие\s+в\s+магазинах|наличие\s+в\s+(?:других\s+)?магазинах|другие\s+магазины|'
 r'ваша\s+корзина|корзина|cart\s+total|итого\s+заказ|сумма\s+заказа|добавить\s+в\s+корзину|add\s+to\s+cart)(?:\d+)?\b',re.I)
_STOCK=re.compile(r'(?:в\s+наличии|(?:со|на)\s+склад[ае]|под\s+заказ|на\s+заказ)',re.I)
_NEGATIVE=re.compile(r'(?:нет|отсутствует)\s+(?:в\s+наличии|на\s+складе)',re.I)
_REQUEST=re.compile(r'^\s*(?:цена|стоимость)\s*(?:[:：]\s*)?по\s+запросу\s*$',re.I)

def _status(line):
    states=set()
    if _NEGATIVE.search(line):states.add('out_of_stock')
    if re.search(r'\b(?:под|на)\s+заказ\b',line,re.I):states.add('on_order')
    # Negated availability is not an affirmative signal embedded in the clause.
    positive=_NEGATIVE.sub('',line)
    physical=re.search(r'(?:в\s+наличии(?=\s|\d|[;,.]|$)|(?:со|на)\s+склад[ае])',positive,re.I)
    quantities=re.findall(r'(?<![\w.,])(\d+(?:[.,]\d+)?)\s*(?:м\b|метр\w*|шт\b|штук\w*|кг\b|л\b)',positive,re.I)
    if physical and quantities:
        values={_number(v) for v in quantities}
        if len(values)==1:states.add('in_stock' if next(iter(values))>0 else 'out_of_stock')
        else:states.add('unknown')
    elif re.search(r'\bв\s+наличии(?=\s|\d|[;,.]|$)',positive,re.I):states.add('in_stock')
    return next(iter(states)) if len(states)==1 else 'unknown' if states else None

def _purchase_line(line):
    return bool(_STOCK.search(line) or _QTY.fullmatch(line) or _TOTAL.fullmatch(line)
                or _CASH.fullmatch(line) or re.fullmatch(r'[−–-]\s*\+|\+\s*[−–-]',line)
                or re.search(r'^(?:кратность|минимальн|мин\.\s*кол|количество|цена\s+за|за(?=\s|\d)|/)',line,re.I))

def _states(lines):
    states=set();i=0
    while i<len(lines):
        line=lines[i]
        if i+1<len(lines) and ((_STOCK.search(line) and _QTY.fullmatch(lines[i+1]))
                               or (_QTY.fullmatch(line) and _STOCK.search(lines[i+1]))):
            value=_status(line+' '+lines[i+1]);i+=2
        else:value=_status(line);i+=1
        if value is not None:states.add(value)
    return states

def _selected_cash(line,raw):
    # Tax annotation remains literal policy, never a stock or price inference.
    literal=re.sub(r'\s+(?:с|без)\s+НДС\s*$','',line,flags=re.I)
    match=_CASH.fullmatch(literal)
    return bool(match and math.isclose(_number(match[1]),raw,rel_tol=1e-8,abs_tol=.005))

def scoped_availability(text, selected_raw, *, owner_bound, unit_basis=None):
    """selected_raw is chosen by the producer; this helper never selects cash."""
    if not owner_bound or type(text) is not str:return 'unknown'
    lines=[s.strip() for s in text.splitlines() if s.strip()]
    if unit_basis is not None:
        try:
            start,end=unit_basis['cash_span']
            if (type(start) is not int or type(end) is not int or not 0<=start<end<=len(text)
                or unit_basis.get('source_sha256')!=hashlib.sha256(text.encode()).hexdigest()
                or unit_basis.get('raw_amount')!=selected_raw
                or not _selected_cash(text[start:end],selected_raw)):return 'unknown'
            anchors=[len([line for line in text[:start].splitlines() if line.strip()])]
        except (KeyError,TypeError,ValueError,OverflowError):return 'unknown'
    elif type(selected_raw) in (int,float) and math.isfinite(selected_raw) and selected_raw>0:
        anchors=[i for i,line in enumerate(lines) if _selected_cash(line,selected_raw)]
    elif selected_raw is None:
        anchors=[i for i,line in enumerate(lines) if _REQUEST.fullmatch(line)]
    else:return 'unknown'
    if len(anchors)!=1:return 'unknown'
    anchor=anchors[0]
    # A cash literal inside a named description/other-store/cart region is not primary.
    if any(_BOUNDARY.search(line) for line in lines[:anchor]):return 'unknown'
    begin=anchor
    while begin>0 and (_STOCK.search(lines[begin-1]) or _QTY.fullmatch(lines[begin-1])):
        begin-=1
    end=anchor+1
    while end<len(lines) and not _BOUNDARY.search(lines[end]) and _purchase_line(lines[end]):end+=1
    states=_states(lines[begin:end])
    return next(iter(states)) if len(states)==1 else 'unknown'
