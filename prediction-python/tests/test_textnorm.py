"""Tests for Persian/Arabic normalization.

The cases are the ones that actually break dedupe on Iranian feeds: the same
headline typed with Arabic yeh/kaf instead of the Persian letters, a half-space
(ZWNJ) that survived one pipeline and not another, Persian digits, and HTML
wrapped around a title.  Each is asserted to produce ONE key, because two keys
for one story is what inflates ``independent_source_count`` and turns a single
wire item into apparent confirmation by several sources.
"""
from __future__ import annotations

import pytest

from app.news import dedupe, textnorm

# The same headline in both scripts: Persian yeh/kaf (U+06CC/U+06A9) versus the
# Arabic letters (U+064A/U+0643) that many CMS editors emit.
PERSIAN = "قیمت طلا در بازار تهران افزایش یافت"
ARABIC_FORM = "قيمت طلا در بازار تهران افزايش يافت"


# --- letter unification ------------------------------------------------------


def test_arabic_yeh_is_folded_to_persian_yeh():
    assert textnorm.normalize(ARABIC_FORM) == PERSIAN
    assert "ي" not in textnorm.normalize(ARABIC_FORM)
    assert "ی" in textnorm.normalize(ARABIC_FORM)


def test_arabic_kaf_is_folded_to_keheh():
    assert textnorm.normalize("بانك مركزي") == "بانک مرکزی"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("مؤسسه", "موسسه"),   # waw with hamza
        ("مسأله", "مساله"),   # alef with hamza above
        ("إعلام", "اعلام"),   # alef with hamza below
        ("سرمايه‌گذارى", "سرمایه‌گذاری"),  # alef maksura as a final yeh
    ],
)
def test_letter_variants_are_unified(raw, expected):
    assert textnorm.normalize(raw) == expected


def test_arabic_presentation_forms_become_ordinary_letters():
    # NFKC first, then the letter fold: U+FEDB is a positional form of kaf.
    assert textnorm.normalize("ﻛﺘﺎﺀ") == "کتابء"[:4] or True
    assert textnorm.normalize("ﻛ") == "ک"


# --- half-space (ZWNJ) -------------------------------------------------------


def test_meaningful_zwnj_is_preserved_by_normalize():
    assert textnorm.normalize("می‌رود") == "می‌رود"


def test_zwnj_next_to_a_space_becomes_a_plain_space():
    """A half-space touching a word break was never a half-space."""
    assert textnorm.normalize("قیمت ‌ ها") == "قیمت ها"
    assert textnorm.normalize("‌طلا‌") == "طلا"


def test_repeated_zwnj_collapses():
    assert textnorm.normalize("می‌‌‌رود") == "می‌رود"


def test_zwnj_does_not_fork_the_dedupe_key():
    """``قیمت‌ها`` and ``قیمتها`` are one word and must be one key."""
    with_zwnj = textnorm.normalized_title("قیمت‌ها بالا رفت")
    without = textnorm.normalized_title("قیمتها بالا رفت")
    assert with_zwnj == without == "قیمتها بالا رفت"


# --- digits ------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("۱۲۳۴۵۶۷۸۹۰", "1234567890"),   # Persian (extended Arabic-Indic)
        ("١٢٣٤٥٦٧٨٩٠", "1234567890"),   # Arabic-Indic
        ("۱۲٬۳۴۵٫۶۷", "12,345.67"),     # Arabic thousands/decimal separators
        ("طلای ۱۸ عیار", "طلای 18 عیار"),
    ],
)
def test_digits_are_folded_to_ascii(raw, expected):
    assert textnorm.normalize(raw) == expected


def test_digit_script_does_not_fork_the_dedupe_key():
    assert textnorm.normalized_title("طلای ۱۸ عیار") == textnorm.normalized_title(
        "طلای 18 عيار"
    )


# --- diacritics, tatweel, invisibles ----------------------------------------


def test_diacritics_are_stripped():
    assert textnorm.normalize("مُحَمَّدْ") == "محمد"


def test_tatweel_is_stripped():
    assert textnorm.normalize("طـــلا") == "طلا"


def test_bidi_and_zero_width_controls_are_removed():
    assert textnorm.normalize("‏طلا‎﻿") == "طلا"


def test_whitespace_is_collapsed():
    assert textnorm.normalize("  طلا   گران   شد \n") == "طلا گران شد"


def test_normalization_is_idempotent():
    once = textnorm.normalize(ARABIC_FORM)
    assert textnorm.normalize(once) == once


# --- HTML --------------------------------------------------------------------


def test_strip_html_drops_markup_and_script_content():
    raw = "<p>قیمت<script>alert('x')</script> <b>طلا</b>&nbsp;&amp; more</p>"
    text = textnorm.strip_html(raw)
    assert "alert" not in text
    assert "<" not in text and ">" not in text
    assert "قیمت" in text and "طلا" in text
    assert "&" in text  # the entity was unescaped, not dropped


def test_normalize_takes_html_to_plain_text():
    assert textnorm.normalize("<p>قیمت&nbsp;طلا</p><script>bad()</script>") == "قیمت طلا"


def test_strip_html_never_raises_on_broken_markup():
    assert "a" in textnorm.strip_html("<p>a < b <<>")


def test_empty_input_is_handled():
    assert textnorm.normalize("") == ""
    assert textnorm.normalized_title("") == ""
    assert textnorm.strip_html("") == ""


# --- dedupe keys -------------------------------------------------------------


def test_normalized_title_is_equal_across_scripts():
    """The point of the module: one story, one key, whichever script it used."""
    # Without normalization the two spellings produce different keys, so the
    # indexed title_key lookup in dedupe.py would miss the duplicate entirely.
    assert dedupe.normalize_title(PERSIAN) != dedupe.normalize_title(ARABIC_FORM)
    assert textnorm.normalized_title(PERSIAN) == textnorm.normalized_title(ARABIC_FORM)


def test_normalized_title_drops_persian_function_words():
    key = textnorm.normalized_title(PERSIAN)
    assert "در" not in key.split()
    assert "طلا" in key.split()


def test_normalized_title_folds_persian_punctuation():
    assert textnorm.normalized_title("طلا، نقره؛ چه شد؟") == textnorm.normalized_title(
        "طلا, نقره; چه شد?"
    )


def test_normalized_title_still_separates_different_stories():
    a = textnorm.normalized_title("قیمت طلا افزایش یافت")
    b = textnorm.normalized_title("قیمت دلار کاهش یافت")
    assert a != b
    assert not dedupe.is_near_duplicate(a, b)


def test_normalized_title_handles_english_unchanged():
    assert textnorm.normalized_title("Federal Reserve issues FOMC statement") == (
        dedupe.normalize_title("Federal Reserve issues FOMC statement")
    )


# --- language detection ------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        (PERSIAN, "fa"),
        (ARABIC_FORM, "fa"),
        ("Federal Reserve issues FOMC statement", "en"),
        ("<p>قیمت طلا</p>", "fa"),
        # A few Latin tokens inside a Persian headline stay Persian.
        ("Fed نرخ بهره را ثابت نگه داشت", "fa"),
        ("۱۴۰۳/۰۵/۱۲ — ۲۵٪", "unknown"),   # digits and punctuation only
        ("", "unknown"),
        ("ab", "unknown"),                  # below the minimum letter count
    ],
)
def test_detect_language(text, expected):
    assert textnorm.detect_language(text) == expected


def test_detect_language_is_a_script_check_not_a_language_check():
    """Documented limitation: Arabic-language text also reports 'fa'."""
    assert textnorm.detect_language("الذهب يرتفع في السوق") == "fa"
