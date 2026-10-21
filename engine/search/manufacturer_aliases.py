"""Exact, evidence-reviewed manufacturer names; never transliterate unknown names."""
from dataclasses import dataclass
import re

@dataclass(frozen=True)
class ManufacturerAlias:
    canonical: str
    names: tuple[str, ...]
    sources: tuple[str, ...]
    evidence_type: str = 'manufacturer_primary_publication'

REGISTRY = (ManufacturerAlias('DKC', ('DKC', 'ДКС'), (
    'https://www.dkc.ru/ru/about/history/',
    'https://www.dkc.ru/ru/catalog/index.html',
)),)

def normalize(value):
    return ' '.join(str(value).casefold().replace('ё', 'е').split())

def names_for(value):
    key = normalize(value)
    for entry in REGISTRY:
        if key in {normalize(name) for name in entry.names}:
            return entry.names
    return (str(value).strip(),) if key else ()

def manufacturer_equal(left, right):
    expected, actual = normalize(left), normalize(right)
    if not expected:
        return False
    aliases = {normalize(n) for n in names_for(left)}
    if actual in aliases:
        return True
    # A composite label is evidence only when every complete component is an
    # already reviewed name of this same manufacturer. Unknown names keep the
    # existing exact-only comparison; arbitrary prose is never split into names.
    entry = next((v for v in REGISTRY if expected in {normalize(n) for n in v.names}), None)
    if entry is None or len(actual) > 512:
        return False
    match = re.fullmatch(r'([^()]+?)((?:\s*\([^()]+\))+)', actual)
    if not match:
        return False
    components = [match[1], *re.findall(r'\(([^()]+)\)', match[2])]
    return all(normalize(component) in aliases for component in components)

def manufacturer_in_title(expected, title):
    text = normalize(title)
    return any(re.search(r'(?<!\w)' + re.escape(normalize(name)) + r'(?!\w)', text)
               for name in names_for(expected))
