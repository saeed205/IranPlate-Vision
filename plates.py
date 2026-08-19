"""
Canonical Iranian license-plate parsing / normalisation.

Both the HTTP API (app.py) and the RTSP workers (camera_manager.py) used to keep
their own copy of the digit table and of a regex that had three defects:

  * ``[؀-ۿ]`` matches *any* character of the Arabic block, so a plate made of
    eight digits validated as a real plate.
  * it only accepted a single-character letter, so ``الف`` (government plates)
    never matched.
  * ``ه`` (U+0647) and ``ھ`` (U+06BE) were treated as different letters, so half
    of the province table was unreachable.

Everything plate-shaped now goes through this module, which produces one
canonical string (``24ن144-66``) used for the DB, the whitelist and the UI.
"""
import json
import os
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

FA_DIGITS = '۰۱۲۳۴۵۶۷۸۹'
AR_DIGITS = '٠١٢٣٤٥٦٧٨٩'
TO_EN_DIGITS = str.maketrans(FA_DIGITS + AR_DIGITS, '0123456789' * 2)

# Characters an OCR pass may emit that carry no meaning for us.
_STRIP_RE = re.compile('[\u200c\u200d\u200e\u200f\u202a-\u202e\u2066-\u2069\u0640\u064b-\u0652\u0670]')

# Letter spellings that must collapse onto one canonical letter.
_LETTER_ALIASES = {
    'ي': 'ی', 'ى': 'ی', 'ﻯ': 'ی', 'ﻱ': 'ی',
    'ك': 'ک', 'ﻙ': 'ک',
    'ھ': 'ه', 'ﻩ': 'ه', 'ة': 'ه', 'ۀ': 'ه',
    'أ': 'الف', 'إ': 'الف', 'آ': 'الف', 'ٱ': 'الف', 'ا': 'الف',
    'ﺍ': 'الف', 'الف': 'الف', 'ألف': 'الف',
}


def _load_letters():
    """Every letter that appears in plate_data.json, in canonical spelling."""
    try:
        with open(os.path.join(BASE_DIR, 'plate_data.json'), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return set()
    letters = set()
    for t in data.get('carplate_types', []):
        for raw in t.get('letters', []):
            letters.add(canonical_letter(raw))
    return {letter for letter in letters if letter}


def canonical_letter(raw):
    """Collapse a letter to its canonical spelling (``ھ``→``ه``, ``ا``→``الف``)."""
    s = _STRIP_RE.sub('', str(raw or '')).strip()
    if not s:
        return ''
    if s in _LETTER_ALIASES:
        return _LETTER_ALIASES[s]
    if len(s) == 1:
        return _LETTER_ALIASES.get(s, s)
    # Multi-character spellings we do not know: normalise char by char.
    return ''.join(_LETTER_ALIASES.get(c, c) for c in s)


LETTERS = _load_letters() or {
    'الف', 'ب', 'ت', 'ث', 'ج', 'د', 'ز', 'س', 'ش', 'ص', 'ط', 'ع', 'ف', 'ق',
    'ل', 'م', 'ن', 'ه', 'و', 'پ', 'ژ', 'ک', 'گ', 'ی',
}

# Longest first so that "الف" wins over "ا".
_LETTER_ALT = '|'.join(
    re.escape(spelling)
    for spelling in sorted(LETTERS | set(_LETTER_ALIASES), key=len, reverse=True)
)

# 2 digits · letter · 3 digits · 2 digits, with optional separators between groups.
PLATE_RE = re.compile(
    r'^(?P<prefix>\d{2})[\s\-_.]*'
    r'(?P<letter>' + _LETTER_ALT + r')[\s\-_.]*'
    r'(?P<middle>\d{3})[\s\-_.]*'
    r'(?P<suffix>\d{2})$'
)


def to_en_digits(text):
    return str(text or '').translate(TO_EN_DIGITS)


def parse(raw):
    """
    Parse any plate spelling into its parts.

    Returns ``{'prefix','letter','middle','suffix','canonical'}`` or ``None``
    when *raw* is not a valid Iranian civil plate.
    """
    s = _STRIP_RE.sub('', str(raw or ''))
    s = to_en_digits(s).strip()
    m = PLATE_RE.match(s)
    if not m:
        return None
    letter = canonical_letter(m.group('letter'))
    if letter not in LETTERS:
        return None
    return {
        'prefix': m.group('prefix'),
        'letter': letter,
        'middle': m.group('middle'),
        'suffix': m.group('suffix'),
        'canonical': f"{m.group('prefix')}{letter}{m.group('middle')}-{m.group('suffix')}",
    }


def normalize(raw):
    """Canonical plate string (``24ن144-66``), or ``''`` if *raw* is not a plate."""
    p = parse(raw)
    return p['canonical'] if p else ''


def is_valid(raw):
    return parse(raw) is not None
