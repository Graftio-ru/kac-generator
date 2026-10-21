"""Chromium-only conservative pseudo paint envelopes, not visibility certificates.

Snapshot APIs return support data. Production consumes only the current-Element
bound native_visibility fallback, never a saved diagnostic envelope.
No clipping is applied. A supported rectangle is an upper bound on simple empty
pseudo box paint; intersection must still refuse, regardless of hit/stack order.
Snapshots and their string tables remain ephemeral and must never be persisted.
"""
import math
import time
import hashlib
import json
import re

STYLE_NAMES = (
    'content', 'position', 'display', 'visibility', 'opacity', 'transform',
    'translate', 'rotate', 'scale', 'filter', 'backdrop-filter', 'box-shadow',
    'text-shadow', 'outline-style', 'border-image-source', 'clip-path',
    'mask-image', 'perspective', 'contain', 'will-change', 'zoom',
    'writing-mode', 'background-image', 'overflow-x', 'overflow-y',
    'overflow-clip-margin',
    'left', 'top', 'right', 'bottom', 'width', 'height', 'box-sizing',
    'margin-left', 'margin-right', 'margin-top', 'margin-bottom',
    'padding-left', 'padding-right', 'padding-top', 'padding-bottom',
    'border-left-width', 'border-right-width', 'border-top-width', 'border-bottom-width',
    'direction', 'column-count', 'column-width',
)


def _unknown(reason):
    return {'supported': False, 'diagnostic': 'unsupported_geometry', 'reason': reason}


def _finite(values):
    return all(type(v) in (int, float) and math.isfinite(v) for v in values)


def _rare(table):
    if not isinstance(table, dict): raise ValueError('invalid rare table')
    indexes, values = table.get('index', []), table.get('value', [])
    if (not isinstance(indexes, list) or not isinstance(values, list)
            or any(type(v) is not int or v < 0 for v in indexes + values)
            or len(indexes) != len(values) or len(set(indexes)) != len(indexes)):
        raise ValueError('invalid rare table')
    return dict(zip(indexes, values))


def _snapshot_shape(snapshot):
    """Validate consumed CDP arrays before their contents can confer authority."""
    if not isinstance(snapshot, dict): raise ValueError('invalid snapshot')
    docs, strings = snapshot.get('documents'), snapshot.get('strings')
    if (not isinstance(docs, list) or not docs or not isinstance(strings, list)
            or any(not isinstance(v, str) for v in strings)):
        raise ValueError('invalid snapshot arrays')
    for doc in docs:
        if not isinstance(doc, dict): raise ValueError('invalid document')
        n, layout = doc.get('nodes'), doc.get('layout')
        if not isinstance(n, dict) or not isinstance(layout, dict):
            raise ValueError('invalid native tables')
        arrays = [n.get(k) for k in ('parentIndex','backendNodeId','nodeType','nodeName')]
        if (any(not isinstance(a, list) for a in arrays) or not arrays[0]
                or any(len(a) != len(arrays[0]) for a in arrays)
                or any(type(v) is not int for a in arrays for v in a)):
            raise ValueError('invalid node arrays')
        count = len(arrays[0])
        parents, backend, types, names = arrays
        if (types[0] != 9 or parents[0] != -1
                or any(v not in (1,3,4,7,8,9,10,11) for v in types)
                or any(v == 9 for v in types[1:])):
            raise ValueError('invalid_node_type')
        if (any(v <= 0 for v in backend) or len(set(backend)) != count
                or any(not 0 <= v < i for i,v in enumerate(parents) if i)):
            raise ValueError('ambiguous_node_identity')
        if any(not 0 <= v < len(strings) for v in names):
            raise ValueError('invalid node name index')
        for key in ('contentDocumentIndex','pseudoType','shadowRootType'):
            rare = _rare(n.get(key, {}))
            if any(i >= count for i in rare): raise ValueError('invalid rare owner')
            if key != 'contentDocumentIndex' and any(v >= len(strings) for v in rare.values()):
                raise ValueError('invalid rare string')
        columns = [layout.get(k) for k in ('nodeIndex','bounds','styles','paintOrders')]
        if (any(not isinstance(a, list) for a in columns)
                or any(len(a) != len(columns[0]) for a in columns)):
            raise ValueError('invalid layout arrays')
        for ni, bounds, styles, paint in zip(*columns):
            if type(paint) is not int or paint < 0: raise ValueError('invalid_paint_order')
            if (type(ni) is not int or not 0 <= ni < count
                    or not isinstance(bounds, list) or len(bounds) != 4 or not _finite(bounds)
                    or bounds[2] < 0 or bounds[3] < 0
                    or not isinstance(styles, list)
                    or any(type(v) is not int or not 0 <= v < len(strings) for v in styles)):
                raise ValueError('invalid layout row')
    return docs, strings


def _guard(style, pseudo=False):
    if style.get('position') not in ('static','relative','absolute','fixed'):
        return 'unsupported_position'
    none = ('transform', 'translate', 'rotate', 'scale', 'filter',
            'backdrop-filter', 'box-shadow', 'text-shadow', 'outline-style',
            'border-image-source', 'clip-path', 'mask-image', 'perspective',
            'contain', 'background-image')
    if any(style.get(k) != 'none' for k in none):
        return 'unsupported_paint_effect'
    if style.get('will-change') != 'auto' or style.get('zoom') != '1':
        return 'unsupported_coordinate_effect'
    if style.get('writing-mode') != 'horizontal-tb':
        return 'unsupported_writing_mode'
    if style.get('visibility') != 'visible' or style.get('opacity') != '1':
        return 'unsupported_visibility_state'
    if style.get('display') in ('none', 'contents'):
        return 'unsupported_display_state'
    if pseudo and style.get('content') not in ('""', "''"):
        return 'unsupported_generated_content'
    return ''


_PSEUDO_PAINT_CATEGORIES = frozenset(('finite_box_paint','finite_inward_clip',
    'unknown_generated_paint','unknown_expanding_paint','unknown_coordinate_paint',
    'unknown_style_state'))

def _pseudo_paint_category(style, pseudo=False):
    """Classify support, never infer paint bounds from an owner's box."""
    if type(style) is not dict or any(type(style.get(k)) is not str for k in STYLE_NAMES):
        return 'unknown_style_state'
    for key in ('background-image','mask-image','clip-path'):
        value=style[key]
        if value!='none' and not (re.match(r'^(?:url|(?:repeating-)?(?:linear|radial|conic)-gradient|image-set|cross-fade|image|paint|inset|circle|ellipse|polygon|path|rect|xywh)\(',value) and value.endswith(')')):
            return 'unknown_style_state'
    if pseudo and style['content'] not in ('""', "''"):
        return 'unknown_generated_paint'
    if any(style[k]!='none' for k in ('filter','backdrop-filter','box-shadow',
             'text-shadow','outline-style','border-image-source')):
        # Border-image-outset, shadows and filters can expand painted pixels.
        return 'unknown_expanding_paint'
    if any(style[k]!='none' for k in ('transform','translate','rotate','scale',
             'perspective','contain')) or style['will-change']!='auto' or style['zoom']!='1':
        # A pseudo has no independent live Element border-box API. Native layout
        # alone is not a certificate for transformed or escaping generated paint.
        return 'unknown_coordinate_paint'
    if style['clip-path']!='none' or style['mask-image']!='none':
        return 'finite_inward_clip'
    return 'finite_box_paint'

def _finite_pseudo_guard(style, pseudo=False):
    category=_pseudo_paint_category(style,pseudo)
    if category=='unknown_style_state':return 'ambiguous_computed_styles'
    if category=='unknown_generated_paint':return 'unsupported_generated_content'
    if category in ('unknown_expanding_paint','unknown_coordinate_paint'):
        return 'unsupported_paint_effect'
    # Fill images and inward clips cannot extend an EMPTY pseudo's native border
    # fragments. Keep their full union, do not shrink by guessed clipping. Parent
    # fill images similarly do not expand this descendant's certified fragments.
    reduced={**style,'background-image':'none','clip-path':'none','mask-image':'none'}
    return _guard(reduced,pseudo)


_FRAME_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"

def _finite_frame_coordinates(style):
    """Finite 2D effects only; native/live host bounds remain the authority."""
    transform = style.get('transform')
    if transform != 'none':
        match = re.fullmatch(r'matrix\(\s*(' + _FRAME_NUMBER + r'(?:\s*,\s*' + _FRAME_NUMBER + r'){5})\s*\)', transform or '')
        if not match or not _finite([float(v) for v in match[1].split(',')]):return False
    translate = style.get('translate')
    if translate != 'none':
        parts = (translate or '').split()
        if not 1 <= len(parts) <= 2:return False
        if any(not re.fullmatch(_FRAME_NUMBER + r'(?:px|%)', v) for v in parts):return False
        if not _finite([float(re.sub(r'(?:px|%)$', '', v)) for v in parts]):return False
    rotate = style.get('rotate')
    if rotate != 'none':
        if not re.fullmatch(_FRAME_NUMBER + r'(?:deg|rad|grad|turn)', rotate or ''):return False
        if not _finite([float(re.sub(r'(?:deg|rad|grad|turn)$', '', rotate))]):return False
    scale = style.get('scale')
    if scale != 'none':
        parts = (scale or '').split()
        if not 1 <= len(parts) <= 2 or any(not re.fullmatch(_FRAME_NUMBER, v) for v in parts):return False
        if not _finite([float(v) for v in parts]):return False
    return True


def _frame_footprint_guard(style):
    """Frame content uses its full measured border bound, never guessed clips.

    Coordinate effects are supported only with live/native box agreement below.
    Main-document ancestor backgrounds are independently visibility-checked.
    Expanding/unknown painting effects cannot confer a finite child bound.
    """
    if style.get('position') not in ('static','relative','absolute','fixed','sticky'):return 'unsupported_position'
    if not _finite_frame_coordinates(style):return 'unsupported_coordinate_effect'
    if any(style.get(k) != 'none' for k in ('filter','backdrop-filter','box-shadow','text-shadow','outline-style','border-image-source','perspective','contain')):
        return 'unsupported_paint_effect'
    if style.get('will-change') != 'auto' or style.get('zoom') != '1':return 'unsupported_coordinate_effect'
    if style.get('writing-mode') != 'horizontal-tb':return 'unsupported_writing_mode'
    if style.get('visibility') != 'visible' or style.get('opacity') != '1':return 'unsupported_visibility_state'
    if style.get('display') in ('none','contents'):return 'unsupported_display_state'
    return ''



_FRAME_SUBTYPES = frozenset(('missing_layout', 'inconsistent_fragment_style', 'no_positive_layout',
    'unsupported_position', 'unsupported_paint_effect', 'unsupported_coordinate_effect',
    'unsupported_writing_mode', 'unsupported_visibility_state', 'unsupported_display_state'))
_FRAME_PROPERTIES = frozenset(('position', 'transform', 'translate', 'rotate', 'scale', 'filter',
    'backdrop-filter', 'box-shadow', 'text-shadow', 'outline-style', 'border-image-source',
    'clip-path', 'mask-image', 'perspective', 'contain', 'background-image', 'will-change',
    'zoom', 'writing-mode', 'visibility', 'opacity', 'display'))

def _frame_guard_property(style, reason):
    if reason == 'unsupported_paint_effect':
        return next((k for k in ('transform', 'translate', 'rotate', 'scale', 'filter',
            'backdrop-filter', 'box-shadow', 'text-shadow', 'outline-style', 'border-image-source',
            'clip-path', 'mask-image', 'perspective', 'contain', 'background-image') if style.get(k) != 'none'), None)
    if reason == 'unsupported_coordinate_effect': return 'will-change' if style.get('will-change') != 'auto' else 'zoom'
    if reason == 'unsupported_visibility_state': return 'visibility' if style.get('visibility') != 'visible' else 'opacity'
    return {'unsupported_writing_mode':'writing-mode', 'unsupported_display_state':'display'}.get(reason, 'position')

def _safe_frame_detail(value):
    if not isinstance(value, dict): return {}
    subtype=value.get('subtype'); role=value.get('role')
    if not isinstance(subtype,str) or subtype not in _FRAME_SUBTYPES: return {}
    if not isinstance(role,str) or role not in ('host','ancestor'): return {}
    host=value.get('host_backend_id');depth=value.get('ancestor_depth')
    if type(host) is not int or not 0<host<2**53 or type(depth) is not int or not 0<=depth<=4096:return {}
    out={'subtype':subtype,'role':role,'host_backend_id':host,'ancestor_depth':depth}
    prop=value.get('property')
    if isinstance(prop,str) and prop in _FRAME_PROPERTIES:out['property']=prop
    box=value.get('box')
    if isinstance(box,list) and len(box)==4 and _finite(box) and box[2]>=box[0] and box[3]>=box[1]:out['box']=list(box)
    return out

def _document_selection(docs, strings, document_backend_id):
    """Validate the complete embedding roster, then select the live Document ID.

    Child documents contribute only embedding identities, never layout or text.
    """
    if not isinstance(docs, list) or not docs:
        raise ValueError('missing documents')
    roots = []; edges = []; all_ids = set()
    for di, doc in enumerate(docs):
        n = doc['nodes']; ids = n['backendNodeId']; parents = n['parentIndex']
        types = n['nodeType']; names = n['nodeName']
        if (not ids or len(ids) != len(parents) or len(ids) != len(types)
                or len(ids) != len(names) or types[0] != 9 or parents[0] != -1
                or any(type(v) is not int or v <= 0 or v in all_ids for v in ids)
                or len(set(ids)) != len(ids)
                or any(type(v) is not int or v not in (1,3,4,7,8,9,10,11) for v in types)
                or any(type(v) is not int or not 0 <= v < len(strings) or not isinstance(strings[v],str) for v in names)
                or any(type(v) is not int or not 0 <= v < i for i,v in enumerate(parents) if i)
                or any(v == 9 for v in types[1:])):
            raise ValueError('ambiguous documents')
        all_ids.update(ids); roots.append(ids[0])
        children = _rare(n.get('contentDocumentIndex', {}))
        for ni, child in children.items():
            if (type(ni) is not int or not 0 <= ni < len(ids) or types[ni] != 1
                    or strings[names[ni]].upper() != 'IFRAME'
                    or type(child) is not int or not 0 <= child < len(docs) or child == di):
                raise ValueError('invalid embedding')
            edges.append((di, ni, child, ids[ni]))
        # A frame host without a captured document is not an empty frame proof.
        if any(strings[name].upper() in ('OBJECT','EMBED','FRAME')
               or strings[name].upper() == 'IFRAME' and i not in children
               for i, name in enumerate(names)):
            raise ValueError('incomplete frame roster')
    if type(document_backend_id) is not int or roots.count(document_backend_id) != 1:
        raise ValueError('missing current document')
    main = roots.index(document_backend_id)
    incoming = [child for _, _, child, _ in edges]
    if main in incoming or any(incoming.count(i) != 1 for i in range(len(docs)) if i != main):
        raise ValueError('ambiguous embedding')
    reached = {main}
    while True:
        more = {child for parent, _, child, _ in edges if parent in reached}
        if more <= reached: break
        reached.update(more)
    if len(reached) != len(docs): raise ValueError('disconnected embedding')
    # Canonical identities, independent of document array order.
    roster = sorted((roots[parent], host, roots[child]) for parent, _, child, host in edges)
    return docs[main], [ni for parent, ni, _, _ in edges if parent == main], roster


def snapshot_envelopes(snapshot, expected=(), *, document_backend_id=None, diagnostic_sink=None, _provisional_nonpainting=False):
    """Parse an ephemeral CDP snapshot; expected is (owner backend ID, part).

    All positive fragments are retained, including zero-content-height borders.
    Missing native data is unknown. Returned data contains only integer IDs,
    numeric rectangles/paint order and fixed diagnostic strings, never DOM text.
    Explicit live Document selection permits only complete validated embedding
    rosters. Shadow trees and unsupported frame enclosures remain unknown.
    """
    try:
        try:
            docs, strings = _snapshot_shape(snapshot)
        except ValueError as error:
            return _unknown(error.args[0] if error.args in (('invalid_paint_order',),('invalid_node_type',),('ambiguous_node_identity',)) else 'malformed_native_snapshot')
        if document_backend_id is None:
            if len(docs) != 1: return _unknown('multiple_documents')
            d = docs[0]; frame_nodes = []; roster = []
        else:
            try:
                d, frame_nodes, roster = _document_selection(docs, strings, document_backend_id)
            except (KeyError, IndexError, TypeError, ValueError, AttributeError):
                return _unknown('invalid_document_roster')
        n = d['nodes']; layout = d['layout']
        parents, backend = n['parentIndex'], n['backendNodeId']
        if (n['nodeType'][0] != 9 or parents[0] != -1
                or any(type(v) is not int or v not in (1,3,4,7,8,9,10,11) for v in n['nodeType'])):
            return _unknown('invalid_node_type')
        if document_backend_id is None and any(strings[v].upper() in ('IFRAME', 'OBJECT', 'EMBED', 'FRAME') for v in n['nodeName']):
            return _unknown('unsupported_tree_boundary')
        if (len(parents) != len(backend) or len(set(backend)) != len(backend)
                or any(type(v) is not int or v <= 0 for v in backend)
                or any(type(v) is not int or v < -1 or v >= len(backend) for v in parents)):
            return _unknown('ambiguous_node_identity')
        if n.get('shadowRootType', {}).get('index') or document_backend_id is None and n.get('contentDocumentIndex', {}).get('index'):
            return _unknown('unsupported_tree_boundary')
        scroll = [d['scrollOffsetX'], d['scrollOffsetY']]
        if not _finite(scroll):
            return _unknown('invalid_scroll')
        pseudo = _rare(n.get('pseudoType', {}))
        rows = {}
        for i, ni in enumerate(layout['nodeIndex']):
            if type(ni) is not int or not 0 <= ni < len(backend):
                return _unknown('invalid_layout_owner')
            b = layout['bounds'][i]
            if len(b) != 4 or not _finite(b) or b[2] < 0 or b[3] < 0:
                return _unknown('invalid_layout_bounds')
            values = layout['styles'][i]
            if len(values) != len(STYLE_NAMES) and n['nodeType'][ni] != 9:
                return _unknown('missing_computed_styles')
            style = {k: strings[v] for k, v in zip(STYLE_NAMES, values)}
            po = layout['paintOrders'][i]
            if type(po) is not int or po < 0:
                return _unknown('invalid_paint_order')
            rows.setdefault(ni, []).append((b, style, po))
        frames = []
        try:
            if isinstance(diagnostic_sink, dict): diagnostic_sink['frame_host_count'] = len(frame_nodes)
        except Exception: pass
        def frame_failure(subtype, current, host, entries=(), prop=None):
            # Optional observation only: never alter parser return or geometry.
            try:
                detail = {'subtype':subtype, 'host_backend_id':backend[host],
                          'ancestor_depth':len(seen)-1,
                          'role':'host' if current == host else 'ancestor'}
                if prop: detail['property'] = prop
                boxes = [b for b, _, _ in entries if b[2] > 0 and b[3] > 0]
                if boxes:
                    detail['box'] = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                                     max(b[0]+b[2] for b in boxes), max(b[1]+b[3] for b in boxes)]
                if isinstance(diagnostic_sink, dict): diagnostic_sink['frame_enclosure'] = detail
            except Exception: pass
            return _unknown('unsupported_frame_enclosure')
        for ni in frame_nodes:
            if _provisional_nonpainting is True and not rows.get(ni):
                # Absence is NOT nonpainting proof. Only the live bridge may
                # resolve these exact identities and prove display:none twice.
                chain = []; current = ni; visited = set()
                while current >= 0:
                    if current in visited or len(visited) >= 128:
                        return _unknown('invalid_ancestor_chain')
                    visited.add(current)
                    if n['nodeType'][current] != 9:
                        if n['nodeType'][current] != 1: return _unknown('invalid_ancestor_chain')
                        chain.append((backend[current], None, None))
                    current = parents[current]
                frames.append({'backend_id':backend[ni], 'chain':chain,
                               'enclosure':None, 'nonpainting_pending':True})
                continue
            chain = []; current = ni; seen = set()
            while current >= 0:
                if current in seen: return _unknown('invalid_ancestor_chain')
                seen.add(current)
                if n['nodeType'][current] != 9:
                    entries = rows.get(current, [])
                    if not entries or any(e[1] != entries[0][1] for e in entries):
                        return frame_failure('missing_layout' if not entries else 'inconsistent_fragment_style', current, ni, entries)
                    style = entries[0][1]
                    guard = _frame_footprint_guard(style)
                    if guard:
                        try: prop = _frame_guard_property(style, guard)
                        except Exception: prop = None
                        return frame_failure(guard or 'unsupported_position', current, ni, entries, prop)
                    boxes = [b for b, _, _ in entries if b[2] > 0 and b[3] > 0]
                    if not boxes: return frame_failure('no_positive_layout', current, ni, entries)
                    box = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                           max(b[0]+b[2] for b in boxes), max(b[1]+b[3] for b in boxes)]
                    chain.append((backend[current], style, box))
                current = parents[current]
            box = chain[0][2]
            frames.append({'backend_id': backend[ni], 'chain': chain,
                           'enclosure': [box[0]-scroll[0], box[1]-scroll[1],
                                         box[2]-scroll[0], box[3]-scroll[1]]})
        results = []
        signatures = []
        found = set()
        for ni, pi in pseudo.items():
            if type(ni) is not int or type(pi) is not int or not 0 <= ni < len(backend) or not 0 <= pi < len(strings):
                return _unknown('invalid_pseudo_identity')
            part = strings[pi]
            if part not in ('before', 'after'):
                return _unknown('unsupported_pseudo_type')
            owner = parents[ni]
            if not 0 <= owner < len(backend) or n['nodeType'][owner] != 1 or n['nodeType'][ni] != 1:
                return _unknown('missing_pseudo_owner')
            key = (backend[owner], '::' + part)
            if key in found:
                return _unknown('ambiguous_pseudo_owner')
            found.add(key)
            reason = ''; paint_category='unknown_style_state'; chain = []; current = ni; visited = set()
            while current >= 0:
                if current in visited or current >= len(backend):
                    return _unknown('invalid_ancestor_chain')
                visited.add(current)
                # Document has no painted style; every element/pseudo needs it.
                if n['nodeType'][current] != 9:
                    entries = rows.get(current, [])
                    if not entries:
                        reason = 'missing_native_layout'; paint_category='unknown_style_state'; break
                    styles = [e[1] for e in entries]
                    if any(s != styles[0] for s in styles):
                        reason = 'ambiguous_computed_styles'; paint_category='unknown_style_state'; break
                    paint_category=_pseudo_paint_category(styles[0],current==ni)
                    reason = _finite_pseudo_guard(styles[0], current == ni)
                    if reason: break
                    positive = [b for b, _, _ in entries if b[2] > 0 and b[3] > 0]
                    if not positive: positive = [b for b, _, _ in entries]
                    box = ([min(b[0] for b in positive), min(b[1] for b in positive),
                            max(b[0]+b[2] for b in positive), max(b[1]+b[3] for b in positive)]
                           if positive else None)
                    chain.append((backend[current], styles[0], box))
                current = parents[current]
            entries = rows.get(ni, [])
            fragments = [{'left': b[0] - scroll[0], 'top': b[1] - scroll[1],
                          'right': b[0] - scroll[0] + b[2], 'bottom': b[1] - scroll[1] + b[3]}
                         for b, _, _ in entries if b[2] > 0 and b[3] > 0]
            if not fragments and not reason:
                reason = 'missing_positive_native_fragment'
            item = {'owner_backend_id': key[0], 'pseudo_backend_id': backend[ni], 'part': key[1]}
            if reason:
                item.update(_unknown(reason));item['paint_category']=paint_category;item['paint_depth']=len(visited)-1
            else:
                item.update(supported=True, envelope={'left': min(f['left'] for f in fragments),
                    'top': min(f['top'] for f in fragments), 'right': max(f['right'] for f in fragments),
                    'bottom': max(f['bottom'] for f in fragments)}, fragments=fragments,
                    paint_orders=[po for b, _, po in entries if b[2] > 0 and b[3] > 0])
            results.append(item)
            signatures.append((backend[ni], key, chain, [(b, po) for b, _, po in entries]))
        if not results or any(tuple(key) not in found for key in expected):
            return _unknown('missing_expected_pseudo')
        return {'supported': all(r['supported'] for r in results) and not any(f.get('nonpainting_pending') for f in frames), 'envelopes': results,
                'state': {'document_backend_id': backend[0], 'scroll': scroll},
                # Private ephemeral binding: styles never cross the public capture API.
                '_binding': (parents, backend, signatures, roster, frames), '_frames': frames}
    except (KeyError, IndexError, TypeError, ValueError, OverflowError, AttributeError):
        return _unknown('malformed_native_snapshot')


def capture_native_envelopes(page, deadline, expected=()):
    """Double sample a same-document diagnostic envelope under caller deadline.

    Deadline checks are cooperative; sync CDP has no hard cancellation. This API
    therefore MUST NOT be used to relax production visibility until a separately
    verified deadline and DOM-owner bridge exists. It never attests a screenshot.
    """
    session = None
    try:
        if time.monotonic() >= deadline:
            return _unknown('deadline_exceeded')
        session = page.context.new_cdp_session(page)
        params = {'computedStyles': list(STYLE_NAMES), 'includePaintOrder': True}
        first = snapshot_envelopes(session.send('DOMSnapshot.captureSnapshot', params), expected)
        if time.monotonic() >= deadline:
            return _unknown('deadline_exceeded')
        state_script = '() => ({width:innerWidth,height:innerHeight,x:scrollX,y:scrollY,scale:visualViewport?.scale,left:visualViewport?.offsetLeft,top:visualViewport?.offsetTop})'
        state = page.evaluate(state_script)
        second = snapshot_envelopes(session.send('DOMSnapshot.captureSnapshot', params), expected)
        after = page.evaluate(state_script)
        if time.monotonic() >= deadline:
            return _unknown('deadline_exceeded')
        if not first.get('supported') or not second.get('supported'):
            result = first if not first.get('supported') else second
            return {k: v for k, v in result.items() if not k.startswith('_')}
        if first != second or state != after:
            return _unknown('unstable_native_geometry')
        if not _finite(state.values()) or state['width'] <= 0 or state['height'] <= 0:
            return _unknown('invalid_viewport')
        if state['scale'] != 1 or state['left'] != 0 or state['top'] != 0:
            return _unknown('unsupported_visual_viewport')
        if second['state']['scroll'] != [state['x'], state['y']]:
            return _unknown('unstable_scroll_mapping')
        return {k: v for k, v in second.items() if not k.startswith('_')} | {'viewport': state}
    except Exception:
        return _unknown('native_capability_unavailable')
    finally:
        if session is not None:
            try: session.detach()
            except Exception: pass




_NONPAINTING_DISPLAYS = frozenset(('none','block','inline','inline-block','flow-root',
    'flex','inline-flex','grid','inline-grid','table','inline-table','table-row-group',
    'table-header-group','table-footer-group','table-row','table-cell','table-column-group',
    'table-column','table-caption','list-item','contents','ruby','ruby-base','ruby-text',
    'ruby-base-container','ruby-text-container'))

def _valid_nonpainting_facts(value, frames):
    """Untrusted Runtime payload must cover each exact pending host once."""
    if type(value) is not list or type(frames) is not list:return False
    pending = [f for f in frames if f.get('nonpainting_pending') is True]
    if len(value) != len(pending):return False
    expected = {f['index']:len(f['chain']) for f in pending}
    if len(expected) != len(pending):return False
    seen=set()
    for row in value:
        if type(row) is not dict or set(row) != {'index','witness_depth','displays','client_rect_count'}:return False
        index=row['index']; depth=row['witness_depth']; displays=row['displays']; count=row['client_rect_count']
        if type(index) is not int or index not in expected or index in seen:return False
        if type(depth) is not int or not 0<=depth<expected[index]:return False
        if type(count) is not int or count!=0:return False
        if (type(displays) is not list or len(displays)!=expected[index]
                or any(type(v) is not str or v not in _NONPAINTING_DISPLAYS for v in displays)
                or 'none' not in displays or displays.index('none')!=depth):return False
        seen.add(index)
    return seen==set(expected)

def _valid_frame_bounds(value, frames):
    """Every rendered host has a complete finite current live chain once."""
    if type(value) is not list or type(frames) is not list:return False
    rendered=[f for f in frames if f.get('nonpainting_pending') is not True]
    expected={f['index']:len(f['chain']) for f in rendered}
    if len(expected)!=len(rendered) or len(value)!=len(rendered):return False
    seen=set()
    for row in value:
        if type(row) is not dict or set(row)!={'index','boxes'}:return False
        index=row['index'];boxes=row['boxes']
        if type(index) is not int or index not in expected or index in seen:return False
        if type(boxes) is not list or len(boxes)!=expected[index]:return False
        if any(type(b) is not list or len(b)!=4 or not _finite(b) or b[2]<=b[0] or b[3]<=b[1] for b in boxes):return False
        seen.add(index)
    return seen==set(expected)

_NONPAINTING_VALIDATION = r"""function(state,frames,...nodes){
  if(this!==document || innerWidth!==state.viewport.width || innerHeight!==state.viewport.height
     || location.href!==state.url || scrollX!==state.scroll[0] || scrollY!==state.scroll[1]
     || visualViewport?.scale!==1 || visualViewport?.offsetLeft!==0 || visualViewport?.offsetTop!==0
     || document.fullscreenElement || document.querySelector(':modal,:popover-open'))return {valid:false};
  const live=[...document.querySelectorAll('iframe')];
  if(live.length!==frames.length || frames.some(f=>!live.includes(nodes[f.index])))return {valid:false};
  const facts=[];
  for(const f of frames.filter(f=>f.nonpainting_pending)){
    const host=nodes[f.index];
    if(!host || host.tagName!=='IFRAME' || host.getClientRects().length!==0)return {valid:false};
    let ancestor=host; const displays=[];
    for(const a of f.chain){const expected=nodes[a.index];
      if(ancestor!==expected || !expected.isConnected || expected.ownerDocument!==document
         || expected.shadowRoot)return {valid:false};
      displays.push(getComputedStyle(expected).display);ancestor=ancestor.parentElement;
    }
    if(ancestor!==null || !displays.includes('none'))return {valid:false};
    facts.push({index:f.index, witness_depth:displays.indexOf('none'), displays, client_rect_count:0});
  }
  return {valid:true,facts};
}"""

_FINITE_FRAME_VALIDATION = r"""function(state,frames,names,...nodes){
  const hidden=(""" + _NONPAINTING_VALIDATION + r""").apply(this,[state,frames,...nodes]);
  if(!hidden.valid)return {valid:false};
  const frame_bounds=[];
  for(const f of frames.filter(f=>!f.nonpainting_pending)){
    let ancestor=nodes[f.index]; const boxes=[];
    for(const a of f.chain){const expected=nodes[a.index];
      if(ancestor!==expected || !expected.isConnected || expected.ownerDocument!==document || expected.shadowRoot)return {valid:false};
      const style=getComputedStyle(expected);
      if(!names.every(k=>style.getPropertyValue(k)===a.styles[k]))return {valid:false};
      const r=expected.getBoundingClientRect();const b=[r.left+scrollX,r.top+scrollY,r.right+scrollX,r.bottom+scrollY];
      if(!b.every(Number.isFinite) || b[2]<=b[0] || b[3]<=b[1])return {valid:false};
      boxes.push(b);ancestor=ancestor.parentElement;
    }
    if(ancestor!==null)return {valid:false};
    frame_bounds.push({index:f.index,boxes});
  }
  return {valid:true,facts:hidden.facts,frame_bounds};
}"""

def _native_visibility_impl(page, required, deadline, visibility_script, phase):
    """Fallback only for unresolved manual pseudo geometry, bound to live Elements.

    Remote node references, style/parent/rect checks and glyph evaluation execute
    in one Runtime.callFunctionOn. Native snapshots bracket that evaluation.
    No DOM attributes/styles are changed. All requests use cooperative elapsed
    checks under the caller's existing capture deadline.
    """
    expired = {'verified':False,'reason':'unverifiable_pseudo_extent',
               'native_geometry':{'version':1,'diagnostic':'unsupported_geometry','reason':'deadline_exceeded'}}
    if time.monotonic() >= deadline:
        return expired
    phase[0] = 'manual_check'
    baseline = page.evaluate(visibility_script, required)
    if time.monotonic() >= deadline:
        return expired
    if not isinstance(baseline, dict) or baseline.get('reason') != 'unverifiable_pseudo_extent':
        return baseline
    phase[1] = True
    from .search.failure_policy import ResearchControlError
    session = None
    group = 'kac-native-visibility'
    def refused(reason):
        return {**baseline, 'native_geometry': {'version':1, 'diagnostic':'unsupported_geometry', 'reason':reason}}
    try:
        session = page.context.new_cdp_session(page)
        def send(method, params):
            if time.monotonic() >= deadline: raise TimeoutError()
            value = session.send(method, params)
            if time.monotonic() >= deadline: raise TimeoutError()
            return value
        if time.monotonic() >= deadline: raise TimeoutError()
        phase[0] = 'viewport_check'
        current = page.evaluate('() => ({viewport:{width:innerWidth,height:innerHeight},url:location.href,scroll:[scrollX,scrollY],scale:visualViewport?.scale,left:visualViewport?.offsetLeft,top:visualViewport?.offsetTop})')
        if time.monotonic() >= deadline: raise TimeoutError()
        if (not isinstance(current,dict) or not isinstance(current.get('viewport'),dict)
                or not _finite([current['viewport'].get('width'),current['viewport'].get('height')])
                or current['viewport']['width']<=0 or current['viewport']['height']<=0
                or current.get('scale')!=1 or current.get('left')!=0 or current.get('top')!=0):
            return refused('unsupported_visual_viewport')
        params = {'computedStyles': list(STYLE_NAMES), 'includePaintOrder': True}
        phase[0] = 'native_snapshot_before'
        root = send('DOM.getDocument', {'depth':0, 'pierce':False}).get('root', {})
        document_id = root.get('backendNodeId')
        if root.get('nodeType') != 9 or type(document_id) is not int or document_id <= 0:
            return refused('invalid_document_roster')
        snapshot = send('DOMSnapshot.captureSnapshot', params)
        if len(phase) > 2:
            phase[2]['document_count'] = len(snapshot.get('documents', [])) if isinstance(snapshot.get('documents'), list) else 0
            phase[2]['document_backend_id'] = document_id
        first = snapshot_envelopes(snapshot, document_backend_id=document_id, diagnostic_sink=phase[2], _provisional_nonpainting=True)
        if not first.get('envelopes') or '_binding' not in first:
            return refused(first.get('reason','missing_native_layout'))
        if first['state']['scroll'] != current.get('scroll'):
            return refused('stale_native_scroll')
        if len(phase) > 2: phase[2]['frame_host_count'] = len(first['_frames'])
        state={**first['state'], 'viewport':current['viewport'], 'url':current.get('url')}
        ids = [first['state']['document_backend_id']]
        bindings = {signature[0]:signature[2] for signature in first['_binding'][2]}
        records = []
        for envelope in first['envelopes']:
            chain = bindings[envelope['pseudo_backend_id']]
            for backend, _, _ in chain[1:]:
                if backend not in ids: ids.append(backend)
            owner = envelope['owner_backend_id']
            if owner not in ids: ids.append(owner)
            records.append({**envelope, 'owner_index':ids.index(owner),
                'chain':[{'index':ids.index(backend), 'styles':style, 'box':box}
                         for backend, style, box in chain[1:]],
                'pseudo_styles':chain[0][1] if chain else {}})
        frame_records = []
        for frame in first['_frames']:
            for backend, _, _ in frame['chain']:
                if backend not in ids: ids.append(backend)
            frame_records.append({'index':ids.index(frame['backend_id']),
                'enclosure':frame['enclosure'], 'nonpainting_pending':frame.get('nonpainting_pending',False),
                'chain':[{'index':ids.index(backend), 'styles':style, 'box':box}
                         for backend, style, box in frame['chain']]})
        phase[0] = 'resolve_nodes'
        objects = [send('DOM.resolveNode', {'backendNodeId':backend,'objectGroup':group})['object']['objectId'] for backend in ids]
        # No saved selector/order mapping: these are actual current Element refs.
        declaration = '''function(required,records,state,names,frames,...nodes){
          if(this!==document)return {verified:false,reason:'unverifiable_pseudo_extent'};
          const used=[];let invalid=false;
          const currentState=()=>innerWidth===state.viewport.width&&innerHeight===state.viewport.height&&location.href===state.url&&scrollX===state.scroll[0]&&scrollY===state.scroll[1]
             &&visualViewport?.scale===1&&visualViewport?.offsetLeft===0&&visualViewport?.offsetTop===0;
          const sameStyle=(element,part,expected)=>{const s=getComputedStyle(element,part);return names.every(k=>s.getPropertyValue(k)===expected[k]);};
          const sameBox=(element,b)=>{if(!b)return false;const r=element.getBoundingClientRect();
            return [r.left+scrollX,r.top+scrollY,r.right+scrollX,r.bottom+scrollY].every((v,i)=>Math.abs(v-b[i])<=.01);};
          // A frame may paint even with pointer-events:none. Its entire native
          // host border box is an upper bound; never shrink by guessed clipping.
          if(document.fullscreenElement || document.querySelector(':modal,:popover-open'))
            return {verified:false,reason:'unverifiable_pseudo_extent'};
          const frameUnknown=reason=>({verified:false,reason:'unverifiable_pseudo_extent',
            _native_state_valid:currentState(),native_geometry:{version:1,diagnostic:'unsupported_geometry',reason}});
          const liveFrames=[...document.querySelectorAll('iframe')];
          if(liveFrames.length!==frames.length || frames.some(f=>!liveFrames.includes(nodes[f.index])))
            return frameUnknown('stale_frame_roster');
          const frameProof=(''' + _FINITE_FRAME_VALIDATION + ''').apply(document,[state,frames,names,...nodes]);
          if(!frameProof.valid)return frameUnknown('stale_frame_enclosure');
          for(const f of frames){
            if(f.nonpainting_pending)continue;
            const live=frameProof.frame_bounds.find(v=>v.index===f.index).boxes[0];
            const left=Math.min(f.enclosure[0],live[0]-scrollX),top=Math.min(f.enclosure[1],live[1]-scrollY);
            const right=Math.max(f.enclosure[2],live[2]-scrollX),bottom=Math.max(f.enclosure[3],live[3]-scrollY);
            for(const r of required){
              if(![r.x,r.y,r.width,r.height].every(Number.isFinite) || r.width<=0 || r.height<=0)
                return {verified:false,reason:'unverifiable_pseudo_extent'};
              const x=r.x-scrollX,y=r.y-scrollY;
              if(left<x+r.width && right>x && top<y+r.height && bottom>y)
                return frameUnknown('frame_intersection');
            }
          }
          const nativePseudoExtent=(owner,part)=>{
            const unknown=(reason,paint_category,paint_depth)=>{invalid=true;return {unverifiable:true,part,reason,diagnostic:'unsupported_geometry',...(paint_category?{paint_category,paint_depth}:{})};};
            if(!currentState())return unknown('stale_native_viewport');
            const owned=records.filter(r=>nodes[r.owner_index]===owner);
            if(!owned.some(r=>r.part===part))return unknown('missing_current_native_owner');
            // Returning from manual unknown exits its two-part loop. Revalidate
            // and retain BOTH current pseudo parts so no earlier/later cover is lost.
            for(const p of ['::before','::after']){
              const content=getComputedStyle(owner,p).content;
              if(content&&content!=='none'&&content!=='normal'&&!owned.some(r=>r.part===p))return unknown('missing_current_native_part');
            }
            for(const r of owned){
              if(!r.supported)return unknown(r.reason||'unsupported_native_geometry',r.paint_category,r.paint_depth);
              const s=r.pseudo_styles;
              if(['absolute','fixed'].includes(s.position)&&((s.left==='auto'&&s.right==='auto')||(s.top==='auto'&&s.bottom==='auto')))return unknown('unbound_native_static_position');
              if(!owner.isConnected||owner.ownerDocument!==document||!sameStyle(owner,r.part,r.pseudo_styles))return unknown('stale_native_owner_or_style');
              let ancestor=owner;
              for(const a of r.chain){const expected=nodes[a.index];
                if(ancestor!==expected||!expected.isConnected||expected.ownerDocument!==document||!sameStyle(expected,null,a.styles)||!sameBox(expected,a.box))return unknown('stale_native_ancestor');
                ancestor=ancestor.parentElement;
              }
              if(ancestor!==null)return unknown('incomplete_native_ancestry');
            }
            for(const r of owned)if(!used.some(u=>u.pseudo_backend_id===r.pseudo_backend_id))used.push({owner_backend_id:r.owner_backend_id,pseudo_backend_id:r.pseudo_backend_id,part:r.part,envelope:r.envelope});
            return {extents:owned.map(r=>({...r.envelope,part:r.part,source:'native_conservative_envelope'}))};
          };
          const result=(''' + visibility_script + ''')(required,nativePseudoExtent);
          if(invalid&&result.verified)return {verified:false,reason:'unverifiable_pseudo_extent'};
          return {...result,_native_used:used,_native_viewport:{width:innerWidth,height:innerHeight},_native_state_valid:currentState(),_native_nonpainting:frameProof.facts,_native_frame_bounds:frameProof.frame_bounds};
        }'''
        phase[0] = 'native_evaluation'
        call = send('Runtime.callFunctionOn', {'objectId':objects[0], 'functionDeclaration':declaration,
            'arguments':[{'value':required},{'value':records},{'value':state},{'value':list(STYLE_NAMES)},{'value':frame_records}]
                        +[{'objectId':o} for o in objects],
            'returnByValue':True})
        if call.get('exceptionDetails') or 'value' not in call.get('result',{}):
            return refused('native_runtime_unavailable')
        value = call['result']['value']
        if not isinstance(value,dict):return refused('native_runtime_unavailable')
        pending = any(f.get('nonpainting_pending') for f in first['_frames'])
        if pending and value.get('verified') is True and not _valid_nonpainting_facts(value.get('_native_nonpainting'),frame_records):
            return refused('nonpainting_frame_unconfirmed')
        if frame_records and value.get('verified') is True and not _valid_frame_bounds(value.get('_native_frame_bounds',[]),frame_records):
            return refused('stale_frame_enclosure')
        phase[0] = 'native_snapshot_after'
        second = snapshot_envelopes(send('DOMSnapshot.captureSnapshot', params), document_backend_id=document_id, diagnostic_sink=phase[2], _provisional_nonpainting=True)
        if first != second or not value.get('_native_state_valid'):
            return refused('stale_native_snapshot')
        nonpainting = value.pop('_native_nonpainting', [])
        frame_bounds = value.pop('_native_frame_bounds', [])
        if frame_records and value.get('verified') is True:
            check = send('Runtime.callFunctionOn', {'objectId':objects[0],
                'functionDeclaration':_FINITE_FRAME_VALIDATION,
                'arguments':[{'value':state},{'value':frame_records},{'value':list(STYLE_NAMES)}]+[{'objectId':o} for o in objects],
                'returnByValue':True})
            after = check.get('result',{}).get('value')
            if (check.get('exceptionDetails') or not isinstance(after,dict)
                    or after.get('valid') is not True
                    or not _valid_nonpainting_facts(after.get('facts'),frame_records)
                    or after.get('facts') != nonpainting
                    or not _valid_frame_bounds(after.get('frame_bounds',[]),frame_records)
                    or after.get('frame_bounds',[]) != frame_bounds):
                return refused('stale_nonpainting_frame')
            if len(phase)>2: phase[2]['nonpainting_frame_count']=len(nonpainting)
        used = value.pop('_native_used', [])
        viewport = value.pop('_native_viewport', None)
        value.pop('_native_state_valid', None)
        if value.get('verified') and (not used or viewport != value.get('viewport')):
            return refused('missing_native_binding')
        if used:
            signature = hashlib.sha256(json.dumps((first['_binding'],state,nonpainting,frame_bounds) if frame_records else (first['_binding'],state),sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
            value['native_geometry']={'version':1,'document_backend_id':first['state']['document_backend_id'],
                'signature_sha256':signature,'used':sorted(used,key=lambda r:(r['owner_backend_id'],r['part']))}
        return value
    except TimeoutError:
        return refused('deadline_exceeded')
    except ResearchControlError:
        raise
    except Exception:
        return refused('native_capability_unavailable')
    finally:
        if session is not None:
            try: session.send('Runtime.releaseObjectGroup', {'objectGroup':group})
            except Exception: pass
            try: session.detach()
            except Exception: pass


# Observational metadata only. No DOM/CDP strings, text, URLs, selectors or
# exception messages cross this boundary; the native return/proof is unchanged.
_DIAGNOSTIC_REASONS = frozenset((
    'invalid_document_roster', 'unsupported_frame_enclosure', 'frame_intersection',
    'stale_frame_roster', 'stale_frame_enclosure', 'nonpainting_frame_unconfirmed', 'stale_nonpainting_frame',
    'deadline_exceeded', 'unsupported_visual_viewport', 'missing_native_layout',
    'stale_native_scroll', 'native_runtime_unavailable', 'stale_native_snapshot',
    'missing_native_binding', 'native_capability_unavailable', 'multiple_documents',
    'unsupported_tree_boundary', 'invalid_node_type', 'ambiguous_node_identity',
    'ambiguous_pseudo_owner', 'invalid_ancestor_chain', 'invalid_layout_bounds',
    'invalid_layout_owner', 'invalid_paint_order', 'invalid_pseudo_identity',
    'invalid_scroll', 'invalid_viewport', 'malformed_native_snapshot',
    'missing_computed_styles', 'missing_expected_pseudo', 'missing_pseudo_owner',
    'unstable_native_geometry', 'unstable_scroll_mapping', 'unsupported_pseudo_type',
    'unsupported_position', 'unsupported_paint_effect', 'unsupported_coordinate_effect',
    'unsupported_writing_mode', 'unsupported_visibility_state', 'unsupported_display_state',
    'unsupported_generated_content', 'missing_current_native_owner',
    'missing_current_native_part', 'unsupported_native_geometry',
    'unbound_native_static_position', 'stale_native_owner_or_style',
    'stale_native_ancestor', 'incomplete_native_ancestry', 'stale_native_viewport',
    'unverifiable_pseudo_extent', 'unverifiable_pseudo_paint', 'unverifiable_paint_clip',
    'occluded', 'field_not_located', 'offscreen', 'no_glyphs', 'glyph_limit',
))
_DIAGNOSTIC_PHASES = frozenset(('admission', 'manual_check', 'viewport_check',
    'native_snapshot_before', 'resolve_nodes', 'native_evaluation', 'native_snapshot_after'))

_DETAIL_SENTINELS = frozenset(('absent','malformed','unknown'))
_FIELD_REASONS = _DIAGNOSTIC_REASONS | frozenset((
    'unbounded_pseudo_paint','transformed_fixed_containing_block',
    'transformed_absolute_containing_block','missing_absolute_containing_block',
    'scrolled_pseudo_containing_block','pseudo_margin_geometry',
    'overconstrained_pseudo_box','unresolved_pseudo_box','unverifiable_pseudo_clip',
    'unverifiable_pseudo_clip_margin'))
_WITNESS_BRANCHES = frozenset(('unverifiable_pseudo_extent','bounded_pseudo_intersection',
    'effective_clip_unverifiable','painted_overlay_intersection','topmost_hit_mismatch'))
_FIELD_ROLES = {'heading':'heading','title':'heading','article':'article',
                'price':'price','unit':'unit','basis_or_unit':'unit'}

def _detail_code(value, allowed):
    if value is None or type(value) is str and value=='':return 'absent'
    if type(value) is not str:return 'malformed'
    return value if value in allowed or value in _DETAIL_SENTINELS else 'unknown'

def _detail_box(value):
    if type(value) is dict:
        if all(k in value for k in ('left','top','right','bottom')):
            value=[value[k] for k in ('left','top','right','bottom')]
        elif all(k in value for k in ('x','y','width','height')):
            v=[value[k] for k in ('x','y','width','height')]
            if not _finite(v):return None
            value=[v[0],v[1],v[0]+v[2],v[1]+v[3]]
    if (type(value) is list and len(value)==4 and _finite(value)
            and all(abs(v)<=10**7 for v in value) and value[2]>value[0] and value[3]>value[1]):
        return list(value)
    return None

def _safe_visibility_detail(value):
    """Publication allowlist for the pure observation, never proof authority."""
    if type(value) is not dict:return {}
    out={'fields_state':_detail_code(value.get('fields_state'),frozenset(('present',))),
         'detail_origin':_detail_code(value.get('detail_origin'),frozenset(('native_geometry','field_pseudo','none'))),
         'detail_reason':_detail_code(value.get('detail_reason'),_FIELD_REASONS)}
    for key in ('field_count',):
        v=value.get(key)
        if type(v) is int and 0<=v<=4096:out[key]=v
    if type(value.get('truncated')) is bool:out['truncated']=value['truncated']
    rows=value.get('fields');out['fields']=[]
    if type(rows) is not list:return out
    for row in rows[:4]:
        if type(row) is not dict:continue
        role=_detail_code(row.get('role'),frozenset(_FIELD_ROLES))
        item={'role':_FIELD_ROLES.get(role,role),
              'reason':_detail_code(row.get('reason'),_FIELD_REASONS),
              'branch':_detail_code(row.get('branch'),_WITNESS_BRANCHES),
              'part':_detail_code(row.get('part'),frozenset(('::before','::after'))),
              'part_reason':_detail_code(row.get('part_reason'),_FIELD_REASONS),
              'paint_category':_detail_code(row.get('paint_category'),_PSEUDO_PAINT_CATEGORIES),
              'witness_state':_detail_code(row.get('witness_state'),frozenset(('present',))),
              'pseudo_state':_detail_code(row.get('pseudo_state'),frozenset(('present',)))}
        if type(row.get('uncovered')) is bool:item['uncovered']=row['uncovered']
        for key in ('rect_count','sample_count','paint_depth'):
            v=row.get(key)
            if type(v) is int and 0<=v<=(4096 if key=='paint_depth' else 10**7):item[key]=v
        for key in ('glyph_box','owner_box','blocker_box'):
            b=_detail_box(row.get(key))
            if b:item[key]=b
        out['fields'].append(item)
    return out

def _returned_visibility_detail(value):
    """Read existing result only; no DOM access or native/proof mutation."""
    if type(value) is not dict:return {'fields_state':'malformed','detail_origin':'none','detail_reason':'absent','fields':[]}
    fields=value.get('fields');state='absent' if fields is None else 'present' if type(fields) is list else 'malformed'
    rows=[]
    for field in fields[:4] if type(fields) is list else []:
        if type(field) is not dict:
            rows.append({'role':'malformed','reason':'malformed'});continue
        raw_witness=field.get('witness');witness=raw_witness if type(raw_witness) is dict else {}
        raw_pseudo=witness.get('pseudo');pseudo=raw_pseudo if type(raw_pseudo) is dict else {}
        rects=field.get('rects');rects=rects if type(rects) is list else []
        item={'role':field.get('role'),'reason':field.get('reason'),'branch':witness.get('branch'),
              'part':pseudo.get('part'),'part_reason':pseudo.get('reason'),'paint_category':pseudo.get('paint_category'),'paint_depth':pseudo.get('paint_depth'),
              'witness_state':'absent' if raw_witness is None else 'present' if type(raw_witness) is dict else 'malformed',
              'pseudo_state':'absent' if raw_pseudo is None else 'present' if type(raw_pseudo) is dict else 'malformed',
              'uncovered':field.get('uncovered'),'rect_count':min(len(rects),4096),
              'sample_count':field.get('samples'),'glyph_box':witness.get('glyph') or (rects[0] if rects else None)}
        for key in ('owner','blocker'):
            node=witness.get(key)
            if type(node) is dict:item[key+'_box']=node.get('rect')
        rows.append(item)
    raw_native=value.get('native_geometry');native=raw_native if type(raw_native) is dict else {}
    reason='malformed' if raw_native is not None and type(raw_native) is not dict else _detail_code(native.get('reason'),_FIELD_REASONS)
    origin='native_geometry' if reason!='absent' else 'none'
    if reason=='absent':
        for row in rows:
            candidate=_detail_code(row.get('part_reason'),_FIELD_REASONS)
            if candidate!='absent':reason=candidate;origin='field_pseudo';break
    return _safe_visibility_detail({'fields_state':state,'field_count':min(len(fields),4096) if type(fields) is list else None,
        'truncated':type(fields) is list and len(fields)>4,'fields':rows,'detail_origin':origin,'detail_reason':reason})


def safe_native_visibility_diagnostics(value):
    """Validate the bounded public ledger again at the publication boundary."""
    if not isinstance(value, dict): return {}
    count=value.get('observations')
    if type(count) is not int or count < 0: return {}
    rows=[]
    for row in value.get('recent', [])[:12] if isinstance(value.get('recent'), list) else []:
        if not isinstance(row, dict): continue
        if not isinstance(row.get('phase'),str) or row['phase'] not in _DIAGNOSTIC_PHASES: continue
        out={'phase':row['phase'], 'route':'native_fallback' if row.get('route')=='native_fallback' else 'manual',
             'outcome':row.get('outcome') if isinstance(row.get('outcome'),str) and row['outcome'] in ('verified','refused','control','exception') else 'refused',
             'reason':row.get('reason') if isinstance(row.get('reason'),str) and row['reason'] in _DIAGNOSTIC_REASONS else 'unrecorded_reason',
             'native_reason':row.get('native_reason') if isinstance(row.get('native_reason'),str) and row['native_reason'] in _DIAGNOSTIC_REASONS else 'unrecorded_reason'}
        for key in ('elapsed_ms', 'remaining_ms'):
            v=row.get(key)
            if type(v) in (int,float) and math.isfinite(v) and v>=0: out[key]=round(v,3)
        for key in ('native_used_count','document_count','frame_host_count','nonpainting_frame_count'):
            v=row.get(key)
            if type(v) is int and 0<=v<=4096:out[key]=v
        v=row.get('document_backend_id')
        if type(v) is int and 0<v<2**53:out['document_backend_id']=v
        detail=_safe_frame_detail(row.get('frame_enclosure'))
        if detail:out['frame_enclosure']=detail
        detail=_safe_visibility_detail(row.get('visibility_detail'))
        if detail:out['visibility_detail']=detail
        rows.append(out)
    return {'version':1,'observational_only':True,'observations':count,'recent':rows}


def native_visibility(page, required, deadline, visibility_script):
    """Retain final bridge metadata without changing calls, decisions or budget."""
    phase=['admission',False,{}];started=time.monotonic();value=None;outcome='exception'
    try:
        value=_native_visibility_impl(page,required,deadline,visibility_script,phase)
        outcome='verified' if isinstance(value,dict) and value.get('verified') is True else 'refused'
        return value
    except BaseException as error:
        from .search.failure_policy import ResearchControlError
        outcome='control' if isinstance(error,ResearchControlError) else 'exception'
        raise
    finally:
        try:
            finished=time.monotonic();native=value.get('native_geometry',{}) if isinstance(value,dict) else {}
            native=native if isinstance(native,dict) else {}
            old=safe_native_visibility_diagnostics(getattr(page,'_kac_native_visibility_diagnostics',None))
            row={'phase':phase[0],'route':'native_fallback' if phase[1] else 'manual',
                 'outcome':outcome,'reason':value.get('reason') if isinstance(value,dict) else None,
                 'native_reason':native.get('reason'),'elapsed_ms':max(0,(finished-started)*1000),
                 'remaining_ms':max(0,(deadline-finished)*1000),
                 'native_used_count':len(native.get('used',[])) if isinstance(native.get('used'),list) else 0}
            row.update(phase[2])
            row['visibility_detail']=_returned_visibility_detail(value)
            ledger={'observations':old.get('observations',0)+1,'recent':(old.get('recent',[])+[row])[-12:]}
            page._kac_native_visibility_diagnostics=safe_native_visibility_diagnostics(ledger)
        except Exception:pass  # All observational failures leave return/control unchanged.
