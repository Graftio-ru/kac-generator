"""Nonblocking native document observations, distinct from row recognition issues."""
import math

NOTE_VERSION = 'native-document-notes-v1'
NOTE_CODE = 'native_name_only_fragment'
NUMBER_TEXT_CODE = 'native_unnumbered_number_text'

def native_document_note(fragment):
    cell = fragment.get('original_cell', 'name')
    code = NOTE_CODE if cell == 'name' else NUMBER_TEXT_CODE
    return {'version': NOTE_VERSION, 'producer': 'native_table_positions',
            'code': code, 'scope': 'document', 'severity': 'warning', **fragment}

def native_name_only_note(fragment):
    return native_document_note(fragment)

def valid_document_note(note):
    if not isinstance(note, dict) or any(note.get(k) != v for k, v in {
            'version': NOTE_VERSION, 'producer': 'native_table_positions',
            'scope': 'document', 'severity': 'warning'}.items()):
        return False
    if not ((note.get('code') == NOTE_CODE and note.get('original_cell', 'name') == 'name')
            or (note.get('code') == NUMBER_TEXT_CODE and note.get('original_cell') == 'number')):
        return False
    if any(type(note.get(k)) is not int or note[k] < (1 if k == 'page' else 0)
           for k in ('page', 'table', 'row')):
        return False
    box = note.get('bbox')
    return (isinstance(note.get('text'), str) and bool(note['text'].strip())
            and isinstance(box, list) and len(box) == 4
            and all(type(v) in (int, float) and math.isfinite(v) for v in box)
            and box[2] > box[0] and box[3] > box[1])

def document_notes(positions):
    """Read only new producer notes; old row warnings are never reclassified."""
    result, seen = [], set()
    for value in positions:
        position = getattr(value, 'position', value)
        evidence = getattr(position, 'source_evidence', {})
        if not isinstance(evidence, dict) or evidence.get('reader') != 'native_pdf':
            continue
        source_hash, source_index = evidence.get('source_hash', ''), evidence.get('source_index')
        if not isinstance(source_hash, str) or source_index is not None and type(source_index) is not int:
            continue
        notes = evidence.get('document_notes', [])
        if not isinstance(notes, list):
            continue
        for note in notes:
            if not valid_document_note(note):
                continue
            key = (source_hash, source_index,
                   note['page'], note['table'], note['row'], note['text'], tuple(note['bbox']))
            if key not in seen:
                seen.add(key); result.append(dict(note, source_hash=str(evidence.get('source_hash', '')),
                                              source_index=evidence.get('source_index')))
    return result

def note_description(note):
    role = 'безномерный текст из графы номера, назначение не определено' if note.get('code') == NUMBER_TEXT_CODE else 'безномерный текст из графы наименования'
    return f"{role}. Стр. {note['page']}, таблица {note['table']}, строка {note['row']}: {note['text']}"
