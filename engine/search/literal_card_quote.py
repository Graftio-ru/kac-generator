"""Literal quotes from a hash-bound HTTP main product, never narrative guesses."""
import math
import re


def current_literal_quote(facts, *, financial_outcome=None):
    from .http_card import read_source
    from .product_identity import canonical_card_url, valid_identity
    from ..source_quote import (owned_purchase_facts, selected_price_context,
                               observe_captured_quote, quote_review_reason, strict_unit_decision,
                               complete_kit_pack_context, complete_kit_identity_metadata)
    from ..price_capture import _unit, visible_prices
    from .agent import _price_per_requested_unit, _product_length_metres

    source = facts.get('http_source') or facts.get('rendered_source')
    identity = facts.get('identity_evidence') or {}
    if not source or not valid_identity(identity, facts['expected_type_mark'], facts['expected_name']):
        return None
    retained = read_source(source,allow_rendered=True)
    if not retained or not retained[1]:
        return None
    record, owned = retained
    if record.get('role')=='http_primary_dom':
        from .http_card import http_primary_source
        source=http_primary_source(source)
        if source is None:return None
    title, text = owned['title'], owned['text']
    if (canonical_card_url(record['url']) != canonical_card_url(facts['url'])
            or record['observed_at'] != facts['captured_at']
            or title != facts['main_title'] or text != facts['text']
            or owned['article'] != identity.get('main_article', '')):
        return None
    if len(text) > 20000:
        return None
    typed_purchase=owned.get('purchase_contract')
    if typed_purchase is not None:
        # read_source has independently rederived this exact owned quotation.
        if typed_purchase.get('version')!=2 or typed_purchase.get('role')!='current_owned_purchase_row':
            return None
        listed,sale=typed_purchase['raw_amount'],typed_purchase['sale_unit']
        if listed!=owned['listed_price']:
            return None
    else:
        purchase = owned_purchase_facts(text)
        listed, sale = purchase['raw_amount'], purchase['sale_unit']
        if purchase['reason'] or listed != owned['listed_price']:
            return None
    if owned.get('factory_reference'):
        from .factory_reference import derive_singleton,FactoryEvidenceError
        position=dict(name=facts['expected_name'],type_mark=facts['expected_type_mark'],unit=facts['expected_unit'])
        try:proof=derive_singleton(source,position)
        except (FactoryEvidenceError,OSError,ValueError,KeyError):return None
        data=dict(product_title=title,matches_requested_product=True,match_reason='Заводской артикул и состав: выведенная эквивалентность одной упаковки комплекту',listed_price_with_vat=listed,price_unit=sale,price_with_vat=listed,package_quantity=1,listed_price_covers_multiple_requested_units=True,factory_unit_proof=proof)
        if typed_purchase is not None:
            from .owned_purchase_row import derive
            row=derive(source,url=facts['url'],observed_at=facts['captured_at'],title=title,article=identity.get('main_article',''),text=text)
            if not row or row.get('version')!=2:return None
            data['purchase_row_v2']=row
        evidence=observe_captured_quote(text,actual_url=facts['url'],observed_at=facts['captured_at'],url=facts['url'],title=title,article=identity.get('main_article',''),listed=listed,normalized=listed,sale_unit=sale,requested_unit=facts['expected_unit'],data=data,expected_name=facts['expected_name'],expected_mark=facts['expected_type_mark'])
        view=dict(data,url=facts['url'],identity_evidence=identity,source_quote_evidence=evidence,normalized_price_unit=facts['expected_unit'])
        return data if evidence and not quote_review_reason(view,position) else None
    # A second amount/denomination or an unspecified pack must keep fallback.
    if set(visible_prices(text)) != {listed}:
        return None
    if re.search(r'\d[\d .,]*\s*(?:USD|EUR|\$|€)', text, re.I):
        return None
    labels = re.findall(r'(?im)^\s*(?:Единица\s+измерения|Ед\.?\s*изм\.?|Sale\s+unit|Unit\s+of\s+sale)\s*[:：]?\s*([^\r\n]+)', text)
    if any(_unit(label.strip()) != _unit(sale) for label in labels):
        return None
    kit_pack = _unit(sale) == 'уп' and _unit(facts['expected_unit']) == 'компл'
    denomination_unit = lambda value: 'уп' if kit_pack and re.fullmatch(r'упаковку', value, re.I) else _unit(value)
    _, unit_text = selected_price_context(text, listed)
    denomination = r'(\d+(?:[.,]\d+)?)?\s*(шт\.?|штук[аиу]?|м|метр(?:а|ов)?|компл(?:ект)?|упаковк[ау])(?![а-яa-z])'
    inline = re.findall(r'(?:/|\bза\s*)\s*'+denomination, unit_text, re.I)
    if any((float(quantity.replace(',', '.')) if quantity else 1) != 1
           or denomination_unit(unit) != _unit(sale) for quantity, unit in inline):
        return None
    # An explicit but unfamiliar denominator is unresolved, not a unit label.
    clauses = re.findall(r'(?im)^\s*(?:Цена\s+за|Price\s+(?:per|for))\s*([^\r\n]+)', text)
    currency = r'(?:₽|руб(?:\.|лей|ля|ль)?|RUB)(?![^\W\d_])'
    clauses += re.findall(currency+r'\s*(?:[—–:-]\s*)?(?:за\b|/|per\b|for\b)\s*([^\r\n]+)', text, re.I)
    for clause in clauses:
        denominator = re.fullmatch(denomination+r'\s*[:：]?', clause.strip(), re.I)
        if (not denominator or (float(denominator[1].replace(',', '.')) if denominator[1] else 1) != 1
                or denomination_unit(denominator[2]) != _unit(sale)):
            return None
    packs = re.findall(r'(?im)^\s*(?:Кол-во\s+в\s+упак\.?|Количество\s+в\s+упаковке|Pack\s+quantity|Упаковка|Фасовка)\s*[:：]?\s*([^\r\n]+)', text)
    if not kit_pack and any(not re.fullmatch(r'1(?:[.,]0+)?(?:\s*шт\.?)?', value.strip()) for value in packs):
        return None
    def raw_rejected():
        if financial_outcome is None:return
        # The source quotation must pass its original owner/role/price selectors
        # in its printed denomination before requested-unit rejection is typed.
        raw=dict(product_title=title,matches_requested_product=True,
                 listed_price_with_vat=listed,price_unit=sale,price_with_vat=listed)
        evidence=observe_captured_quote(text,actual_url=facts['url'],observed_at=facts['captured_at'],url=facts['url'],title=title,article=identity.get('main_article',''),listed=listed,normalized=listed,sale_unit=sale,requested_unit=sale,data=raw,expected_name=facts['expected_name'],expected_mark=facts['expected_type_mark'])
        view=dict(raw,url=facts['url'],identity_evidence=identity,source_quote_evidence=evidence,normalized_price_unit=sale)
        if evidence and not quote_review_reason(view,dict(unit=sale,type_mark=facts['expected_type_mark'],name=facts['expected_name'])):
            from .financial_outcome import RawFinancialRejection
            financial_outcome(RawFinancialRejection(dict(raw,price_with_vat=None),'requested_unit_unresolved'))
    wanted = _unit(facts['expected_unit'])
    normalized = listed
    decision = None
    metadata=complete_kit_identity_metadata(title,identity.get('main_article',''),facts['expected_name'],facts['expected_type_mark'])
    if kit_pack:
        unit_text = complete_kit_pack_context(text, listed)
        decision, reason = strict_unit_decision(listed=listed, sale_unit=sale,
            requested_unit=facts['expected_unit'], title=title, unit_text=unit_text,identity_metadata=metadata)
        if reason:
            raw_rejected();return None
        normalized = decision['price']
    elif {_unit(sale), wanted} == {'шт', 'м'}:
        # L3000 explicitly names length in a product designation. An arbitrary
        # 3000-mm dimension, body prose or an unlabelled attribute cannot do so.
        lengths = [float(value) / 1000 for value in re.findall(r'(?i)\bL\s*(\d{3,4})(?![\w.,])', title)]
        lengths += [float(value.replace(',', '.')) / (1000 if unit.lower() == 'мм' else 1)
                    for value, unit in re.findall(r'(?i)\bдлина\s*[:=—-]?\s*(\d+(?:[.,]\d+)?)\s*(мм|м)(?!\w)', title)]
        if len(set(lengths)) != 1 or not lengths[0] > 0:
            raw_rejected();return None
        length = lengths[0]
        if _product_length_metres(title) != length:
            return None  # Existing downstream conversion remains authoritative.
        normalized = listed / length if _unit(sale) == 'шт' else listed * length
    elif _unit(sale) != wanted:
        raw_rejected();return None
    if not math.isfinite(normalized) or normalized <= 0:
        return None
    data = dict(product_title=title, matches_requested_product=True,
                match_reason='Явные сведения сохранённой основной HTTP-карточки',
                listed_price_with_vat=listed, price_unit=sale, price_with_vat=normalized)
    if decision:
        data.update(package_quantity=decision['quantity'], listed_price_covers_multiple_requested_units=True)
    if _price_per_requested_unit(data, facts['expected_unit'], title, owned_unit_context=unit_text,identity_metadata=metadata) is None:
        return None
    evidence = observe_captured_quote(text, actual_url=facts['url'],
        observed_at=facts['captured_at'], url=facts['url'], title=title,
        article=identity.get('main_article', ''), listed=listed, normalized=normalized,
        sale_unit=sale, requested_unit=facts['expected_unit'], data=data,expected_name=facts['expected_name'],expected_mark=facts['expected_type_mark'])
    view = dict(data, url=facts['url'], identity_evidence=identity,
                source_quote_evidence=evidence, normalized_price_unit=facts['expected_unit'])
    position = dict(unit=facts['expected_unit'], type_mark=facts['expected_type_mark'], name=facts['expected_name'])
    return data if evidence and not quote_review_reason(view, position) else None
