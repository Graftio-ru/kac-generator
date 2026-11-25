"""Схемы запросов/ответов API (тело JSON между UI и движком)."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .config import CACHE_DIR
from .offer_selection import unit_review_pending
from .models import (OrgStatus, PositionResult, PriceOffer, Requisites,
                     SpecPosition)


def _resolve_shot(name: str) -> str | None:
    """Имя файла из кэша → абсолютный путь (или None)."""
    if not name:
        return None
    p = CACHE_DIR / Path(name).name
    return str(p) if p.exists() else None


class OfferIn(BaseModel):
    """Оффер, заданный/отредактированный вручную в UI."""
    price_with_vat: float
    url: str = ""
    org_name: str = ""
    inn: str = ""
    kpp: str = ""
    city: str = ""
    status: int = 2
    from_letter: bool = False
    product_title: str = ""
    page_title: str = ""
    identity_evidence: dict = Field(default_factory=dict)
    page_article: str = ""
    price_unit: str = ""
    normalized_price_unit: str = ""
    source_quote_evidence: dict = Field(default_factory=dict)
    article_match_evidence: dict = Field(default_factory=dict)  # display input is never trusted
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
    listed_price_with_vat: float | None = None
    match_review_required: bool = False
    model_review_accepted: bool = False
    unit_review_required: bool = False
    unit_review_reason: str = ""
    refresh_failed: bool = False
    refresh_failure_reason: str = ""
    refresh_checked_at: float = 0.0
    in_moscow_region: bool | None = None
    region_status: Literal["", "unknown", "confirmed", "incompatible"] = ""
    region_evidence: str = ""  # warehouse/pickup/delivery observation, never inferred from city
    # имена файлов скриншотов в кэше движка (отдаются через GET /cache/{name})
    screenshot_product: str = ""
    screenshot_context: str = ""
    context_capture_evidence: dict = Field(default_factory=dict)
    context_capture_review_reason: str = ""
    screenshot_requisites: str = ""


class PositionIn(BaseModel):
    """Позиция в редактируемой таблице UI."""
    number: int
    name: str
    full_name: str = ""
    type_mark: str = ""
    alternative_articles: list[str] = Field(default_factory=list)
    alternative_names: list[str] = Field(default_factory=list)

    @field_validator('alternative_articles','alternative_names',mode='before')
    @classmethod
    def validate_alternatives(cls,value,info):
        from .models import normalize_alternatives
        return normalize_alternatives(value,article=info.field_name=='alternative_articles')

    unit: str = "шт"
    qty: float = 0.0
    is_unique: bool = False
    needs_review: bool = False        # OCR source row must be checked against original
    review_issue: str = ""            # why the source cannot be verified completely
    review_accepted: bool = False      # explicit decision to use the uncertain row
    review_fields: dict[str, str] = Field(default_factory=dict)
    source_evidence: dict = Field(default_factory=dict)
    source_is_image: bool = False     # provenance, never automatic human acceptance
    source_index: int | None = None    # индекс файла в исходном списке спецификаций
    discipline: str = "ЭМ"
    offers: list[OfferIn] = Field(default_factory=list)
    # ручные данные ТСН (если сметчик вводит сам)
    tsn_base_price: float | None = None
    tsn_coefficient: float | None = None

    def to_result(self) -> PositionResult:
        pos = SpecPosition(
            number=self.number, name=self.name, full_name=self.full_name,
            type_mark=self.type_mark, unit=self.unit, qty=self.qty,
            alternative_articles=self.alternative_articles,alternative_names=self.alternative_names,
            is_unique=self.is_unique, discipline=self.discipline,
            review_fields=self.review_fields, review_issue=self.review_issue,
            source_evidence=self.source_evidence,
        )
        from .search.product_identity import current_offer_article_evidence
        offers = [
            PriceOffer(
                price_with_vat=o.price_with_vat, url=o.url, from_letter=o.from_letter,
                product_title=o.product_title,
                page_title=o.page_title,
                page_article=o.page_article,
                identity_evidence=o.identity_evidence,
                price_unit=o.price_unit,
                normalized_price_unit="" if unit_review_pending(o) else o.normalized_price_unit or self.unit,
                price_unit_evidence=o.price_unit_evidence,
                source_quote_evidence=o.source_quote_evidence,
                article_match_evidence=current_offer_article_evidence(o,pos),
                estimator_approval_ref=o.estimator_approval_ref, estimator_approval=o.estimator_approval,
                catalogue_ref=o.catalogue_ref,
                catalogue_observed_at=o.catalogue_observed_at,
                catalogue_source_sha256=o.catalogue_source_sha256,
                catalogue_image=o.catalogue_image,
                seller_evidence=o.seller_evidence,
                catalogue_vat_status=o.catalogue_vat_status,
                catalogue_vat_notice=o.catalogue_vat_notice,
                price_capture_evidence=o.price_capture_evidence,
                price_capture_review_reason=o.price_capture_review_reason,
                listed_price_with_vat=o.listed_price_with_vat,
                match_review_required=o.match_review_required,
                model_review_accepted=o.model_review_accepted,
                unit_review_required=unit_review_pending(o),
                unit_review_reason=o.unit_review_reason or ("Не указана единица продажи карточки" if unit_review_pending(o) else ""),
                refresh_failed=o.refresh_failed,
                refresh_failure_reason=o.refresh_failure_reason,
                refresh_checked_at=o.refresh_checked_at,
                in_moscow_region=o.in_moscow_region,
                region_status=o.region_status, region_evidence=o.region_evidence,
                requisites=Requisites(name=o.org_name, inn=o.inn, kpp=o.kpp,
                                      city=o.city, status=OrgStatus(o.status)),
                screenshot_product=_resolve_shot(o.screenshot_product),
                screenshot_context=o.screenshot_context,context_capture_evidence=o.context_capture_evidence,context_capture_review_reason=o.context_capture_review_reason,
                screenshot_requisites=_resolve_shot(o.screenshot_requisites),
            )
            for o in sorted(self.offers, key=lambda x: x.price_with_vat)
        ]
        return PositionResult(position=pos, offers=offers)


class ParseResponse(BaseModel):
    source_outcomes: list[dict] = Field(default_factory=list)
    positions: list[PositionIn]
    research_job_id: str | None = None


class BuildRequest(BaseModel):
    source_outcomes: list[dict] = Field(default_factory=list)
    object_name: str
    fix_date: str = ""
    use_tsn: bool = True
    skip_search: bool = False           # не искать в интернете (цены заданы вручную)
    tsn_work_kind: str = "электромонтаж"  # вид работ для индекса пересчёта ТСН
    positions: list[PositionIn]


class ResearchDiagnosticIn(BaseModel):
    code: str = ""
    reason: str = ""
    url: str = ""
    observed_article: str = ""


class ResearchReportPositionIn(PositionIn):
    candidates: list[OfferIn] = Field(default_factory=list)
    research_status: str = ""
    research_reason: str = ""
    research_target: int = 0
    diagnostics: list[ResearchDiagnosticIn] = Field(default_factory=list)
    source_checks: list[dict] = Field(default_factory=list)
    catalogue_refresh_attempts: list[dict] = Field(default_factory=list)
    catalogue_coverage_warning: str = ""
    catalogue_read: bool = False
    catalogue_product_id: str = ""
    catalogue_revision: int = 0


class ResearchReportRequest(BaseModel):
    object_name: str = ""
    positions: list[ResearchReportPositionIn] = Field(min_length=1, max_length=5000)
