"""Offline reader controls. Never feed expected values back into recognition."""
from decimal import Decimal, InvalidOperation

from .cell_transcription import validate_readings


def evaluate_reader(response, labels, controls):
    """Score available readings even when the batch fails structural validation.

    Passing controls qualifies only this finite control set, not an entire reader.
    ``labels`` contains all requested cells; controls may cover a strict subset.
    """
    labels = list(labels)
    if (not labels or any(not isinstance(label, str) or not label for label in labels)
            or len(set(labels)) != len(labels)):
        raise ValueError('requested cell IDs must be unique nonempty strings')
    if not isinstance(controls, list) or not controls:
        raise ValueError('independent controls must be a nonempty list')
    seen = set()
    for control in controls:
        if (not isinstance(control, dict) or set(control) != {'id', 'expected', 'kind'}
                or not isinstance(control['id'], str) or control['id'] not in labels
                or control['id'] in seen or not isinstance(control['expected'], str)
                or control['kind'] not in ('literal', 'number')):
            raise ValueError('invalid independent control')
        seen.add(control['id'])
        if control['kind'] == 'number':
            try:
                value = Decimal(control['expected'].replace(',', '.'))
            except InvalidOperation:
                raise ValueError('numeric control must be finite') from None
            if not value.is_finite():
                raise ValueError('numeric control must be finite')

    readings, issues = validate_readings(response, labels)
    checks = []
    for control in controls:
        reading = readings[control['id']]
        observed = reading['text']
        readable = reading['state'] in ('read', 'blank')
        matches = readable and observed == control['expected']
        if readable and control['kind'] == 'number':
            try:
                value = Decimal(observed.replace(',', '.'))
                matches = value.is_finite() and value == Decimal(control['expected'].replace(',', '.'))
            except InvalidOperation:
                matches = False
        checks.append({**control, 'observed': observed, 'state': reading['state'],
                       'issue': reading['issue'], 'ok': matches,
                       'unflagged_error': readable and not matches})
    return {'status': 'controls_passed' if not issues and all(c['ok'] for c in checks) else 'rejected',
            'requested_cells': len(labels), 'controlled_cells': len(checks),
            'uncontrolled_cells': len(labels) - len(checks),
            'passed_controls': sum(c['ok'] for c in checks),
            'unflagged_errors': sum(c['unflagged_error'] for c in checks),
            'structural_issues': issues, 'checks': checks,
            'reader_admitted': False}
