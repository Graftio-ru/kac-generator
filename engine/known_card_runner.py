"""Owned, bounded card acquisition; the HTTP layer supplies native job adapters.

This module never searches for URLs, changes supplier methods, retries an unknown
card operation, or grants full-research/report readiness. Transport and financial
qualification remain in the existing capture and catalogue components.
"""

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from functools import partial
from operator import itemgetter
from pathlib import Path
import time
import uuid
from typing import Literal, NotRequired, Protocol, TypedDict

from .equipment_output_policy import is_graftio_equipment
from .llm import LLMRequestGuard, job_request_limits
from .models import SpecPosition
from .offer_selection import offer_is_comparable, seller_review_reason
from .runner_protocol import KnownCardTarget, known_cards_payload
from .schemas import PositionIn
from .search.acquisition_lease import AcquisitionLeaseKeeper
from .search.agent import capture_card_diagnostics
from .search.card_attachments import collect_current
from .search.catalogue import Acquisition, CatalogueStore, CatalogueStoreError, identity_for_position
from .search.catalogue_producer import CataloguePublicationError, capture_and_publish
from .search.catalogue_projection import catalogue_scope, project_product
from .search.catalogue_storage import create_image_store
from .search.failure_policy import ResearchControlError
from .search.session import (
    _CANCELLED,
    PersistentSupplierHealth,
    SearchBudgetExceeded,
    bounded_timeout,
    research_deadline,
    research_session,
)
from .supplier_methods import MethodUnavailable, selected


class KnownCardResult(TypedDict):
    """Public per-target outcome; UNKNOWN never represents a qualified quote."""

    url: str
    status: Literal["unknown", "observed", "refused"]
    reason_code: str
    observation_id: str | None
    quote_qualified: bool
    error_type: NotRequired[str]
    publication_error_code: NotRequired[str]
    source_sha256: NotRequired[str | None]
    observed_at: NotRequired[float | None]
    qualification_reason: NotRequired[str]
    fact_observations: NotRequired[list[dict]]
    article_correspondence: NotRequired[str]
    price_eligibility: NotRequired[str]


class KnownRowBuilder(Protocol):
    """Existing catalogue projection adapter; native records remain dynamic."""

    def __call__(
        self, position_input: PositionIn, *, store: CatalogueStore,
        product: dict | None, hosts: Sequence[str],
    ) -> dict | None:
        """Project a known native product or return None when no product exists; may raise native storage errors."""
        ...


class UncertainCompletion(Protocol):
    """Native closed diagnostic classifier, supplied without arbitrary error text."""

    def __call__(self, diagnostics: list, error: Exception | None = None) -> dict:
        """Return existing completion evidence/code for diagnostics or a caught class without mutating storage."""
        ...


@dataclass(frozen=True)
class KnownCardDependencies:
    """Native adapters injected by app.py, with no control-local catalogue.

    The record callbacks intentionally use the existing dynamic catalogue/job
    record interface. They do not assert a new schema for legacy persisted data.
    """

    store_factory: Callable[[], CatalogueStore]
    cache_dir: Path
    browser_factory: Callable[[], AbstractContextManager[object]]
    save_job: Callable[[str], bool]
    build_row: KnownRowBuilder
    publish_row: Callable[[str, int, dict], None]
    uncertain_completion: UncertainCompletion


class _UnknownSupplier(ValueError):
    """The fixed supplier-method registry has no entry for the submitted URL."""


class _NewCaptureBrowserRequired(ValueError):
    """Explicit retained-input authority currently supports the selected browser route only."""


class KnownCardRun:
    """One dispatch's acquisition lifecycle, with explicit job and native adapters."""

    def __init__(
        self,
        job_id: str,
        positions: Sequence[PositionIn],
        job: dict,
        targets: list[KnownCardTarget],
        *,
        extract: bool,
        dependencies: KnownCardDependencies,
    ) -> None:
        """Validate the frontier and capture parent authority before opening storage.

        Missing runner cancellation/deadline context is refused. The captured
        callback is also used by the lease thread, which has no parent ContextVars.
        Native job/storage records retain their existing runtime validation.
        Only an executor-issued private context enables a distinct capture; its
        expiry also shortens the existing request deadline and never extends it.
        """
        known_cards_payload({
            "positions": [position.model_dump(mode="json") for position in positions],
            "targets": targets,
            "extract": extract,
        })
        deadline = research_deadline()
        parent_cancelled = _CANCELLED.get()
        if deadline is None or parent_cancelled is None:
            raise ValueError("known_cards_requires_runner_control")
        self.deadline = deadline
        self.parent_cancelled = parent_cancelled
        self.job_id = job_id
        self.positions = positions
        self.job = job
        self.targets = targets
        self.extract = extract
        self.dependencies = dependencies
        self.store = dependencies.store_factory()
        from .search.known_capture_intent import active_intent
        self.new_capture_intent = active_intent()
        if self.new_capture_intent is not None:
            remaining = self.new_capture_intent.value["expires_at"] - time.time()
            self.deadline = min(self.deadline, time.monotonic() + max(0, remaining))
        self.images = create_image_store(dependencies.cache_dir)
        self.guard: LLMRequestGuard | None = None
        self.keeper: AcquisitionLeaseKeeper | None = None

    def cancelled(self) -> bool:
        """Read the native job cancellation flag for lease revocation semantics."""
        return bool(self.job.get("cancel_requested"))

    def check_control(self) -> None:
        """Refuse more work after parent ownership, cancellation, or deadline loss.

        Called both on the job thread and lease-renewal thread. Raises the native
        control exception; it never starts transport or repairs lost ownership.
        """
        if self.parent_cancelled():
            raise SearchBudgetExceeded("Внешнее управление остановило обработку карточек")
        if time.monotonic() >= self.deadline:
            raise SearchBudgetExceeded("Общий срок обработки завершён")
        if self.guard is not None:
            self.guard.check()
        bounded_timeout(0)
        if self.cancelled():
            raise SearchBudgetExceeded("Задание отменено; карточка не начата")

    def error_kind(self, error: Exception) -> str:
        """Return only the native closed diagnostic class, never exception text."""
        completion = self.dependencies.uncertain_completion([], error)
        return completion["evidence"]["error_type"]

    def run(self) -> None:
        """Persist each target outcome and always stop/revoke the owned lease keeper.

        Native failures become explicit error/cancelled job states; saved card
        intents remain durable. DONE covers only this frontier, not business
        readiness. Keeper-stop or persistence failure propagates to the executor.
        """
        self.job.update(
            status="running", known_cards=True,
            business_complete=False, report_unavailable=True,
        )
        self.dependencies.save_job(self.job_id)
        try:
            # These limits bound mechanics; they do not authorize paid model use.
            with job_request_limits({
                "cloud": 3 if self.extract else 0,
                "search_query": 0,
                "search_account": 0,
            }):
                self.guard = LLMRequestGuard()
                health = PersistentSupplierHealth(hosts=self.job["configured_hosts"])
                with research_session(health, llm_guard=self.guard, deadline=self.deadline):
                    self.keeper = AcquisitionLeaseKeeper(
                        self.store, self.check_control,
                        owner=self.job_id, cancelled=self.cancelled,
                    )
                    self.keeper.start()
                    for target in sorted(self.targets, key=itemgetter("index")):
                        self.process_target(target, self.keeper)
                    self.job.update(
                        status="done", completed=len(self.positions),
                        known_cards_scope_complete=True,
                    )
        except ResearchControlError as error:
            self.job.update(
                status="cancelled" if self.cancelled() else "error",
                error_code="known_card_control_stopped", error_type=self.error_kind(error),
                error="Обработка заданных карточек остановлена; сохранённые результаты не повторяются",
            )
        except Exception as error:
            self.job.update(
                status="error", error_code="known_card_operation_failed",
                error_type=self.error_kind(error),
                error="Обработка заданных карточек не завершена; сохранённые результаты не повторяются",
            )
        finally:
            if self.keeper is not None:
                self.keeper.stop(revoke=True, reason="Обработка заданных карточек завершена")
            self.dependencies.save_job(self.job_id)

    def allocate(self, position: PositionIn, keeper: AcquisitionLeaseKeeper) -> Acquisition:
        """Acquire the same missing-data rights as native research, or refuse.

        The snapshot revision guards an existing product; no coverage receipt is
        minted and no other supplier URL is captured by this allocation.
        """
        identity = identity_for_position(position)
        view = self.store.read_snapshot([identity])
        product = view["products"][identity.key]
        if product is None:
            admission = self.store.acquire_unknown(
                identity, self.job["configured_hosts"], owner=self.job_id,
            )
        else:
            admission = self.store.acquire_missing(
                identity, self.job["configured_hosts"], owner=self.job_id,
                expected_revision=view["revision"], mode="missing_data",
            )
        if not admission.allowed:
            raise CatalogueStoreError("known_card_allocation_refused")
        keeper.add(admission, identity=identity)
        return admission

    def process_target(self, target: KnownCardTarget, keeper: AcquisitionLeaseKeeper) -> None:
        """Capture at most once, settle allocation, then publish the native row.

        Without a private explicit-action grant, old unknown intents deny capture.
        A valid grant spends a separate operation while preserving every parent.
        Control loss propagates without beginning the next target; ordinary capture failures retain an
        UNKNOWN result and never expose raw errors or resolve unknown sends.
        Publication refusals retain the producer's closed diagnostic and code;
        this detail never admits an observation, qualifies a price or retries.
        """
        self.check_control()
        index = target["index"]
        position_input = self.positions[index]
        url = target["url"]
        if position_input.is_unique or is_graftio_equipment(position_input):
            raise ValueError("known_card_position_not_collectable")
        # Conversion must fail before acquiring rights or writing a card intent.
        position = position_input.model_copy(update={"offers": []}).to_result().position
        admission = self.allocate(position_input, keeper)
        result: KnownCardResult = {
            "url": url, "status": "unknown", "reason_code": "card_no_observation",
            "observation_id": None, "quote_qualified": False,
        }
        diagnostics = []
        operation = None
        allocation_error = ""
        try:
            grant = selected(url)
            if grant is None:
                raise _UnknownSupplier()
            host = grant["host"]
            if self.new_capture_intent is not None and grant["method"] != "browser":
                raise _NewCaptureBrowserRequired()
            keeper.begin(admission)
            operation = (
                self.new_capture_intent.operation_id(url)
                if self.new_capture_intent is not None else uuid.uuid4().hex
            )
            target_scope = (
                self.new_capture_intent.bind_target(
                    self.store, admission.product_id, host, url, admission,
                ) if self.new_capture_intent is not None else nullcontext()
            )
            with target_scope:
                intent = self.store.card_operation(
                    admission.product_id, host, url,
                    operation_id=operation, acquisition=admission,
                    evidence={"job_id": self.job_id, "generation": admission.generation},
                    new_intent=self.new_capture_intent,
                )
                if not intent["allowed"]:
                    result["reason_code"] = "card_pending_unknown"
                    allocation_error = "card_pending_unknown"
                else:
                    if self.new_capture_intent is not None:
                        self.new_capture_intent.check()
                    browser_scope = (
                        self.dependencies.browser_factory()
                        if grant["method"] == "browser" else nullcontext(None)
                    )
                    with browser_scope as browser, capture_card_diagnostics() as diagnostics:
                        keeper.check(admission)
                        if self.new_capture_intent is not None:
                            self.new_capture_intent.check()
                        observation = capture_and_publish(
                            self.store, identity_for_position(position_input),
                            position,
                            url, browser=browser, image_store=self.images,
                            capture=partial(collect_current, extract=self.extract),
                            acquisition=admission, supplier_host=host, observation_id=operation,
                        )
                        keeper.check(admission)
                    if observation is not None:
                        self.store.card_operation(
                            admission.product_id, host, url, operation_id=operation,
                            acquisition=admission, resolve=True, evidence={"basis": "observation"},
                        )
                        result.update(
                            status="observed", reason_code=observation.state,
                            observation_id=observation.observation_id,
                        )
                    else:
                        completion = self.dependencies.uncertain_completion(diagnostics)
                        result["reason_code"] = completion["evidence"]["code"]
                        allocation_error = "card_outcome_unknown"
        except _NewCaptureBrowserRequired:
            result.update(status="refused", reason_code="new_capture_requires_browser")
            allocation_error = result["reason_code"]
        except _UnknownSupplier:
            result.update(status="refused", reason_code="supplier_not_configured")
            allocation_error = result["reason_code"]
        except MethodUnavailable:
            result.update(status="refused", reason_code="supplier_method_requires_configuration")
            allocation_error = result["reason_code"]
        except ResearchControlError as error:
            self.job["known_card_stop"] = {
                "index": index, "url": url, "operation_id": operation,
                "diagnostics": list(diagnostics), "error_type": self.error_kind(error),
            }
            raise
        except CataloguePublicationError as error:
            diagnostics.append(error.diagnostic())
            result.update(
                reason_code="card_operation_unknown", error_type=self.error_kind(error),
                publication_error_code=error.code,
            )
            allocation_error = "card_operation_unknown"
        except Exception as error:
            result.update(reason_code="card_operation_unknown", error_type=self.error_kind(error))
            allocation_error = "card_operation_unknown"
        finally:
            if not keeper.finish(admission, error=allocation_error):
                raise CatalogueStoreError("known_card_allocation_not_settled")
        self.check_control()
        self.publish_target(index, position_input, position, result, diagnostics, operation)

    def publish_target(
        self, index: int, position_input: PositionIn, position: SpecPosition,
        result: KnownCardResult,
        diagnostics: list, operation: str | None,
    ) -> None:
        """Project and save only the exact new observation's native eligibility.

        An older offer at the same URL cannot make this target qualified. The
        existing unit/seller/assembly predicates remain the only quote authority.
        Independent fact refs and literal article correspondence are published
        alongside that decision; they never supply its normalized price/proof.
        """
        identity = identity_for_position(position_input)
        product = self.store.lookup(identity)
        row = self.dependencies.build_row(
            position_input, store=self.store, product=product,
            hosts=self.job["configured_hosts"],
        )
        if row is None:
            # Preserve the previous missing-row failure class without relying
            # on an accidental None.update() call or inventing an empty row.
            raise AttributeError("known_card_projection_unavailable")
        row.update(
            known_card_result=result, diagnostics=list(diagnostics),
            new_cards_checked=1 if operation else 0,
        )
        with catalogue_scope(self.store, snapshot=product):
            projected = project_product(self.store, identity, position, snapshot=product)
            observation_id = result["observation_id"]
            current_offer = None
            if observation_id:
                for offer in projected.all_offers:
                    if offer.url == result["url"] and (offer.catalogue_ref or {}).get("observation_id") == observation_id:
                        current_offer = offer
                        break
            result["quote_qualified"] = (
                current_offer is not None and offer_is_comparable(current_offer, position)
            )
            if current_offer is not None:
                result.update(
                    source_sha256=current_offer.catalogue_source_sha256,
                    observed_at=current_offer.catalogue_observed_at,
                    qualification_reason=(
                        current_offer.unit_review_reason
                        or seller_review_reason(current_offer, projected.all_offers)
                        or ("" if result["quote_qualified"] else "native_eligibility_refused")
                    ),
                )
        self.job["rows"][index] = row
        # Raw observations and literal correspondence are separate from the
        # native eligibility decision. A retained amount cannot promote it.
        observations = [
            diagnostic["observed_facts"] for diagnostic in diagnostics
            if diagnostic.get("code") == "independent_observed_facts"
            and isinstance(diagnostic.get("observed_facts"), dict)
        ]
        article_statuses = {observation.get("article_status") for observation in observations}
        result["fact_observations"] = observations
        result["article_correspondence"] = (
            "article_mismatch" if "article_mismatch" in article_statuses
            else "article_equal" if article_statuses == {"article_equal"}
            else "unknown"
        )
        result["price_eligibility"] = "qualified" if result["quote_qualified"] else "not_qualified"
        self.dependencies.publish_row(self.job_id, index, row)
