"""Derived unit basis from one owned product preview, never cash discovery."""
import hashlib,json,math,re

_MONEY=r'((?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?)\s*(₽|руб(?:\.|лей|ля|ль)?|RUB)'
_QTY=re.compile(r'^\s*(\d+(?:[.,]\d+)?)\s*(м|метр|шт\.?|штук[аи]?|кг|килограмм|л|литр|уп|упак|упаковк[аи])\.?\s*$',re.I)
_TOTAL=re.compile(r'^\s*(?:на\s+сумму|сумма\s+товара|стоимость\s+товара|product\s+subtotal)\s*[:：]?\s*'+_MONEY+r'\s*$',re.I)
_CASH=re.compile(r'^\s*(?:текущая\s+цена\s*[:：]?\s*|цена\s*[:：]?\s*)?'+_MONEY+r'\s*$',re.I)
_LOGISTICS=re.compile(r'(?:кратность|минимальн|остаток|в\s+наличии|(?:со|на)\s+склад[ае]|количество\s+заказа|stock|minimum)',re.I)
_STOP=re.compile(r'^\s*(?:описание|отзывы|рекоменд|похожие|сопутств|ваша\s+корзина|корзина|итого\s+заказ|сумма\s+заказа|cart\s+total)\b',re.I)

def _number(value):
    return float(re.sub(r'\s','',value).replace(',','.'))

def derive_unit_basis(observation, cash_evidence):
    """Trusted producer supplies separately selected cash; receipt is re-derived."""
    from ..source_quote import primary_text,selected_price_context,firm_price_role_reason,_pricing_terms
    from ..price_capture import _unit
    try:
        text=observation['source_text'];raw=observation['raw_amount'];stamp=observation['observed_at']
        if (type(cash_evidence) is not dict or type(raw) not in (int,float) or not math.isfinite(raw) or raw<=0
            or observation['currency']!='RUB' or observation.get('state') not in {'observed','review'}
            or hashlib.sha256(text.encode()).hexdigest()!=observation['source_sha256']):return None
        price_text,unit_text=selected_price_context(text,raw)
        if (type(cash_evidence.get('version')) is not int or cash_evidence.get('version')!=1 or cash_evidence.get('source')!='product_card_text'
            or cash_evidence.get('url')!=observation['url'] or cash_evidence.get('observed_at')!=stamp
            or cash_evidence.get('title')!=observation['title'] or cash_evidence.get('listed_price')!=raw
            or cash_evidence.get('price_text')!=price_text
            or firm_price_role_reason(price_text,unit_text,observation['purchase_terms']+' '+_pricing_terms(text,raw))):return None
        # Offsets point into original text; whitespace is never rewritten as proof.
        lines=[];offset=0
        for line in text.splitlines(keepends=True):
            if line.strip():lines.append((line.strip(),offset+len(line)-len(line.lstrip()),offset+len(line.rstrip())))
            offset+=len(line)
        primary=primary_text(text);previews=[]
        for i,(line,start,end) in enumerate(lines[:-1]):
            quantity=_QTY.fullmatch(line);subtotal=_TOTAL.fullmatch(lines[i+1][0])
            if not quantity or not subtotal or lines[i+1][2]>len(primary):continue
            if any(_STOP.search(v[0]) for v in lines[:i+1]):continue
            count=_number(quantity[1]);total=_number(subtotal[1]);unit=_unit(quantity[2])
            if not math.isfinite(count) or count<=0 or not total>0:continue
            # Exactly one primary cash literal of the independently selected amount.
            cash=[v for v in lines[:i] if _CASH.fullmatch(v[0]) and math.isclose(_number(_CASH.fullmatch(v[0])[1]),raw,rel_tol=1e-8,abs_tol=.005)]
            if len(cash)!=1 or not math.isclose(raw*count,total,rel_tol=1e-8,abs_tol=.01):continue
            if not _CASH.fullmatch(price_text.strip()):
                # The unmodified selected receipt can name the same amount twice:
                # one primary cash, then its explicitly owned one-unit subtotal.
                # Keep all source roles/offsets; any extra context remains ambiguous.
                if (count!=1 or not math.isclose(raw,total,rel_tol=1e-8,abs_tol=.005)
                        or price_text!=cash[0][0]+'\n'+lines[i+1][0]):continue
            # Cash starts the main purchase epoch. Logistical labels after it
            # remain active across filler lines until an explicit quantity control.
            cash_index=lines.index(cash[0]);logistical=False
            for item in lines[cash_index+1:i]:
                if _LOGISTICS.search(item[0]):logistical=True
                elif re.fullmatch(r'(?:[−–-]\s*\+|\+\s*[−–-]|(?:выбрано|количество\s+товара)\s*[:：]?)',item[0],re.I):
                    logistical=False
            if logistical:continue

            # A package receipt cannot choose one favourable total from several
            # primary previews, even if only one matches the selected cash.
            if unit=='уп':
                if sum(bool(_TOTAL.fullmatch(v[0])) for v in lines if v[2]<=len(primary))!=1:continue
                # Count distinct primary cash too, not just literals equal to raw.
                # A standalone old-price/installment heading owns only the next
                # cash row; unrelated amounts cannot inherit that exclusion.
                package_cash=[]
                for j,value in enumerate(lines):
                    if value[2]>len(primary) or any(_STOP.search(v[0]) for v in lines[:j+1]):break
                    if not _CASH.fullmatch(value[0]):continue
                    if j and re.fullmatch(_MONEY,value[0],re.I) and re.fullmatch(r'(?:старая\s+цена|прошлая\s+цена|цена\s+до\s+скидки|old\s+price|плати\s+частями|оплата\s+частями|рассрочка|installments?)\s*[:：]?',lines[j-1][0],re.I):continue
                    package_cash.append(value)
                if package_cash!=cash:continue
            if unit not in {'м','шт','кг','л','уп'}:continue
            previews.append(dict(cash_span=list(cash[0][1:]),quantity_span=[start,end],subtotal_span=list(lines[i+1][1:]),
                                 selected_quantity=count,subtotal=total,sale_unit=unit))
        if len(previews)!=1:return None
        selected={k:cash_evidence[k] for k in ('version','source','url','observed_at','title','listed_price','price_text')}
        return dict(version=1,kind='owned_product_preview',source_sha256=observation['source_sha256'],
                    url=observation['url'],observed_at=stamp,raw_amount=raw,currency='RUB',
                    price_quantity=1,selected_cash=selected,**previews[0])
    except (KeyError,TypeError,ValueError,OverflowError):return None

def valid_unit_basis(observation, receipt):
    if type(receipt) is not dict or not receipt:return False
    expected=derive_unit_basis(observation,receipt.get('selected_cash'))
    if expected is None:return False
    try:
        # JSON type identity matters: bool must not compare equal to denominator1.
        encode=lambda value:json.dumps(value,sort_keys=True,allow_nan=False,separators=(',',':'))
        return encode(receipt)==encode(expected)
    except (TypeError,ValueError,OverflowError):return False


PREVIEW_REASONS = {
    'preview_selected_cash_missing': 'Независимо выбранная цена основного товара не сохранена; единица предпросмотра не подтверждена',
    'preview_cash_or_roles_unconfirmed': 'Привязка выбранной цены, количества и суммы товара не подтверждена',
}

def preview_reason_text(code):
    return PREVIEW_REASONS.get(code,'')

def has_preview_sequence(text):
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    return any(_QTY.fullmatch(line) and _TOTAL.fullmatch(lines[i+1]) for i,line in enumerate(lines[:-1]))
