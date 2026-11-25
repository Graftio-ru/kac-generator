"""Доменные модели, общие для всех модулей движка."""
from __future__ import annotations

from enum import IntEnum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class OrgStatus(IntEnum):
    MANUFACTURER = 1  # Производитель
    SUPPLIER = 2      # Поставщик


def normalize_alternatives(value, *, article=False):
    import unicodedata
    if type(value)is not list or len(value)>20:raise ValueError('At most20 alternatives required')
    result=[];seen=set();limit=128 if article else 500
    for item in value:
        if type(item)is not str or any(unicodedata.category(c)in {'Cc','Cf','Cs'} for c in item):raise ValueError('Alternative must be a plain string')
        item=item.strip()
        if not item or len(item)>limit:raise ValueError('Alternative length invalid')
        if item.casefold()not in seen:result.append(item);seen.add(item.casefold())
    return result


class SpecPosition(BaseModel):
    """Позиция спецификации (вход)."""
    number: int                      # № п.п. (графа A)
    name: str                        # наименование строительного ресурса (графа C)
    full_name: str = ""              # полное наименование (графа D); по умолчанию = name
    type_mark: str = ""              # тип, марка, обозначение
    alternative_articles: list[str] = Field(default_factory=list)
    alternative_names: list[str] = Field(default_factory=list)

    @field_validator("alternative_articles", "alternative_names", mode="before")
    @classmethod
    def validate_alternatives(cls,value,info):
        return normalize_alternatives(value,article=info.field_name=="alternative_articles")

    review_issue: str = ""           # unresolved source recognition details
    review_fields: dict[str, str] = Field(default_factory=dict)
    source_evidence: dict = Field(default_factory=dict)  # provenance, not verified cells
    source_reader: str = ""          # actual parser path, never a verification claim
    unit: str = "шт"                 # ед. изм. (графа E)
    qty: float = 0.0                 # количество (для ТСН)
    is_unique: bool = False          # уникальное оборуд. из письма (цена не ищется в интернете)
    discipline: str = "ЭМ"           # дисциплина/лист: ЭМ | АК

    def model_post_init(self, _ctx) -> None:
        if not self.full_name:
            self.full_name = self.name


class Requisites(BaseModel):
    """Реквизиты организации-продавца (для граф K, L, M, O)."""
    seller_source: dict | None = None  # server-owned retained product-bound terms only
    name: str = ""                   # наименование (графа K)
    inn: str = ""                    # ИНН (графа M)
    kpp: str = ""                    # КПП (графа L)
    ogrn: str = ""                   # ОГРН/ОГРНИП (для панели реквизитов в ТКП)
    address: str = ""                # юридический адрес (для панели реквизитов в ТКП)
    city: str = ""             # населённый пункт склада (графа O)
    status: OrgStatus = OrgStatus.SUPPLIER  # графа P


class PriceOffer(BaseModel):
    """Одно ценовое предложение по позиции (одна строка КАЦ + один скриншот ТКП)."""
    price_with_vat: float            # графа G — цена С НДС
    url: str = ""                    # графа N — ссылка на карточку
    product_title: str = ""          # как называется товар на сайте
    page_title: str = ""             # заголовок карточки, иногда содержит точный артикул
    identity_evidence: dict = Field(default_factory=dict)
    page_article: str = ""           # артикул основного товара из характеристик карточки
    price_unit: str = ""              # единица продажи, подтверждённая на карточке
    normalized_price_unit: str = ""   # единица price_with_vat после пересчёта
    source_quote_evidence: dict = Field(default_factory=dict)
    article_match_evidence: dict = Field(default_factory=dict)  # server-derived display only
    estimator_approval_ref: dict = Field(default_factory=dict)
    estimator_approval: dict = Field(default_factory=dict)
    catalogue_ref: dict = Field(default_factory=dict)
    catalogue_observed_at: float = 0.0
    catalogue_source_sha256: str = ""
    catalogue_image: dict = Field(default_factory=dict)
    seller_evidence: dict | None = None
    catalogue_vat_status: Literal["", "included", "excluded", "unknown"] = ""
    catalogue_vat_notice: str = ""
    price_capture_evidence: dict = Field(default_factory=dict)
    price_capture_review_reason: str = ""
    price_unit_evidence: str = ""    # explicit main-card selling-unit text
    listed_price_with_vat: Optional[float] = None  # цена в единице продажи
    match_review_required: bool = False
    model_review_accepted: bool = False
    unit_review_required: bool = False
    unit_review_reason: str = ""
    refresh_failed: bool = False     # historical quote; latest refresh did not confirm it
    refresh_failure_reason: str = ""
    refresh_checked_at: float = 0.0  # refusal observation, never the old capture date
    in_moscow_region: Optional[bool] = None  # unknown is not confirmed pickup
    region_status: Literal["", "unknown", "confirmed", "incompatible"] = ""
    region_evidence: str = ""  # warehouse/pickup/delivery observation, never inferred from city
    requisites: Requisites = Field(default_factory=Requisites)
    # пути к скриншотам (карточка + панель реквизитов) для сборки страницы ТКП
    screenshot_product: Optional[str] = None
    screenshot_context: str = ""
    context_capture_evidence: dict = Field(default_factory=dict)
    context_capture_review_reason: str = ""
    screenshot_requisites: Optional[str] = None
    # для позиций из писем: цена задана напрямую, в ТКП не попадает
    from_letter: bool = False

    def price_without_vat(self, divisor: float) -> float:
        """Графа H = G / divisor (НДС)."""
        return self.price_with_vat / divisor


class PositionResult(BaseModel):
    """Итог по позиции: до 3 ценовых предложений + метаданные периода."""
    position: SpecPosition
    offers: list[PriceOffer] = Field(default_factory=list)  # отсортированы по возрастанию цены
    # все найденные кандидаты (≥ offers) — для замены позиции в топ-3 в UI; в сборку не идут
    all_offers: list[PriceOffer] = Field(default_factory=list)
    year: str = ""                   # графа I
    quarter: str = ""                # графа J
    tkp_page: Optional[int] = None   # графа Q — № страницы в Томе ТКП
    excluded_by_tsn: bool = False    # ТСН-2001 > КАЦ → исключить из КАЦ
    # статистика поиска (для индикатора «найдено N из target»)
    sources_checked: int = 0         # сколько карточек обойдено
    sources_found: int = 0           # сколько сопоставимых офферов найдено
    sources_target: int = 0          # цель (MIN_SOURCES)
    catalogue_revision: int = 0
    catalogue_product_id: str = ""
    catalogue_coverage: dict = Field(default_factory=dict)
    catalogue_refresh_attempts: list[dict] = Field(default_factory=list)

    @property
    def max_offer(self) -> Optional[PriceOffer]:
        return self.offers[-1] if self.offers else None


class TsnRow(BaseModel):
    """Строка файла сравнения с ТСН-2001."""
    number: int
    name: str
    unit: str
    qty: float
    tsn_base_price: Optional[float] = None     # базовая расценка ТСН-2001
    coefficient: Optional[float] = None        # коэффициент пересчёта (mos.ru)
    tsn_current_price: Optional[float] = None  # расценка в текущих ценах
    kac_max_price: Optional[float] = None      # макс. цена из КАЦ
    ratio_percent: Optional[float] = None      # КАЦ / ТСН, %
    decision: str = ""                         # "включить" | "исключить (ТСН>КАЦ)"
