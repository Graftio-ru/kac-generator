"""Temporary, layout-independent page bands for a separate cloud reading."""
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image


@contextmanager
def page_bands(path):
    """Keep row numbers and all columns together; cover every pixel with overlap.

    Resizing cannot recover hidden characters. These are alternate views of the
    same source, not independent ground truth. Files live only during the call.
    """
    with TemporaryDirectory(prefix='kac-cloud-views-') as directory:
        with Image.open(path) as source:
            image = source.convert('RGB')
        width, height = image.size
        views = []
        for index, (top, bottom) in enumerate(((0, .4), (.3, .7), (.6, 1))):
            band = image.crop((0, int(height * top), width,
                               max(int(height * top) + 1, int(height * bottom))))
            scale = min(2, 4096 / max(band.size))
            band = band.resize((max(1, round(band.width * scale)),
                                max(1, round(band.height * scale))))
            target = Path(directory) / f'band-{index}.png'
            band.save(target)
            views.append(str(target))
        yield views
