"""Оркестрация полного пайплайна конъюнктурного анализа.

Этапы:
  1. Парсинг спецификаций → позиции; парсинг писем → офферы уникального оборудования.
  2. Поиск цен по каждой неуникальной позиции (≥MIN_SOURCES → TOP максимальных).
  3. Сборка Тома ТКП (проставляет № страниц = графа Q).
  4. Сборка КАЦ.
  5. ТСН-2001: расценки + коэффициент → файл ТСН + фильтр (ТСН>КАЦ → исключить).
  6. Пересборка КАЦ с учётом фильтра.

Состояние задачи (progress/лог) хранится в JobState для отдачи в UI.
"""
from __future__ import annotations

import threading
import tempfile
import os
import traceback
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .builders.kac_xlsx import build_kac
from .builders.tkp_pdf import build_tkp
from .builders.tsn_xlsx import build_tsn
from .config import OUTPUT_DIR
from .models import PositionResult


@dataclass
class JobState:
    job_id: str
    object_name: str
    status: str = "pending"          # pending|running|done|error
    step: str = ""
    progress: float = 0.0            # 0..1
    log: list[str] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)  # kind -> path
    error: str = ""
    coverage_warnings: list[dict] = field(default_factory=list)

    def say(self, msg: str, progress: float | None = None) -> None:
        self.log.append(msg)
        if progress is not None:
            self.progress = progress


@dataclass
class JobInputs:
    object_name: str
    spec_results: list[PositionResult]     # уже распарсенные позиции (+ офферы писем)
    use_tsn: bool = True
    fix_date: str = ""
    skip_search: bool = False              # не искать в интернете (цены заданы вручную)
    tsn_work_kind: str = "электромонтаж"   # вид работ для выбора индекса пересчёта (см. local_db)
    # ручные данные ТСН по № позиции: {number: (base_price, coefficient)}
    manual_tsn: dict[int, tuple[float | None, float | None]] = field(default_factory=dict)

    catalogue_store: object = None
    catalogue_snapshot: dict | None = None
    catalogue_known_indices: set[int] = field(default_factory=set)


def run_pipeline(job: JobState, inputs: JobInputs, browser_factory) -> None:
    from .search.catalogue_projection import catalogue_scope
    from .search.document_context import active_document_operation
    operation=active_document_operation()
    if inputs.catalogue_store is not None:
        if operation is not None:
            with operation.publication(job,inputs),catalogue_scope(inputs.catalogue_store,snapshot=inputs.catalogue_snapshot):
                return _run_pipeline_scoped(job,inputs,browser_factory)
        with catalogue_scope(inputs.catalogue_store,snapshot=inputs.catalogue_snapshot):
            return _run_pipeline_scoped(job,inputs,browser_factory)
    return _run_pipeline_scoped(job,inputs,browser_factory)


def _run_pipeline_scoped(job: JobState, inputs: JobInputs, browser_factory) -> None:
    """Синхронный прогон. browser_factory() → контекстный менеджер Browser (или None для демо)."""
    from .search.agent import research_position
    from .search import base_db
    from .tsn.local_db import build_tsn_rows

    def _apply(res, found):
        res.offers = found.offers
        res.all_offers = found.all_offers
        res.year, res.quarter = year, quarter
        res.sources_checked = found.sources_checked
        res.sources_found = found.sources_found
        res.sources_target = found.sources_target

    from .search.document_context import active_document_operation
    operation=active_document_operation()
    output_root=operation.output_dir if operation is not None else OUTPUT_DIR
    staged_outputs={};published=[];staging=None
    def check_output_control():
        from .search.session import bounded_timeout
        bounded_timeout(0)
    def stage_path(name):
        nonlocal staging
        check_output_control()
        if staging is None:staging=tempfile.TemporaryDirectory(prefix='.final-',dir=output_root)
        return Path(staging.name)/name
    try:
        job.status = "running"
        results = inputs.spec_results
        year = str(date.today().year)
        quarter = str((date.today().month - 1) // 3 + 1)
        # Предложения уже могут быть заполнены из базы или вручную; поиск тогда
        # пропускается, но КАЦ всё равно требует год и квартал фиксации цены.
        for res in results:
            res.year = res.year or year
            res.quarter = res.quarter or quarter

        # 2. Поиск цен (неуникальные позиции без офферов)
        from .equipment_output_policy import is_graftio_equipment, missing_own_price, MISSING_PRICE, financial_summary
        if inputs.catalogue_store is not None:
            unknown=[r.position.number for index,r in enumerate(results) if index not in inputs.catalogue_known_indices and not r.position.is_unique and not is_graftio_equipment(r.position) and not any(o.price_with_vat>0 for o in (r.all_offers or r.offers))]
            if unknown:raise ValueError('catalogue_acquisition_required: сначала запустите обычное заполнение каталога для позиций '+', '.join(map(str,unknown)))
        to_search = [] if inputs.skip_search else [
            r for index,r in enumerate(results) if index not in inputs.catalogue_known_indices and not r.position.is_unique and not is_graftio_equipment(r.position) and not r.offers]
        if inputs.skip_search:
            job.say("Поиск пропущен (цены заданы вручную).", 0.6)
        if to_search:
            job.say(f"Поиск цен по {len(to_search)} позициям…", 0.1)
            # cache-first: сперва берём готовое из базы (без браузера), вживую ищем только промахи
            misses = []
            for res in to_search:
                hit = base_db.lookup(res.position, year, quarter)
                if hit and base_db.has_enough_sources(hit):
                    _apply(res, hit)
                    job.say(f"  из базы: {res.position.name[:40]} ({len(hit.offers)} цен)", 0.1)
                else:
                    if hit:
                        job.say(f"  база неполная: {res.position.name[:40]} "
                                f"({hit.sources_found}/{hit.sources_target} источников)")
                    misses.append(res)
            job.say(f"Из базы: {len(to_search) - len(misses)}, искать вживую: {len(misses)}", 0.15)
            if misses:
                with browser_factory() as browser:
                    for i, res in enumerate(misses, 1):
                        job.step = f"Поиск: {res.position.name[:40]}"
                        from .search.candidate_queue import candidate_scope
                        with candidate_scope(res.position):
                            found = research_position(browser, res.position, year, quarter)
                        _apply(res, found)
                        base_db.upsert(res.position, found)  # пополняем базу
                        flag = "" if found.sources_found >= found.sources_target else "  ⚠ мало источников"
                        job.say(f"  [{i}/{len(misses)}] найдено {found.sources_found}/"
                                f"{found.sources_target} ист., в КАЦ {len(res.offers)} цен{flag}",
                                0.15 + 0.45 * i / len(misses))

        # One shared finite attachment pass also covers cached/manual skip_search.
        from .search.card_attachments import attach_result
        from .search.session import research_session, PersistentSupplierHealth,research_deadline
        from .llm import LLMRequestGuard,_REQUEST_GUARD
        attachment_results=[result for index,result in enumerate(results) if index not in inputs.catalogue_known_indices and not is_graftio_equipment(result.position)]
        if attachment_results:
            with browser_factory() as browser, research_session(PersistentSupplierHealth(),deadline=research_deadline(),llm_guard=_REQUEST_GUARD.get() or LLMRequestGuard()):
                for result in attachment_results:
                    attach_result(result,browser)

        from .final_eligibility import prepare_final_results
        from .search.selected_proofs import final_result
        results=[final_result(result) for result in results]
        prepare_final_results(results)
        if inputs.catalogue_store is not None:
            from .app import _catalogue_final_preflight,_catalogue_export_snapshot
            job.coverage_warnings=_catalogue_final_preflight([r.position for r in results],inputs.catalogue_snapshot,inputs.catalogue_known_indices,inputs.catalogue_store)
            for warning in job.coverage_warnings:job.say(f"№{warning['number']}: {warning['reason']}")
            # The same server-projected research report accompanies final documents.
            # Actual supplier outcomes remain review/error/incomplete, never absence.
            from .schemas import ResearchReportRequest,ResearchReportPositionIn
            from .builders.research_report_xlsx import build_research_report
            from .builders.research_package import build_research_package
            report=ResearchReportRequest(object_name=inputs.object_name,positions=[ResearchReportPositionIn.model_validate(r.position.model_dump()) for r in results])
            report,_,report_snapshot,_=_catalogue_export_snapshot(report)
            if report_snapshot!=inputs.catalogue_snapshot:raise ValueError('Состояние каталога изменилось перед формированием документов')
            for kind,suffix,builder in [('research_xlsx','.xlsx',build_research_report),('research_package','.zip',build_research_package)]:
                check_output_control();content=builder(report);check_output_control()
                path=stage_path(f'{job.job_id}_{kind}{suffix}');path.write_bytes(content);check_output_control();staged_outputs[kind]=str(path)


        # 3. Том ТКП
        job.say("Сборка Тома ТКП…", 0.65)
        tkp_path, _ = build_tkp(results, inputs.object_name, stage_path(f"{job.job_id}_TKP.pdf"),
                                fix_date=inputs.fix_date or None)
        check_output_control()
        staged_outputs["tkp"] = str(tkp_path)

        # 4. КАЦ
        job.say("Сборка КАЦ…", 0.75)
        by_disc: dict[str, list[PositionResult]] = {}
        for r in results:
            by_disc.setdefault(r.position.discipline, []).append(r)
        kac_path = build_kac(by_disc, inputs.object_name, stage_path(f"{job.job_id}_KAC.xlsx"))
        check_output_control()
        staged_outputs["kac"] = str(kac_path)

        # 5. ТСН-2001 + фильтр
        if inputs.use_tsn:
            check_output_control()
            manual = inputs.manual_tsn or {}
            have_manual = any(manual.get(r.position.number, (None, None))[0] is not None
                              for r in results)
            if have_manual or inputs.skip_search:
                # ручные базовые расценки/коэффициенты (без обращения к mos.ru)
                from .models import TsnRow
                tsn_rows = []
                for r in results:
                    base, coeff = manual.get(r.position.number, (None, None))
                    tsn_rows.append(TsnRow(
                        number=r.position.number, name=r.position.name,
                        unit=r.position.unit, qty=r.position.qty,
                        tsn_base_price=base, coefficient=coeff,
                        kac_max_price=r.max_offer.price_with_vat if r.max_offer else None))
                job.say("Сравнение с ТСН-2001 (ручные расценки)…", 0.85)
            else:
                job.say("Сравнение с ТСН-2001 (локальный справочник)…", 0.85)
                tsn_rows = build_tsn_rows(results, inputs.tsn_work_kind)
            missing_rows = {r.position.number: r for r in results if missing_own_price(r.position, r.offers)}
            for row in tsn_rows:
                if row.number in missing_rows:
                    row.name = f"{row.name}; {missing_rows[row.number].position.type_mark}"
                    row.kac_max_price = None
                    row.decision = MISSING_PRICE
            tsn_path, excluded = build_tsn(tsn_rows, inputs.object_name,
                                           stage_path(f"{job.job_id}_TSN.xlsx"))
            check_output_control()
            staged_outputs["tsn"] = str(tsn_path)
            if excluded:
                job.say(f"Исключены из КАЦ (ТСН>КАЦ): {sorted(excluded)}", 0.92)
                for r in results:
                    if r.position.number in excluded:
                        r.excluded_by_tsn = True
                check_output_control()
                build_kac(by_disc, inputs.object_name, kac_path)  # пересборка
                check_output_control()

        # Only the complete, control-safe set becomes downloadable. Files remain
        # private during builders. Hard links publish exclusively without replacing
        # a historical/foreign output; failure removes only our own links.
        check_output_control()
        if inputs.catalogue_store is not None:
            from .app import _catalogue_final_preflight
            _catalogue_final_preflight([r.position for r in results],inputs.catalogue_snapshot,inputs.catalogue_known_indices,inputs.catalogue_store)
        final_outputs={}
        for kind,value in staged_outputs.items():
            check_output_control();source=Path(value);destination=output_root/source.name
            os.link(source,destination);published.append((source,destination))
            check_output_control();final_outputs[kind]=str(destination)
        check_output_control()
        job.step = ""
        summary = financial_summary(results)
        job.say((f"Документы готовы. Известная часть стоимости: {summary['known_subtotal']:.2f} ₽; полная стоимость не определена: {MISSING_PRICE}." if summary['missing_positions'] else "Готово."), 1.0)
        check_output_control()
        job.outputs=final_outputs
        job.status = "done"  # Publication commit; later cancellation is not retroactive.
    except Exception as e:  # pragma: no cover
        job.outputs.clear()
        for source,destination in published:
            try:
                if destination.exists() and os.path.samefile(source,destination):destination.unlink()
            except OSError:pass
        job.status = "error"
        job.error = f"{e}\n{traceback.format_exc()}"
        job.say(f"Ошибка: {e}")
    finally:
        if staging is not None:staging.cleanup()


def run_pipeline_async(job: JobState, inputs: JobInputs, browser_factory) -> threading.Thread:
    from contextvars import copy_context
    context=copy_context()
    t = threading.Thread(target=context.run, args=(run_pipeline,job,inputs,browser_factory), daemon=True)
    from .search.document_context import active_document_operation
    operation=active_document_operation()
    if operation is not None:operation.bind_worker(job,t)
    t.start()
    return t
