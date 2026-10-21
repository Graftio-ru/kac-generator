"""Literal single Offer purchase row; metadata and stock never grant a unit."""
import base64
import hashlib
import json
import math
import re


def derive(source, *, url, observed_at, title, article, text):
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
    if not facts.get('rendered_source'):
        return None
    try:
        return derive(facts['rendered_source'], url=facts['url'],
                      observed_at=facts['captured_at'], title=facts['main_title'],
                      article=facts['page_article'], text=facts['text'])
    except (KeyError,TypeError):
        return None


def verify(proof, observation):
    if type(proof) is not dict:
        return None
    derived = derive(proof.get('source') or {}, url=observation['url'],
                     observed_at=observation['observed_at'], title=observation['title'],
                     article=observation['manufacturer_code'], text=observation['source_text'])
    if (derived != proof or not derived or derived['raw_amount'] != observation['raw_amount']
            or derived['currency'] != observation['currency'] or derived['sale_unit'] != observation['sale_unit']
            or derived['source_sha256'] != observation['source_sha256']):
        return None
    if any(value is not None and (type(value) not in (int,float) or value!=1)
           for value in (observation.get('lot_quantity'),observation.get('package_quantity'))):
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
