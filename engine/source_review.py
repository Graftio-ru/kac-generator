"""Explicit recognition issues; image provenance alone is not an issue."""
import re
import math

LABELS = {'name': 'Наименование', 'type_mark': 'Артикул/марка',
          'unit': 'Единица', 'qty': 'Количество', 'source': 'Исходная строка'}


def recognition_fields(position, *, scanned=False):
    issues = {key: str(value).strip() for key, value in position.review_fields.items()
              if key in LABELS and str(value).strip()}
    detail = position.review_issue.strip()
    if detail:
        matched = False
        for key, pattern in [('type_mark', r'артикул|марк|штамп'),
                             ('qty', r'количеств'), ('unit', r'единиц'),
                             ('name', r'наименован')]:
            if re.search(pattern, detail, re.I):
                issues.setdefault(key, detail)
                matched = True
        if not matched and detail not in issues.values():
            issues.setdefault('source', detail)
    if not position.name.strip():
        issues['name'] = 'Не распознано наименование'
    if not position.unit.strip():
        issues['unit'] = 'Не распознана единица измерения'
    if not math.isfinite(position.qty) or position.qty <= 0:
        issues['qty'] = 'Не распознано положительное количество'
    if re.search(r'согласовано|архангельское|заместитель|производств', position.type_mark, re.I):
        issues['type_mark'] = 'Артикул смешан с текстом штампа'
    # Identical OCR passes do not independently verify an alphanumeric code:
    # both can read D as 0, I as 1 or S as 5. Keep that risk explicit rather
    # than silently substituting characters or borrowing catalogue values.
    mark = position.type_mark
    numeric_catalogue = bool(re.fullmatch(r'\d+[A-Za-zА-Яа-я]{0,3}', mark.strip()))
    # A visual model and native OCR can independently repeat the same 0/D/O
    # mistake. Optical script agreement is not verification of these glyphs.
    ambiguous = r'[0OoОо]' if position.source_reader == 'vision' else r'[01258OIlS]'
    if (scanned and not numeric_catalogue
            and re.search(r'[A-Za-zА-Яа-я]', mark) and re.search(ambiguous, mark)):
        issues.setdefault('type_mark', 'Буквенно-цифровая марка содержит визуально неоднозначные '
                          'символы распознавания; точную запись нужно сверить по исходнику')
    if scanned and position.unit.strip() and position.unit.lower().strip().rstrip('.') not in {
            'м', 'шт', 'компл', 'кг', 'уп', 'упак', 'т', 'л', 'м2', 'м3', 'м²', 'м³',
            'пог.м', 'км', 'ч', 'ед', 'пар', 'набор', 'рулон', 'бухта', 'пач', 'маш.-ч'}:
        issues.setdefault('unit', f'Не подтверждена единица OCR «{position.unit}»; сверить обозначение')
    return issues


def review_summary(fields):
    return '; '.join(f'{LABELS.get(key, key)}: {value}' for key, value in fields.items())


def review_pending(position):
    return bool(position.needs_review or position.review_fields) and not (
        position.review_accepted and position.review_issue.strip())
