"""Literal single Offer purchase row; metadata and stock never grant a unit."""
import base64
import hashlib
import json
import math
import re


def _exact(left,right):
    """JSON-shaped proof equality without Python bool/number coercion."""
    if type(left)is not type(right):return False
    if type(left)is dict:return left.keys()==right.keys() and all(_exact(left[k],right[k])for k in left)
    if type(left)is list:return len(left)==len(right)and all(_exact(a,b)for a,b in zip(left,right))
    if type(left)is float and not math.isfinite(left):return False
    return left==right


def derive(source, *, url, observed_at, title, article, text):
    from . import http_card as dom
    if type(source)is dict and str(source.get('ref','')).startswith('http_cards/'):
        retained=dom.read_source(source,allow_rendered=True)
        if retained and retained[0].get('role')=='http_primary_dom':
            source=dom.http_primary_source(source)
            if source is None:return None
    modern=_derive_v2(source,url=url,observed_at=observed_at,title=title,article=article,text=text)
    if modern is not None:return modern
    return _derive_v1(source,url=url,observed_at=observed_at,title=title,article=article,text=text)


def _derive_v1(source,*,url,observed_at,title,article,text):
    from . import http_card as dom
    from .product_identity import canonical_card_url
    from ..source_quote import owned_purchase_facts, firm_price_role_reason
    try:
        retained = dom.read_source(source, allow_rendered=True)
        if not retained or not retained[1] or source.get('role') != 'rendered_primary_dom':
            return None
        record, owned = retained
        if (record['url'] != canonical_card_url(url) or record['observed_at'] != observed_at
                or owned['title'] != title or owned['article'] != article or owned['text'] != text):
            return None
        # read_source already checks the bounded locator, frame, owner and hashes.
        raw = (dom.CACHE_DIR / source['ref']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != source['record_sha256']:
            return None
        html = base64.b64decode(json.loads(raw)['body_base64'], validate=True).decode('utf-8')
        row = parse_dom(html, title=title, article=article)
        if not row:
            return None
        from ..source_quote import primary_text, labelled_sale_units
        from ..price_capture import visible_prices, _unit
        primary=primary_text(text)
        if (visible_prices(primary) != [row['raw_amount']]
                or re.search(r'\d[\d .,]*\s*(?:USD|EUR|\$|€)',primary,re.I)
                or any(_unit(v)!=_unit(row['sale_unit']) for v in labelled_sale_units(primary))):
            return None
        # Seller calendar dates are informational, not capture freshness.
        # read_source and the native observation gates own epoch/TTL checks.
        row.pop('row_text')
        row.update(version=1, role='current_owned_purchase_row', source=source,
                   url=record['url'], observed_at=observed_at, title=title, article=article,
                   source_sha256=hashlib.sha256(text.encode()).hexdigest())
        return row
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None


def parse_dom(html, *, title, article):
    """Parse only source fields; requested quantities/units never enter this API."""
    from . import http_card as dom
    from ..source_quote import owned_purchase_facts, firm_price_role_reason
    parser = dom.MainParser(html, check_controls=False)
    products = [n for n in parser.root.descendants() if dom.visible(n) and dom.itemtype(n, 'Product')]
    if len(products) != 1:
        return None
    product = products[0]
    nodes = dom._owned_nodes(product)
    headings = [n for n in nodes if n.tag == 'h1']
    if len(headings) != 1 or headings[0].value().strip() != title:
        return None
    rows = [n for n in nodes if dom.itemtype(n, 'Offer')]
    if len(rows) != 1 or rows[0].tag != 'tr' or not dom.prop(rows[0], 'offers'):
        return None
    row = rows[0]
    if any(dom.itemtype(n, 'AggregateOffer') for n in nodes):
        return None
    cells = [n for n in row.children if n.tag == 'td' and dom.visible(n)]
    price_cells = [c for c in cells if re.search(r'₽|\bRUB\b|руб\.', c.value(), re.I)]
    if len(price_cells) != 1:
        return None
    price_cell = price_cells[0]
    cash_match = re.fullmatch(
        r'([\d\s\u00a0\u202f.,]+\s*(?:₽|RUB|руб\.))'
        r'(?:\s*(?:/|за)\s*(шт\.?|компл\.?|м))?', price_cell.value().strip(), re.I)
    if not cash_match:
        return None
    cash, cash_unit = cash_match.groups()
    inputs = [n for n in row.descendants() if n.tag == 'input' and dom.visible(n)]
    if len(inputs) > 1:
        return None
    field = inputs[0] if inputs else None
    input_cell = None
    if field is not None:
        if (field.attrs.get('type', 'text').lower() not in {'text','number'}
                or field.attrs.get('value') != '1'
                or any(k in field.attrs for k in ('disabled','readonly'))
                or field.attrs.get('aria-disabled') == 'true'):
            return None
        input_cells = [c for c in cells if field in list(c.descendants())]
        if len(input_cells) != 1 or input_cells[0] is price_cell:
            return None
        input_cell = input_cells[0]
    # Unit authority is local to this unique Offer's cash/order quantity.
    # Explicit stock/filter roles veto the relation even with a buy button.
    named = []
    for cell in [price_cell] + ([input_cell] if input_cell else []):
        scope = list(cell.descendants())
        ancestor = cell.parent
        while ancestor is not None:
            scope.append(ancestor)
            if ancestor is product:
                break
            ancestor = ancestor.parent
        role_text = ' '.join(n.attrs.get(k, '') for n in scope for k in
                             ('name','id','class','aria-label','title','role','itemprop'))
        if re.search(r'stock|inventory|availability|filter|search|остат|склад|налич|фильтр|поиск', role_text, re.I):
            return None
    if input_cell:
        controls = [n for n in input_cell.descendants() if dom.visible(n)
                    and (n.tag == 'button' or n.attrs.get('role') == 'button')
                    and 'disabled' not in n.attrs and n.attrs.get('aria-disabled') != 'true']
        named = [n for n in controls if re.fullmatch(
            r'(?:В корзину|Добавить в корзину|Купить|Add to cart|Buy)',
            (n.attrs.get('aria-label') or n.value()).strip(), re.I)]
        if len(named) > 1:
            return None
    input_unit = input_cell.value().strip() if input_cell else ''
    if named and named[0].value().strip():
        input_unit = input_unit.replace(named[0].value().strip(), '', 1).strip()
    if input_unit and not re.fullmatch(r'(?:шт\.?|компл\.?|м)', input_unit, re.I):
        return None
    if not (cash_unit or input_unit):
        return None
    from ..price_capture import _unit
    if cash_unit and input_unit and _unit(cash_unit) != _unit(input_unit):
        return None
    unit_text = cash_unit or input_unit
    # Availability may state inventory units separately, but no other cell
    # may introduce a competing sale unit or monetary denominator.
    for cell in cells:
        if cell is price_cell or cell is input_cell:
            continue
        scope = list(cell.descendants())
        availability = any(dom.prop(n,'availability') for n in scope)
        role = ' '.join(n.attrs.get(k,'') for n in scope for k in
                        ('itemprop','name','id','class','aria-label','title','role'))
        if availability or re.search(r'stock|inventory|availability|остат|склад|налич', role, re.I):
            continue
        if re.fullmatch(r'\s*(?:шт\.?|компл\.?|м)\s*', cell.value(), re.I):
            return None
    literal = owned_purchase_facts(cash + '\nЕдиница измерения: ' + unit_text)
    if literal['reason'] or literal['raw_amount'] is None:
        return None
    # Identity is printed in this row, independently of schema price/adapter.
    identity_cells = [c for c in cells if re.search(r'(?<![\w.-])' + re.escape(article) + r'(?![\w.-])', c.value(), re.I)]
    if len(identity_cells) != 1 or identity_cells[0] in (price_cells[0], input_cell):
        return None
    row_text = row.value()
    if firm_price_role_reason(cash, unit_text, row_text) or re.search(
            r'старая|прошлая|до\s+скидки|рассроч|кредит|в\s+месяц|клуб|по\s+карте|оптов|при\s+(?:покупк|заказ)|old\s+price|installment', row_text, re.I):
        return None
    return dict(raw_amount=literal['raw_amount'], currency=literal['currency'],
                sale_unit=unit_text, purchase_quantity=1,
                price_text=cash, unit_text='Единица измерения: ' + unit_text,
                row_path=row.path(), cash_path=price_cells[0].path(),
                input_path=field.path() if field else None,
                unit_path=(price_cell if cash_unit else input_cell).path(),
                purchase_control_path=named[0].path() if named else None,
                row_text=row_text,
                row_sha256=hashlib.sha256(html[row.start:row.end].encode()).hexdigest())


def from_facts(facts):
    source=facts.get('rendered_source')
    if source is None and facts.get('http_source'):
        from . import http_card as dom
        retained=dom.read_source(facts['http_source'],allow_rendered=True)
        if retained and retained[0].get('role')=='http_primary_dom':source=dom.http_primary_source(facts['http_source'])
    if not source:
        if 'purchase_row_v2'in facts:raise ValueError('present_purchase_witness_invalid')
        return None
    try:
        if 'purchase_row_v2'in facts:
            proof=facts['purchase_row_v2']
            if 'derived_from_capture' in facts:_validate_replay_facts(facts)
            modern=_derive_v2(source,url=facts['url'],observed_at=facts['captured_at'],title=facts['main_title'],article=facts['page_article'],text=facts['text'])
            if type(proof)is not dict or proof.get('version')!=2 or not _exact(modern,proof):raise ValueError('present_purchase_witness_invalid')
            return modern
        return derive(source, url=facts['url'],
                      observed_at=facts['captured_at'], title=facts['main_title'],
                      article=facts['page_article'], text=facts['text'])
    except (KeyError,TypeError):
        if 'purchase_row_v2'in facts:raise ValueError('present_purchase_witness_invalid')
        return None


def verify(proof, observation):
    if type(proof) is not dict:
        return None
    reader=_derive_v1 if proof.get('version')==1 else derive
    derived = reader(proof.get('source') or {}, url=observation['url'],
                     observed_at=observation['observed_at'], title=observation['title'],
                     article=observation['manufacturer_code'], text=observation['source_text'])
    if (not _exact(derived,proof) or not derived or derived['raw_amount'] != observation['raw_amount']
            or derived['currency'] != observation['currency'] or derived['sale_unit'] != observation['sale_unit']
            or derived['source_sha256'] != observation['source_sha256']):
        return None
    expected_lot=derived.get('price_denominator',1) if derived.get('version')==2 else 1
    if observation.get('lot_quantity') not in (None,expected_lot):return None
    if any(value is not None and (type(value) not in (int,float) or value!=1)
           for value in (observation.get('package_quantity'),)):
        return None
    return derived


def retain_decision(snapshot, data, *, operation_id, origin, outcome):
    """Append a bounded safe decision; preserve captured/model records unchanged."""
    from ..config import CACHE_DIR
    from .session import bounded_timeout
    import uuid
    bounded_timeout(1)
    if origin not in {'current_owned_row','existing_deterministic','model','none'}:
        raise ValueError('invalid_extraction_origin')
    if outcome not in {'fields_selected','no_fields','finish_no_offer','offer_created','extraction_failed'}:
        raise ValueError('invalid_extraction_outcome')
    row=from_facts(snapshot.facts())
    amount=data.get('listed_price_with_vat') if type(data) is dict else None
    kind='null' if amount is None else 'number' if type(amount) in (int,float) and math.isfinite(amount) else 'invalid'
    value=dict(version=1,role='capture_extraction_decision',operation_id=operation_id,
               card_id=snapshot.card_id,snapshot_sha256=snapshot.sha256,
               observed_at=snapshot.facts()['captured_at'],field_origin=origin,
               raw_amount_kind=kind,outcome=outcome,
               row_decision='current_owned_row_bound' if row else 'current_row_contract_not_closed',
               row_sha256=row['row_sha256'] if row else None,
               source_record_sha256=row['source']['record_sha256'] if row else None)
    raw=json.dumps(value,sort_keys=True,allow_nan=False).encode()
    root=CACHE_DIR.resolve();folder=root/'extraction_decisions'
    folder.mkdir(exist_ok=True)
    if folder.is_symlink() or folder.resolve().parent!=root:raise ValueError('unowned_extraction_receipt')
    path=folder/(uuid.uuid4().hex+'.json')
    with path.open('xb') as stream:stream.write(raw)
    return {'ref':str(path.relative_to(root)),'sha256':hashlib.sha256(raw).hexdigest()}


# V2 extends this module's single source-witness authority. Structural validation
# is private/acyclic; no request unit or normalized price enters the parser.

def _primary_structured_contract(record,owned,amounts=None):
    """Reconcile primary assertions before any layout-dependent fallback."""
    from urllib.parse import urljoin
    from . import http_card as dom
    from decimal import Decimal
    nodes=owned['_nodes'];parser=owned['_parser'];norm=lambda value:re.sub(r'\s+',' ',value).strip()
    from .manufacturer_aliases import manufacturer_equal,manufacturer_in_title
    from .product_identity import canonical_card_url
    from .agent import _type_mark_match_status,_conflicting_model_code_in_title
    from ..source_quote import firm_price_role_reason
    structured_evidence=[];structured_terms=[]
    def text_value(value):
        if isinstance(value,dict):
            if set(value)-{'@type','name'}:raise ValueError('unresolved_identity_reference')
            value=value.get('name')
        if type(value)is not str or not value.strip()or len(value)>2048 or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]',value):raise ValueError('structured_identity_shape')
        return norm(value)
    def has_type(value,kind):
        declared=value.get('@type')
        return declared==kind or type(declared)is list and all(type(v)is str for v in declared)and kind in declared
    def semantic_values(prop):
        return {norm(n.attrs.get('content')or dom._capture_text(n))for n in nodes if dom.prop(n,prop)and n.tag!='meta'and norm(dom._capture_text(n))}
    def named_values(kind):
        values=semantic_values(kind)
        labels=r'Бренд|Торговая\s+марка|Brand'if kind=='brand'else'Производитель|Manufacturer'
        values.update(norm(v)for v in re.findall(r'(?im)^\s*(?:'+labels+r')(?!\s+(?:code|article|number|код|артикул|номер)\b)(?=\s|[:：]|$)\s*[:：]?\s*\n?([^\n]{1,512})',owned['text']))
        return values
    def product_identity(product):
        if 'mpn'in product and text_value(product['mpn'])!=owned['article']:raise ValueError('primary_mpn_conflict')
        for key,role in (('model','model'),('sku','seller_sku')):
            if key not in product:continue
            value=text_value(product[key]);visible={v['value']for v in owned['code_observations']if v['role']==role}|semantic_values(key)
            if visible:
                if visible!={value}:raise ValueError('primary_typed_code_conflict')
            elif key=='sku':
                if owned['identity_basis']!='legacy_article'or value!=owned['article']:raise ValueError('unbound_primary_sku')
            elif _type_mark_match_status(value,owned['title'])is not True or _conflicting_model_code_in_title(value,owned['title']):raise ValueError('unbound_primary_model')
        names={}
        for kind in ('brand','manufacturer'):
            if kind not in product:continue
            value=text_value(product[kind]);visible=named_values(kind)
            if visible:
                if not all(manufacturer_equal(v,value)or manufacturer_equal(value,v)for v in visible):raise ValueError('primary_manufacturer_conflict')
            elif not manufacturer_in_title(value,owned['title']):raise ValueError('unbound_primary_manufacturer')
            names[kind]=value
        if 'name'in product:
            name=text_value(product['name'])
            if name!=owned['title']and owned['title']!=name+' '+names.get('brand','')+' '+owned['article']:raise ValueError('primary_name_conflict')
        for key in ('productID','gtin','gtin8','gtin12','gtin13','gtin14'):
            if key in product and semantic_values(key)!={text_value(product[key])}:raise ValueError('unbound_primary_identifier')
        if any(key in product for key in ('isVariantOf','hasVariant','inProductGroupWithID','productGroupID','additionalProperty','identifier','@reverse')):raise ValueError('unsupported_primary_identity_constraint')
        if 'description'in product:structured_terms.append(text_value(product['description']))
    # Geography/delivery/stock remain contextual under the existing product
    # policy; they neither supply cash nor impose a new warehouse/region gate.
    contextual={'eligibleRegion','ineligibleRegion','areaServed','availableAtOrFrom','availableDeliveryMethod','shippingDetails','shippingDestination','deliveryLeadTime','hasMerchantReturnPolicy','merchantReturnLink','availability','inventoryLevel','availabilityStarts','availabilityEnds','image','sameAs','category','identifier'}
    allowed={'@type','@id','url','price','priceCurrency','lowPrice','highPrice','offerCount','priceValidUntil','name','description'}|contextual
    def money_value(value):
        from decimal import InvalidOperation
        if type(value)not in (str,int,float):raise ValueError('primary_money_shape')
        try:amount=Decimal(str(value))
        except InvalidOperation:raise ValueError('primary_money_shape')from None
        if not amount.is_finite()or amount<=0:raise ValueError('primary_money_value')
        return amount
    def offer_conditions(value):
        if isinstance(value,list):
            if len(value)!=1:raise ValueError('multiple_primary_structured_offers')
            value=value[0]
        if type(value)is not dict or not(has_type(value,'Offer')or has_type(value,'AggregateOffer')):raise ValueError('unresolved_primary_offer')
        if set(value)-allowed:raise ValueError('unsupported_primary_price_condition')
        if '@id'in value:raise ValueError('unresolved_primary_offer_reference')
        if 'url'in value and (type(value['url'])is not str or canonical_card_url(urljoin(record['url'],value['url']))!=record['url']):raise ValueError('foreign_primary_offer')
        if value.get('priceCurrency','RUB')!='RUB':raise ValueError('currency_conflict')
        if not any(k in value for k in ('price','lowPrice','highPrice')):raise ValueError('unresolved_primary_price')
        if 'price'in value and value['price']is None:raise ValueError('primary_price_missing')
        if amounts is not None and value.get('price') is not None and money_value(value['price'])not in amounts:raise ValueError('price_conflict')
        if 'offerCount'in value and(type(value['offerCount'])is not int or value['offerCount']!=1):raise ValueError('multiple_primary_offers')
        if 'lowPrice'in value or 'highPrice'in value:
            if 'lowPrice'not in value or 'highPrice'not in value:raise ValueError('incomplete_primary_range')
            bounds=[money_value(value[k])for k in ('lowPrice','highPrice')]
            if len(set(bounds))!=1 or amounts is not None and bounds[0]not in amounts:raise ValueError('range_conflict')
        if 'priceValidUntil'in value:
            from datetime import date,datetime,timezone
            expires=value['priceValidUntil']
            if type(expires)is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',expires)or date.fromisoformat(expires)<datetime.fromtimestamp(record['observed_at'],timezone.utc).date():raise ValueError('primary_price_expired')
        for key in ('name','description'):
            if key in value:structured_terms.append(text_value(value[key]))
        return dict(sha256=hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest(),contextual_fields=sorted(set(value)&contextual))
    def associated_products(value,inside,depth=0):
        if depth>32:raise ValueError('structured_depth')
        if isinstance(value,dict):
            if has_type(value,'Product'):
                urls=[urljoin(record['url'],v) for v in (value.get('url'),value.get('@id'))if isinstance(v,str)and v]
                if any(canonical_card_url(v)==record['url']for v in urls)or not urls and inside:yield value
                return
            for child in value.values():yield from associated_products(child,inside,depth+1)
        elif isinstance(value,list):
            for child in value:yield from associated_products(child,inside,depth+1)
    def associated_offers(value,inside,depth=0):
        if depth>32:raise ValueError('structured_depth')
        if isinstance(value,dict):
            if has_type(value,'Product'):return # reconciled through its own offers above
            if has_type(value,'Offer')or has_type(value,'AggregateOffer'):
                linked=value.get('url')
                if type(linked)is str and canonical_card_url(urljoin(record['url'],linked))==record['url']or linked is None and inside:yield value
                return
            for child in value.values():yield from associated_offers(child,inside,depth+1)
        elif isinstance(value,list):
            for child in value:yield from associated_offers(child,inside,depth+1)
    for n in parser.root.descendants():
        if n.tag!='script'or n.attrs.get('type','').lower()!='application/ld+json':continue
        value=json.loads(''.join(c for c in n.text if isinstance(c,str)))
        products=list(associated_products(value,n in nodes))
        if len(products)>8:raise ValueError('structured_primary_bound')
        for product in products:
            product_identity(product)
            offer=offer_conditions(product['offers'])if 'offers'in product else None
            structured_evidence.append(dict(script_path=n.path(),product_sha256=hashlib.sha256(json.dumps(product,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest(),offer=offer))
        for offer in associated_offers(value,n in nodes):
            structured_evidence.append(dict(script_path=n.path(),offer=offer_conditions(offer)))
    terms='\n'.join(structured_terms)
    if len(terms)>4096 or firm_price_role_reason(terms,terms_text=terms)or re.search(r'клуб|по\s+карт[еы]|оптов|при\s+(?:покупк|заказ)|minimum|member|conditional|tier|subscription|installment|only\s+for',terms,re.I):raise ValueError('primary_price_terms_unconfirmed')
    if len(structured_evidence)>8:raise ValueError('structured_primary_bound')
    return structured_evidence,structured_terms


def _parse_validated_owner(record,owned):
    from .http_card import _immutable_dom
    return _immutable_dom(_parse_owner)(record,owned)

def _parse_owner(record, owned):
    from decimal import Decimal, InvalidOperation
    from . import http_card as dom
    from ..price_capture import _unit
    try:
        owner=owned['_owner'];nodes=owned['_nodes'];parser=owned['_parser']
        money=re.compile(r'(?<![\d.,])((?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?)\s*(₽|руб(?:\.|лей|ля|ль)?|RUB|USD|EUR|\$|€)(?![^\W\d_])',re.I)
        norm=lambda text:re.sub(r'\s+',' ',text).strip()
        cash=lambda match:Decimal(re.sub(r'\s','',match[1]).replace(',','.'))
        node_text=lambda n:norm(dom._capture_text(n,complete=True))
        all_visible=[n for n in owner.descendants()if not dom._capture_excluded(n)and n.tag not in {'script','style','template','meta','input'}]
        atoms=[]
        for n in all_visible:
            matches=list(money.finditer(node_text(n)))
            if not matches:continue
            # Lowest textual currency-bearing owner, never duplicate ancestors.
            if any(money.search(node_text(c))for c in n.children if not dom._capture_excluded(c)):continue
            if len(matches)!=1:return None
            atoms.append((n,matches[0]))
        if not atoms or len(atoms)>64:return None
        role_scopes={}
        def retain_role(node,parent,value):role_scopes[node]=parent;return value
        def delivery_scope(node):
            # The role belongs to this one cash atom and its local labelled row,
            # never to every price under an ancestor containing delivery text.
            declaration=r'(?:^|[\s_-])(?:delivery|shipping)(?:$|[\s_-])'
            hazard=r'обязательн|required|mandatory|рассроч|installment|плат[её]ж|в\s+кредит|в\s+месяц|per\s+month|monthly|подписк|subscription|цена\s+от|\bот\s+\d|\bпри\b|\bесли\b|в\s+случае|услови|\bif\b|\bwhen\b|subject\s+to|минимальн|НДС|VAT|(?:цена|стоимость)\s+(?:товар|издели|продукт)|\b(?:артикул|sku|модель|вариант|product)\b'
            for parent in dom._ancestors(node):
                if parent is owner:break
                if parent.tag not in {'tr','li','p','div','section'}:continue
                value=node_text(parent)
                direct=re.match(r'^(?:(?:стоимость|цена|плата\s+за)\s+)?(?:доставк[аи]|shipping|delivery)\b',value,re.I)
                carrier=re.match(r'^(?:курьер(?:ская\s+доставка)?|самовывоз|пункт\s+выдачи|courier|pickup)\b',value,re.I)
                if not direct and not carrier:continue
                contexts=[]
                for a in dom._ancestors(parent):
                    if a is owner:break
                    if re.search(declaration,' '.join(a.attrs.get(k,'')for k in ('class','id','role','aria-label')),re.I):contexts.append(a)
                if not direct and not contexts:continue
                if len(value)>256:continue
                quotes=list(money.finditer(value))
                if len(quotes)!=1 or re.search(r'[-−]\s*$',value[:quotes[0].start()]):raise ValueError('delivery_scope_ambiguous')
                if quotes[0][2].casefold()not in {'₽','руб','руб.','рублей','рубля','рубль','rub'}or re.search(hazard,value,re.I):raise ValueError('delivery_scope_ambiguous')
                if any(re.search(r'обязательн|required|mandatory|\bпри\b|\bесли\b|в\s+случае|услови|\bif\b|\bwhen\b|subject\s+to',node_text(a),re.I)for a in contexts):raise ValueError('delivery_scope_conditional')
                if any(re.search(r'mandatory|required|installment|subscription|conditional|tier',' '.join(str(k)+' '+str(v)for k,v in a.attrs.items()),re.I)for a in [parent,*contexts]):raise ValueError('delivery_scope_conditional')
                if any(dom.itemtype(n,'Product')or dom.itemtype(n,'Offer')or dom.itemtype(n,'AggregateOffer')or dom.prop(n,'price')or dom.prop(n,'mpn')or dom.prop(n,'sku')or n.tag in {'h1','input','button'}for n in parent.descendants()):raise ValueError('delivery_purchase_scope_ambiguous')
                if re.search(r'(?:/|за)\s*(?:\d+\s*)?(?:шт|м\b|уп|компл)',value,re.I):raise ValueError('delivery_product_unit_ambiguous')
                return parent
            return None

        def role(node):
            delivery=delivery_scope(node)
            if delivery is not None:return retain_role(node,delivery,'delivery')
            for parent in dom._ancestors(node):
                if parent is owner:break
                attrs=' '.join(parent.attrs.get(k,'')for k in ('class','id','role','aria-label','title')).lower()
                value=node_text(parent)
                if parent.tag in {'del','s'}or re.search(r'old[-_]?price|oldprice|previous[-_]?price|старая\s+цена',attrs,re.I)or 'line-through'in parent.attrs.get('style',''):return retain_role(node,parent,'historical')
                if len(value)<=256 and len(list(money.finditer(value)))==1:
                    if re.search(r'экономи[яи]|saving|скидка\s*:',value,re.I):return retain_role(node,parent,'saving')
                    if re.search(r'плати\s+частями|рассроч|installment|плат[её]ж|в\s+кредит',value,re.I):return retain_role(node,parent,'installment')
                    if re.search(r'на\s+сумму|итого|subtotal|order\s+total',value,re.I):return retain_role(node,parent,'subtotal')
            return 'current'
        classified=[(n,m,role(n))for n,m in atoms]
        current=[(n,m)for n,m,r in classified if r=='current']
        if not current:return None
        if any(cash(m)<=0 or re.search(r'[-−]\s*$',node_text(n)[:m.start()])for n,m in current):return None
        if any(m[2].casefold()not in {'₽','руб','руб.','рублей','рубля','рубль','rub'}for _,m in current):return None
        # All declared offers/controls remain under the same primary owner.
        independent=[n for n in nodes if dom.itemtype(n,'Offer')]
        if len(independent)>1:return None
        controls=[n for n in nodes if n.tag in {'a','button','input'}and not dom._capture_excluded(n)
                  and re.fullmatch(r'Купить|В корзину|Добавить в корзину|Buy|Add to cart',norm(n.attrs.get('aria-label')or n.attrs.get('value')or dom._capture_text(n)),re.I)]
        if len(controls)!=1:return None
        control=controls[0];pid=owner.attrs.get('data-product-id')
        if pid and control.attrs.get('data-id')!=pid:return None
        # Minimal common ancestor is accepted only inside already verified owner
        # and binds the SAME purchase control, not a neighboring product card.
        region=control
        while region is not None and not all(n in region.descendants()for n,_ in current):region=region.parent
        if region is None or region not in nodes:return None
        if any(dom.itemtype(n,'Product')for n in region.descendants()if n is not owner):return None
        quantity_inputs=[n for n in nodes if n.tag=='input'and not dom._capture_excluded(n)
                         and n.attrs.get('type','text').lower()in {'number','text','search'}and re.fullmatch(r'\d+(?:[.,]\d+)?',n.attrs.get('value',''))
                         and ('data-price'in n.attrs or re.search(r'qty|quantity|колич', ' '.join(n.attrs.get(k,'')for k in ('id','name','class')),re.I))]
        if len(quantity_inputs)!=1:return None
        field=quantity_inputs[0]
        while region is not None and field not in region.descendants():region=region.parent
        if region is None or region not in nodes:return None
        order_quantity=Decimal(field.attrs['value'].replace(',','.'))
        if order_quantity<=0:return None
        if control.attrs.get('data-quantity')is not None and Decimal(control.attrs['data-quantity'])!=order_quantity:return None
        denominations=[]
        unit_pattern=r'(шт\.?|штук[аиу]?|м|метр(?:а|ов)?|уп(?:ак(?:овк[аи])?)?\.?)'
        for node,match in current:
            found=None
            for parent in dom._ancestors(node):
                if parent not in nodes:break
                text=norm(dom._capture_text(parent))
                if len(text)>512:break
                values=list(money.finditer(text))
                matching=[(i,v)for i,v in enumerate(values)if cash(v)==cash(match)]
                if len(matching)!=1:continue
                index,selected=matching[0]
                end=values[index+1].start()if index+1<len(values)else len(text)
                tail=text[selected.end():end]
                unit=re.fullmatch(r'\s*/\s*(\d+(?:[.,]\d+)?)?\s*'+unit_pattern+r'\s*(?:(?:Цена|Price)\s*:\s*)?',tail,re.I)
                if unit:
                    literal=selected.group(0)+' / '+(unit[1]+' 'if unit[1]else'')+unit[2]
                    found=(Decimal((unit[1]or'1').replace(',','.')),_unit(unit[2]),parent,literal);break
            if found is None:
                # Independent visible current cash + explicit order subtotal/unit.
                sums=[]
                for n,m,r in classified:
                    if r!='subtotal' or n not in region.descendants():continue
                    for parent in dom._ancestors(n):
                        if parent not in nodes:break
                        text=node_text(parent)
                        if len(text)>256:break
                        parsed=re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*'+unit_pattern+r'\s*на\s+сумму\s*'+money.pattern,text,re.I)
                        if parsed:
                            amount=Decimal(re.sub(r'\s','',parsed[3]).replace(',','.'))
                            if Decimal(parsed[1].replace(',','.'))!=order_quantity or amount!=cash(match)*order_quantity:return None
                            sums.append((_unit(parsed[2]),parent,text));break
                if len(sums)!=1:return None
                found=(Decimal(1),sums[0][0],sums[0][1],sums[0][2])
            denominator,unit,parent,text=found
            if denominator<=0 or unit not in {'шт','м','уп'}:return None
            denominations.append(dict(amount=str(cash(match)),currency='RUB',unit=unit,denominator=str(denominator),price_path=node.path(),denominator_path=parent.path(),literal=text))
        if len({v['unit']for v in denominations})!=1:return None
        # Exact economic equality; no floating tolerance or requested-unit math.
        first=denominations[0]
        if any(Decimal(v['amount'])*Decimal(first['denominator'])!=Decimal(first['amount'])*Decimal(v['denominator'])for v in denominations):return None
        singles=[v for v in denominations if Decimal(v['denominator'])==1]
        if len(singles)>1 or not singles and len(denominations)!=1:return None
        chosen=singles[0] if singles else denominations[0]
        metadata=field.attrs.get('data-price')
        if metadata is not None and Decimal(metadata).quantize(Decimal('.01'))!=Decimal(chosen['amount']):return None
        # A distinct current cash not reconciled above can never disappear.
        excluded_nodes={role_scopes[n]for n,_,r in classified if r!='current'}
        def terms(node):
            if dom._capture_excluded(node)or node in excluded_nodes:return ''
            value=''.join(terms(v)if isinstance(v,dom.Node)else v for v in node.text)
            return '\n'+value+'\n'if node.tag in {'div','p','section','article','h1','h2','h3','h4','h5','h6','br','li','dt','dd','tr','td','th'}else value
        complete_terms=terms(owner)
        from ..source_quote import _pricing_terms,firm_price_role_reason
        terms_text=_pricing_terms(complete_terms,float(Decimal(chosen['amount'])))
        disclosures=[v.strip() for v in complete_terms.splitlines() if re.search(r'НДС|VAT|клуб|по\s+карт[еы]|оптов|при\s+(?:покупк|заказ)|tier|conditional|member\s+price',v,re.I)]
        terms_text='\n'.join(dict.fromkeys([terms_text,*disclosures])).strip()
        if len(terms_text)>4096:return None
        for node,match,kind in classified:
            if kind in {'installment','saving'}and re.search(r'оптов|клуб|по\s+карт[еы]|при\s+(?:покупк|заказ)|без\s+НДС|tier|conditional',node_text(role_scopes[node]),re.I):return None
        if re.search(r'цена\s+от|клуб|по\s+карт[еы]|оптов|розничн\w*\s+цен|при\s+(?:покупк|заказ)|минимальн\w*\s+(?:заказ|цен)|tier|conditional|member\s+price',terms_text,re.I):return None
        # Declared semantic price/currency must corroborate a visible currentquote.
        amounts={Decimal(v['amount'])for v in denominations}
        for n in nodes:
            if dom.prop(n,'priceCurrency')and(n.attrs.get('content')or dom._capture_text(n).strip())!='RUB':return None
            if dom.prop(n,'price')and n.tag=='meta'and Decimal(n.attrs.get('content','NaN'))not in amounts:return None
        structured_evidence,structured_terms=_primary_structured_contract(record,owned,amounts)
        terms_text='\n'.join([terms_text,*structured_terms]).strip()
        if len(terms_text)>4096 or firm_price_role_reason(chosen['literal'],chosen['literal'],terms_text)or re.search(r'клуб|по\s+карт[еы]|оптов|при\s+(?:покупк|заказ)|minimum|member|conditional|tier|subscription|installment|only\s+for',terms_text,re.I):return None
        roles=[dict(path=n.path(),amount=str(cash(m)),currency=m[2],role=r,role_path=role_scopes[n].path()if n in role_scopes else n.path())for n,m,r in classified]
        return dict(version=2,role='current_owned_purchase_row',raw_amount=float(Decimal(chosen['amount'])),currency='RUB',sale_unit=chosen['unit'] if Decimal(chosen['denominator'])==1 else chosen['denominator']+' '+chosen['unit'],purchase_quantity=float(order_quantity),price_denominator=float(Decimal(chosen['denominator'])),
                    price_text=chosen['literal'],unit_text=chosen['literal'],terms_text=terms_text,
                    quotations=denominations,selected_quote=denominations.index(chosen),monetary_roles=roles,structured_evidence=structured_evidence,
                    row_path=region.path(),row_sha256=hashlib.sha256(parser.source[region.start:region.end].encode()).hexdigest(),
                    input_path=field.path(),purchase_control_path=control.path(),owner_path=owner.path(),owner_sha256=owned['scope_sha256'],text_projection='neutral-visible-owner-v1')
    except (ValueError,TypeError,KeyError,InvalidOperation,RecursionError):return None


def _derive_v2(source,*,url,observed_at,title,article,text):
    from . import http_card as dom
    retained=dom._validated_rendered_owner(source)
    if not retained:return None
    record,owned=retained
    if record['url']!=url or record['observed_at']!=observed_at or owned['title']!=title or owned['article']!=article or owned['text']!=text:return None
    quote=_parse_validated_owner(record,owned)
    if not quote:return None
    return dict(quote,source=source,url=url,observed_at=observed_at,title=title,article=article,source_sha256=hashlib.sha256(text.encode()).hexdigest())


def decision(proof,position,*,factory_proof=None):
    """The source witness selects a denomination; native unit policy converts."""
    from ..source_quote import strict_unit_decision,_validated_owned_length
    from . import http_card as dom
    from ..price_capture import _unit
    field=lambda key:position.get(key,'')if isinstance(position,dict)else getattr(position,key,'')
    if not proof or proof.get('version')!=2:return None,'Текущая покупная строка не подтверждена'
    retained=dom._validated_rendered_owner(proof.get('source'))
    if not retained:return None,'Источник покупной строки не подтверждён'
    record,owned=retained
    quote=_parse_validated_owner(record,owned)
    derived=dict(quote,source=proof['source'],url=record['url'],observed_at=record['observed_at'],title=owned['title'],article=owned['article'],source_sha256=hashlib.sha256(owned['text'].encode()).hexdigest()) if quote else None
    if not _exact(derived,proof):return None,'Текущая покупная строка изменилась'
    if factory_proof is not None and(any(factory_proof.get(k)!=proof.get(v)for k,v in [('supplier_source','source'),('supplier_url','url'),('observed_at','observed_at'),('source_sha256','source_sha256'),('listed_price','raw_amount')])):return None,'Заводское основание относится к другой строке покупки'
    length=None
    if {_unit(proof['sale_unit']),_unit(field('unit'))}=={'шт','м'}:
        # Full purchase/photo source was validated above. Characteristics use
        # their own exact locator schema; attachment metadata grants no length
        # authority and remains unchanged in the original purchase proof.
        characteristics_source={key:proof['source'][key] for key in ('role','ref','record_sha256','body_sha256','main_facts')}
        characteristics_source['main_facts']=dict(characteristics_source['main_facts'])
        retained=dom.read_primary_characteristics_source(characteristics_source,expected_code=proof['article'])
        if retained:
            record,owned=retained
            length=dict(version=1,source=characteristics_source,url=record['url'],observed_at=record['observed_at'],title=owned['title'],article=proof['article'],source_sha256=hashlib.sha256(owned['text'].encode()).hexdigest(),length=owned['characteristics']['length'])
    answer,reason=strict_unit_decision(listed=proof['raw_amount'],sale_unit=proof['sale_unit'],requested_unit=field('unit'),title=proof['title'],unit_text=proof['unit_text'],owned_length=length,factory_proof=factory_proof,request_position=position)
    if answer and length:answer['owned_length']=length
    return answer,reason


def _retained_input(reference):
    from . import http_card as dom
    if type(reference)is not dict or set(reference)!={'ref','record_sha256','snapshot_sha256','card_id'}:raise ValueError('retained_input_reference')
    if any(type(reference[k])is not str for k in reference) or not re.fullmatch('[a-f0-9]{32}',reference['card_id']) or any(not re.fullmatch('[a-f0-9]{64}',reference[k])for k in ('record_sha256','snapshot_sha256')):raise ValueError('retained_input_reference')
    if not re.fullmatch(r'captured_inputs/[a-f0-9]{32}\.json',reference['ref']):raise ValueError('retained_input_path')
    root=dom.CACHE_DIR.resolve();path=dom.CACHE_DIR/reference['ref']
    if path.is_symlink()or path.parent.is_symlink()or path.resolve().parent!=root/'captured_inputs'or path.stat().st_size>3*1024*1024:raise ValueError('retained_input_boundary')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=reference['record_sha256']:raise ValueError('retained_input_changed')
    original=json.loads(raw);payload=original['payload']
    if original['id']!=reference['card_id']or original['sha256']!=reference['snapshot_sha256']or hashlib.sha256(payload.encode()).hexdigest()!=original['sha256']:raise ValueError('retained_input_payload_changed')
    return original,json.loads(payload)


def _validate_replay_facts(facts):
    original,raw=_retained_input(facts['derived_from_capture'])
    actual=dict(facts)
    for key in ('derived_from_capture','purchase_row_v2'):actual.pop(key,None)
    # Only the independently verified DOM locator is added to the local view.
    source=actual.pop('rendered_source',None)
    if 'rendered_source' in raw:actual['rendered_source']=raw['rendered_source']
    identity=dict(actual.get('identity_evidence')or {})
    if 'rendered_source'not in (raw.get('identity_evidence')or {}):identity.pop('rendered_source',None)
    actual['identity_evidence']=identity
    if not _exact(actual,raw):raise ValueError('retained_input_raw_facts_changed')
    from . import http_card as dom
    expected=raw.get('rendered_source')
    if expected is None:expected=dom.rendered_source(dom._read_render_frame(raw['main_facts']),raw['main_facts'])
    if not _exact(source,expected) or not _exact(facts['identity_evidence'].get('rendered_source'),expected):raise ValueError('retained_input_source_changed')
    return original


def validate_snapshot(snapshot):
    if hashlib.sha256(snapshot.payload.encode()).hexdigest()!=snapshot.sha256:raise ValueError('snapshot_changed')
    facts=snapshot.facts()
    if 'derived_from_capture' in facts:
        original=_validate_replay_facts(facts)
        if not _exact(snapshot.deadline,original.get('deadline')):raise ValueError('retained_input_deadline_changed')
    return facts


def replay_snapshot(reference):
    """New local view of a trusted pinned retained input; never a new capture."""
    from . import http_card as dom
    from .card_batch import CardSnapshot
    import copy,uuid
    original,facts=_retained_input(reference)
    source=facts.get('rendered_source')
    if source is None:
        frame=dom._read_render_frame(facts['main_facts'])
        source=dom.rendered_source(frame,facts['main_facts'])
    if not dom._validated_rendered_owner(source):raise ValueError('retained_input_owner_unconfirmed')
    quote=_derive_v2(source,url=facts['url'],observed_at=facts['captured_at'],title=facts['main_title'],article=facts['page_article'],text=facts['text'])
    if quote is None:raise ValueError('retained_purchase_unconfirmed')
    # Only independently verified bindings are added. Directory capture is NEVER rerun.
    facts=copy.deepcopy(facts);facts['rendered_source']=source;facts['purchase_row_v2']=quote
    facts['identity_evidence']={**facts['identity_evidence'],'rendered_source':source}
    facts['derived_from_capture']=dict(reference)
    retained=json.dumps(facts,ensure_ascii=False,sort_keys=True,allow_nan=False)
    return CardSnapshot(uuid.uuid4().hex,retained,hashlib.sha256(retained.encode()).hexdigest(),original.get('deadline'))

