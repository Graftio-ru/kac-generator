"""Portable supplier research workbook and cache-bound product screenshots."""
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image

from ..config import CACHE_DIR
from .research_report_xlsx import build_research_report, _evidence, _context_key


def _screenshot(reference):
    if not reference:
        return None
    try:
        root = CACHE_DIR.resolve()
        path = Path(reference)
        path = (path if path.is_absolute() else root / path).resolve()
        if path.parent != root or path.suffix.lower() not in {'.png', '.jpg', '.jpeg'}:
            return None
        if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
            return None
        data = path.read_bytes()
        with Image.open(BytesIO(data)) as image:
            if image.format not in {'PNG', 'JPEG'}:
                return None
            suffix = '.png' if image.format == 'PNG' else '.jpg'
            image.verify()
        return data, suffix
    except (OSError, ValueError, RuntimeError, Image.DecompressionBombError):
        return None


def can_reuse_standalone_workbook(request):
    # Packaged and standalone notices/links differ whenever an attachment
    # locator is present, even if its local file is currently unavailable.
    return not any(offer.screenshot_product or offer.screenshot_context
                   for position in request.positions for offer in _evidence(position))


def build_research_package(request, *, standalone_workbook=None, interruption_reason=None):
    """Package the workbook and locally available evidence, including any stop reason."""
    files = {}
    links = {}
    context_links = {}
    total = 0
    for position in request.positions:
        for offer in _evidence(position):
            reference = offer.screenshot_product
            if offer.from_letter or reference in links:
                continue
            captured = _screenshot(reference)
            if captured is None:
                continue
            data, suffix = captured
            name = 'screenshots/' + sha256(data).hexdigest()[:24] + suffix
            links[reference] = name
            if name not in files:
                total += len(data)
                if total > 128 * 1024 * 1024:
                    raise ValueError('Скриншоты превышают лимит пакета 128 МБ')
                files[name] = data
    from ..review_context import context_valid
    for position in request.positions:
        for offer in _evidence(position):
            reference=offer.screenshot_context
            if offer.from_letter or not reference:continue
            captured=_screenshot(reference)
            if captured is None:continue
            data,suffix=captured
            if not context_valid(offer.context_capture_evidence,reference,offer.url,data):continue
            name='screenshots/context-'+sha256(data).hexdigest()[:24]+suffix
            context_links[_context_key(offer)]=name
            if name not in files:
                total+=len(data)
                if total>128*1024*1024:raise ValueError('Скриншоты превышают лимит пакета 128 МБ')
                files[name]=data
    if standalone_workbook is not None:
        if type(standalone_workbook) is not bytes or not can_reuse_standalone_workbook(request) or links or context_links:
            raise ValueError('standalone_workbook_links_differ')
        workbook = standalone_workbook
    else:
        workbook = build_research_report(
            request, screenshot_links=links, context_links=context_links,
            interruption_reason=interruption_reason,
        )
    output = BytesIO()
    with ZipFile(output, 'w', compression=ZIP_DEFLATED) as archive:
        archive.writestr('supplier-research-summary.xlsx', workbook)
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr('README.txt', (
            'Распакуйте ZIP целиком, сохранив папку screenshots рядом с Excel.\n'
            'На листе «Позиции × сайты» показаны все сайты из текущих списков поставщиков '
            'и дополнительные ресурсы из сохранённых результатов.\n'
            'Зелёный цвет: предварительно выбранные предложения для сметы. '
            'Недостаток трёх источников и замечания по исходнику остаются на листе «Позиции».\n'
            '«Не проверено» и «Поиск не завершён» не означают отсутствия товара.\n'
            'Ссылки на скриншоты ведут к вложенным изображениям. Недоступный скриншот указан явно.\n'
            'Контекст карточки для проверки — отдельный снимок условий продажи, не доказательство цены и не допуск в смету. Ограничение области снимка отмечено в Excel.\n'
            'Ошибки и неопределённые источники не означают отсутствия товара и не отменяют проверенные предложения. Полнота проверки указана отдельно в Excel.\n'
            'Это сводка поиска, а не утверждённая смета или том ТКП. '
            'При сборке ТКП скриншоты выбранных карточек включаются в PDF.\n'
        ).encode('utf-8'))
    return output.getvalue()
