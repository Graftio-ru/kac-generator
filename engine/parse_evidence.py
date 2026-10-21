"""Caller-owned page provenance. No optical accuracy or cell-location claims."""
from hashlib import sha256
from pathlib import Path


def page_evidence(path: str, page: int) -> dict:
    from PIL import Image
    source = Path(path)
    digest = sha256(source.read_bytes()).hexdigest()
    with Image.open(source) as image:
        width, height = image.size
    return {'page': page, 'page_id': f'{digest}:{page}', 'page_hash': digest,
            'width': width, 'height': height, 'scope': 'whole_page',
            'rotation': 0, 'bounds': [0, 0, width, height]}
