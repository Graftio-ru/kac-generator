"""Offline ruled-table geometry. Cells are evidence regions, not recognized fields.

No OCR/model, catalogue values or fixed specification columns are used. Skewed
and borderless pages fail explicitly; additional ruled regions remain unresolved.
"""
from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from statistics import median

from PIL import Image

LAYOUT_VERSION = 'ruled-grid-v1'


def _dark_runs(values, gap):
    start = last = None
    runs = []
    for index, value in enumerate(values):
        if value:
            if start is None or index - last > gap + 1:
                if start is not None:
                    runs.append((start, last + 1))
                start = index
            last = index
    if start is not None:
        runs.append((start, last + 1))
    return runs


def _clusters(indices):
    groups = []
    for index in indices:
        if not groups or index > groups[-1][-1] + 1:
            groups.append([])
        groups[-1].append(index)
    return [round(median(group)) for group in groups]


def _original_bounds(box, width, height, rotation):
    def point(x, y):
        if rotation == 90:
            return y, height - x
        if rotation == 180:
            return width - x, height - y
        if rotation == 270:
            return width - y, x
        return x, y
    left, top, right, bottom = box
    points = [point(x, y) for x in (left, right) for y in (top, bottom)]
    return [min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points)]


def analyze_table(path, *, rotation=0):
    """Detect one dominant grid in submitted pixel axes; mapping remains unresolved."""
    if rotation not in (0, 90, 180, 270):
        raise ValueError('rotation must be 0, 90, 180 or 270 clockwise degrees')
    path = Path(path)
    snapshot = path.read_bytes()
    digest = sha256(snapshot).hexdigest()
    base = {'version': LAYOUT_VERSION, 'source_hash': digest, 'rotation': rotation,
            'field_mapping': 'unresolved', 'rows': [], 'cells': [], 'issues': []}
    with Image.open(BytesIO(snapshot)) as source:
        if getattr(source, 'n_frames', 1) != 1:
            return {**base, 'status': 'unsupported', 'code': 'multiframe_source'}
        original_width, original_height = source.size
        exif_orientation = source.getexif().get(274, 1)
        if original_width * original_height > 16_000_000:
            return {**base, 'status': 'unsupported', 'code': 'image_pixel_limit',
                    'original_size': [original_width, original_height]}
        image = source.convert('L').rotate(-rotation, expand=True)
    width, height = image.size
    result = {**base,
              'original_size': [original_width, original_height], 'normalized_size': [width, height],
              'source_exif_orientation': exif_orientation, 'orientation_policy': 'explicit_pixel_rotation',
              'field_mapping': 'unresolved', 'rows': [], 'cells': [], 'issues': []}
    pixels = list(image.getdata())
    horizontal = []
    gap = max(1, round(width * .01))
    for y in range(height):
        for left, right in _dark_runs((v < 210 for v in pixels[y * width:(y + 1) * width]), gap):
            if right - left >= max(60, width * .35):
                horizontal.append((y, left, right))
    # Group repeatedly aligned long rules rather than treating the page frame as a table.
    groups = []
    tolerance = max(3, width * .025)
    for y, left, right in horizontal:
        group = next((g for g in groups if abs(left - g[0][1]) <= tolerance
                      and abs(right - g[0][2]) <= tolerance), None)
        if group is None:
            group = []; groups.append(group)
        group.append((y, left, right))
    candidates = []
    for group in groups:
        ys = _clusters([v[0] for v in group])
        l, r = round(median(v[1] for v in group)), round(median(v[2] for v in group))
        segments = [[]]
        for y in ys:
            if segments[-1]:
                previous = segments[-1][-1]
                span = range(previous + 2, y - 1)
                edge_present = [sum(any(pixels[yy * width + x] < 210 for x in
                                         range(max(0, edge - 1), min(width, edge + 2)))
                                    for yy in span) for edge in (l, r - 1)]
                if y - previous > 4 and max(edge_present) < max(1, y - previous - 3) * .5:
                    segments.append([])
            segments[-1].append(y)
        for segment in segments:
            if len(segment) >= 4:
                candidates.append((segment, l, r))
    candidates.sort(key=lambda c: len(c[0]), reverse=True)
    if not candidates:
        return {**result, 'status': 'unsupported', 'code': 'ruled_table_not_found'}
    ys, left, right = candidates[0]
    top, bottom = ys[0], ys[-1]
    # Partial long rules can extend into the drawing frame or be interrupted by
    # stamps/QR blocks. Recover their y evidence inside the dominant region.
    ys = _clusters([y for y, l, r in horizontal if top <= y <= bottom
                    and min(r, right) - max(l, left) >= (right - left) * .75])
    additional = [c for c in candidates[1:] if
                  c[0][0] < top - 3 or c[0][-1] > bottom + 3
                  or c[1] < left - tolerance or c[2] > right + tolerance]
    result['additional_regions'] = [
        {'bounds': [c[1], c[0][0], c[2], c[0][-1] + 1],
         'state': 'unclassified_ruled_region'} for c in additional]
    if additional:
        result['issues'].append('additional_ruled_regions_require_mapping')
    vertical = [x for x in range(left, right) if
                sum(pixels[y * width + x] < 210 for y in range(top, bottom + 1))
                >= (bottom - top + 1) * .65]
    xs = _clusters(vertical)
    if len(xs) < 3 or abs(xs[0] - left) > tolerance or abs(xs[-1] - (right - 1)) > tolerance:
        return {**result, 'status': 'unsupported', 'code': 'column_grid_not_found'}
    identity = f'{digest}:{LAYOUT_VERSION}:r{rotation}'
    for row_index, (y1, y2) in enumerate(zip(ys, ys[1:])):
        if y2 - y1 < 4:
            return {**result, 'status': 'ambiguous', 'code': 'overlapping_row_rules', 'rows': [], 'cells': []}
        row_id = f'{identity}:row{row_index}'
        row = {'row_id': row_id, 'row_index': row_index,
               'bounds': [xs[0], y1, xs[-1] + 1, y2 + 1], 'cell_ids': []}
        # Missing internal separators can be a merged cell or an obscured line.
        # Retain that ambiguity instead of asserting independent field cells.
        dividers = [0]
        for column in range(1, len(xs) - 1):
            x = xs[column]
            present = sum(any(pixels[y * width + probe] < 210
                              for probe in range(max(0, x - 1), min(width, x + 2)))
                          for y in range(y1 + 2, y2 - 1))
            if present >= max(1, y2 - y1 - 3) * .6:
                dividers.append(column)
        dividers.append(len(xs) - 1)
        for c1, c2 in zip(dividers, dividers[1:]):
            cell_id = f'{row_id}:col{c1}-{c2}'
            box = [xs[c1], y1, xs[c2] + 1, y2 + 1]
            cell = {'cell_id': cell_id, 'row_id': row_id, 'column_start': c1,
                    'column_span': c2 - c1, 'bounds': box,
                    'original_bounds': _original_bounds(box, original_width, original_height, rotation),
                    'state': 'unread', 'geometry_issue': 'merged_or_missing_divider' if c2 - c1 > 1 else ''}
            result['cells'].append(cell); row['cell_ids'].append(cell_id)
        result['rows'].append(row)
    result.update(status='geometry_detected', code='', x_rules=xs, y_rules=ys,
                  table_bounds=[xs[0], top, xs[-1] + 1, bottom + 1])
    return result


def export_crops(path, layout, directory, *, padding=2):
    """Save normalized source pixels plus explicit crop origins; originals untouched."""
    if layout.get('status') != 'geometry_detected':
        raise ValueError('only detected geometry can be exported')
    if padding < 0:
        raise ValueError('padding must be nonnegative')
    snapshot = Path(path).read_bytes()
    if sha256(snapshot).hexdigest() != layout['source_hash']:
        raise ValueError('source changed since layout analysis')
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    with Image.open(BytesIO(snapshot)) as source:
        image = source.convert('RGB').rotate(-layout['rotation'], expand=True)
        # Crops use declared pixel rotation, never inherited EXIF auto-orientation.
        image.info.clear()
    records = []
    for index, cell in enumerate(layout['cells']):
        left, top, right, bottom = cell['bounds']
        crop_box = [max(0, left - padding), max(0, top - padding),
                    min(image.width, right + padding), min(image.height, bottom + padding)]
        crop_key = sha256(f"{cell['cell_id']}:pad{padding}".encode()).hexdigest()[:24]
        target = directory / f'cell-{crop_key}.png'
        image.crop(crop_box).save(target)
        ow, oh = layout['original_size']
        l, t = crop_box[:2]
        transform = {0: [1, 0, l, 0, 1, t], 90: [0, 1, t, -1, 0, oh - l],
                     180: [-1, 0, ow - l, 0, -1, oh - t],
                     270: [0, -1, ow - t, 1, 0, l]}[layout['rotation']]
        records.append({**cell, 'coordinate_convention': 'pixel_edges_half_open', 'crop_to_original': transform, 'crop_file': target.name, 'crop_bounds': crop_box,
                        'crop_original_bounds': _original_bounds(crop_box, ow, oh, layout['rotation']),
                        'crop_size': [crop_box[2] - crop_box[0], crop_box[3] - crop_box[1]],
                        'rotation': layout['rotation'], 'scale': 1})
    return records
