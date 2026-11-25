"""Additive saved-fact journal at existing delivered-page seams, before matching.

This adapter reads only owned capture files, not arbitrary paths or network.
Failures are explicit diagnostics; they never mint or replace catalogue heads.
"""

import base64
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Iterator

from .observed_card_facts import SavedPage, ObservedCardFacts, article_comparison, extract_observed_facts


@dataclass
class FactCapture:
    """One caller's diagnostic reports; raw journal records contain no requested position."""

    root: Path
    requested_article: str
    reports: list[dict] = field(default_factory=list)

    def observe(self, reference: object) -> None:
        """Validate a saved capture, retain independent facts, and report literal correspondence.

        Source/parser/journal failure remains source_invalid, never no-price or
        success. No exception message, private payload, or guessed fact is logged.
        Duplicate delivery of the same source produces one report per scope.
        """
        try:
            validate_cache_root(self.root)
            body, source = read_capture(self.root, reference)
            facts = extract_observed_facts(body, source)
            payload = facts.model_dump_json(indent=2).encode("utf-8")
            digest = hashlib.sha256(payload).hexdigest()
            folder = self.root / "observed_facts"
            if folder.is_symlink():
                raise ValueError("observed_fact_journal_path_invalid")
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (digest + ".json")
            if path.is_symlink():
                raise ValueError("observed_fact_journal_path_invalid")
            try:
                with path.open("xb") as stream:
                    stream.write(payload)
            except FileExistsError:
                if path.read_bytes() != payload:
                    raise ValueError("observed_fact_journal_changed") from None
            # Exact read-back binds the emitted hash/ref, not just a valid
            # but potentially replaced JSON shape, to this observation.
            retained = path.read_bytes()
            if path.is_symlink() or retained != payload or hashlib.sha256(retained).hexdigest() != digest:
                raise ValueError("observed_fact_journal_changed")
            ObservedCardFacts.model_validate_json(retained)
            report = {
                "facts_ref": str(path.relative_to(self.root)), "facts_sha256": digest,
                "source_id": source.source_id, "source_sha256": source.body_sha256,
                "observed_at": source.observed_at, "parse_state": facts.state,
                "article_status": article_comparison(facts, self.requested_article),
                "price_state": (
                    "observed" if facts.amount.state == "known"
                    else "conflicting" if facts.amount.state == "conflicting"
                    else "not_observed" if facts.state == "observed"
                    else "unavailable"
                ),
                "sale_unit_state": facts.sale_unit.state,
                "package_quantity_state": facts.package_quantity.state,
                "financial_eligibility": "not_evaluated",
            }
        except (OSError, ValueError, TypeError, KeyError, LookupError, RecursionError):
            report = {
                "parse_state": "source_invalid", "article_status": "unknown",
                "price_state": "unknown", "financial_eligibility": "not_evaluated",
            }
        if report not in self.reports:
            self.reports.append(report)


_ACTIVE: ContextVar[FactCapture | None] = ContextVar("observed_fact_capture", default=None)


def fact_capture_active() -> bool:
    """Tell the existing renderer whether this caller requested raw-fact observation; inactive callers perform no extra checks or writes."""
    return _ACTIVE.get() is not None


def validate_cache_root(root: Path) -> None:
    """Refuse relative, missing or symlinked cache roots/ancestors before reading or retaining public source bytes."""
    if not root.is_absolute() or not root.is_dir() or any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError("observed_fact_cache_root_invalid")
    if root.resolve() != root:
        raise ValueError("observed_fact_cache_root_invalid")


@contextmanager
def fact_capture_scope(root: Path, requested_article: str) -> Iterator[FactCapture]:
    """Bind per-collection diagnostics and restore context even after native cancellation/error."""
    capture = FactCapture(root, requested_article)
    token = _ACTIVE.set(capture)
    try:
        yield capture
    finally:
        _ACTIVE.reset(token)


def read_capture(root: Path, reference: object) -> tuple[bytes, SavedPage]:
    """Read one existing hashed HTTP/rendered record with bounded allowlisted ownership.

    This deliberately does not call cash/identity admission readers: a wrong
    article or unknown unit must not erase raw facts. Original dates, body bytes
    and representation are preserved, including an explicit non-200 response.
    """
    validate_cache_root(root)
    if type(reference) is not dict:
        raise ValueError("observed_fact_capture_ref_invalid")
    relative = reference.get("ref", "")
    if not isinstance(relative, str) or not re.fullmatch(r"(?:http_cards|rendered_cards)/[a-f0-9]{32}\.json", relative):
        raise ValueError("observed_fact_capture_ref_invalid")
    path = root / relative
    if path.is_symlink() or path.parent.is_symlink() or path.resolve().parent.parent != root.resolve():
        raise ValueError("observed_fact_capture_path_invalid")
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("observed_fact_capture_too_large")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != reference.get("record_sha256"):
        raise ValueError("observed_fact_capture_changed")
    record = json.loads(raw)
    if type(record) is not dict or type(record.get("version")) is not int or record["version"] != 1:
        raise ValueError("observed_fact_capture_invalid")
    body = base64.b64decode(record["body_base64"], validate=True)
    digest = hashlib.sha256(body).hexdigest()
    if digest != record.get("body_sha256") or digest != reference.get("body_sha256"):
        raise ValueError("observed_fact_body_changed")
    rendered = relative.startswith("rendered_cards/")
    if rendered and (record.get("role") != "rendered_primary_dom" or record.get("representation") != "visibility_annotated_dom"):
        raise ValueError("observed_fact_representation_invalid")
    source = SavedPage(
        source_id=relative, url=record["url"], observed_at=record["observed_at"],
        body_sha256=digest, encoding=record.get("encoding") or "utf-8",
        representation="rendered_dom" if rendered else "http_html",
        # The retained rendered frame does not certify an HTTP response code.
        status=None if rendered else record["status"],
    )
    return body, source


def observe_http(reference: object) -> None:
    """Observe an already-retained HTTP response before request-specific admission; no active scope means no work."""
    active = _ACTIVE.get()
    if active is not None:
        active.observe(reference)


def observe_rendered(record: object) -> None:
    """Observe the existing owner-verified neutral DOM packet, preserving its saved representation and timestamp."""
    active = _ACTIVE.get()
    if active is None:
        return
    if type(record) is not dict:
        active.reports.append({
            "parse_state": "source_invalid", "article_status": "unknown",
            "price_state": "unknown", "financial_eligibility": "not_evaluated",
        })
        return
    coherent = record.get("coherent")
    source = coherent.get("rendered_source") if isinstance(coherent, dict) else None
    if isinstance(source, dict):
        active.observe(source)
    else:
        report = {
            "parse_state": "source_unavailable", "article_status": "unknown",
            "price_state": "unknown", "financial_eligibility": "not_evaluated",
        }
        if report not in active.reports:
            active.reports.append(report)
