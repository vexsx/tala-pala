"""Hardened HTTP fetching for news sources.

News collection is the only subsystem that follows URLs which come from a
database row rather than from code, and that parses documents written by
someone else.  Both properties turn an ordinary ``httpx.get`` into a liability:

* **SSRF.** A feed URL (or a redirect a feed URL sends us to) that resolves to
  ``169.254.169.254``, ``127.0.0.1`` or a RFC1918 address turns the collector
  into a proxy for the host's own metadata service and internal ports.  Every
  hop is therefore resolved and every resolved address is checked against the
  non-public ranges *before* a connection is made, and the host must be in an
  allowlist supplied by the caller — the caller derives it from the approved
  source row, so an attacker who can only change a path cannot reach elsewhere.
* **Resource exhaustion.** A feed that answers with a 2 GB body, or with 8 kB
  of gzip that inflates to 8 GB, takes the whole service down.  The body is
  streamed with a wire-byte cap, a decompressed-byte cap and a compression
  ratio guard, and the transfer is aborted mid-stream rather than after.
* **Parser attacks.** XML entity expansion (billion laughs) and deeply nested
  JSON are denial-of-service primitives in the standard library's default
  configuration.  :func:`parse_xml_safely` and :func:`parse_json_safely` are
  the only parsers news code may use.

Two honesty rules this module enforces on its callers:

* ``FetchResult.fetched_at`` is *our* clock — the moment the response was
  received.  It is ingest time, never publication time; a caller that stores it
  in ``news_articles.source_published_at`` is writing a falsehood (see
  migration 0017).
* the User-Agent is the honest project string by default.  It is configurable
  because some sources' own policy asks for a specific UA (the convention
  ``app/providers/base.py`` already documents), not so we can pretend to be a
  browser to get past a block.  A 401/403 is never retried and never worked
  around.

Retry/backoff/courtesy-delay behaviour mirrors :class:`app.providers.base
.Provider` so news traffic is no less polite than price traffic.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as _stdlib_etree

import httpx

from ..providers.base import USER_AGENT

try:  # pragma: no cover - exercised by whichever branch the deployment has
    from defusedxml import ElementTree as _defused_etree
except ImportError:
    _defused_etree = None

log = logging.getLogger(__name__)

# Content types a news source may legitimately answer with.  Anything else
# (octet-stream, images, PDFs) is either a misconfiguration or an attempt to
# feed the parser something it was not written for.
DEFAULT_CONTENT_TYPES = frozenset(
    {
        "application/atom+xml",
        "application/json",
        "application/rss+xml",
        "application/xhtml+xml",
        "application/xml",
        "text/html",
        "text/json",
        "text/plain",
        "text/xml",
    }
)

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# Never retried: an auth wall or bot challenge is a policy answer, not a blip.
NEVER_RETRY_STATUSES = frozenset({401, 403, 407, 451})

MAX_ERROR_DETAIL = 2000

# Parameter names whose value must never reach a log line, an exception string
# or news_collection_attempts.error_detail.
SECRET_PARAM_NAMES = (
    "access_token", "api_key", "apikey", "auth", "authorization", "key",
    "passwd", "password", "private_key", "secret", "session", "sig",
    "signature", "token",
)
_SECRET_QS_RE = re.compile(
    r"(?i)\b(" + "|".join(SECRET_PARAM_NAMES) + r")\s*[=:]\s*([^&\s\"'<>]+)"
)
_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/@\s]+)@")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._~+/=\-]{8,})")

REDACTED = "REDACTED"


# --- exceptions --------------------------------------------------------------


class FetchError(Exception):
    """Base class: one ``except`` clause covers every failure this module has."""


class FetchBlocked(FetchError):
    """Refused by policy before or during the transfer.

    Covers scheme/host/port/IP rejections, redirect violations, disallowed
    content types, undecodable bodies and parser guards.  Never retried: the
    answer would be identical next time.
    """


class FetchTooLarge(FetchError):
    """Body exceeded a wire, decompressed or ratio cap.  Transfer was aborted."""


class FetchTimeout(FetchError):
    """Connect or read deadline expired."""


class FetchHTTPError(FetchError):
    """Non-success HTTP status, or a transport failure with no status at all.

    ``status_code`` is None for connection-level failures; ``retryable`` says
    whether the caller's retry budget should be spent on it.
    """

    def __init__(
        self, message: str, *, status_code: Optional[int] = None, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class FetchParseError(FetchError):
    """Payload was well-formed enough to accept but not to parse."""


# --- redaction ---------------------------------------------------------------


def redact(value: object) -> str:
    """Strip credentials from a string before it is logged or stored.

    Applied to every message this module raises, because news URLs are taken
    from a database row that may legitimately carry an API key, and an
    exception string ends up in ``news_collection_attempts.error_detail``.
    """
    text = str(value)
    text = _USERINFO_RE.sub(rf"\1{REDACTED}@", text)
    text = _SECRET_QS_RE.sub(rf"\1={REDACTED}", text)
    text = _BEARER_RE.sub(rf"\1 {REDACTED}", text)
    return text


# --- policy ------------------------------------------------------------------


@dataclass(frozen=True)
class FetchPolicy:
    """Limits applied to one fetch.  Every field is a hard stop, not a hint."""

    connect_timeout: float = 5.0
    read_timeout: float = 15.0
    # Wire bytes.  4 MiB is ~40x the largest press feed observed; a feed that
    # exceeds it has changed shape and should be looked at, not ingested.
    max_bytes: int = 4 * 1024 * 1024
    # Decompressed bytes: the cap that actually stops a gzip bomb, since the
    # wire cap sees only the compressed size.
    max_decompressed_bytes: int = 16 * 1024 * 1024
    # Secondary bomb guard: text feeds compress ~5-10x, so a ratio above this
    # is not a feed.  Only applied past the floor, where the ratio is meaningful.
    max_compression_ratio: float = 100.0
    compression_ratio_floor_bytes: int = 1024 * 1024
    max_redirects: int = 3
    max_attempts: int = 3
    courtesy_delay: float = 1.0
    backoff_base: float = 0.75
    # https only, and only the default port: an approved source on :9200 is a
    # misconfiguration or an attempt to reach an internal service.
    allowed_ports: tuple[int, ...] = (443,)
    allowed_content_types: frozenset[str] = DEFAULT_CONTENT_TYPES
    user_agent: str = USER_AGENT
    accept: str = (
        "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
        "application/json;q=0.9, text/xml;q=0.8, */*;q=0.1"
    )
    # An IP-literal URL is never a news feed; allowing one only makes the host
    # allowlist easier to sidestep.
    allow_ip_literals: bool = False
    chunk_size: int = 64 * 1024


DEFAULT_POLICY = FetchPolicy()

Resolver = Callable[[str], Sequence[str]]


@dataclass(frozen=True)
class FetchResult:
    """A validated response body plus the evidence about how it was obtained."""

    url: str                      # final URL after redirects
    requested_url: str
    status_code: int
    content: bytes
    text: str
    content_type: str
    charset: str
    bytes_received: int           # wire bytes, for news_collection_attempts
    elapsed_s: float
    redirect_chain: tuple[str, ...]
    resolved_ips: tuple[str, ...]
    # OUR clock: when the response arrived.  Ingest time.  Storing this as a
    # source publication time is exactly the falsehood 0017 forbids.
    fetched_at: datetime


# --- address / target validation ---------------------------------------------


def _default_resolver(host: str) -> list[str]:
    """All A/AAAA answers for ``host`` (every one of them gets validated)."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    seen: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in seen:
            seen.append(address)
    return seen


def _classify_address(raw: str) -> Optional[str]:
    """Reason ``raw`` is not a public destination, or None when it is fine."""
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return "not an IP address"
    # An IPv4-mapped IPv6 address hides a v4 destination behind a v6 literal.
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    # Ordered most specific first: several of these overlap (0.0.0.0 is both
    # unspecified and private, ::1 is both loopback and reserved) and the
    # message should name the reason a reader would recognize.
    if address.is_unspecified:
        return "unspecified address"
    if address.is_loopback:
        return "loopback address"
    if address.is_link_local:
        return "link-local address (cloud metadata range)"
    if address.is_multicast:
        return "multicast address"
    if address.is_private:
        return "private address"
    if address.is_reserved:
        return "reserved address"
    if not address.is_global:
        # Catches the ranges the named checks miss (100.64/10 CGNAT, 192.0.0/24,
        # documentation ranges) without having to enumerate them.
        return "non-global address"
    return None


def _normalize_host(host: str) -> str:
    """Lowercased, IDNA-encoded host, so allowlist comparison is on one form."""
    host = (host or "").strip().rstrip(".").lower()
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            # Leave it as-is: it will simply fail the allowlist check below.
            pass
    return host


def _host_allowed(host: str, allowed_hosts: Iterable[str]) -> bool:
    """Exact match, or subdomain match for entries written as ``.example.org``."""
    for entry in allowed_hosts:
        candidate = _normalize_host(entry)
        if not candidate:
            continue
        if candidate.startswith("."):
            if host == candidate[1:] or host.endswith(candidate):
                return True
        elif host == candidate:
            return True
    return False


def validate_target(
    url: str,
    allowed_hosts: Iterable[str],
    policy: FetchPolicy = DEFAULT_POLICY,
    resolver: Optional[Resolver] = None,
) -> tuple[str, tuple[str, ...]]:
    """Check scheme/host/port and every resolved address.  Returns (host, ips).

    Raises :class:`FetchBlocked` on any violation.  Called for the initial URL
    and again for every redirect hop, because a redirect is an attacker-chosen
    URL even when the first one was not.

    Residual risk, stated plainly: between this resolution and the connection
    the transport makes its own lookup, so a DNS entry that flips to a private
    address in that window (rebinding) is not defeated by resolution alone —
    defeating it would require pinning the socket to the validated IP, which
    breaks TLS hostname verification.  The host allowlist is the control that
    carries the weight: rebinding requires an *approved source's own* DNS to be
    hostile, which is a different and much louder failure.
    """
    resolve = resolver or _default_resolver
    parts = urlsplit((url or "").strip())

    if parts.scheme.lower() != "https":
        raise FetchBlocked(redact(f"scheme {parts.scheme!r} is not https: {url}"))
    if parts.username or parts.password:
        raise FetchBlocked(redact(f"URL carries credentials: {url}"))

    host = _normalize_host(parts.hostname or "")
    if not host:
        raise FetchBlocked(redact(f"URL has no host: {url}"))
    if not _host_allowed(host, allowed_hosts):
        raise FetchBlocked(f"host {host!r} is not in the allowlist for this source")

    try:
        port = parts.port
    except ValueError as exc:
        raise FetchBlocked(redact(f"invalid port in {url}: {exc}")) from exc
    if port is not None and port not in policy.allowed_ports:
        raise FetchBlocked(f"port {port} is not allowed for {host!r}")

    is_ip_literal = _classify_address(host) != "not an IP address"
    if is_ip_literal and not policy.allow_ip_literals:
        raise FetchBlocked(f"IP-literal URLs are not allowed: {host!r}")

    try:
        addresses = list(resolve(host)) if not is_ip_literal else [host]
    except OSError as exc:
        # Fail closed and do not retry here: the registry circuit breaker is
        # what handles a source whose DNS is down.
        raise FetchBlocked(f"cannot resolve {host!r}: {exc}") from exc
    if not addresses:
        raise FetchBlocked(f"{host!r} resolved to no addresses")

    for address in addresses:
        reason = _classify_address(address)
        if reason is not None:
            raise FetchBlocked(f"{host!r} resolves to {address} ({reason})")
    return host, tuple(addresses)


# --- response validation -----------------------------------------------------


def _split_content_type(raw: str) -> tuple[str, str]:
    """(media type, charset) from a Content-Type header, both lowercased."""
    main, _, params = (raw or "").partition(";")
    charset = ""
    for param in params.split(";"):
        name, _, value = param.partition("=")
        if name.strip().lower() == "charset":
            charset = value.strip().strip('"').lower()
    return main.strip().lower(), charset


def _check_content_type(media_type: str, policy: FetchPolicy, url: str) -> None:
    if not media_type:
        raise FetchBlocked(redact(f"{url}: response has no Content-Type"))
    if media_type in policy.allowed_content_types:
        return
    # Structured-syntax suffixes (application/vnd.example+json) are the same
    # payload shape under a vendor name.
    if media_type.startswith(("application/", "text/")) and media_type.endswith(
        ("+json", "+xml")
    ):
        return
    raise FetchBlocked(redact(f"{url}: disallowed Content-Type {media_type!r}"))


_XML_DECL_ENCODING_RE = re.compile(
    rb"""<\?xml[^>]*?encoding\s*=\s*["']([A-Za-z0-9._\-]+)["']"""
)


def _declared_charset(content: bytes, header_charset: str) -> str:
    """Charset the source says it used: HTTP header, else the XML declaration.

    Persian feeds still ship windows-1256 occasionally, so defaulting to UTF-8
    without looking at the declaration would corrupt titles — and a corrupted
    title silently produces a wrong dedupe key.
    """
    if header_charset:
        return header_charset
    match = _XML_DECL_ENCODING_RE.search(content[:200])
    if match:
        return match.group(1).decode("ascii", "ignore").lower()
    return "utf-8"


def _decode_strictly(content: bytes, charset: str, url: str) -> str:
    """Decode with the declared charset, strictly.

    Strict on purpose: ``errors='replace'`` would hand the pipeline mojibake
    that looks like text, and every downstream hash and dedupe key would be
    computed over corruption we could not later detect.
    """
    if not content:
        return ""
    try:
        return content.decode(charset)
    except LookupError as exc:
        raise FetchBlocked(redact(f"{url}: unknown charset {charset!r}")) from exc
    except UnicodeDecodeError as exc:
        if charset.replace("_", "-") != "utf-8":
            try:
                return content.decode("utf-8")
            except UnicodeDecodeError:
                pass
        raise FetchBlocked(
            redact(f"{url}: body is not decodable as {charset!r}: {exc.reason}")
        ) from exc


def _read_capped(response: httpx.Response, policy: FetchPolicy, url: str) -> bytes:
    """Stream the body, aborting the transfer the moment any cap is passed."""
    declared_length = response.headers.get("content-length")
    if declared_length and declared_length.isdigit():
        if int(declared_length) > policy.max_bytes:
            raise FetchTooLarge(
                redact(
                    f"{url}: Content-Length {declared_length} exceeds "
                    f"max_bytes={policy.max_bytes}"
                )
            )

    chunks: list[bytes] = []
    decoded = 0
    # Never read a chunk larger than the cap itself, so the overshoot before
    # the guard fires is bounded by the cap rather than by the chunk size.
    chunk_size = max(1, min(policy.chunk_size, policy.max_bytes))
    for chunk in response.iter_bytes(chunk_size=chunk_size):
        decoded += len(chunk)
        wire = response.num_bytes_downloaded
        if wire > policy.max_bytes:
            raise FetchTooLarge(
                redact(f"{url}: body exceeded max_bytes={policy.max_bytes} on the wire")
            )
        if decoded > policy.max_decompressed_bytes:
            raise FetchTooLarge(
                redact(
                    f"{url}: decompressed body exceeded "
                    f"max_decompressed_bytes={policy.max_decompressed_bytes}"
                )
            )
        if (
            decoded > policy.compression_ratio_floor_bytes
            and wire > 0
            and decoded / wire > policy.max_compression_ratio
        ):
            raise FetchTooLarge(
                redact(
                    f"{url}: compression ratio {decoded / wire:.0f}x exceeds "
                    f"{policy.max_compression_ratio:.0f}x (decompression bomb)"
                )
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _strip_credential_headers(headers: dict[str, str]) -> dict[str, str]:
    """Drop caller credentials when a redirect crosses to another host."""
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in ("authorization", "cookie", "proxy-authorization")
    }


# --- fetch -------------------------------------------------------------------


def _attempt(
    url: str,
    allowed_hosts: Iterable[str],
    policy: FetchPolicy,
    headers: dict[str, str],
    resolver: Optional[Resolver],
    client: httpx.Client,
) -> FetchResult:
    started = time.monotonic()
    current = url
    request_headers = dict(headers)
    origin_host, resolved = validate_target(current, allowed_hosts, policy, resolver)
    chain: list[str] = [current]

    for _hop in range(policy.max_redirects + 1):
        with client.stream("GET", current, headers=request_headers) as response:
            status = response.status_code

            if status in REDIRECT_STATUSES:
                location = response.headers.get("location", "")
                if not location:
                    raise FetchHTTPError(
                        redact(f"{current}: HTTP {status} without a Location header"),
                        status_code=status,
                    )
                # The body of a redirect is never read: it is not our payload
                # and reading it only spends bandwidth on an untrusted host.
                target = urljoin(current, location)
                if len(chain) > policy.max_redirects:
                    raise FetchBlocked(
                        redact(
                            f"{url}: more than {policy.max_redirects} redirects "
                            f"(last hop {target})"
                        )
                    )
                host, resolved = validate_target(target, allowed_hosts, policy, resolver)
                if host != origin_host:
                    request_headers = _strip_credential_headers(request_headers)
                    origin_host = host
                current = target
                chain.append(current)
                continue

            if status in NEVER_RETRY_STATUSES:
                raise FetchHTTPError(
                    redact(
                        f"{current}: access denied (HTTP {status}); not retried, "
                        "not worked around"
                    ),
                    status_code=status,
                    retryable=False,
                )
            if status == 429 or status >= 500:
                raise FetchHTTPError(
                    redact(f"{current}: transient HTTP {status}"),
                    status_code=status,
                    retryable=True,
                )
            if status >= 400:
                raise FetchHTTPError(
                    redact(f"{current}: HTTP {status}"), status_code=status
                )

            media_type, header_charset = _split_content_type(
                response.headers.get("content-type", "")
            )
            _check_content_type(media_type, policy, current)
            content = _read_capped(response, policy, current)
            wire_bytes = response.num_bytes_downloaded or len(content)

        charset = _declared_charset(content, header_charset)
        text = _decode_strictly(content, charset, current)
        return FetchResult(
            url=current,
            requested_url=url,
            status_code=status,
            content=content,
            text=text,
            content_type=media_type,
            charset=charset,
            bytes_received=wire_bytes,
            elapsed_s=time.monotonic() - started,
            redirect_chain=tuple(chain),
            resolved_ips=resolved,
            fetched_at=datetime.now(timezone.utc),
        )

    raise FetchBlocked(redact(f"{url}: more than {policy.max_redirects} redirects"))


def fetch(
    url: str,
    *,
    allowed_hosts: Iterable[str],
    policy: FetchPolicy = DEFAULT_POLICY,
    headers: Optional[Mapping[str, str]] = None,
    resolver: Optional[Resolver] = None,
    transport: Optional[httpx.BaseTransport] = None,
) -> FetchResult:
    """GET ``url`` under ``policy``, or raise a :class:`FetchError` subclass.

    ``allowed_hosts`` is required and has no default: it comes from the
    approved source row, which is what keeps a database-supplied URL from
    reaching an arbitrary destination.  ``resolver`` and ``transport`` exist so
    the guards can be tested without a network.

    Retries follow ``app/providers/base.py``: bounded attempts with exponential
    backoff, only for timeouts, 429 and 5xx.  A blocked target, an oversized
    body and an auth wall are permanent answers and are raised immediately.
    """
    allowed = tuple(allowed_hosts)
    if not allowed:
        raise FetchBlocked("no allowed hosts supplied; refusing to fetch")

    request_headers = {
        "User-Agent": policy.user_agent,
        "Accept": policy.accept,
        # Compression is accepted (feeds are large and this is the polite
        # thing to do); the decompressed cap is what makes it safe.
        "Accept-Encoding": "gzip, deflate",
    }
    if headers:
        request_headers.update({str(k): str(v) for k, v in headers.items()})

    timeout = httpx.Timeout(
        connect=policy.connect_timeout,
        read=policy.read_timeout,
        write=policy.read_timeout,
        pool=policy.connect_timeout,
    )
    last_error: Optional[FetchError] = None

    for attempt in range(max(1, policy.max_attempts)):
        if policy.courtesy_delay > 0:
            time.sleep(policy.courtesy_delay)
        client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,  # redirects are walked by hand and revalidated
            transport=transport,
            trust_env=False,  # an env proxy would bypass every check above
        )
        try:
            return _attempt(url, allowed, policy, request_headers, resolver, client)
        except httpx.TimeoutException as exc:
            last_error = FetchTimeout(
                redact(f"{url}: timed out after {policy.read_timeout}s ({exc!r})")
            )
        except httpx.HTTPError as exc:
            last_error = FetchHTTPError(
                redact(f"{url}: transport failure: {exc}"), retryable=True
            )
        except FetchHTTPError as exc:
            if not exc.retryable:
                raise
            last_error = exc
        finally:
            client.close()

        if attempt < policy.max_attempts - 1:
            log.debug(
                "safefetch retry %d/%d: %s", attempt + 1, policy.max_attempts, last_error
            )
            if policy.backoff_base > 0:
                time.sleep(policy.backoff_base * (2**attempt))

    if last_error is None:  # unreachable: the loop runs at least once
        raise FetchError(redact(f"{url}: fetch produced neither a result nor an error"))
    raise last_error


# --- parsers -----------------------------------------------------------------

# A DOCTYPE has no place in a feed or an API response, and it is the entry
# point for every entity attack.  Rejecting it in the prolog (the only place it
# is legal) is a stronger guard than any parser setting, and it is the guard
# that still holds when defusedxml is not installed.
_PROLOG_END_RE = re.compile(r"<[A-Za-z_]")
_DOCTYPE_RE = re.compile(r"<!\s*DOCTYPE", re.IGNORECASE)
_ENTITY_DECL_RE = re.compile(r"<!\s*ENTITY", re.IGNORECASE)


def _reject_doctype(text: str, label: str) -> None:
    match = _PROLOG_END_RE.search(text)
    prolog = text[: match.start()] if match else text
    if _DOCTYPE_RE.search(prolog) or _ENTITY_DECL_RE.search(prolog):
        raise FetchBlocked(
            f"{label}: document declares a DTD/entity; refused before parsing "
            "(entity expansion and external entity resolution are attacks, not "
            "features, in a feed)"
        )


def _prepare_xml_bytes(data: bytes | str) -> tuple[bytes, str]:
    """Bytes for the parser plus the text used for the prolog guard.

    A ``str`` input has already been decoded, so any encoding declaration it
    still carries is stale: expat would re-decode the UTF-8 we hand it under
    the source's original charset and either fail or mangle every Persian
    character.  The declaration is rewritten to utf-8 to match the bytes.
    """
    if isinstance(data, str):
        text = data
        encoded = text.encode("utf-8")
        match = _XML_DECL_ENCODING_RE.search(encoded[:200])
        if match and match.group(1).lower() not in (b"utf-8", b"utf8"):
            encoded = encoded[: match.start(1)] + b"utf-8" + encoded[match.end(1):]
        return encoded, text
    return data, data.decode("utf-8", "replace")


def parse_xml_safely(
    data: bytes | str, *, max_bytes: int = DEFAULT_POLICY.max_decompressed_bytes
) -> _stdlib_etree.Element:
    """Parse XML with entity expansion and external entities disabled.

    Uses ``defusedxml`` when it is installed.  It is deliberately not a hard
    requirement — ``requirements.txt`` is unchanged by this module, so the
    branch that actually runs today is the stdlib one — and the stdlib branch
    is safe for a specific reason worth stating rather than assuming:
    :mod:`xml.etree.ElementTree` never installs an external-entity handler, so
    a ``SYSTEM`` entity is not fetched (no file or SSRF disclosure), but expat
    *does* expand entities declared in an internal subset, which is the
    billion-laughs amplification.  The prolog DOCTYPE rejection above closes
    exactly that hole, and it runs on both branches so the two behave alike.
    """
    if len(data) > max_bytes:
        raise FetchTooLarge(f"XML payload of {len(data)} bytes exceeds {max_bytes}")
    payload, text = _prepare_xml_bytes(data)
    _reject_doctype(text, "XML")
    try:
        if _defused_etree is not None:
            return _defused_etree.fromstring(
                payload, forbid_dtd=True, forbid_entities=True, forbid_external=True
            )
        return _stdlib_etree.fromstring(payload)
    except FetchError:
        raise
    except Exception as exc:  # ParseError, defusedxml's EntitiesForbidden, ...
        raise FetchParseError(redact(f"XML parse failed: {exc}")) from exc


def _json_depth_ok(text: str, max_depth: int) -> bool:
    """True when bracket nesting stays within ``max_depth``.

    Checked before :func:`json.loads` because the stdlib decoder has no depth
    limit: deep nesting recurses until the interpreter's stack gives out, and a
    RecursionError inside a C extension is not a failure mode worth relying on.
    String contents are skipped so a bracket in a headline does not count.
    """
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > max_depth:
                return False
        elif char in "]}":
            depth -= 1
    return True


def _reject_constant(name: str) -> Any:
    raise FetchBlocked(
        f"JSON contains the non-standard constant {name!r}; refused "
        "(NaN/Infinity silently poison every downstream numeric aggregate)"
    )


def parse_json_safely(
    data: bytes | str,
    *,
    max_bytes: int = DEFAULT_POLICY.max_decompressed_bytes,
    max_depth: int = 64,
) -> Any:
    """Parse JSON with size and nesting limits, and no NaN/Infinity."""
    if len(data) > max_bytes:
        raise FetchTooLarge(f"JSON payload of {len(data)} bytes exceeds {max_bytes}")
    text = data.decode("utf-8", "strict") if isinstance(data, bytes) else data
    if not _json_depth_ok(text, max_depth):
        raise FetchBlocked(f"JSON nesting exceeds max_depth={max_depth}")
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except FetchError:
        raise
    except ValueError as exc:
        raise FetchParseError(redact(f"JSON parse failed: {exc}")) from exc
