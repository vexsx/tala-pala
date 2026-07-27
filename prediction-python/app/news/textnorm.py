"""Persian/Arabic text normalization for news ingestion.

Persian is written with a script that has several ways to spell the same word,
and Iranian CMSs use all of them.  Three failures follow directly from that,
and this module exists to prevent them:

* **Dedupe silently stops working.**  ``قيمت`` (Arabic yeh U+064A) and ``قیمت``
  (Persian yeh U+06CC) are the same word and different strings.  A title key
  computed without unification puts one story into two rows, and a duplicate
  group then reports two "independent sources" for what is one wire item —
  the exact inflation ``news_duplicate_groups.independent_source_count``
  exists to prevent.
* **The half-space (ZWNJ, U+200C) is invisible and unreliable.**  ``قیمت‌ها``
  and ``قیمتها`` are the same word; the ZWNJ survives some pipelines and not
  others.  The dedupe key therefore drops it entirely, so a story does not
  fork on a character nobody can see.
* **Persian digits are digits.**  ``۱۴۰۳`` and ``1403`` must compare equal, and
  any later numeric extraction has to see ASCII.

Deliberately dependency-free, for the same reason as ``dedupe.py``: this runs
on every ingested item on a small single host, and a normalization table is
not worth an extra wheel.

Scope note: ``normalize`` produces a canonical *analysis* form, not a display
form — it folds digits and punctuation that a reader would expect to see
unchanged.  Store the original text; normalize only what you compare.
"""
from __future__ import annotations

import html
import re
import unicodedata
from html.parser import HTMLParser

from .dedupe import normalize_title as _lexical_key

# Arabic-script letters that Persian spells differently.  Unifying towards the
# Persian form (not the Arabic one) matches how Persian sources render text
# when their editor is configured correctly, so the canonical form is the one
# most rows already carry.
LETTER_MAP = {
    "ي": "ی",  # ARABIC YEH          -> FARSI YEH
    "ى": "ی",  # ALEF MAKSURA        -> FARSI YEH
    "ې": "ی",  # ARABIC LETTER E     -> FARSI YEH
    "ۍ": "ی",  # YEH WITH TAIL       -> FARSI YEH
    "ك": "ک",  # ARABIC KAF          -> KEHEH
    "ڪ": "ک",  # SWASH KAF           -> KEHEH
    "ة": "ه",  # TEH MARBUTA         -> HEH   (Persian writes ه)
    "ۀ": "ه",  # HEH WITH YEH ABOVE  -> HEH
    "أ": "ا",  # ALEF WITH HAMZA ABOVE -> ALEF
    "إ": "ا",  # ALEF WITH HAMZA BELOW -> ALEF
    "ٱ": "ا",  # ALEF WASLA          -> ALEF
    "ؤ": "و",  # WAW WITH HAMZA      -> WAW   (مؤسسه/موسسه)
    "\u0640": "",   # TATWEEL: pure justification padding, never meaning
}
_LETTER_TABLE = str.maketrans(LETTER_MAP)

# Arabic-Indic (U+0660..) and Persian/extended Arabic-Indic (U+06F0..) digits.
_DIGIT_TABLE = str.maketrans(
    {
        **{chr(0x0660 + i): str(i) for i in range(10)},
        **{chr(0x06F0 + i): str(i) for i in range(10)},
        "٫": ".",   # ARABIC DECIMAL SEPARATOR
        "٬": ",",   # ARABIC THOUSANDS SEPARATOR
    }
)

# Punctuation folded only on the dedupe path: the same headline arrives with
# Persian and Latin punctuation from different CMSs.
_PUNCT_TABLE = str.maketrans(
    {
        "،": ",",   # ARABIC COMMA
        "؛": ";",   # ARABIC SEMICOLON
        "؟": "?",   # ARABIC QUESTION MARK
        "٪": "%",   # ARABIC PERCENT SIGN
        "«": '"',
        "»": '"',
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "–": "-",
        "—": "-",
    }
)

# Harakat, Quranic marks and the superscript alef.  They are pronunciation
# aids: two spellings of one word differ by nothing else.
_DIACRITIC_RE = re.compile(
    "[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u08D3-\u08FF]"
)

# Zero-width and bidi controls other than ZWNJ.  They carry no text, they are
# invisible, and they break every string comparison they appear in.
_INVISIBLE_RE = re.compile(
    "[\u00AD\u200B\u200D\u200E\u200F\u202A-\u202E\u2060-\u2064\u2066-\u2069\uFEFF]"
)

ZWNJ = "\u200C"
# One pass over "whitespace and/or ZWNJ" runs: whitespace anywhere in the run
# means the run was a word break that someone also typed a half-space into.
_ZWNJ_RUN_RE = re.compile(r"[\s\u200C]*\u200C[\s\u200C]*")

_SPACE_RE = re.compile(
    "[\t\n\r\f\v\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]"
)
_WS_RUN_RE = re.compile(r" {2,}")

# Persian function words, the gap ``dedupe.STOPWORDS`` documents ("a Persian
# list must be added before any Persian-language source is approved").  Only
# particles and conjunctions: verbs and plural suffixes are content and
# removing them would collapse genuinely different headlines.
PERSIAN_STOPWORDS = frozenset(
    {
        "از", "به", "با", "در", "بر", "که", "را", "این", "آن", "و", "یا",
        "تا", "هم", "نیز", "برای", "روی", "طی", "پس", "اما", "ولی", "چون",
        "هر", "بی", "بدون", "درباره", "طبق", "توسط",
    }
)

# Blocks whose letters count as Persian/Arabic script for language detection.
_ARABIC_BLOCKS = (
    (0x0600, 0x06FF),  # Arabic
    (0x0750, 0x077F),  # Arabic Supplement
    (0x08A0, 0x08FF),  # Arabic Extended-A
    (0xFB50, 0xFDFF),  # Arabic Presentation Forms-A
    (0xFE70, 0xFEFF),  # Arabic Presentation Forms-B
)
# Below this many letters a ratio is noise, so the answer is 'unknown' rather
# than a coin flip.
MIN_LETTERS_FOR_DETECTION = 3
LANGUAGE_RATIO = 0.6

_BLOCK_TAGS = frozenset(
    {
        "address", "article", "blockquote", "br", "div", "dd", "dl", "dt", "h1",
        "h2", "h3", "h4", "h5", "h6", "hr", "li", "ol", "p", "pre", "section",
        "table", "td", "th", "tr", "ul",
    }
)
# Content of these is markup or code, never article text.
_SKIP_TAGS = frozenset({"head", "iframe", "noscript", "script", "style", "svg", "template"})

_TAG_FALLBACK_RE = re.compile(r"<[^>]*>")


class _PlainTextExtractor(HTMLParser):
    """Collect text nodes only — no DOM, no CSS, no script, no requests.

    "Strip HTML safely" means exactly this: a tolerant tokenizer that keeps
    character data and throws away everything structural.  Nothing here can be
    made to fetch a URL or execute anything, which a rendering engine could.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    @property
    def text(self) -> str:
        return "".join(self._parts)


def strip_html(value: str) -> str:
    """Plain text from (possibly malformed) HTML, without rendering it.

    Never raises: feeds ship broken markup constantly, and an ingestion path
    that dies on a stray ``<`` loses the whole batch.  On a tokenizer failure
    it falls back to tag removal plus entity unescaping.
    """
    if not value:
        return ""
    parser = _PlainTextExtractor()
    try:
        parser.feed(value)
        parser.close()
        return parser.text
    except Exception:  # malformed beyond the tokenizer's tolerance
        return html.unescape(_TAG_FALLBACK_RE.sub(" ", value))


def unify_letters(value: str) -> str:
    """Arabic letter forms folded to their Persian equivalents."""
    return value.translate(_LETTER_TABLE)


def fold_digits(value: str) -> str:
    """Arabic-Indic and Persian digits (and separators) folded to ASCII."""
    return value.translate(_DIGIT_TABLE)


def fold_punctuation(value: str) -> str:
    """Persian/typographic punctuation folded to its ASCII equivalent."""
    return value.translate(_PUNCT_TABLE)


def strip_diacritics(value: str) -> str:
    """Harakat, Quranic marks and tatweel removed."""
    return _DIACRITIC_RE.sub("", value.replace("\u0640", ""))


def normalize_zwnj(value: str) -> str:
    """Half-space cleanup: runs collapsed, spurious ones next to spaces dropped.

    A ZWNJ that touches whitespace is not a half-space — the word break is
    already there — so the run becomes one ordinary space.  A ZWNJ between two
    letters is meaningful and is kept (``normalized_title`` is where it is
    finally dropped, for comparison only).
    """
    def _replace(match: re.Match[str]) -> str:
        return " " if match.group(0).strip(ZWNJ) else ZWNJ

    return _ZWNJ_RUN_RE.sub(_replace, value).strip(" " + ZWNJ)


def collapse_whitespace(value: str) -> str:
    """All Unicode spaces to U+0020, runs collapsed, ends trimmed."""
    return _WS_RUN_RE.sub(" ", _SPACE_RE.sub(" ", value)).strip()


def normalize(value: str) -> str:
    """Canonical analysis form of a Persian/Arabic (or Latin) string.

    Order matters: NFKC first, because it turns Arabic presentation forms
    (``ﻙ`` U+FEDB) into ordinary letters that :data:`LETTER_MAP` can then fold;
    diacritics after the letter fold, so a decomposed hamza that NFKC
    recomposed is handled as a letter rather than as a stray mark.
    """
    if not value:
        return ""
    text = strip_html(str(value))
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE_RE.sub("", text)
    text = unify_letters(text)
    text = fold_digits(text)
    text = strip_diacritics(text)
    text = normalize_zwnj(text)
    return collapse_whitespace(text)


def normalized_title(title: str) -> str:
    """Dedupe key for a headline — the value stored in ``news_articles.title_key``.

    Persian normalization, then the *same* lexical key algorithm the rest of
    the pipeline already uses (:func:`app.news.dedupe.normalize_title`:
    casefold, punctuation stripped, English stopwords removed), then Persian
    function words.  Delegating keeps one key algorithm in the codebase instead
    of two that drift.

    The ZWNJ is removed outright here — ``قیمت‌ها`` and ``قیمتها`` must produce
    one key, and dropping the character is the direction that survives a
    pipeline which lost it.  The rarer typo ``قیمت ها`` (a real space) stays a
    different key and is left to the similarity scan in ``dedupe.py``.
    """
    text = normalize(title)
    text = fold_punctuation(text).replace(ZWNJ, "")
    key = _lexical_key(text)
    return " ".join(token for token in key.split() if token not in PERSIAN_STOPWORDS)


def _script_of(char: str) -> str:
    code = ord(char)
    for low, high in _ARABIC_BLOCKS:
        if low <= code <= high:
            return "fa"
    if char.isascii():
        return "en"
    return "other"


def detect_language(text: str) -> str:
    """``'fa'`` | ``'en'`` | ``'unknown'`` by character-class ratio.

    Character class only, and the label says more than it knows: an Arabic- or
    Urdu-language headline is written in the same block and also returns
    ``'fa'``.  That is acceptable here because it is used to route text to the
    right normalizer and stopword list, not to assert what language a source
    publishes in — ``news_articles.original_language`` should record what the
    *source* declares, and this only fills the gap when it declares nothing.
    """
    counts = {"fa": 0, "en": 0, "other": 0}
    for char in strip_html(text or ""):
        if char.isalpha():
            counts[_script_of(char)] += 1
    total = sum(counts.values())
    if total < MIN_LETTERS_FOR_DETECTION:
        return "unknown"
    if counts["fa"] / total >= LANGUAGE_RATIO:
        return "fa"
    if counts["en"] / total >= LANGUAGE_RATIO:
        return "en"
    return "unknown"
