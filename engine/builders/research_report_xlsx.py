"""Readable, independently downloadable summary of supplier research."""
from __future__ import annotations

from io import BytesIO
import math
from datetime import datetime, timezone
import re
from urllib.parse import urlparse

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ..schemas import ResearchReportRequest
from ..region_policy import region_notice, region_admission_label
from ..coverage import annotate_coverage
from ..price_capture import capture_review_reason, unit_capture_review_reason
from ..offer_selection import unit_review_pending, select_top_offers, offer_is_comparable, seller_review_reason

_STATUS = {
    "complete": "Цены найдены",
    "partial": "Частичный результат",
    "failed": "Нет допустимых предложений",
    "skipped": "Поиск пропущен",
    "pending": "Ожидает поиска",
    "stale": "Данные изменены",
    "error": "Ошибка поиска",
}
_HEAD_FILL = PatternFill("solid", fgColor="17365D")
_HEAD_FONT = Font(color="FFFFFF", bold=True)
_SELECTED_FILL = PatternFill("solid", fgColor="C6EFCE")
_WARN_FILL = PatternFill("solid", fgColor="FFF2CC")
_ILLEGAL_EXCEL_CHARS = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]")


def _context_notice(offer):
    from ..search.selected_proofs import NOT_SELECTED
    value=offer.context_capture_review_reason if offer else ""
    if value==NOT_SELECTED:
        value="Снимок не выбран на этапе сбора цен; фото получают только выбранные источники"
    # Report explicit capture metadata; never infer image or quotation validity.
    if offer and offer.context_capture_evidence.get('truncated') is True:
        notice="Контекст ограничен размером снимка; нижняя/правая часть страницы не приложена"
        if notice not in value:
            value='; '.join(filter(None,[value,notice]))
    return value


def _safe_text(value: object) -> str:
    # Excel caps strings at 32,767 chars; never let supplier text become a formula.
    return _ILLEGAL_EXCEL_CHARS.sub("", str(value or ""))[:32767]


def _put(sheet, row: int, column: int, value: object, *, url: bool = False) -> None:
    cell = sheet.cell(row, column)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        cell.value = value
    else:
        cell.value = _safe_text(value)
        cell.data_type = "s"
    if url and isinstance(value, str):
        try:
            parsed = urlparse(value)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                cell.hyperlink = _safe_text(value)
                cell.font = Font(color="0563C1", underline="single")
        except ValueError:
            pass


def _sheet(wb: Workbook, title: str, headers: list[str], widths: list[int]):
    sheet = wb.create_sheet(title)
    sheet.append(headers)
    for cell in sheet[1]:
        cell.fill = _HEAD_FILL
        cell.font = _HEAD_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    sheet.row_dimensions[1].height = 34
    for col, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(col)].width = width
    sheet.freeze_panes = "C2"
    return sheet


def _write_coverage_warning(sheet, row: int, number: int, text: str) -> int:
    """Keep complete literal warning text visible within Excel's row-height cap.

    Conservative column capacity leaves space for word wrapping. Continuations
    preserve every original character; concatenating their cells recovers text.
    """
    # The Excel limit applies to each continuation cell, not the full warning.
    text = _ILLEGAL_EXCEL_CHARS.sub("", str(text or ""))
    start = column = lines = 0
    for index, char in enumerate(text):
        if char == "\n":
            lines += 1
            column = 0
        else:
            column += 1
            if column >= 85:
                lines += 1
                column = 0
        if lines >= 21:
            _put(sheet, row, 1, number)
            _put(sheet, row, 2, text[start:index + 1])
            sheet.cell(row, 2).alignment = Alignment(wrap_text=True, vertical="top")
            sheet.row_dimensions[row].height = 396
            row += 1
            start, column, lines = index + 1, 0, 0
    if start < len(text) or not text:
        _put(sheet, row, 1, number)
        _put(sheet, row, 2, text[start:])
        sheet.cell(row, 2).alignment = Alignment(wrap_text=True, vertical="top")
        sheet.row_dimensions[row].height = max(34, (lines + 1) * 18 + 12)
        row += 1
    return row


def _evidence(position):
    evidence = list(position.candidates)
    for offer in position.offers:
        index = next((i for i, c in enumerate(evidence) if offer.url and c.url == offer.url), None)
        if index is None:
            evidence.append(offer)
        else:
            evidence[index] = offer
    return [o for o in evidence if math.isfinite(o.price_with_vat) and o.price_with_vat > 0]


def _host(url):
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _offer_key(offer):
    return (offer.url, offer.inn, offer.price_with_vat)


def _checked_date(value):
    if value is None or value == "":
        return "Дата не сохранена"
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, TypeError, OverflowError, OSError):
        return "Дата не сохранена"


def _quote_details(offer, position):
    """Export local denomination separately from immutable supplier evidence."""
    if offer.catalogue_ref:
        from ..search.catalogue_projection import catalogue_raw_facts
        raw_facts = catalogue_raw_facts(offer, position)
        from ..source_quote import quote_review_reason
        if quote_review_reason(offer, position):
            return dict(quantity=None, unit_text=offer.price_unit_evidence,
                        calculation='Сведения каталога требуют проверки',
                        observed_at=raw_facts['observed_at'] if raw_facts else None)
        raw, projected = offer.listed_price_with_vat, offer.price_with_vat
        return dict(quantity=raw/projected, unit_text=offer.price_unit_evidence,
                    calculation=f'{raw} / {raw/projected} = {projected} ₽ / {offer.normalized_price_unit}',
                    observed_at=offer.catalogue_observed_at)
    quote = offer.source_quote_evidence or offer.price_capture_evidence
    return dict(quantity=quote.get('quantity'), unit_text=quote.get('unit_text', ''),
                calculation=(f"{quote.get('listed_price')} / {quote.get('quantity')} = {quote.get('normalized_price')} ₽ / {quote.get('requested_unit')}"
                             if quote else 'Основание пересчёта не сохранено'),
                observed_at=quote.get('observed_at'))


def _display_price(offer, position):
    """Review rows show owned supplier cash/unit, not an unproved conversion."""
    if offer.catalogue_ref:
        from ..search.catalogue_projection import catalogue_raw_facts
        facts = catalogue_raw_facts(offer, position)
        if not facts:
            return None, 'Не подтверждена'
        if not offer_is_comparable(offer, position):
            return facts['raw_amount'], (facts['sale_unit'] if facts['sale_unit'] not in {'', 'unknown'}
                                         else 'Не подтверждена')
    return offer.price_with_vat, ('Не подтверждена' if unit_review_pending(offer)
                                 else offer.normalized_price_unit or position.unit)


def _latest_catalogue_attempt(position, host, offer):
    attempts = [a for a in position.catalogue_refresh_attempts
                if a.get('host') == host and
                type(a.get('attempted_at')) in (int, float) and
                math.isfinite(a['attempted_at']) and a['attempted_at'] > 0 and
                (offer is None or offer.url in {a.get('url'), a.get('source_url')})]
    return max(attempts, key=lambda a: a.get('attempted_at') or 0, default={})


def _seller_origin_notice(offer):
    if offer is None:return ""
    if offer.from_letter:return "Цена из письма"
    from ..requisites.supplier import seller_origin_notice
    return seller_origin_notice(offer.seller_evidence)


def _catalogue_tax_notice(offer):
    if offer is None or not offer.catalogue_ref:
        return ''
    label = {'included': 'С НДС', 'excluded': 'Без НДС',
             'unknown': 'НДС не подтверждён'}.get(offer.catalogue_vat_status, '')
    return '; '.join(dict.fromkeys(filter(None, [label, offer.catalogue_vat_notice])))


def _site_status(position, issues):
    from ..equipment_output_policy import is_graftio_equipment, missing_own_price, MISSING_PRICE
    if position.is_unique or is_graftio_equipment(position):
        return "Поиск не нужен", MISSING_PRICE if missing_own_price(position, position.offers) else position.research_reason or "Цена из письма"
    codes = {i.code for i in issues}
    reason = "; ".join(dict.fromkeys(i.reason for i in issues if i.reason))
    if codes & {"captcha", "http_blocked", "site_error", "empty_page", "supplier_paused"}:
        return "Доступ не подтверждён", reason
    if codes & {"search_budget", "job_budget", "discovery_deferred"}:
        return "Поиск не завершён", reason
    if "not_found" in codes:
        return "Не найдено по проверенным запросам", reason
    if "price_on_request" in codes:
        return "На карточке цена по запросу", reason
    if "out_of_stock" in codes:
        return "На карточке нет в наличии", reason
    if codes & {"article_mismatch", "page_article_mismatch", "product_mismatch", "listing", "not_direct_supplier"}:
        return "Подходящий товар не подтверждён", reason
    if issues:
        return "Требует проверки", reason
    return "Не проверено", "Нет сохранённого результата проверки этого сайта"


def _context_key(offer):
    values=(offer.screenshot_context,offer.url,offer.context_capture_evidence.get('sha256'))
    return values if all(type(v) is str for v in values) else None

def build_research_report(request: ResearchReportRequest, screenshot_links=None, context_links=None) -> bytes:
    # A retained browser draft cannot bypass a newer server-known refusal.
    # This is review projection, not the final minimum-three admission gate.
    from ..search.base_db import refresh_receipt_snapshot, project_refresh_offers
    legacy = any(not offer.catalogue_ref for p in request.positions
                 for offer in [*p.offers, *p.candidates])
    snapshot = refresh_receipt_snapshot() if legacy else {}
    request = request.model_copy(deep=True)
    for position in request.positions:
        def projected(offers):
            old = [o for o in offers if not o.catalogue_ref]
            refreshed = iter(project_refresh_offers(position, old, snapshot=snapshot)) if old else iter(())
            return [o if o.catalogue_ref else next(refreshed) for o in offers]
        position.offers = projected(position.offers)
        position.candidates = projected(position.candidates)
    packaged = screenshot_links is not None
    screenshot_links = screenshot_links or {}
    context_links = context_links or {}
    wb = Workbook()
    wb.remove(wb.active)
    from ..source_notes import document_notes
    notes = document_notes(request.positions)
    if notes:
        warnings = _sheet(wb, "Предупреждения к документу", ["Исходник SHA256", "Страница", "Таблица", "Строка", "Текст", "Координаты PDF", "Статус", "Тип наблюдения", "Исходная графа"], [70, 12, 12, 12, 75, 55, 65, 35, 20])
        for row, note in enumerate(notes, 2):
            for col, value in enumerate([note["source_hash"], note["page"], note["table"], note["row"], note["text"], str(note["bbox"]), "Предупреждение к документу; известные позиции обрабатываются", note["code"], note.get("original_cell", "name")], 1):
                _put(warnings, row, col, value)
    if any(p.catalogue_coverage_warning for p in request.positions):
        warnings=_sheet(wb,"Полнота проверки",["№ позиции","Предупреждение"],[15,110])
        row = 2
        for p in (p for p in request.positions if p.catalogue_coverage_warning):
            row = _write_coverage_warning(warnings, row, p.number, p.catalogue_coverage_warning)
    positions = _sheet(wb, "Позиции", [
        "№", "Наименование", "Марка / артикул", "Ед.", "Кол-во",
        "Статус поиска", "Предложений с ценой", "Цель: источников",
        "Результат / причина", "Сверка исходника", "Замечание по исходнику",
        "Оборудование Графтио", "Сопоставимых источников", "Минимум для сметы", "Покрытие",
    ], [8, 48, 24, 10, 12, 22, 20, 20, 70, 23, 55, 22, 22, 20, 22])
    offers = _sheet(wb, "Предложения", [
        "№ позиции", "Поставщик", "ИНН", "Город склада", "Товар на сайте",
        "Артикул карточки", "Цена, ₽", "Ед. цены", "Карточка товара",
        "Источник", "Сверка марки", "Ед. продажи на сайте", "Цена на сайте, ₽",
        "Проверка единицы/комплектации", "Причина проверки", "Регион поставки / предупреждение",
        "Для сметы", "Скриншот карточки", "Количество единиц в цене сайта", "Основание пересчёта", "Расчёт цены за единицу спецификации", "Контекст для проверки (не доказательство цены)", "Дата снимка контекста (UTC)", "Замечание по контексту", "Дата цены (UTC)",
        "НДС / предупреждение",
    ], [12, 32, 17, 24, 55, 25, 20, 16, 75, 20, 23, 23, 25, 30, 70, 45, 27, 48, 25, 60, 45, 48, 26, 65, 26, 60])
    matrix = _sheet(wb, "Позиции × сайты", [
        "№ позиции", "Наименование", "Марка / артикул", "Сайт", "Список поставщиков",
        "Результат проверки", "Цена, ₽", "Ед. цены", "Для сметы",
        "Карточка товара", "Скриншот карточки", "Замечание / причина", "Сверка исходника",
        "Дата проверки (UTC)", "Дата отсрочки (UTC)", "Повторить после (UTC)", "Код причины", "Контекст для проверки (не доказательство цены)", "Дата снимка контекста (UTC)", "Замечание по контексту", "Дата цены (UTC)", "Последнее обновление (UTC)", "Результат обновления",
        "НДС / предупреждение",
    ], [12, 48, 25, 27, 22, 38, 20, 23, 27, 65, 48, 85, 55, 26, 26, 26, 30, 48, 26, 65, 26, 26, 55, 60])
    from ..search.whitelist import whitelist_hosts
    primary = whitelist_hosts()
    configured = list(dict.fromkeys(primary))
    diagnostics = _sheet(wb, "Причины отклонения", [
        "№ позиции", "Код причины", "Артикул на сайте", "Объяснение",
        "Карточка / источник",
    ], [12, 30, 28, 90, 75])

    position_row = offer_row = matrix_row = diagnostic_row = 2

    for position in request.positions:
        good_offers = _evidence(position)
        from ..search.selected_proofs import selected_for_report
        selected = {_offer_key(o) for o in selected_for_report(position.offers, position)
                    if not o.url or _host(o.url)}
        from ..source_review import review_pending, review_summary
        review = ("Требует сверки" if review_pending(position)
                  else "Принято с замечанием" if position.needs_review
                  else "Автоматически распознано; замечаний нет" if position.source_is_image
                  else "Нет замечаний")
        coverage = annotate_coverage({**position.model_dump(exclude={'offers', 'candidates'}),
                                     'candidates': [o.model_dump() for o in good_offers],
                                     'status': position.research_status})
        display_status = position.research_status
        if coverage['quote_status'] == 'sufficient':
            display_status = 'complete'
        elif coverage['quote_status'] == 'insufficient' and display_status == 'complete':
            display_status = 'partial'
        if any(o.catalogue_ref for o in good_offers) and len(selected)<3 and display_status=='complete':
            display_status='partial'
        region_warnings = [f"{o.url or o.org_name}: {region_notice(o)}" for o in good_offers if region_notice(o)]
        from ..equipment_output_policy import missing_own_price, is_graftio_equipment, MISSING_PRICE
        own_missing = missing_own_price(position, position.offers)
        values = [
            position.number, position.name, position.type_mark, position.unit,
            position.qty, _STATUS.get(display_status, "Поиск не запускался"),
            len(good_offers), position.research_target or None,
            "; ".join(filter(None, [MISSING_PRICE if own_missing else position.research_reason, *region_warnings])), review, review_summary(position.review_fields) or position.review_issue,
            "Да" if is_graftio_equipment(position) else "Нет",
            coverage['comparable_sources'], 3,
            {'wide': 'Широкое (5 и более)', 'limited': 'Ограниченное (3–4)',
             'insufficient': 'Недостаточное (менее 3)'}[coverage['coverage']],
        ]
        row = position_row
        position_row += 1
        for col, value in enumerate(values, 1):
            _put(positions, row, col, value)
        if display_status in {"failed", "partial", "stale", "error"}:
            positions.cell(row, 6).fill = _WARN_FILL

        for offer in good_offers:
            row = offer_row
            offer_row += 1
            details = _quote_details(offer, position)
            displayed_price, displayed_unit = _display_price(offer, position)
            values = [
                position.number, offer.org_name, offer.inn, offer.city,
                offer.product_title or offer.page_title, offer.page_article,
                displayed_price, displayed_unit, offer.url,
                "Письмо" if offer.from_letter else "Сайт",
                ("Нужна сверка" if offer.match_review_required
                 and not offer.model_review_accepted else "Сверена"),
                offer.price_unit, offer.listed_price_with_vat,
                "Нужна проверка" if unit_review_pending(offer) else "Подтверждена",
                "; ".join(filter(None,[offer.unit_review_reason or ("Не указана единица продажи карточки" if unit_review_pending(offer) else ""),unit_capture_review_reason(offer, position),seller_review_reason(offer, good_offers),_seller_origin_notice(offer),offer.refresh_failure_reason,
                    "Проверено: " + datetime.fromtimestamp(offer.refresh_checked_at,timezone.utc).isoformat() if offer.refresh_checked_at else ""])),
                region_admission_label(offer),
                "Выбрано предварительно" if _offer_key(offer) in selected else "Нет",
                screenshot_links.get(offer.screenshot_product, "") or
                ("Нет доступного файла скриншота" if packaged else "В пакете со скриншотами") if offer.screenshot_product else "Скриншот не получен",
                details['quantity'], details['unit_text'], details['calculation'],
            ]
            context_link=context_links.get(_context_key(offer),'')
            values.extend([context_link or ('Контекст недоступен или не прошёл проверку файла' if packaged else 'В пакете со скриншотами') if offer.screenshot_context else 'Контекст не получен',_checked_date(offer.context_capture_evidence.get('captured_at')),_context_notice(offer),_checked_date(details['observed_at'])])
            values.append(_catalogue_tax_notice(offer))
            for col, value in enumerate(values, 1):
                _put(offers, row, col, value, url=col == 9)
            if context_link:offers.cell(row,22).hyperlink=context_link
            if _offer_key(offer) in selected:
                for col in range(1, 22):
                    cell = offers.cell(row, col)
                    cell.fill = _SELECTED_FILL
            if region_notice(offer):
                offers.cell(row, 16).fill = _WARN_FILL
            if screenshot_links.get(offer.screenshot_product):
                offers.cell(row, 18).hyperlink = screenshot_links[offer.screenshot_product]
                offers.cell(row, 18).value = "Открыть скриншот"
                offers.cell(row, 18).font = Font(color="0563C1", underline="single")
            offers.cell(row, 7).number_format = '#,##0.00'

        no_search = is_graftio_equipment(position)
        by_host = {}
        for offer in good_offers:
            if offer.url or no_search:
                host = _host(offer.url)
                # Keep regional subdomains under their configured supplier.
                host = next((h for h in configured if host == h or host.endswith("." + h)), host)
                by_host.setdefault(host, []).append(offer)
        issues_by_host = {}
        for issue in position.diagnostics:
            host = _host(issue.url)
            host = next((h for h in configured if host == h or host.endswith("." + h)), host)
            if host:
                issues_by_host.setdefault(host, []).append(issue)
        checks = {c.get('host'): c for c in position.source_checks}
        hosts = list(dict.fromkeys(configured + list(by_host) + list(issues_by_host)
                                   + [host for host in checks if host]))
        if no_search:
            hosts = list(by_host) or ['']
        check_labels = {'pending':'Ожидает проверки','running':'Проверяется','incomplete':'Проверка не завершена',
                        'skipped':'Поиск пропущен','no_match':'Подходящая карточка не найдена',
                        'error':'Ошибка проверки','review':'Требует проверки','found':'Цена найдена'}
        for host in hosts:
            bucket = by_host.get(host, [])
            group = "Поиск не нужен" if no_search else "Единый согласованный" if host in primary else "Найденный ресурс"
            for offer in bucket or [None]:
                chosen = offer is not None and _offer_key(offer) in selected
                check = checks.get(host, {})
                if offer is not None:
                    status = "Цена найдена" if offer_is_comparable(offer, position) else "Цена требует проверки"
                    reason = "; ".join(filter(None,[offer.unit_review_reason,unit_capture_review_reason(offer, position),seller_review_reason(offer, good_offers)]))
                    if offer.refresh_failed:
                        status = "Обновление цены не подтверждено"
                        reason = "; ".join(filter(None, [reason,
                            "Сохранённая цена исключена из автоматического выбора: обновление не подтверждено",
                            offer.refresh_failure_reason,
                            "Проверено: " + datetime.fromtimestamp(offer.refresh_checked_at,timezone.utc).isoformat() if offer.refresh_checked_at else "",
                            check.get('reason', ''),
                            *[i.reason for i in issues_by_host.get(host, []) if i.reason]]))
                    if host in checks and (checks[host].get('errors') or checks[host].get('card_errors')
                                           or checks[host].get('deferred_check')
                                           or checks[host].get('status') in {'incomplete', 'review', 'error'}):
                        reason = '; '.join(filter(None,[reason,checks[host].get('reason','')]))
                    if not host and not no_search:
                        status = "Некорректная ссылка"
                        reason = "Не удалось определить сайт по ссылке карточки"
                    reason = "; ".join(filter(None, [reason, region_notice(offer)]))
                    if offer.match_review_required and not offer.model_review_accepted:
                        reason = "; ".join(filter(None, [reason, "Марка/артикул требует сверки"]))
                    shot = screenshot_links.get(offer.screenshot_product, "")
                    if not shot:
                        reason = "; ".join(filter(None, [reason, ("Нет доступного файла скриншота" if packaged else "Скриншот не приложен к этому Excel; скачайте пакет") if offer.screenshot_product else "Скриншот не получен"]))
                else:
                    status, reason = _site_status(position, issues_by_host.get(host, []))
                    if host in checks:
                        check = checks[host]
                        status = check_labels.get(check.get('status'), 'Проверка не завершена')
                        reason = check.get('reason','')
                    shot = ""
                attempt = _latest_catalogue_attempt(position, host, offer)
                if attempt and attempt.get('outcome') != 'observed':
                    reason = '; '.join(filter(None, [reason,
                        'Последнее обновление не дало новых данных; показана сохранённая цена' if offer else 'Последнее обновление не дало новых данных',
                        attempt.get('reason') or attempt.get('outcome')]))
                displayed_price, displayed_unit = _display_price(offer, position) if offer else (None, '')
                values = [position.number, position.name, position.type_mark, host, group, status,
                          displayed_price, displayed_unit,
                          "Выбрано предварительно" if chosen else "Нет",
                          offer.url if offer else next((i.url for i in issues_by_host.get(host, []) if i.url), ""),
                          shot or (("Нет доступного файла скриншота" if packaged else "В пакете со скриншотами") if offer and offer.screenshot_product else ""),
                          "; ".join(filter(None, [reason, _seller_origin_notice(offer)])), position.review_issue if position.needs_review and not position.review_accepted else "",
                          _checked_date(check.get('checked_at') or check.get('observed_at')),
                          _checked_date(check.get('deferred_at')) if check.get('deferred_at') is not None else "",
                          _checked_date(check.get('retry_at')) if check.get('retry_at') is not None else "",
                          check.get('failure_code', '')]
                context_link=context_links.get(_context_key(offer),'') if offer else ''
                values.extend([context_link or ('Контекст недоступен или не прошёл проверку файла' if packaged else 'В пакете со скриншотами') if offer and offer.screenshot_context else 'Контекст не получен',_checked_date(offer.context_capture_evidence.get('captured_at')) if offer else '',_context_notice(offer)])
                details = _quote_details(offer, position) if offer else {}
                values.extend([_checked_date(details.get('observed_at')) if offer else '',
                               _checked_date(attempt.get('attempted_at')) if attempt else '',
                               '; '.join(filter(None, [attempt.get('outcome'), attempt.get('reason')]))])
                values.append(_catalogue_tax_notice(offer))
                row = matrix_row
                matrix_row += 1
                for col, value in enumerate(values, 1):
                    _put(matrix, row, col, value, url=col == 10)
                if context_link:matrix.cell(row,18).hyperlink=context_link
                matrix.cell(row, 7).number_format = '#,##0.00'
                if shot:
                    matrix.cell(row, 11).hyperlink = shot
                    matrix.cell(row, 11).value = "Открыть скриншот"
                    matrix.cell(row, 11).font = Font(color="0563C1", underline="single")
                if chosen:
                    for col in range(1, 18):
                        cell = matrix.cell(row, col)
                        cell.fill = _SELECTED_FILL
                if offer and region_notice(offer):
                    matrix.cell(row, 12).fill = _WARN_FILL

        for issue in position.diagnostics:
            if issue.code == "phase_timing":
                continue
            row = diagnostic_row
            diagnostic_row += 1
            for col, value in enumerate([
                position.number, issue.code, issue.observed_article,
                issue.reason, issue.url,
            ], 1):
                _put(diagnostics, row, col, value, url=col == 5)

    for sheet in wb.worksheets:
        sheet.auto_filter.ref = f"A1:{get_column_letter(sheet.max_column)}{sheet.max_row}"
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                wrapped = ((sheet.title == "Полнота проверки" and cell.column == 2)
                           or sheet == matrix
                           or (sheet == positions and cell.column in {2, 3, 9, 10, 11})
                           or (sheet == offers and cell.column in {2, 4, 5, 6, 9, 15, 16, 17, 18})
                           or (sheet == diagnostics and cell.column in {2, 3, 4, 5}))
                cell.alignment = Alignment(vertical="top", wrap_text=wrapped)
            if sheet in {positions, matrix, offers, diagnostics}:
                lines = max((math.ceil(len(str(c.value or "")) /
                             (sheet.column_dimensions[c.column_letter].width * 0.85))
                             for c in row if c.alignment.wrap_text), default=1)
                sheet.row_dimensions[row[0].row].height = min(409, max(30, lines * 15))

    # A visible source note keeps this export separate from the approved KAC/TKP.
    wb.properties.title = "Сводка поиска цен по спецификации"
    wb.properties.subject = _safe_text(request.object_name)
    output = BytesIO()
    wb.save(output)
    return output.getvalue()
