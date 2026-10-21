"""Optical script/typography equivalence, never digit-letter correction."""
import re

# Only letters with the same uppercase glyph. Digits are deliberately absent.
_GLYPHS = dict(zip('АВЕКМНОРСТХ', 'ABEKMHOPCTX'))
_FOLD = str.maketrans(_GLYPHS)
_DASHES = str.maketrans({char: '-' for char in '‐‑‒–—−'})


def source_code_key(value: str) -> str:
    return re.sub(r'\s+', '', value).upper().translate(_FOLD).translate(_DASHES).replace('×', 'X').replace(',', '.')


def discovery_script_alias(value: str) -> str:
    """An extra query only: retain original article and strict quotation gates."""
    has_digit = any(c.isdigit() for c in value)
    def fold(match):
        token = match.group()
        cyrillic = [c.upper() for c in token if 'а' <= c.lower() <= 'я']
        if not cyrillic or any(c not in _GLYPHS for c in cyrillic):
            return token
        if not has_digit and not re.search('[A-Za-z]', token):
            return token
        return token.upper().translate(_FOLD)
    return re.sub('[A-Za-zА-Яа-я0-9]+', fold, value)
