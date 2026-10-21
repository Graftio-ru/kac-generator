"""Cross-reader evidence by position number, independent of table coordinates."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from .llm import ModelOutputInvalid
from .source_codes import source_code_key

_FIELD_LABELS = {'name': 'наименования', 'type_mark': 'марки/артикула',
                 'unit': 'единицы измерения', 'qty': 'количества',
                 'maker': 'изготовителя'}


def position_number(row):
    value = row.get('number')
    if isinstance(value, bool):
        raise ModelOutputInvalid('invalid_rows',output_kind='positions')
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0 or number != number.to_integral_value():
            raise ValueError
        return int(number)
    except (InvalidOperation, ValueError, TypeError):
        raise ModelOutputInvalid('invalid_rows',output_kind='positions') from None


def _warn(row, field, reason):
    fields = row.get('review_fields')
    fields = dict(fields) if isinstance(fields, dict) else {}
    prior = fields.get(field)
    fields[field] = '; '.join(dict.fromkeys(str(v) for v in (prior, reason) if v))
    row['review_fields'] = fields
    row['review_issue'] = '; '.join(dict.fromkeys(str(v) for v in
        (row.get('review_issue'), reason) if v))


def _key(field, value):
    if field == 'qty':
        if isinstance(value, bool):
            return None
        try:
            result = Decimal(str(value).replace(',', '.'))
            return result if result.is_finite() and result > 0 else None
        except InvalidOperation:
            return None
    text = str(value or '').strip()
    if field == 'type_mark':
        return source_code_key(text)
    return ' '.join(text.casefold().split()).rstrip('.')


def compare_position_readings(primary, independent, *, independent_label="локальный OCR"):
    """Keep primary values; extra OCR rows remain explicitly provisional.

    Agreement does not certify correctness: optical readers may share mistakes.
    Duplicates cannot be paired safely and receive a source warning.
    """
    from collections import defaultdict
    if any(not isinstance(row, dict) for row in primary):
        raise ModelOutputInvalid('invalid_rows',output_kind='positions')
    primary = deepcopy(primary)
    observed = defaultdict(list)
    counts = defaultdict(int)
    unpaired = []
    for row in independent:
        try:
            number = position_number(row) if isinstance(row, dict) else None
            if number is None:
                raise ModelOutputInvalid('invalid_rows',output_kind='positions')
        except ModelOutputInvalid:
            unpaired.append(str(row.get('name') or 'нечитаемая строка')[:100]
                            if isinstance(row, dict) else 'нечитаемая строка')
            continue
        observed[number].append(row)
    for row in primary:
        row['number'] = position_number(row)
        counts[row['number']] += 1
    for row in primary:
        number = position_number(row)
        if unpaired:
            _warn(row, 'source', f'{len(unpaired)} строк второго чтения ({independent_label}) без надёжного номера '
                  'не сопоставлены: ' + ' / '.join(unpaired))
        candidates = observed.get(number, [])
        if counts[number] != 1 or len(candidates) > 1:
            _warn(row, 'source', 'Номер позиции повторяется; чтения нельзя надёжно сопоставить')
            continue
        if not candidates:
            _warn(row, 'source', f'Позиция не найдена во втором чтении ({independent_label}); сверить строку с оригиналом')
            continue
        other = candidates[0]
        for field in ('name', 'type_mark', 'unit', 'qty', 'maker'):
            key, other_key = _key(field, row.get(field)), _key(field, other.get(field))
            # Maker and mark may be genuinely absent from a specification.
            # Missing required name/unit/quantity remains review evidence.
            if key != other_key or (field not in {'type_mark', 'maker'} and not other_key):
                label = 'source' if field == 'maker' else field
                _warn(row, label, f'Чтения {_FIELD_LABELS[field]} расходятся или неполны: '
                      f'изображение «{row.get(field, "")}», {independent_label} «{other.get(field, "")}»; сверить оригинал')
        fields = other.get('review_fields')
        if isinstance(fields, dict):
            for field, reason in fields.items():
                if field in {'name', 'type_mark', 'unit', 'qty', 'source'} and reason:
                    _warn(row, field, f'Второе чтение ({independent_label}): {reason}')
        # Keep independent maker evidence for conservative own-product exclusion.
        row['_source_makers'] = [str(other['maker'])] if other.get('maker') else []
    for number, candidates in observed.items():
        if number in counts:
            continue
        for candidate in candidates:
            row = deepcopy(candidate)
            row['number'] = number
            row['_independent_only'] = True
            row.pop('_source_reader', None)
            row.pop('_source_makers', None)
            _warn(row, 'source', f'Строка найдена только независимым чтением ({independent_label}); проверить наличие и все поля в оригинале')
            if len(candidates) > 1:
                _warn(row, 'source', 'Номер позиции повторяется в независимом OCR')
            primary.append(row)
    return primary
