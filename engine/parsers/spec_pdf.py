"""Парсер спецификации → список SpecPosition. Принимает PDF, изображения, DOCX, XLSX/XLS.

Форматы:
  • PDF с поддерживаемыми текстовыми таблицами → ячейки напрямую (PyMuPDF), без LLM.
  • Остальные PDF с текстовым слоем → текст для структуризации LLM.
  • PDF-скан/изображение с DeepSeek → прямое vision-чтение и независимое сравнение с OCR.
    Anthropic сохраняет прежний OCR-путь и vision-фолбэк.
  • DOCX → текст абзацев и таблиц (python-docx).
  • XLSX/XLS → строки листов как текст (openpyxl).

Структуризация неподдерживаемых таблиц выполняется LLM. Без ключа — бросает LLMUnavailable,
UI предлагает ручной ввод. Завод-изготовитель из CUSTOM_MAKERS → авто-пометка уникального.
"""
from __future__ import annotations

import io
import math
import re
from pathlib import Path

import fitz

from ..config import CACHE_DIR, settings
from ..llm import LLMAccountError, LLMUnavailable, ModelOutputInvalid, complete_json
from ..models import SpecPosition

SYSTEM = (
    "Ты извлекаешь позиции из спецификации оборудования (рабочая документация). "
    "Верни JSON-объект с единственным полем positions — массивом объектов с полями: "
    "number (int), name (string — наименование и "
    "техническая характеристика), type_mark (string — тип/марка/обозначение, если есть), "
    "unit (string — ед. изм., напр. шт/м/компл), qty (number — количество), "
    "maker (string — завод-изготовитель, если указан). "
    "Сохраняй название изготовителя без сокращений и догадок; если оно не читается, "
    "поставь review_fields.source с причиной. "
    "Если отдельная колонка кода/артикула изделия содержит дополнительное обозначение, "
    "сохрани его вместе с типом/маркой в type_mark, не потеряв класс точности и мощность. "
    "Для КАЖДОЙ строки отдельно прочитай обе колонки: «Тип, марка» и «Код оборудования, "
    "изделия, материала» (или их аналоги). Пустая колонка типа НЕ означает отсутствие "
    "артикула: если код читается в соседней колонке, верни этот код в type_mark. "
    "Если обе колонки заполнены, сохрани оба обозначения. Если код виден, но не читается, "
    "оставь явное review_fields.type_mark; не переноси код из соседней строки. "
    "review_issue (string) — что невозможно прочитать или надёжно связать со строкой. "
    "review_fields (object) — конкретные сомнительные поля name/type_mark/unit/qty "
    "с причиной для каждого; пустой объект, если замечаний нет. Не присваивай "
    "уверенность только на основании правдоподобия значения. "
    "Не дополняй закрытые штампом артикулы по догадке, не включай текст штампа в марки. "
    "Подпозиции «в составе» объединяй в родительскую позицию. "
    "НЕ включай строки-ЗАГОЛОВКИ РАЗДЕЛОВ — это строки, где есть только название раздела без "
    "количества, единицы измерения и марки (напр. «Прочее», «Расходные материалы», "
    "«Оборудование», «Кабельная продукция», «Раздел 1»). Такие строки в массив не добавляй. "
    "Если OCR-текст содержит строки вида y=... | x=...:текст, координата x показывает колонку "
    "таблицы, а одинаковая y — одну визуальную строку; связывай значения по координатам, не по "
    "порядку блоков текста. Не сдвигай марки, единицы и количества между соседними позициями. "
    "Не выдумывай позиции. Сохраняй порядок. Только JSON вида {\"positions\": [...]}."
)

def _is_graftio_equipment(name: str, type_mark: str, maker: str) -> bool:
    """Keep Graftio products out of supplier search despite common OCR maker errors."""
    maker_key = re.sub(r"[^a-zа-я]", "", maker.casefold().replace("ё", "е"))
    if maker_key in {"графтио", "граффио", "гратио", "гфтио", "graftio"}:
        return True
    name_key = " ".join(name.casefold().replace("ё", "е").split())
    mark_key = re.sub(r"[^a-zа-я0-9]", "", type_mark.casefold())
    return (
        ("зарядная станция" in name_key and mark_key.startswith("pev"))
        or ("вторичный распределительный узел" in name_key and mark_key.startswith("вру"))
        or "монтажный комплект для установки зарядной станции" in name_key
    )


def extract_raw_text(pdf_path: str | Path) -> tuple[str, bool]:
    """Возвращает (текст, is_scanned). Для сканов текст пустой → нужен OCR/vision."""
    doc = fitz.open(pdf_path)
    parts = [doc[i].get_text() for i in range(doc.page_count)]
    text = "\n".join(parts)
    is_scanned = len(text.strip()) < 40 * doc.page_count  # эвристика
    doc.close()
    return text, is_scanned


def text_from_docx(path: str | Path) -> str:
    """Текст из DOCX: абзацы + ячейки таблиц (таблицы — построчно, через табуляцию)."""
    import docx

    doc = docx.Document(str(path))
    parts: list[str] = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append("\t".join(cells))
    return "\n".join(parts)


def text_from_xlsx(path: str | Path, max_rows: int = 4000) -> str:
    """Текст из XLSX/XLS: строки всех листов через табуляцию (пустые строки пропускаются)."""
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    parts: list[str] = []
    for ws in wb.worksheets:
        parts.append(f"# Лист: {ws.title}")
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= max_rows:
                break
            cells = ["" if v is None else str(v) for v in row]
            if any(c.strip() for c in cells):
                parts.append("\t".join(cells))
    wb.close()
    return "\n".join(parts)


def render_pages(pdf_path: str | Path, dpi: int = 170, max_pages: int = 12) -> list[str]:
    """Рендерит страницы PDF в PNG (для распознавания сканов через Claude vision)."""
    doc = fitz.open(pdf_path)
    paths = []
    for i in range(min(max_pages, doc.page_count)):
        out = CACHE_DIR / f"spec_{Path(pdf_path).stem}_{i+1}.png"
        doc[i].get_pixmap(dpi=dpi).save(out)
        paths.append(str(out))
    doc.close()
    return paths


def _ocr_backend() -> str:
    """Доступный локальный OCR: 'vision' (Apple, macOS) → 'tesseract' → '' (нет)."""
    try:
        import ocrmac  # noqa: F401
        return "vision"
    except Exception:
        pass
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return "tesseract"
    except Exception:
        return ""


def ocr_available() -> bool:
    """Доступно ли локальное распознавание сканов (Apple Vision или Tesseract)."""
    return bool(_ocr_backend())


def _vision_layout_text(annotations: list[tuple]) -> str:
    """Сериализует Apple Vision OCR с координатами, сохраняя визуальные строки таблицы."""
    items: list[tuple[float, float, float, str]] = []
    for annotation in annotations:
        if len(annotation) < 3:
            continue
        text = str(annotation[0]).strip()
        bbox = annotation[2]
        if not text or not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
            continue
        x, y, _width, height = (float(value) for value in bbox[:4])
        items.append((y + height / 2, x, max(height, 0.001), text))

    items.sort(key=lambda item: (-item[0], item[1]))
    lines: list[dict] = []
    for center_y, x, height, text in items:
        if lines:
            previous = lines[-1]
            tolerance = max(0.004, min(0.008, max(height, previous["height"]) * 0.55))
            if abs(center_y - previous["center_y"]) <= tolerance:
                previous["cells"].append((x, text))
                count = len(previous["cells"])
                previous["center_y"] = (
                    previous["center_y"] * (count - 1) + center_y
                ) / count
                previous["height"] = max(previous["height"], height)
                continue
        lines.append({
            "center_y": center_y,
            "height": height,
            "cells": [(x, text)],
        })

    rendered: list[str] = []
    for line in lines:
        cells = " | ".join(
            f"x={x:.3f}:{text}" for x, text in sorted(line["cells"])
        )
        rendered.append(f"y={line['center_y']:.3f} | {cells}")
    return "\n".join(rendered)


def _annotation_center(annotation: tuple) -> tuple[float, float]:
    bbox = annotation[2]
    return float(bbox[0]) + float(bbox[2]) / 2, float(bbox[1]) + float(bbox[3]) / 2


def _merge_vision_annotations(base: list[tuple], additions: list[tuple]) -> list[tuple]:
    """Добавляет OCR-фрагменты, если в основном проходе нет ячейки в том же месте."""
    merged = list(base)
    for addition in additions:
        if len(addition) < 3 or not str(addition[0]).strip():
            continue
        add_x, add_y = _annotation_center(addition)
        occupied = False
        for existing in merged:
            if len(existing) < 3:
                continue
            current_x, current_y = _annotation_center(existing)
            if abs(add_x - current_x) <= 0.015 and abs(add_y - current_y) <= 0.008:
                occupied = True
                break
        if not occupied:
            merged.append(addition)
    return merged


def _vision_annotations_with_narrow_columns(img, ocr_class) -> list[tuple]:
    """Основной OCR плюс увеличенные узкие колонки номера, единицы и количества."""
    base = ocr_class(
        img, language_preference=["ru-RU", "en-US"]
    ).recognize()
    width, height = img.size
    # На полном листе Apple Vision часто пропускает короткие значения в этих колонках.
    for left_ratio, right_ratio, scale in (
        (0.07, 0.115, 3),   # номер позиции
        (0.73, 0.79, 4),    # единица измерения
        (0.74, 0.785, 6),   # повтор для самых мелких единиц, пропущенных первым проходом
        (0.79, 0.85, 3),    # количество
    ):
        left = int(width * left_ratio)
        right = int(width * right_ratio)
        crop = img.crop((left, 0, right, height))
        crop = crop.resize((crop.width * scale, crop.height * scale))
        local = ocr_class(crop, language_preference=["ru-RU", "en-US"]).recognize()
        mapped: list[tuple] = []
        span = right_ratio - left_ratio
        for text, confidence, bbox in local:
            x, y, box_width, box_height = (float(value) for value in bbox[:4])
            mapped.append((
                text,
                confidence,
                [left_ratio + x * span, y, box_width * span, box_height],
            ))
        base = _merge_vision_annotations(base, mapped)
    # Read Latin/digit articles separately at a larger scale. Preserve different
    # readings of the same cell rather than silently correcting O/0, I/1, etc.
    left, right, scale = int(width * 0.568), int(width * 0.65), 4
    crop = img.crop((left, 0, right, height))
    crop = crop.resize((crop.width * scale, crop.height * scale))
    for languages in (["en-US"], ["ru-RU", "en-US"]):
        local = ocr_class(crop, language_preference=languages).recognize()
        for text, confidence, bbox in local:
            code = _article_cell_code(str(text))
            if not code or confidence < 0.8:
                continue
            x, y, bw, bh = (float(value) for value in bbox[:4])
            mapped = (code, confidence, [
                (left + x * (right-left)) / width, y,
                bw * (right-left) / width, bh])
            my = _annotation_center(mapped)[1]
            if not any(_article_cell_code(str(a[0])) == code
                       and 0.568 <= float(a[2][0]) <= 0.65
                       and abs(_annotation_center(a)[1] - my) < 0.006 for a in base):
                base.append(mapped)
    # A tight, padded quantity strip avoids table borders. English-only and
    # fast recognition recover small digits missed by the broad mixed-language
    # pass. Keep conflicting readings visible; never pick an arbitrary number.
    anchors = [a for a in base if str(a[0]).isdigit()
               and 0.07 <= float(a[2][0]) <= 0.115]
    if len(anchors) >= 3:
        from PIL import ImageOps
        ys = [_annotation_center(a)[1] for a in anchors]
        top = max(0, int(height * (1 - max(ys) - 0.018)))
        bottom = min(height, int(height * (1 - min(ys) + 0.018)))
        left, right, scale, pad = int(width * 0.798), int(width * 0.844), 6, 40
        crop = img.crop((left, top, right, bottom))
        crop = ImageOps.expand(crop.resize((crop.width * scale, crop.height * scale)),
                               border=pad, fill="white")
        for level in ("accurate", "fast"):
            local = ocr_class(crop, recognition_level=level,
                              language_preference=["en-US"]).recognize()
            for text, confidence, bbox in local:
                if not re.fullmatch(r"\d+(?:[.,]\d+)?", str(text).strip()):
                    continue
                x, y, bw, bh = (float(value) for value in bbox[:4])
                mapped = (text, confidence, [
                    (left + (x * crop.width - pad) / scale) / width,
                    1 - (top + ((1 - y) * crop.height - pad) / scale) / height,
                    bw * crop.width / scale / width,
                    bh * crop.height / scale / height])
                mx, my = _annotation_center(mapped)
                nearby = [a for a in base if 0.795 <= float(a[2][0]) <= 0.85
                          and abs(_annotation_center(a)[1] - my) <= 0.006]
                if not any(str(a[0]).strip() == str(text).strip() for a in nearby):
                    base.append(mapped)
    # Independently read the type/model column. Keep disagreements as source
    # evidence instead of guessing visually similar Latin letters and digits.
    left, right, scale = int(width * 0.438), int(width * 0.566), 4
    crop = img.crop((left, 0, right, height))
    crop = crop.resize((crop.width * scale, crop.height * scale))
    for languages, level in ((["en-US"], 'accurate'), (["ru-RU", "en-US"], 'accurate'),
                             (["en-US"], 'fast')):
        for text, confidence, bbox in ocr_class(crop, language_preference=languages,
                                               recognition_level=level).recognize():
            if not str(text).strip() or confidence < 0.8:
                continue
            x, y, bw, bh = (float(value) for value in bbox[:4])
            mapped = (str(text), confidence, [(left+x*(right-left))/width,
                                             y, bw*(right-left)/width, bh])
            my = _annotation_center(mapped)[1]
            if not any(_source_mark(str(a[0])) == _source_mark(str(text))
                       and 0.438 <= float(a[2][0]) <= 0.566
                       and abs(_annotation_center(a)[1]-my) < 0.006 for a in base):
                base.append(mapped)
    return base


def _source_mark(text):
    from ..source_codes import source_code_key
    return source_code_key(text)


def _source_layout_text(img, annotations):
    text = _vision_layout_text(annotations)
    # Blue approval stamps may truncate an otherwise numeric catalogue code.
    # This is a cell visibility warning, never recovery of the hidden digits.
    if 'Позиция' not in text or 'Единица' not in text or 'Кол-' not in text:
        return text
    width, height = img.size
    warned = set()
    for annotation in annotations:
        number = str(annotation[0]).strip()
        if not number.isdigit() or not 0.07 <= float(annotation[2][0]) <= 0.115:
            continue
        y = _annotation_center(annotation)[1]
        crop = img.crop((int(width*.438), max(0,int(height*(1-y-.008))),
                         int(width*.65), min(height,int(height*(1-y+.008)))))
        blue = sum(b > 80 and b > r+30 and b > g+20 for r,g,b in crop.convert('RGB').getdata())
        if blue >= 8 and number not in warned:
            text += f'\nsource_warning number={number} field=type_mark reason=Цветной оттиск/текст в ячейке марки; сверить читаемость'
            warned.add(number)
    return text


def _article_cell_code(text: str) -> str:
    """Only explicit alphanumeric catalogue codes, without character guesses."""
    code = re.sub(r"\s+", "", text).upper()
    if re.fullmatch(r"[A-Z0-9][A-Z0-9_.-]{3,39}", _source_mark(code)) and any(c.isdigit() for c in code):
        return code
    return ""


def ocr_pages(pdf_path: str | Path, dpi: int = 300, max_pages: int = 12) -> str:
    """Локальное распознавание скана в текст. Высокий DPI для таблиц.

    На macOS — Apple Vision (ocrmac, без системных зависимостей); иначе — Tesseract.
    """
    from PIL import Image

    backend = _ocr_backend()
    doc = fitz.open(pdf_path)
    parts: list[str] = []
    try:
        for i in range(min(max_pages, doc.page_count)):
            pix = doc[i].get_pixmap(dpi=dpi)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            if backend == "vision":
                from ocrmac import ocrmac as _ocrmac
                # Apple Vision возвращает (текст, уверенность, bbox), но порядок часто
                # колоночный. Координаты нужны LLM, чтобы не сдвигать значения между строками.
                ann = _vision_annotations_with_narrow_columns(img, _ocrmac.OCR)
                parts.append(f"--- Страница {i + 1} ---\n{_source_layout_text(img, ann)}")
            elif backend == "tesseract":
                import pytesseract
                parts.append(pytesseract.image_to_string(img, lang="rus+eng"))
    finally:
        doc.close()
    return "\n".join(parts)


def ocr_image(path: str | Path) -> str:
    """Распознаёт отдельное изображение спецификации тем же OCR, что PDF-скан."""
    from PIL import Image

    backend = _ocr_backend()
    with Image.open(path) as source:
        img = source.convert("RGB")
    if backend == "vision":
        from ocrmac import ocrmac as _ocrmac
        return _source_layout_text(img, _vision_annotations_with_narrow_columns(img, _ocrmac.OCR))
    if backend == "tesseract":
        import pytesseract
        return pytesseract.image_to_string(img, lang="rus+eng")
    return ""


def _comparison_ocr_image(path):
    """One native OCR pass per page; no form-specific crops or paid vision."""
    from PIL import Image
    backend = _ocr_backend()
    with Image.open(path) as source:
        image = source.convert('RGB')
    if backend == 'vision':
        from ocrmac import ocrmac as native
        return _vision_layout_text(native.OCR(image, language_preference=['ru-RU', 'en-US']).recognize())
    if backend == 'tesseract':
        import pytesseract
        return pytesseract.image_to_string(image, lang='rus+eng')
    return ''


def _join_review_reasons(*values):
    return '; '.join(dict.fromkeys(str(value).strip() for value in values
                                  if value is not None and str(value).strip()))


def _reconcile_layout_cells(raw: list[dict], text: str) -> list[dict]:
    """Bind OCR numbers to cells in tables with the recognized header geometry.

    Only this detected table layout is handled. Missing/conflicting cells remain
    unknown, even when the model supplied a plausible quantity. No reference
    specification or supplier data participates in recovery.
    """
    pages = re.split(r"--- Страница \d+ ---", text)
    fields = {}
    units = {}
    articles = {}
    marks = {}
    makers = {}
    for page in pages:
        if not ("Позиция" in page and "Кол-" in page and "Единица" in page):
            continue
        cells = [(float(y), float(x), value.strip())
                 for line in page.splitlines()
                 if (m := re.match(r"y=([\d.]+) \| (.*)", line))
                 for y in [m[1]]
                 for x, value in re.findall(r"x=([\d.]+):([^|]+)", m[2])]
        if not (any(0.07 <= x <= 0.115 and v == "Позиция" for _, x, v in cells)
                and any(0.795 <= x <= 0.85 and v.startswith("Кол-") for _, x, v in cells)
                and any(0.73 <= x <= 0.79 and v == "Единица" for _, x, v in cells)):
            continue
        anchors = [(y, int(v)) for y, x, v in cells
                   if 0.07 <= x <= 0.115 and v.isdigit()]
        if len(anchors) < 3:
            continue
        for y, number in anchors:
            nearby_gap = min((abs(y - other) for other, _ in anchors if other != y), default=0.02)
            tolerance = min(0.007, nearby_gap / 3)
            values = [v for cy, x, v in cells if abs(cy-y) <= tolerance and 0.795 <= x <= 0.85]
            quantities = {float(v.replace(',', '.')) for v in values
                          if re.fullmatch(r"\d+(?:[.,]\d+)?", v)}
            fields.setdefault(number, []).append(quantities)
            units.setdefault(number, []).append({v.lower().rstrip('.') for cy, x, v in cells
                if abs(cy-y) <= tolerance and 0.73 <= x <= 0.79
                and v.lower().rstrip('.') in {"м", "шт", "компл", "кг", "уп"}})
            articles.setdefault(number, []).append({code for cy, x, v in cells
                if abs(cy-y) <= tolerance and 0.568 <= x <= 0.65
                and (code := _article_cell_code(v))})
            marks.setdefault(number, []).append({v for cy, x, v in cells
                if abs(cy-y) <= tolerance and 0.435 <= x < 0.566
                and len(_source_mark(v)) >= 4})
            makers.setdefault(number, []).append({v for cy,x,v in cells
                if abs(cy-y) <= tolerance and 0.65 <= x < 0.73})
    for item in raw:
        if not isinstance(item, dict):
            continue
        for number, field, reason in re.findall(r'source_warning number=(\d+) field=(\w+) reason=([^\n]+)', text):
            if str(item.get('number')) == number:
                if not isinstance(item.get('review_fields'), dict):
                    item['review_fields'] = {}
                item.setdefault('review_fields', {})[field] = reason
        try:
            readings = fields.get(int(item.get("number")), [])
        except (TypeError, ValueError):
            continue
        if not readings:
            continue
        if len(readings) != 1:
            review = item.get('review_fields')
            review = dict(review) if isinstance(review, dict) else {}
            reason = 'Несколько ячеек OCR с одним номером позиции; сверить количество'
            review['qty'] = _join_review_reasons(review.get('qty'), reason)
            item['review_fields'] = review
            item["qty"] = 0
            item["review_issue"] = "; ".join(filter(None, [item.get("review_issue"),
                "Несколько ячеек OCR с одним номером позиции; сверить количество"]))
            continue
        values = readings[0]
        issues = [str(item.get("review_issue") or "").strip()]

        def warn(field, reason):
            issues.append(reason)
            review = item.get('review_fields')
            review = dict(review) if isinstance(review, dict) else {}
            review[field] = _join_review_reasons(review.get(field), reason)
            item['review_fields'] = review
        maker_readings = makers.get(int(item['number']), [])
        if len(maker_readings) == 1:
            item['_source_makers'] = sorted(maker_readings[0])
            maker = str(item.get('maker') or '').strip()
            if maker_readings[0] and not maker:
                warn('source', 'Не прочитан изготовитель, видимый в ячейке OCR; проверить исключение уникального оборудования')
        mark_readings = marks.get(int(item['number']), [])
        if len(mark_readings) == 1:
            readings_by_key = {_source_mark(v):v for v in mark_readings[0]}
            if len(readings_by_key) > 1:
                warn('type_mark', 'Разные чтения марки в колонке OCR: '
                              + ' / '.join(sorted(readings_by_key.values())) + '; сверить артикул')
            elif readings_by_key and not item.get('type_mark'):
                warn('type_mark', 'Марка видна в колонке OCR, но пропущена при распознавании: '
                              + next(iter(readings_by_key.values())) + '; сверить артикул')
            elif readings_by_key and item.get('type_mark'):
                observed = next(iter(readings_by_key))
                structured = _source_mark(str(item['type_mark']))
                # Additional article-column aliases may be present in the
                # structured value, but the actual type cell must survive.
                if not re.search(r'(?<![A-ZА-Я0-9])'+re.escape(observed)
                                 +r'(?![A-ZА-Я0-9])', structured):
                    warn('type_mark', 'Марка из колонки OCR не совпадает со структурированным артикулом: '
                                  + next(iter(readings_by_key.values())))
        article_readings = articles.get(int(item["number"]), [])
        if len(article_readings) == 1 and article_readings[0]:
            codes = article_readings[0]
            keyed_codes = {_source_mark(code):code for code in codes}
            mark = _source_mark(str(item.get("type_mark") or ""))
            if len(keyed_codes) > 1:
                warn('type_mark', "Противоречивые варианты артикула в ячейке OCR: "
                              + ", ".join(sorted(codes)))
            elif not mark:
                item["type_mark"] = next(iter(codes))
                warn('type_mark', "Артикул восстановлен из ячейки OCR; сверить с оригиналом")
            elif not re.search(r"(?<![A-ZА-Я0-9])" + re.escape(next(iter(keyed_codes)))
                               + r"(?![A-ZА-Я0-9])", mark):
                warn('type_mark', "Артикул в ячейке OCR отличается от распознанной марки: "
                              + next(iter(codes)) + "; сверить с оригиналом")
        old = item.get("qty")
        unit_readings = units.get(int(item["number"]), [])
        if not item.get("unit") and len(unit_readings) == 1 and len(unit_readings[0]) == 1:
            item["unit"] = next(iter(unit_readings[0]))
            warn('unit', "Единица восстановлена из ячейки OCR; сверить с оригиналом")
        elif len(unit_readings) == 1 and unit_readings[0]:
            normalized_unit = str(item.get('unit') or '').lower().strip().rstrip('.')
            if len(unit_readings[0]) != 1 or normalized_unit not in unit_readings[0]:
                warn('unit', 'Единица на изображении расходится с чтениями OCR: '
                              + ' / '.join(sorted(unit_readings[0])) + '; сверить единицу')
        if len(values) == 1 and next(iter(values)) > 0:
            qty = next(iter(values))
            item["qty"] = qty
            if old != qty:
                warn('qty', "Количество восстановлено из ячейки OCR; сверить с оригиналом")
        else:
            item["qty"] = 0
            warn('qty', "Количество не прочитано в ячейке OCR" if not values
                          else "Противоречивые варианты количества в ячейке OCR: "
                               + ", ".join(f"{v:g}" for v in sorted(values)))
        item["review_issue"] = "; ".join(i for i in issues if i)
    return raw


def _compare_visual_cells(raw, text):
    """Compare independent OCR cells, retaining actual visual readings unchanged."""
    from copy import deepcopy
    observed = _reconcile_layout_cells(deepcopy(raw), text)
    for item, other in zip(raw, observed):
        if not isinstance(item, dict) or not isinstance(other, dict):
            continue
        compared_issue = str(other.get('review_issue') or '')
        # The OCR-only reconciler mutates its copy. In this visual comparison
        # path those candidate readings were NOT written back to the source.
        for old, new in [
            ('Артикул восстановлен из ячейки OCR; сверить с оригиналом',
             'Артикул прочитан OCR, но пропущен при чтении изображения: '
             + str(other.get('type_mark') or '') + '; сверить с оригиналом'),
            ('Единица восстановлена из ячейки OCR; сверить с оригиналом',
             'Единица прочитана OCR, но пропущена при чтении изображения; сверить с оригиналом'),
            ('Количество восстановлено из ячейки OCR; сверить с оригиналом',
             'Количество прочитано OCR иначе, чем на изображении; сверить с оригиналом'),
        ]:
            compared_issue = compared_issue.replace(old, new)
            review = other.get('review_fields')
            if isinstance(review, dict):
                other['review_fields'] = {field: str(reason).replace(old, new)
                                          for field, reason in review.items()}
        issues = [str(item.get('review_issue') or ''), compared_issue]
        observed_qty = other.get('qty')
        if (isinstance(observed_qty, (int, float)) and observed_qty > 0
                and observed_qty != item.get('qty')):
            issues.append('Чтение количества на изображении расходится с локальным OCR; сверить количество')
            review = other.get('review_fields')
            review = dict(review) if isinstance(review, dict) else {}
            review['qty'] = (f"Количество расходится: изображение {item.get('qty')}, "
                             f"локальный OCR {observed_qty:g}; сверить с оригиналом")
            other['review_fields'] = review
        item['review_issue'] = '; '.join(dict.fromkeys(i for i in issues if i))
        fields = item.get('review_fields') if isinstance(item.get('review_fields'), dict) else {}
        other_fields = other.get('review_fields') if isinstance(other.get('review_fields'), dict) else {}
        item['review_fields'] = dict(fields)
        for field, reason in other_fields.items():
            item['review_fields'][field] = _join_review_reasons(fields.get(field), reason)
        item['_source_makers'] = other.get('_source_makers', [])
    return raw


def _position_response_schema():
    """Bound scan output to product rows; unknown fields carry explicit warnings."""
    fields = {key: {'type': 'string'}
              for key in ('name', 'type_mark', 'unit', 'maker', 'review_issue')}
    fields.update(number={'type': ['integer', 'null']},
                  qty={'type': ['number', 'null']},
                  review_fields={'type': 'object', 'additionalProperties': False,
                                 'properties': {key: {'type': 'string'} for key in
                                                ('source', 'name', 'type_mark', 'unit', 'qty')},
                                 'required': ['source', 'name', 'type_mark', 'unit', 'qty']})
    return {'type': 'object', 'additionalProperties': False,
            'properties': {'positions': {'type': 'array', 'items': {
                'type': 'object', 'additionalProperties': False,
                'properties': fields, 'required': list(fields)}}},
            'required': ['positions']}


def _structure_positions(text: str = "", images: list[str] | None = None) -> list[dict]:
    """Зовёт LLM (текст или vision) и возвращает сырой массив позиций."""
    # лимит большой: xlsx/docx-спецификации бывают объёмными, DeepSeek/Claude держат контекст
    prompt = ("Распознай спецификацию непосредственно по изображениям. "
              "Полные страницы и их увеличенные перекрывающиеся фрагменты показывают одну "
              "спецификацию: верни каждую позицию ровно один раз. Не восстанавливай скрытые "
              "символы по контексту. Для нечитаемых полей укажи review_fields."
              if images else f"Спецификация (текст):\n\n{text[:120000]}" if text
              else "Распознай таблицу-спецификацию со страниц-изображений.")
    # Image calls select the dedicated vision model; text calls keep fast model settings.
    strict_scan = bool(images) and settings.llm_provider.lower() == 'cloudru'
    schema_args = {'response_schema': _position_response_schema()} if strict_scan else {}
    response = complete_json(prompt, system=SYSTEM, smart=False, max_tokens=8192,
                             images=images, **schema_args)
    if strict_scan and (not isinstance(response, dict) or set(response) != {'positions'}):
        raise ModelOutputInvalid('invalid_rows',output_kind='positions')
    # DeepSeek JSON mode requires a root object. Keep accepting the former root-array
    # response as well so Anthropic and cached fixtures remain compatible.
    raw = response if isinstance(response, list) else response.get('positions') if isinstance(response, dict) else None
    if not isinstance(raw, list):
        raise ModelOutputInvalid('invalid_rows',output_kind='positions')
    if any(not isinstance(item,dict)for item in raw):
        raise ModelOutputInvalid('invalid_rows',output_kind='positions')
    for item in raw:
        if isinstance(item, dict):
            # Reader/provenance metadata belongs to the parser, not the model.
            item.pop('_source_makers', None)
            item.pop('_source_reader', None)
            item.pop('_source_model_metadata', None)
            metadata=getattr(response,'model_metadata',None)
            if isinstance(metadata,dict):item['_source_model_metadata']=dict(metadata)
            item.pop('_source_evidence', None)
            item.pop('_independent_only', None)
            item['_model_output'] = True
    if images:
        for item in raw:
            if isinstance(item, dict):
                item['_source_reader'] = 'vision'
        return _compare_visual_cells(raw, text) if text else raw
    return _reconcile_layout_cells(raw, text)


def _visual_positions(text, images):
    """Full-page context plus four generic overlapping tiles for small source text."""
    from PIL import Image
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(prefix='kac-vision-') as directory:
        views = []
        for page, path in enumerate(images):
            views.append(str(path))
            with Image.open(path) as source:
                img = source.convert('RGB')
            width,height = img.size
            if min(width,height) < 600:
                continue
            for tile,(left,top,right,bottom) in enumerate([
                    (0,0,.55,.55),(.45,0,1,.55),
                    (0,.45,.55,1),(.45,.45,1,1)]):
                crop=img.crop((int(width*left),int(height*top),int(width*right),int(height*bottom)))
                # Upscale pixels without inferring/altering text; provider keeps
                # more useful character detail within each image token budget.
                crop=crop.resize((crop.width*2,crop.height*2))
                crop.thumbnail((4096,4096))
                target=Path(directory)/f'page-{page}-tile-{tile}.png'
                crop.save(target)
                views.append(str(target))
        return _structure_positions(text=text,images=views)


def _cloudru_page_positions(path: str) -> list[dict]:
    from ..recognition_comparison import compare_position_readings, position_number
    # Separate pages prevent one long response from silently losing later rows.
    raw = _structure_positions(images=[path])
    if not raw or any(not isinstance(item, dict) for item in raw):
        raise ModelOutputInvalid('invalid_rows',output_kind='positions')
    for item in raw:
        item['number'] = position_number(item)
        item['_source_reader'] = 'vision'
    independent = None
    independent_problem = ''
    if settings.cloudru_vision_verify:
        from ..vision_views import page_bands
        try:
            # One additional image call per page, no text/local OCR fallback.
            # Do not feed primary values to this separate reading.
            with page_bands(path) as views:
                independent = _structure_positions(images=views)
            if not independent or any(not isinstance(item, dict) for item in independent):
                raise ModelOutputInvalid('invalid_rows',output_kind='positions')
            raw = compare_position_readings(
                raw, independent, independent_label='увеличенные фрагменты')
        except LLMAccountError as exc:
            # Preserve the already paid primary reading without accepting it.
            for item in raw:
                fields = item.get('review_fields')
                fields = dict(fields) if isinstance(fields, dict) else {}
                reason = 'Дополнительное чтение остановлено отказом провайдера; сверить оригинал'
                fields['source'] = _join_review_reasons(fields.get('source'), reason)
                item['review_fields'] = fields
                item['review_issue'] = _join_review_reasons(item.get('review_issue'), reason)
            exc.partial_rows = raw
            raise
        except (LLMUnavailable, OSError):
            independent = None
            independent_problem = 'Проверка увеличенных фрагментов не завершена; сверить оригинал'
    # These are reviewable readings, never automatically verified truth.
    for item in raw:
        fields = item.get('review_fields')
        fields = dict(fields) if isinstance(fields, dict) else {}
        reason = 'Строка прочитана с изображения; совпадение чтений не подтверждает точность исходника'
        if independent is None:
            reason += ('; проверка увеличенных фрагментов не завершена'
                       if settings.cloudru_vision_verify else
                       '; дополнительное чтение фрагментов выключено')
            if independent_problem:
                reason += ': ' + independent_problem
        fields['source'] = _join_review_reasons(fields.get('source'), reason)
        item['review_fields'] = fields
        item['review_issue'] = _join_review_reasons(item.get('review_issue'), reason)
    return raw


def _cloudru_positions(images: list[str], outcomes: list[dict] | None = None) -> list[dict]:
    if settings.cloudru_spec_reader == 'ocr':
        return _cloudru_ocr_positions(images)
    if settings.cloudru_spec_reader != 'vision':
        raise LLMUnavailable('Cloud.ru: CLOUDRU_SPEC_READER должен быть vision или ocr')
    from ..recognition_comparison import position_number
    rows = []
    halted = False
    for page, path in enumerate(images, 1):
        if outcomes is None:
            rows.extend(_cloudru_page_positions(path))
            continue
        from ..parse_evidence import page_evidence
        try:
            evidence = page_evidence(path, page)
        except OSError:
            outcomes.append({'page': page, 'status': 'not_attempted' if halted else 'failed',
                             'code': 'account_stopped' if halted else 'source_image_unreadable',
                             'position_count': 0})
            continue
        evidence.update(reader='cloudru_vision', model=settings.cloudru_model_vision)
        outcome = {**evidence, 'status': 'not_attempted' if halted else 'done',
                   'code': 'account_stopped' if halted else '', 'position_count': 0}
        if halted:
            outcomes.append(outcome)
            continue
        raw = []
        try:
            raw = _cloudru_page_positions(path)
        except LLMAccountError as exc:
            raw = getattr(exc, 'partial_rows', [])
            outcome.update(status='partial' if raw else 'failed', code=exc.code)
            halted = True
        except (LLMUnavailable, OSError, ValueError):
            outcome.update(status='failed', code='recognition_page_failed')
        models={item.get('_source_model_metadata',{}).get('model') for item in raw if isinstance(item.get('_source_model_metadata'),dict)}
        if len(models)==1:
            actual_model=next(iter(models))
            if isinstance(actual_model,str):
                evidence['model']=actual_model;outcome['model']=actual_model
                outcome['model_attempts']=raw[0]['_source_model_metadata'].get('attempts',[])
        outcome['position_count'] = len(raw)
        for row_index, item in enumerate(raw, 1):
            item['_source_evidence'] = {**evidence, 'row_index': row_index,
                    'reading_kind': 'interpreted_row',
                    'reading': {key: item.get(key) for key in
                                ('number', 'name', 'type_mark', 'maker', 'unit', 'qty')}}
        rows.extend(raw)
        outcomes.append(outcome)
    # Number collisions across pages also require review; never deduplicate silently.
    from collections import Counter
    counts = Counter(position_number(row) for row in rows)
    for row in rows:
        if counts[position_number(row)] > 1:
            reason = 'Номер позиции повторяется на страницах; сверить строки'
            row['review_fields']['source'] = _join_review_reasons(row['review_fields']['source'], reason)
            row['review_issue'] = _join_review_reasons(row['review_issue'], reason)
    return rows


def _cloudru_ocr_positions(images: list[str]) -> list[dict]:
    from ..cloudru import recognize_page
    pages = [recognize_page(path) for path in images]
    text = '\n\n'.join(f'Page {i + 1}:\n{page}' for i, page in enumerate(pages))
    if len(text) > 120000:
        raise LLMUnavailable('OCR-текст превышает лимит обработки; разделите спецификацию')
    raw = _structure_positions(text=text)
    if not raw:
        raise ModelOutputInvalid('invalid_rows',output_kind='positions')
    if any(not isinstance(item, dict) for item in raw):
        raise ModelOutputInvalid('invalid_rows',output_kind='positions')
    warning = 'Позиция извлечена из OCR; сверить артикул, количество и единицу с изображением'
    for item in raw:
        if isinstance(item, dict):
            if 'number' in item:
                value = item['number']
                if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
                    raise ModelOutputInvalid('invalid_rows',output_kind='positions')
                try:
                    number = int(value)
                except (TypeError, ValueError, OverflowError):
                    raise ModelOutputInvalid('invalid_rows',output_kind='positions') from None
                if number <= 0:
                    raise ModelOutputInvalid('invalid_rows',output_kind='positions')
            item['review_issue'] = '; '.join(filter(None, [str(item.get('review_issue') or ''), warning]))
            fields = item.get('review_fields')
            fields = dict(fields) if isinstance(fields, dict) else {}
            fields['source'] = '; '.join(filter(None, [str(fields.get('source') or ''), warning]))
            item['review_fields'] = fields
    # Local evidence is independent from the paid OCR. Never replace its readings
    # or perform an additional LLM request to make the comparison.
    if ocr_available():
        try:
            local_pages = [ocr_image(path) for path in images]
        except Exception:
            # This optional native backend has platform-specific exceptions.
            # Preserve provisional rows and do not expose backend error details.
            local_pages = []
        local_text = '\n\n'.join(f'--- Страница {i + 1} ---\n{page}'
                                  for i, page in enumerate(local_pages))
        if local_pages and all(page.strip() for page in local_pages):
            raw = _compare_visual_cells(raw, local_text)
        else:
            reason = 'Независимый локальный OCR не прочитал все страницы; сверить поля с оригиналом'
            for item in raw:
                item['review_issue'] += '; ' + reason
                item['review_fields']['source'] += '; ' + reason
    return raw


def _cloudru_pdf_pages(path: str | Path) -> list[str]:
    # The existing renderer has a 12-page default. Never silently lose page 13+.
    with fitz.open(path) as document:
        if len(document) > 12:
            raise LLMUnavailable('Cloud.ru OCR: PDF содержит больше 12 страниц; разделите файл')
    return render_pages(path, dpi=300, max_pages=12)


def _native_header(value: str) -> str:
    return re.sub(r"[^a-zа-я0-9]", "", value.casefold().replace("ё", "е"))


def _native_columns(row: list) -> dict[str, int] | None:
    """Identify semantic columns, including merged cells, without fixed indices."""
    columns = {}
    for index, cell in enumerate(row):
        header = _native_header(str(cell or ""))
        field = None
        if header in {"позиция", "поз", "номерпозиции", "position", "item", "itemno"}:
            field = "number"
        elif header.startswith("наименование") or header in {"description", "productname"}:
            field = "name"
        elif header in {"типмарка", "марка", "тип", "модель", "model", "type"}:
            field = "model"
        elif header.startswith("кодоборудования") or header in {"код", "артикул", "article", "sku"}:
            field = "article"
        elif "изготовитель" in header or header in {"manufacturer", "maker"}:
            field = "maker"
        elif header.startswith("единица") or header in {"едизм", "unit", "uom"}:
            field = "unit"
        elif header in {"колво", "количество", "qty", "quantity"}:
            field = "qty"
        if field:
            if field in columns:
                return None
            columns[field] = index
    return columns if {"number", "name", "unit", "qty"} <= columns.keys() else None


def _native_framing_lines(page, material_bounds):
    """Positive native drawing-field recognition, never a blanket footer region."""
    if not material_bounds:
        return []
    material_bottom = max(bounds.y1 for bounds in material_bounds)
    lines = []
    for block in page.get_text('dict')['blocks']:
        if block.get('type') != 0:
            continue
        for line in block['lines']:
            text = ' '.join(''.join(span['text'] for span in line['spans']).split())
            bounds = fitz.Rect(line['bbox'])
            if text and bounds.y0 >= material_bottom:
                lines.append((text, bounds))
    words = [word for word in page.get_text('words') if word[1] >= material_bottom]
    date_headers = [fitz.Rect(w[:4]) for w in words
                    if re.fullmatch(r'(?:Дата|Date)', str(w[4]), re.I)]
    text = ' '.join(t for t, _ in lines)
    signature_grid = (bool(date_headers)
        and bool(re.search(r'Подпись|Signature', text, re.I))
        and bool(re.search(r'Изм\.?|\bRev(?:ision)?\b', text, re.I)))
    roles = [(t, b) for t, b in lines if re.fullmatch(
        r'(?:Утв\.?|Разработал|Разраб\.?|Проверил|Пров\.?|ГИП|Н\.?\s*контр\.?|'
        r'Approved|Drawn|Checked)', t, re.I)] if signature_grid else []
    result = []
    def retain(text, bounds, kind):
        result.append({'text': text, 'bbox': list(bounds), 'kind': kind})
    for text, bounds in lines:
        if re.fullmatch(r'(?:Формат|Format)\s+[AА][0-6]', text, re.I):
            retain(text, bounds, 'format')
        elif re.fullmatch(r'Инв\.?\s*(?:№|N(?:o)?\.?)\s*(?:подл\.?|дубл\.?|взам\.?)|'
                          r'Inventory\s+No\.?', text, re.I):
            retain(text, bounds, 'inventory')
        elif signature_grid and re.fullmatch(r'(?:Rev(?:ision)?|Signature|Date|Sheet|Stage|Изм\.?|Кол\.уч\.?|Дата|Подпись|Лист(?:ов)?|Стадия)', text, re.I):
            retain(text, bounds, 'signature_header')
        elif any(text == role and bounds == role_bounds for role, role_bounds in roles):
            retain(text, bounds, 'signature_role')
        elif roles:
            # Values require an observed role on the same native text baseline,
            # with the date/name columns bounded by the actual grid labels.
            for _, role_bounds in roles:
                headers = [b for b in date_headers if b.y1 <= role_bounds.y0
                           and b.x0 > role_bounds.x1]
                if not headers or min(bounds.y1, role_bounds.y1) <= max(bounds.y0, role_bounds.y0):
                    continue
                date_column = min(headers, key=lambda b: b.x0)
                if re.fullmatch(r'(?:\d{2}\.\d{2}(?:\.\d{2,4})?|\d{4}-\d{2}-\d{2})', text) and bounds.x0 < date_column.x1 and bounds.x1 > date_column.x0:
                    retain(text, bounds, 'signature_date')
                    break
                if (bounds.x0 >= role_bounds.x1 and bounds.x1 <= date_column.x0
                        and re.fullmatch(r'[А-ЯЁA-Z][а-яёa-z-]+(?:\s+[А-ЯЁA-Z]\.){0,2}', text)):
                    retain(text, bounds, 'signature_name')
                    break
    caption = r'(?:Спецификация\s+оборудования,?\s+изделий\s+и\s+материалов\.?|Specification\s+of\s+equipment,?\s+products\s+and\s+materials\.?)'
    for text, bounds in lines:
        if not re.match(r'^(?:Спецификация|Specification)\b', text, re.I):
            continue
        if re.fullmatch(caption, text, re.I):
            retain(text, bounds, 'document_caption')
        else:
            for tail, tail_bounds in lines:
                if (tail_bounds.y0 >= bounds.y1 and tail_bounds.y0 - bounds.y1 <= bounds.height
                        and tail_bounds.x0 >= bounds.x0 and tail_bounds.x1 <= bounds.x1
                        and re.fullmatch(caption, text + ' ' + tail, re.I)):
                    retain(text, bounds, 'document_caption')
                    retain(tail, tail_bounds, 'document_caption_continuation')
    return result


def _native_table_positions(path, outcomes=None):
    """Read supported text tables; return None rather than omit unsupported pages."""
    all_rows, pages = [], []
    with fitz.open(path) as doc:
        for page in doc:
            if not page.get_text().strip():
                # An image-only page cannot be declared empty or silently omitted.
                if page.get_images() or page.get_drawings():
                    return None
                continue
            page_rows, supported, accepted_bounds, material_bounds, unassigned = [], False, [], [], []
            for table_index, table in enumerate(page.find_tables().tables):
                extracted = table.extract()
                headers = [(i, _native_columns(row)) for i, row in enumerate(extracted)]
                headers = [(i, cols) for i, cols in headers if cols]
                if not headers:
                    # Unknown numbered tables may contain omitted specification rows.
                    if any(any(re.fullmatch(r"[0-9]+", str(c or "").strip())
                               for c in row) for row in extracted):
                        return None
                    continue
                supported = True
                accepted_bounds.append(fitz.Rect(table.bbox) + (-2, -2, 2, 2))
                header_index, columns = headers[0]
                if any(any(str(cell or "").strip() for cell in row)
                       for row in extracted[:header_index]):
                    return None
                for row_index, row in enumerate(extracted[header_index + 1:], header_index + 1):
                    def value(field):
                        index = columns.get(field)
                        return " ".join(str(row[index] or "").split()) if index is not None else ""
                    number = value("number")
                    if not re.fullmatch(r"[0-9]+", number) or int(number) <= 0:
                        # Titles and framing are unnumbered. A material continuation
                        # cannot be confidently assigned and must use the fallback.
                        if _native_columns(row):
                            continue
                        if any(value(field) for field in ("model", "article", "unit", "qty", "maker")):
                            return None
                        if number:
                            if value("name") or re.fullmatch(r"[+-]?[0-9]+(?:[.,][0-9]+)?", number):
                                return None
                            # Unknown number-column-only text is retained, never
                            # inferred to be a drawing footer or material item.
                            unassigned.append({"page": page.number + 1, "table": table_index,
                                "row": row_index, "text": number, "original_cell": "number",
                                "bbox": list(table.rows[row_index].bbox)})
                        elif value("name"):
                            # Centering alone cannot distinguish a category from
                            # an unassigned material continuation. Preserve both.
                            unassigned.append({"page": page.number + 1, "table": table_index,
                                "row": row_index, "text": value("name"),
                                "bbox": list(table.rows[row_index].bbox)})
                        continue
                    material_bounds.append(fitz.Rect(table.rows[row_index].bbox))
                    issues = {}
                    quantity = value("qty")
                    try:
                        qty = float(quantity.replace(" ", "").replace(",", "."))
                        if not math.isfinite(qty) or qty <= 0:
                            raise ValueError
                    except ValueError:
                        qty = 0
                        issues["qty"] = "Количество в ячейке PDF не определено; сверить исходную строку"
                    name, unit = value("name"), value("unit")
                    for field, text in (("name", name), ("unit", unit)):
                        if not text:
                            issues[field] = "Пустая ячейка PDF; сверить исходную строку"
                    marks = list(dict.fromkeys(v for v in (value("model"), value("article")) if v))
                    page_rows.append({"number": int(number), "name": name,
                        "type_mark": " / ".join(marks), "maker": value("maker"),
                        "unit": unit, "qty": qty, "review_fields": issues,
                        "_source_reader": "native_pdf",
                        "_source_evidence": {"reader": "native_pdf", "page_id": f"page-{page.number + 1}",
                            "page": page.number + 1, "table": table_index, "row": row_index,
                            "cells": {key: value(key) for key in columns}}})
            if not supported or not page_rows:
                return None
            # A supported table is not proof that it contains the whole page.
            # Unknown outside text may be another item or a continuation;
            # conservatively retain the whole-document fallback for that layout.
            framing = _native_framing_lines(page, material_bounds)
            if any(not any(bounds.contains(fitz.Rect(word[:4])) for bounds in accepted_bounds)
                   and not any(fitz.Rect(field['bbox']).contains(fitz.Rect(word[:4])) for field in framing)
                   for word in page.get_text("words") if str(word[4]).strip()):
                return None
            from ..source_notes import native_document_note
            notes = [native_document_note(item) for item in unassigned]
            if notes:
                for row in page_rows:
                    row['_source_evidence']['unassigned_text'] = list(unassigned)
                    row['_source_evidence']['document_notes'] = notes
            all_rows.extend(page_rows)
            pages.append({"unassigned_text": unassigned, "document_notes": notes,
                          "page_id": f"page-{page.number + 1}", "page": page.number + 1,
                          "status": "done", "code": "", "position_count": len(page_rows),
                          "reader": "native_pdf", "framing": framing})
    numbers = [row["number"] for row in all_rows]
    if not numbers or len(numbers) != len(set(numbers)):
        return None
    if outcomes is not None:
        outcomes.extend(pages)
    return all_rows


def _raw_positions_from_pdf(path: str | Path, outcomes: list[dict] | None = None) -> list[dict]:
    text, is_scanned = extract_raw_text(path)
    if not is_scanned:
        native = _native_table_positions(path, outcomes)
        return native if native is not None else _structure_positions(text=text)
    if settings.llm_provider.lower() == 'cloudru':
        return _cloudru_positions(_cloudru_pdf_pages(path), outcomes)
    # Native OCR supplies independent cell evidence, never replaces vision readings.
    ocr_text = ocr_pages(path) if ocr_available() else ""
    if settings.llm_provider.lower() == 'deepseek':
        return _visual_positions(ocr_text,render_pages(path, dpi=300))
    if ocr_text.strip():
        return _structure_positions(text=ocr_text)
    if settings.llm_provider.lower() == "anthropic":
        return _structure_positions(images=render_pages(path))  # фолбэк на vision
    raise LLMUnavailable(
        "Скан без текстового слоя, локальный OCR недоступен, а провайдер "
        f"{settings.llm_provider} не умеет распознавать изображения. Установите Tesseract "
        "(brew install tesseract tesseract-lang) или используйте текстовый PDF/DOCX/XLSX.")


def _raw_positions_from_image(path: str | Path, outcomes: list[dict] | None = None) -> list[dict]:
    if settings.llm_provider.lower() == 'cloudru':
        return _cloudru_positions([str(path)], outcomes)
    text = ocr_image(path) if ocr_available() else ""
    if settings.llm_provider.lower() == 'deepseek':
        return _visual_positions(text,[str(path)])
    if text.strip():
        return _structure_positions(text=text)
    if settings.llm_provider.lower() == "anthropic":
        return _structure_positions(images=[str(path)])
    raise LLMUnavailable(
        "Изображение не удалось распознать локальным OCR; проверьте качество файла "
        "или используйте текстовый PDF/DOCX/XLSX.")


def _raw_positions(path: str | Path, outcomes: list[dict] | None = None) -> list[dict]:
    """Извлекает сырой массив позиций из поддерживаемого файла спецификации."""
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        return _raw_positions_from_pdf(path, outcomes)
    if suffix in (".jpg", ".jpeg", ".png", ".tif", ".tiff"):
        return _raw_positions_from_image(path, outcomes)
    if suffix == ".docx":
        return _structure_positions(text=text_from_docx(path))
    if suffix in (".xlsx", ".xlsm", ".xls"):
        return _structure_positions(text=text_from_xlsx(path))
    raise LLMUnavailable(
        f"Неподдерживаемый формат спецификации: {suffix or 'без расширения'}. "
        "Принимаются PDF, JPG, PNG, TIFF, DOCX, XLSX.")


def parse_spec(
    path: str | Path,
    discipline: str = "ЭМ",
    unique_names: set[str] | None = None,
    page_outcomes: list[dict] | None = None,
) -> list[SpecPosition]:
    """Парсит спецификацию (PDF/изображение/DOCX/XLSX) в список позиций.

    unique_names — подстроки наименований, помечаемые как уникальное оборудование.
    Дополнительно: завод-изготовитель из CUSTOM_MAKERS → авто-пометка уникальным.
    """
    raw = (_raw_positions(path) if page_outcomes is None
           else _raw_positions(path, page_outcomes))

    unique_names = unique_names or set()
    positions: list[SpecPosition] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        # страховка от заголовков разделов: нет количества, единицы и марки → это не позиция
        raw_unit = str(item.get("unit") or "").strip()
        raw_mark = str(item.get("type_mark") or "").strip()
        try:
            raw_qty = float(item.get("qty", 0) or 0)
        except (TypeError, ValueError):
            raw_qty = 0
        if not math.isfinite(raw_qty) or raw_qty < 0:
            raw_qty = 0
        explicit_number = str(item.get('number') or '').isdigit() and int(item['number']) > 0
        independent_candidate = (item.get('_independent_only') is True and explicit_number
                                 and isinstance(item.get('review_fields'), dict)
                                 and bool(item['review_fields'].get('source')))
        if (raw_qty <= 0 and not raw_unit and not raw_mark
                and not (explicit_number and (not name or item.get("_source_reader") == "native_pdf"))
                and not independent_candidate):
            if item.get('_model_output'):
                raise ModelOutputInvalid('invalid_rows',output_kind='positions')
            continue
        if not name and not str(item.get('number') or '').isdigit():
            if item.get('_model_output'):
                raise ModelOutputInvalid('invalid_rows',output_kind='positions')
            continue
        maker = str(item.get("maker") or "").strip()
        is_unique = (
            any(u.lower() in name.lower() for u in unique_names)
            or any(m in maker.lower() for m in settings.custom_makers_list)
            or _is_graftio_equipment(name, raw_mark, maker)
            or any(_is_graftio_equipment(name,raw_mark,m) for m in item.get('_source_makers',[]))
        )
        positions.append(SpecPosition(
            number=int(item.get("number", len(positions) + 1)),
            name=name,
            type_mark=raw_mark,
            review_issue=str(item.get("review_issue") or "").strip(),
            source_evidence=item.get("_source_evidence", {}),
            source_reader=item.get('_source_reader', '') if item.get('_source_reader') in {'vision', 'native_pdf'} else '',
            review_fields={k: str(v) for k, v in (item.get('review_fields') or {}).items()
                           if k in {'name', 'type_mark', 'unit', 'qty', 'source'} and v}
                          if isinstance(item.get('review_fields'), dict) else {},
            unit=raw_unit,
            qty=raw_qty,
            is_unique=is_unique,
            discipline=discipline,
        ))
    if page_outcomes:
        for outcome in page_outcomes:
            if 'page_id' not in outcome:
                continue
            outcome['reading_count'] = outcome['position_count']
            outcome['position_count'] = sum(
                p.source_evidence.get('page_id') == outcome['page_id'] for p in positions)
            if outcome['status'] == 'done' and not outcome['position_count']:
                outcome.update(status='failed', code='no_usable_positions')
    if not positions and settings.llm_provider.lower() == 'cloudru' and not page_outcomes:
        raise ModelOutputInvalid('invalid_rows',output_kind='positions')
    return positions
