"""Article dedupe primitives: canonical URLs, content hashes, near-duplicate titles.

Pure functions — no network, no state, no model.  Deliberately dependency-free:
the deployment is a single small host, and an embedding model (or the torch
stack a sentence-transformer drags in) has no place on an ingestion path whose
entire job is to notice that two strings are the same story.

Lexical similarity is enough for the two shapes that actually occur in a press
feed: the same release re-issued under a new URL, and a headline lightly
reworded between the feed and the page.  Both are caught by an edit ratio over
the normalized title combined with a token-set overlap — the ratio handles
small wording changes, the overlap handles reordered clauses that an edit ratio
punishes disproportionately.
"""
from __future__ import annotations

import hashlib
import html
import re
from difflib import SequenceMatcher
from typing import Iterable, Optional, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

K = TypeVar("K")

# Campaign/tracking parameters carry no identity: the same article arrives with
# different values from different placements.
TRACKING_PREFIXES = ("utm_",)
TRACKING_PARAMS = frozenset(
    {
        "fbclid", "gclid", "dclid", "msclkid", "igshid", "mc_cid", "mc_eid",
        "ref", "ref_src", "referrer", "src", "source", "cmpid", "ncid",
        "s_cid", "spm", "at_medium", "at_campaign",
    }
)
DEFAULT_PORTS = {"http": "80", "https": "443"}

# Titles are compared after these are dropped: they carry no topic information
# and dilute the token-set overlap.  English only, which is sufficient while
# every approved source publishes in English; a Persian list must be added
# before any Persian-language source is approved.
STOPWORDS = frozenset(
    {
        "a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on",
        "or", "the", "to", "with",
    }
)

# Above this, two titles are treated as the same story.  Tuned to accept a
# one-word rewording of a ~7-word headline and reject unrelated releases from
# the same institution, which share a lot of boilerplate vocabulary.
NEAR_DUPLICATE_THRESHOLD = 0.85

_TAG_RE = re.compile(r"<[^>]+>")
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+", re.UNICODE)


def normalize_text(value: str) -> str:
    """Casefolded, markup-free, punctuation-free, whitespace-collapsed text.

    Used for hashing and for title comparison, so a markup or capitalization
    change alone does not register as an edit (which would churn versions) but
    a genuine wording change does.
    """
    if not value:
        return ""
    text = html.unescape(str(value))
    text = _TAG_RE.sub(" ", text)
    text = _NON_WORD_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip().casefold()


def canonical_url(url: str) -> str:
    """Normalized URL used as the per-source dedupe key.

    Lowercases scheme and host, drops ``www.``, default ports, any userinfo
    (never store credentials), tracking parameters and the fragment, and sorts
    the surviving query so parameter order cannot fork one article into two.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if not parts.scheme:
        # Feeds occasionally emit a scheme-less URL ("www.example.org/x").
        # Assuming https keeps the key deterministic, which is what a dedupe
        # key needs even when the input is malformed.
        parts = urlsplit("https://" + raw.lstrip("/"))
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    netloc = host
    port = parts.port
    if port is not None and str(port) != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"

    path = parts.path or ""
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    if path == "/":
        path = ""

    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
        and not key.lower().startswith(TRACKING_PREFIXES)
    ]
    query = urlencode(sorted(kept))

    return urlunsplit((scheme, netloc, path, query, ""))


def content_hash(title: str, summary: str = "", body: str = "") -> str:
    """sha256 over the normalized title/summary/body — the edit detector.

    Fields are joined with a separator that normalization can never produce, so
    text cannot migrate between fields without changing the hash.
    """
    payload = "\n".join(normalize_text(part) for part in (title, summary, body))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def title_tokens(title: str) -> frozenset[str]:
    """Content-word token set of a title (stopwords removed)."""
    return frozenset(
        token for token in normalize_text(title).split() if token not in STOPWORDS
    )


def normalize_title(title: str) -> str:
    """Normalized, stopword-free title — stored as ``news_articles.title_key``.

    Two articles whose keys are equal are the same story by definition, which
    makes the indexed key an exact-match fast path in front of the (linear)
    similarity scan.
    """
    return " ".join(
        token for token in normalize_text(title).split() if token not in STOPWORDS
    )


def title_similarity(left: str, right: str) -> float:
    """Similarity of two titles in [0, 1]; 0 when either normalizes to nothing.

    ``max`` of an edit ratio and a token-set Jaccard: the first catches small
    rewordings, the second catches reordered clauses that the edit ratio
    underrates.  Taking the max is deliberate — either signal alone is evidence
    of the same story.
    """
    a, b = normalize_title(left), normalize_title(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    tokens_a, tokens_b = title_tokens(left), title_tokens(right)
    union = tokens_a | tokens_b
    jaccard = len(tokens_a & tokens_b) / len(union) if union else 0.0
    return max(ratio, jaccard)


def is_near_duplicate(
    left: str, right: str, threshold: float = NEAR_DUPLICATE_THRESHOLD
) -> bool:
    """True when two titles describe the same story."""
    return title_similarity(left, right) >= threshold


def find_near_duplicate(
    title: str,
    candidates: Iterable[tuple[K, str]],
    threshold: float = NEAR_DUPLICATE_THRESHOLD,
) -> Optional[K]:
    """Key of the best-matching candidate ``(key, title)`` above ``threshold``.

    Returns None when nothing matches.  Ties keep the first candidate, so
    passing candidates oldest-first collapses duplicates onto the original.
    """
    best_key: Optional[K] = None
    best_score = 0.0
    for key, candidate_title in candidates:
        score = title_similarity(title, candidate_title)
        if score >= threshold and score > best_score:
            best_key, best_score = key, score
    return best_key
