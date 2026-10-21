"""Парсер писем/счетов с ценами на уникальное оборудование → PriceOffer (from_letter=True).

Из счёта/письма извлекаются: наименование позиции, цена, реквизиты поставщика
(ИНН/КПП/наименование). Эти цены идут в КАЦ напрямую (1 строка) и НЕ попадают в Том ТКП.
"""
from __future__ import annotations

from pathlib import Path

import fitz

from ..llm import ModelOutputInvalid, complete_json
from ..models import OrgStatus, PriceOffer, Requisites

SYSTEM = (
    "Ты извлекаешь позиции с ценами из счёта/письма поставщика. "
    "Верни JSON-объект с полем items — массивом объектов: name (наименование товара), price_with_vat (число, цена за "
    "единицу С НДС), qty (число, если есть), supplier_name (наименование организации-продавца), "
    "supplier_inn (ИНН), supplier_kpp (КПП), vat_included (bool). Только JSON вида {\"items\": [...]}."
)


def parse_letter(pdf_path: str | Path) -> list[dict]:
    """Извлекает позиции письма как список словарей (сырьё для матчинга со спецификацией)."""
    doc = fitz.open(pdf_path)
    text = "\n".join(doc[i].get_text() for i in range(doc.page_count))
    doc.close()
    if not text.strip():
        raise ValueError(f"Пустой текст в письме {pdf_path}")
    response = complete_json(f"Счёт/письмо:\n\n{text[:30000]}", system=SYSTEM, smart=True)
    items=response if isinstance(response,list) else response.get('items') if isinstance(response,dict) else None
    if not isinstance(items,list):
        raise ModelOutputInvalid('invalid_schema',output_kind='letter')
    import math
    for item in items:
        if not isinstance(item,dict) or not isinstance(item.get('name'),str):
            raise ModelOutputInvalid('invalid_rows',output_kind='letter')
        price=item.get('price_with_vat')
        if isinstance(price,bool) or not isinstance(price,(int,float)) or not math.isfinite(price)or price<=0:
            raise ModelOutputInvalid('invalid_rows',output_kind='letter')
    return items


def letter_to_offer(item: dict) -> PriceOffer:
    return PriceOffer(
        price_with_vat=float(item["price_with_vat"]),
        from_letter=True,
        requisites=Requisites(
            name=str(item.get("supplier_name", "")),
            inn=str(item.get("supplier_inn", "")),
            kpp=str(item.get("supplier_kpp", "")),
            status=OrgStatus.SUPPLIER,
        ),
    )
