"""Pure, bounded discovery hints. A query is never evidence of equivalence.

Pass separate source columns when available. Legacy type_mark is preserved whole:
comma-separated marks are not treated as interchangeable models. No SKU tables,
manufacturer aliases, semantic substitutions, or selling-unit conversions live here.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from ..source_codes import discovery_script_alias

_DASHES = str.maketrans({c: "-" for c in "‐‑‒–—−"})
_NUMBER = r"\d+(?:[.,]\d+)?"
_DIMENSION = re.compile(
    rf"{_NUMBER}(?:\s*[xх×]\s*{_NUMBER})+(?:\s*(?:мм|mm|см|cm|м|m))?",
    re.I,
)
_CHARACTERISTIC = re.compile(
    rf"(?:{_NUMBER}\s*(?:мм|mm|см|cm|м|m|квт|kw|кв|kv|в|v|а|a|ва|va|°|град)|"
    rf"[LНH]\s*{_NUMBER}|{_NUMBER}\s*/\s*{_NUMBER}(?:А|A)?)", re.I,
)


def _clean(value: str) -> str:
    return " ".join(value.split())


def _quoted(value: str) -> str:
    return '"' + value.replace('"', r'\"') + '"' if value else ""


def _descriptive_mark(value: str) -> bool:
    """Only wholly dimensional/rating marks; never strip model suffixes."""
    rest = _DIMENSION.sub(" ", value)
    rest = _CHARACTERISTIC.sub(" ", rest)
    return bool(value) and not rest.strip(" ,;()")


def _spelling(value: str) -> tuple[str, ...]:
    # Orthography only. Keep voltage, sizes, suffixes and digits unchanged.
    typography = re.sub(r"(?<=\d)\s*[х×]\s*(?=\d)", "x", value.translate(_DASHES), flags=re.I)
    optical = discovery_script_alias(typography)
    return tuple(dict.fromkeys(v for v in (typography, optical) if v != value))


@dataclass(frozen=True)
class QueryStep:
    query: str
    kind: str
    identity_hint: str


@dataclass(frozen=True)
class ArticleQueryPlan:
    name: str
    article: str
    model: str
    manufacturer: str
    unit: str
    characteristics: str
    identity_source: str
    warnings: tuple[str, ...]
    steps: tuple[QueryStep, ...]
    unplanned_hints: tuple[dict, ...] = ()

    def diagnostic(self):
        return dict(query_budget=3,planned_queries=list(self.queries),unplanned_hints=list(self.unplanned_hints),unplanned_hints_optional=True)

    @property
    def queries(self) -> tuple[str, ...]:
        return tuple(step.query for step in self.steps)


def build_article_query_plan(
    name: str, type_mark: str = "", *, article: str = "",
    manufacturer: str = "", unit: str = "", characteristics: str = "",
    max_queries: int = 3, alternative_articles=None, alternative_names=None,
) -> ArticleQueryPlan:
    """Exact article, full identity context, then one spelling/descriptive hint.

    With no separate article, a non-dimensional type_mark is a model identity
    (including numeric catalogue codes). Numbers in names are never inferred
    as articles. All source identities remain in metadata even in shorter hints.
    Caller must retain existing source uncertainty and card acceptance gates.
    """
    if isinstance(max_queries, bool) or not isinstance(max_queries, int) or not 1 <= max_queries <= 3:
        raise ValueError("max_queries must be an integer between 1 and 3")
    name, mark, article, manufacturer, characteristics = map(
        _clean, (name, type_mark, article, manufacturer, characteristics)
    )
    # Preserve selling unit verbatim; never feed unit synonyms to discovery.
    model = mark
    source = "article" if article else "model" if mark else "description"
    if not article and _descriptive_mark(mark):
        model = ""
        characteristics = _clean(f"{mark} {characteristics}")
        source = "description"
    warnings = []
    if any(re.search(r",\s*(?=[A-Za-zА-Яа-я]+[-\w]*\d)", value)
           for value in (mark, article)):
        warnings.append("multiple_marks_not_equivalent")
    identity = article or model
    maker = _quoted(manufacturer)
    steps = []
    seen = set()

    def add(parts, kind, hint):
        query = _clean(" ".join(p for p in parts if p))
        key = query.casefold()
        if query and key not in seen and len(steps) < max_queries:
            seen.add(key)
            steps.append(QueryStep(query, kind, hint))

    from ..models import normalize_alternatives
    alternatives=normalize_alternatives([]if alternative_articles is None else alternative_articles,article=True)
    names=normalize_alternatives([]if alternative_names is None else alternative_names)
    unplanned=[]
    def hints():
        for value in alternatives:
            before=len(steps);add((_quoted(value),maker,"цена"),'exact_article',value)
            if len(steps)==before and _clean(' '.join(p for p in (_quoted(value),maker,"цена")if p)).casefold()not in seen:unplanned.append(dict(kind='alternative_article',value=value))
        for value in names:
            before=len(steps);add((value,maker,"цена"),'description','')
            if len(steps)==before and _clean(' '.join(p for p in (value,maker,"цена")if p)).casefold()not in seen:unplanned.append(dict(kind='alternative_name',value=value))
    if identity:
        add((_quoted(identity), maker, "цена"), "exact_article" if article else "exact_model", identity)
        hints()
        # Keep both independent source columns, never replace article with model.
        context = model if article and model.casefold() != article.casefold() else ""
        add((_quoted(identity), _quoted(context), maker, name, characteristics, "цена"), "identity_context", identity)
        if not warnings:
            for variant in _spelling(identity):
                add((_quoted(variant), _quoted(context), maker, characteristics, "цена"), "spelling", identity)
        # Without a spelling change, add only a distinct contextual query.
        add((_quoted(identity), _quoted(context), maker, name, characteristics, "купить"), "identity_context", identity)
    elif name or characteristics:
        base = _clean(f"{name} {characteristics}")
        add((base, maker, "цена"), "description", "")
        hints()
        for variant in _spelling(base):
            add((variant, maker, "цена"), "spelling_description", "")
        add((base, maker, "купить"), "description", "")
    return ArticleQueryPlan(name, article, model, manufacturer, unit, characteristics,
                            source, tuple(warnings), tuple(steps),tuple(unplanned))
