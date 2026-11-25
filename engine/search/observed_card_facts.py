"""Request-independent observations from one saved page, never quote authority.

Refs address decoded-source character ranges and actual DOM nodes/JSON fields.
Unknown and conflicting fields remain explicit. No requested SKU, unit, stock
count, cart quantity, model output or price normalization can supply a fact.
"""

import hashlib
import json
import math
import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictStr, model_validator

from .http_card import MainParser, Node, itemtype, prop, visible

ArticleRole = Literal["manufacturer_code", "model", "seller_sku", "unknown", "conflicting"]

class SavedPage(BaseModel):
    """Validated source identity supplied by the existing immutable capture reader."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    source_id: str = Field(min_length=1, max_length=4096)
    url: str = Field(pattern=r"^https?://", max_length=4096)
    observed_at: float = Field(gt=0, allow_inf_nan=False)
    body_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    representation: Literal["http_html", "rendered_dom"]
    status: int | None = Field(ge=100, le=599)
    encoding: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_status_provenance(self) -> Self:
        """Require actual HTTP status on an HTTP record; a rendered DOM may have no retained HTTP-status evidence."""
        if self.representation == "http_html" and self.status is None:
            raise ValueError("observed_http_status_missing")
        return self


class FactRef(BaseModel):
    """Exact decoded-HTML node range; optional JSON pointer names a structured field."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    node_path: list[int]
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    fragment_sha256_utf8: str = Field(pattern=r"^[a-f0-9]{64}$")
    field: str

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        """Reject empty/reversed provenance ranges when persisted refs are loaded."""
        if self.char_end <= self.char_start:
            raise ValueError("observed_fact_empty_range")
        return self


class ObservedField(BaseModel):
    """One sourced value, an explicit absence, or a conflict; never a guessed value."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)
    state: Literal["known", "unknown", "conflicting"] = "unknown"
    value: StrictStr | StrictFloat | None = None
    refs: list[FactRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        """Require a sourced known value and prohibit values on unknown/conflicting states."""
        if self.state == "known":
            if self.value is None or not self.refs or isinstance(self.value, str) and not self.value.strip():
                raise ValueError("observed_fact_known_without_evidence")
        elif self.value is not None:
            raise ValueError("observed_fact_unknown_with_value")
        if self.state == "conflicting" and len(self.refs) < 2:
            raise ValueError("observed_fact_conflict_without_evidence")
        return self


class ObservedCardFacts(BaseModel):
    """Raw facts are independently retained; this type has no normalized/eligible price."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    source: SavedPage
    state: Literal["observed", "access_error", "redirect_unresolved", "unrecognized", "source_invalid"]
    article_role: ArticleRole = "unknown"
    title: ObservedField = Field(default_factory=ObservedField)
    article: ObservedField = Field(default_factory=ObservedField)
    manufacturer: ObservedField = Field(default_factory=ObservedField)
    amount: ObservedField = Field(default_factory=ObservedField)
    currency: ObservedField = Field(default_factory=ObservedField)
    sale_unit: ObservedField = Field(default_factory=ObservedField)
    package_quantity: ObservedField = Field(default_factory=ObservedField)

    @model_validator(mode="after")
    def validate_field_kinds(self) -> Self:
        """Reject malformed persisted field kinds and facts on a failed/unrecognized page.

        A generic sourced-field wrapper is shared, but monetary/count values
        are numbers and identity/denomination values are text at this boundary.
        """
        for name in ("title", "article", "manufacturer", "currency", "sale_unit"):
            field = getattr(self, name)
            if field.state == "known" and not isinstance(field.value, str):
                raise ValueError("observed_fact_text_kind_invalid")
        for name in ("amount", "package_quantity"):
            field = getattr(self, name)
            if field.state == "known" and (type(field.value) is not float or field.value < 0):
                raise ValueError("observed_fact_number_kind_invalid")
        if self.state != "observed":
            if any(getattr(self, name).state != "unknown" for name in ("title", "article", "manufacturer", "amount", "currency", "sale_unit", "package_quantity")):
                raise ValueError("observed_fact_failed_page_has_values")
        return self


def node_reference(html: str, node: Node, field: str) -> FactRef:
    """Bind a value to its real DOM fragment; offsets are characters, not raw-byte offsets."""
    if not 0 <= node.start < node.end <= len(html):
        raise ValueError("observed_fact_node_range_invalid")
    fragment = html[node.start:node.end]
    return FactRef(
        node_path=node.path(), char_start=node.start, char_end=node.end,
        fragment_sha256_utf8=hashlib.sha256(fragment.encode("utf-8")).hexdigest(), field=field,
    )


def one_value(candidates: list[tuple[str | float, FactRef]]) -> ObservedField:
    """Keep all equal-value refs; multiple distinct observations become conflict, not a selection."""
    if not candidates:
        return ObservedField()
    values = {value for value, reference in candidates}
    references = [reference for value, reference in candidates]
    if len(values) != 1:
        return ObservedField(state="conflicting", refs=references)
    return ObservedField(state="known", value=candidates[0][0], refs=references)


def text_value(node: Node) -> str:
    """Read only a node's declared content or visible text, without importing requested values."""
    return node.attrs.get("content") or " ".join(node.value().split())


def decimal_value(value: str) -> float | None:
    """Parse one explicit finite nonnegative monetary/count number; absence remains None."""
    cleaned = value.replace("\u00a0", "").replace(" ", "").replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d+)?", cleaned):
        return None
    number = float(cleaned)
    return number if math.isfinite(number) else None


def structured_products(nodes: list[Node], url: str) -> list[tuple[dict, Node, str]]:
    """Find actual Product JSON objects linked to this page; never use another product's offer.

    The pointer is relative to the original script JSON. Malformed or deeply
    nested data is ignored; no JSON-LD value becomes financial qualification.
    """
    products = []

    def walk(value: object, node: Node, pointer: str, depth: int = 0) -> None:
        """Visit bounded JSON containers and preserve exact field-address paths."""
        if depth > 32:
            return
        if isinstance(value, dict):
            kinds = value.get("@type")
            if (kinds == "Product" or isinstance(kinds, list) and "Product" in kinds):
                linked = value.get("url")
                if isinstance(linked, str) and linked.rstrip("/") == url.rstrip("/"):
                    products.append((value, node, pointer))
            for key, child in value.items():
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                walk(child, node, pointer + "/" + escaped, depth + 1)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, node, pointer + "/" + str(index), depth + 1)

    for node in nodes:
        if node.tag != "script" or node.attrs.get("type", "").lower() != "application/ld+json":
            continue
        try:
            value = json.loads("".join(part for part in node.text if isinstance(part, str)))
            walk(value, node, "")
        except (ValueError, RecursionError):
            continue
    return products


def identity_fields(html: str, owned: list[Node], nodes: list[Node], url: str) -> tuple[dict[str, ObservedField], ArticleRole]:
    """Read explicit manufacturer identity roles, reconciling source conflicts without requested data.

    Model/manufacturer-code observations are compared together. A seller SKU
    is only a fallback role, so it is not confused with a manufacturer code.
    Conflicting peer claims remain conflicting rather than first-value wins.
    """
    fields = {name: ObservedField() for name in ("article", "manufacturer")}
    article_role: ArticleRole = "unknown"
    article_candidates = []
    manufacturer_candidates = []
    for node in owned:
        value = text_value(node)
        if value and (prop(node, "mpn") or prop(node, "model")):
            property_name = "mpn" if prop(node, "mpn") else "model"
            article_candidates.append((value, node_reference(html, node, "itemprop:" + property_name)))
            article_role = "manufacturer_code" if prop(node, "mpn") or article_role == "manufacturer_code" else "model"
        if value and (prop(node, "manufacturer") or prop(node, "brand")):
            # Removing an explicit label changes no value; the entire original
            # DOM row remains the evidence, not an invented substring offset.
            value = re.sub(r"^(?:Производитель|Manufacturer)\s*[:：]?\s*", "", value, flags=re.I)
            property_name = "manufacturer" if prop(node, "manufacturer") else "brand"
            manufacturer_candidates.append((value, node_reference(html, node, "itemprop:" + property_name)))
    if not article_candidates:
        for node in owned:
            value = " ".join(node.value().split())
            if len(value) <= 160:
                match = re.fullmatch(r"(?:Артикул производителя|Код производителя|Manufacturer code|MPN)\s*[:：]?\s+([A-Za-zА-Яа-я0-9._/-]+)", value, re.I)
                if match:
                    article_candidates.append((match[1], node_reference(html, node, "visible_manufacturer_article")))
                    article_role = "manufacturer_code"
    if not article_candidates:
        article_candidates = [
            (text_value(node), node_reference(html, node, "itemprop:sku"))
            for node in owned if prop(node, "sku") and text_value(node)
        ]
        if article_candidates:
            article_role = "seller_sku"
    fields["article"] = one_value(article_candidates)
    fields["manufacturer"] = one_value(manufacturer_candidates)
    structured = structured_products(nodes, url)
    if len(structured) == 1:
        product, node, pointer = structured[0]
        structured_candidates = {"article": list(article_candidates), "manufacturer": list(manufacturer_candidates)}
        if product.get("mpn") or product.get("model"):
            if article_role == "seller_sku":
                # A seller's catalogue number is a different identifier role;
                # it cannot contradict an explicit manufacturer/model code.
                structured_candidates["article"] = []
            article_role = "manufacturer_code" if product.get("mpn") else "model"
        keys = [("article", "mpn"), ("article", "model"), ("manufacturer", "brand"), ("manufacturer", "manufacturer")]
        if article_role in {"unknown", "seller_sku"} and not product.get("mpn") and not product.get("model"):
            keys.append(("article", "sku"))
        for name, key in keys:
            value = product.get(key)
            field_pointer = pointer + "/" + key
            if name == "manufacturer" and isinstance(value, dict):
                value = value.get("name")
                field_pointer += "/name"
            if isinstance(value, str) and value:
                structured_candidates[name].append((value, node_reference(html, node, "json:" + field_pointer)))
                if name == "article" and key == "sku":
                    article_role = "seller_sku"
        for name, candidates in structured_candidates.items():
            fields[name] = one_value(candidates)
    if fields["article"].state == "conflicting":
        article_role = "conflicting"
    return fields, article_role


def purchase_fields(html: str, owned: list[Node]) -> dict[str, ObservedField]:
    """Read one offer's raw monetary fields and explicit pack contents without normalization.

    A cart subtotal/quantity and stock count are not price denomination or
    package contents. Missing or competing evidence stays unknown/conflicting.
    """
    fields = {name: ObservedField() for name in ("amount", "currency", "sale_unit", "package_quantity")}
    offers = [node for node in owned if itemtype(node, "Offer") or itemtype(node, "AggregateOffer")]
    if len(offers) == 1:
        offer_nodes = [node for node in offers[0].descendants() if visible(node)]
        amounts = []
        currencies = []
        units = []
        for node in offer_nodes:
            if prop(node, "price"):
                amount = decimal_value(text_value(node))
                if amount is not None:
                    amounts.append((amount, node_reference(html, node, "itemprop:price")))
            if prop(node, "priceCurrency") and text_value(node):
                currencies.append((text_value(node), node_reference(html, node, "itemprop:priceCurrency")))
            value = " ".join(node.value().split())
            # A denomination printed beside this offer is raw text. Never use
            # stock or the cart's numeric quantity as package contents.
            if len(value) <= 120:
                match = re.search(r"(?:за\s+|/\s*)((?:\d+(?:[.,]\d+)?\s*)?(?:шт\.?|упак\.?|компл\.?|м))(?!\w)", value, re.I)
                if match:
                    units.append((match[1], node_reference(html, node, "visible_sale_unit")))
        fields["amount"] = one_value(amounts)
        fields["currency"] = one_value(currencies)
        fields["sale_unit"] = one_value(units)
    packages = []
    for node in owned:
        value = " ".join(node.value().split())
        if len(value) > 120:
            continue
        match = re.search(r"(?:в упаковке|количество в упаковке)\s*[:：]?\s*(\d+(?:[.,]\d+)?)\s*(?:шт|компл|м)(?!\w)", value, re.I)
        if match:
            quantity = decimal_value(match[1])
            if quantity is not None:
                packages.append((quantity, node_reference(html, node, "visible_package_quantity")))
    fields["package_quantity"] = one_value(packages)
    return fields


def extract_observed_facts(body: bytes, source: SavedPage) -> ObservedCardFacts:
    """Extract independent observations before matching or any financial predicate.

    Source mismatch is an error, not an empty page. Access errors carry no price.
    A saved HTTP DOM may declare raw metadata; computed browser visibility is
    only known for a visibility-annotated rendered DOM. No live capture occurs.
    """
    if len(body) > 5 * 1024 * 1024 or hashlib.sha256(body).hexdigest() != source.body_sha256:
        raise ValueError("observed_fact_source_changed")
    if source.status is not None and source.status >= 400:
        return ObservedCardFacts(source=source, state="access_error")
    if source.status is not None and source.status >= 300:
        return ObservedCardFacts(source=source, state="redirect_unresolved")
    html = body.decode(source.encoding, errors="strict")
    parser = MainParser(html, check_controls=False)
    nodes = list(parser.root.descendants())
    headings = [node for node in nodes if node.tag == "h1" and visible(node)]
    if len(headings) != 1:
        return ObservedCardFacts(source=source, state="unrecognized")
    title = one_value([(text_value(headings[0]), node_reference(html, headings[0], "text"))])
    products = [node for node in nodes if itemtype(node, "Product") and visible(node)]
    owner = products[0] if len(products) == 1 and headings[0] in list(products[0].descendants()) else None
    owned = [node for node in owner.descendants() if visible(node)] if owner else []
    fields, article_role = identity_fields(html, owned, nodes, source.url)
    fields.update(purchase_fields(html, owned))
    return ObservedCardFacts(source=source, state="observed", title=title, article_role=article_role, **fields)


def article_comparison(facts: ObservedCardFacts, requested_article: str) -> Literal["article_equal", "article_mismatch", "unknown"]:
    """Report only literal article correspondence; this never grants native identity or price approval."""
    if facts.article_role not in {"manufacturer_code", "model"} or facts.article.state != "known" or not isinstance(facts.article.value, str) or not requested_article:
        return "unknown"
    return "article_equal" if facts.article.value.casefold() == requested_article.casefold() else "article_mismatch"
