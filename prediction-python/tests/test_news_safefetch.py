"""Tests for the hardened news fetcher.

Every test drives ``app.news.safefetch`` through an ``httpx.MockTransport`` and
an injected resolver, so no test touches DNS or the network — the guards are
exercised against payloads built here and the ``fed_press.xml`` fixture.

The assertions are about refusals, not about happy paths: an SSRF target, a
redirect into the metadata range, an 8 MB gzip bomb and an XXE payload are the
inputs this module exists for, so they are the inputs it is tested with.
"""
from __future__ import annotations

import gzip
from dataclasses import replace

import httpx
import pytest

from app.news import safefetch
from app.news.safefetch import (
    FetchBlocked,
    FetchHTTPError,
    FetchParseError,
    FetchPolicy,
    FetchTimeout,
    FetchTooLarge,
)

from .conftest import load_fixture_text

HOST = "news.example.org"
OTHER_HOST = "internal.example.org"
URL = f"https://{HOST}/feed.xml"
ALLOWED = frozenset({HOST})

# No courtesy delay and no backoff: these tests assert control flow, not
# politeness, and the real defaults would add seconds per retry test.
FAST = FetchPolicy(courtesy_delay=0.0, backoff_base=0.0, max_attempts=1)

PUBLIC_IP = "93.184.216.34"


def public_resolver(host: str) -> list[str]:
    return [PUBLIC_IP]


class Recorder:
    """Mock transport that records the URLs it was asked for."""

    def __init__(self, handler) -> None:
        self.urls: list[str] = []
        self.headers: list[httpx.Headers] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        self.headers.append(request.headers)
        return self._handler(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def xml_response(body: str = "<rss/>", status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=body.encode("utf-8"), headers={"content-type": "text/xml"}
    )


# --- SSRF: target validation -------------------------------------------------


def test_non_https_scheme_is_blocked():
    recorder = Recorder(lambda request: xml_response())
    with pytest.raises(FetchBlocked, match="not https"):
        safefetch.fetch(
            f"http://{HOST}/feed.xml",
            allowed_hosts=ALLOWED,
            policy=FAST,
            resolver=public_resolver,
            transport=recorder.transport,
        )
    assert recorder.urls == []


def test_host_outside_allowlist_is_blocked():
    recorder = Recorder(lambda request: xml_response())
    with pytest.raises(FetchBlocked, match="allowlist"):
        safefetch.fetch(
            "https://evil.example.com/feed.xml",
            allowed_hosts=ALLOWED,
            policy=FAST,
            resolver=public_resolver,
            transport=recorder.transport,
        )
    assert recorder.urls == []


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "192.168.1.10",
        "172.16.0.9",
        # The one that matters most: the cloud metadata endpoint.
        "169.254.169.254",
        "::1",
        "fe80::1",
        # An IPv4 loopback smuggled inside an IPv6 literal.
        "::ffff:127.0.0.1",
        "100.64.0.1",   # CGNAT shared space
        "198.18.0.1",   # benchmarking range
        "0.0.0.0",
        "224.0.0.1",    # multicast
    ],
)
def test_private_addresses_are_rejected_before_any_request(address):
    recorder = Recorder(lambda request: xml_response())
    with pytest.raises(FetchBlocked) as excinfo:
        safefetch.fetch(
            URL,
            allowed_hosts=ALLOWED,
            policy=FAST,
            resolver=lambda host: [address],
            transport=recorder.transport,
        )
    # The refusal names the address it refused, whichever non-public category
    # this interpreter's ipaddress module files it under.
    assert address in str(excinfo.value)
    # The check is pre-connection: nothing was sent anywhere.
    assert recorder.urls == []


@pytest.mark.parametrize(
    "address, expected",
    [
        ("127.0.0.1", "loopback address"),
        ("::1", "loopback address"),
        ("::ffff:127.0.0.1", "loopback address"),
        ("10.1.2.3", "private address"),
        ("192.168.1.10", "private address"),
        ("169.254.169.254", "link-local address (cloud metadata range)"),
        ("224.0.0.1", "multicast address"),
        ("0.0.0.0", "unspecified address"),
        (PUBLIC_IP, None),
    ],
)
def test_address_classification_names_the_category(address, expected):
    assert safefetch._classify_address(address) == expected


def test_any_private_answer_rejects_the_whole_host():
    """A host with one public and one private A record is still refused."""
    recorder = Recorder(lambda request: xml_response())
    with pytest.raises(FetchBlocked, match="private"):
        safefetch.fetch(
            URL,
            allowed_hosts=ALLOWED,
            policy=FAST,
            resolver=lambda host: [PUBLIC_IP, "10.0.0.5"],
            transport=recorder.transport,
        )
    assert recorder.urls == []


def test_ip_literal_url_is_blocked():
    with pytest.raises(FetchBlocked, match="IP-literal"):
        safefetch.fetch(
            f"https://{PUBLIC_IP}/feed.xml",
            allowed_hosts={PUBLIC_IP},
            policy=FAST,
            resolver=public_resolver,
        )


def test_non_default_port_is_blocked():
    with pytest.raises(FetchBlocked, match="port"):
        safefetch.fetch(
            f"https://{HOST}:9200/feed.xml",
            allowed_hosts=ALLOWED,
            policy=FAST,
            resolver=public_resolver,
        )


def test_credentials_in_url_are_blocked_and_redacted():
    with pytest.raises(FetchBlocked) as excinfo:
        safefetch.fetch(
            f"https://user:hunter2@{HOST}/feed.xml",
            allowed_hosts=ALLOWED,
            policy=FAST,
            resolver=public_resolver,
        )
    assert "hunter2" not in str(excinfo.value)


def test_unresolvable_host_fails_closed():
    def resolver(host: str):
        raise OSError("Name or service not known")

    with pytest.raises(FetchBlocked, match="cannot resolve"):
        safefetch.fetch(URL, allowed_hosts=ALLOWED, policy=FAST, resolver=resolver)


# --- SSRF: redirects ---------------------------------------------------------


def test_redirect_to_private_address_is_rejected():
    """The first hop is public and legitimate; the second resolves to metadata."""
    recorder = Recorder(
        lambda request: httpx.Response(
            302, headers={"location": f"https://{OTHER_HOST}/internal"}
        )
    )

    def resolver(host: str) -> list[str]:
        return [PUBLIC_IP] if host == HOST else ["169.254.169.254"]

    with pytest.raises(FetchBlocked, match="link-local"):
        safefetch.fetch(
            URL,
            allowed_hosts={HOST, OTHER_HOST},
            policy=FAST,
            resolver=resolver,
            transport=recorder.transport,
        )
    # The redirect target was validated, not requested.
    assert recorder.urls == [URL]


def test_redirect_to_unlisted_host_is_rejected():
    recorder = Recorder(
        lambda request: httpx.Response(
            302, headers={"location": "https://evil.example.com/x"}
        )
    )
    with pytest.raises(FetchBlocked, match="allowlist"):
        safefetch.fetch(
            URL,
            allowed_hosts=ALLOWED,
            policy=FAST,
            resolver=public_resolver,
            transport=recorder.transport,
        )
    assert recorder.urls == [URL]


def test_redirect_to_http_is_rejected():
    recorder = Recorder(
        lambda request: httpx.Response(302, headers={"location": f"http://{HOST}/x"})
    )
    with pytest.raises(FetchBlocked, match="not https"):
        safefetch.fetch(
            URL,
            allowed_hosts=ALLOWED,
            policy=FAST,
            resolver=public_resolver,
            transport=recorder.transport,
        )


def test_redirect_limit_is_enforced():
    def handler(request: httpx.Request) -> httpx.Response:
        step = int(request.url.params.get("n", "0"))
        return httpx.Response(
            302, headers={"location": f"https://{HOST}/feed.xml?n={step + 1}"}
        )

    recorder = Recorder(handler)
    with pytest.raises(FetchBlocked, match="redirects"):
        safefetch.fetch(
            URL,
            allowed_hosts=ALLOWED,
            policy=FAST,
            resolver=public_resolver,
            transport=recorder.transport,
        )
    # The initial request plus max_redirects hops, and not one more.
    assert len(recorder.urls) == FAST.max_redirects + 1


def test_redirect_within_limit_is_followed_and_recorded():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/feed.xml":
            return httpx.Response(301, headers={"location": f"https://{HOST}/feed-v2.xml"})
        return xml_response("<rss><channel/></rss>")

    recorder = Recorder(handler)
    result = safefetch.fetch(
        URL,
        allowed_hosts=ALLOWED,
        policy=FAST,
        resolver=public_resolver,
        transport=recorder.transport,
    )
    assert result.url == f"https://{HOST}/feed-v2.xml"
    assert result.requested_url == URL
    assert result.redirect_chain == (URL, f"https://{HOST}/feed-v2.xml")


def test_cross_host_redirect_drops_caller_credentials():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == HOST:
            return httpx.Response(302, headers={"location": f"https://{OTHER_HOST}/x"})
        return xml_response()

    recorder = Recorder(handler)
    safefetch.fetch(
        URL,
        allowed_hosts={HOST, OTHER_HOST},
        policy=FAST,
        headers={"Authorization": "Bearer source-key-value"},
        resolver=public_resolver,
        transport=recorder.transport,
    )
    assert recorder.headers[0]["authorization"] == "Bearer source-key-value"
    assert "authorization" not in recorder.headers[1]


# --- size limits -------------------------------------------------------------


def test_oversize_body_aborts_mid_stream():
    """The transfer stops at the cap instead of buffering the whole body."""
    produced: list[int] = []

    def chunks():
        for _ in range(200):
            produced.append(1)
            yield b"x" * 1024

    # A generator body has no Content-Length, so only the streaming guard can
    # catch it.
    recorder = Recorder(
        lambda request: httpx.Response(
            200, content=chunks(), headers={"content-type": "text/xml"}
        )
    )
    policy = replace(FAST, max_bytes=16 * 1024)
    with pytest.raises(FetchTooLarge, match="max_bytes"):
        safefetch.fetch(
            URL,
            allowed_hosts=ALLOWED,
            policy=policy,
            resolver=public_resolver,
            transport=recorder.transport,
        )
    assert len(produced) < 200


def test_declared_content_length_over_cap_is_refused_before_reading():
    recorder = Recorder(
        lambda request: httpx.Response(
            200, content=b"x" * 40_000, headers={"content-type": "text/xml"}
        )
    )
    policy = replace(FAST, max_bytes=1024)
    with pytest.raises(FetchTooLarge, match="Content-Length"):
        safefetch.fetch(
            URL,
            allowed_hosts=ALLOWED,
            policy=policy,
            resolver=public_resolver,
            transport=recorder.transport,
        )


def test_decompression_bomb_is_refused():
    """A few kB on the wire that inflate to megabytes hits the decoded cap."""
    bomb = gzip.compress(b"A" * 4_000_000)
    assert len(bomb) < 64 * 1024  # the wire cap alone would never see this

    recorder = Recorder(
        lambda request: httpx.Response(
            200,
            content=bomb,
            headers={"content-type": "text/xml", "content-encoding": "gzip"},
        )
    )
    policy = replace(FAST, max_bytes=1024 * 1024, max_decompressed_bytes=512 * 1024)
    with pytest.raises(FetchTooLarge, match="decompressed"):
        safefetch.fetch(
            URL,
            allowed_hosts=ALLOWED,
            policy=policy,
            resolver=public_resolver,
            transport=recorder.transport,
        )


# --- content type and encoding -----------------------------------------------


@pytest.mark.parametrize("media_type", ["image/png", "application/octet-stream", "font/woff2"])
def test_disallowed_content_type_is_refused(media_type):
    recorder = Recorder(
        lambda request: httpx.Response(
            200, content=b"\x89PNG", headers={"content-type": media_type}
        )
    )
    with pytest.raises(FetchBlocked, match="Content-Type"):
        safefetch.fetch(
            URL,
            allowed_hosts=ALLOWED,
            policy=FAST,
            resolver=public_resolver,
            transport=recorder.transport,
        )


def test_missing_content_type_is_refused():
    recorder = Recorder(lambda request: httpx.Response(200, content=b"<rss/>"))
    with pytest.raises(FetchBlocked, match="no Content-Type"):
        safefetch.fetch(
            URL,
            allowed_hosts=ALLOWED,
            policy=FAST,
            resolver=public_resolver,
            transport=recorder.transport,
        )


def test_vendor_suffix_content_type_is_accepted():
    recorder = Recorder(
        lambda request: httpx.Response(
            200, content=b"{}", headers={"content-type": "application/vnd.example+json"}
        )
    )
    result = safefetch.fetch(
        URL,
        allowed_hosts=ALLOWED,
        policy=FAST,
        resolver=public_resolver,
        transport=recorder.transport,
    )
    assert result.text == "{}"


def test_body_that_is_not_valid_in_the_declared_charset_is_refused():
    recorder = Recorder(
        lambda request: httpx.Response(
            200,
            content=b"\xff\xfe\x00\x01 not utf-8",
            headers={"content-type": "text/xml; charset=utf-8"},
        )
    )
    with pytest.raises(FetchBlocked, match="not decodable"):
        safefetch.fetch(
            URL,
            allowed_hosts=ALLOWED,
            policy=FAST,
            resolver=public_resolver,
            transport=recorder.transport,
        )


def test_declared_charset_from_header_is_honoured():
    persian = "قيمت طلا افزايش يافت"
    recorder = Recorder(
        lambda request: httpx.Response(
            200,
            content=persian.encode("cp1256"),
            headers={"content-type": "text/xml; charset=windows-1256"},
        )
    )
    result = safefetch.fetch(
        URL,
        allowed_hosts=ALLOWED,
        policy=FAST,
        resolver=public_resolver,
        transport=recorder.transport,
    )
    assert result.text == persian
    assert result.charset == "windows-1256"


def test_charset_falls_back_to_the_xml_declaration():
    persian = "قيمت طلا"
    body = f'<?xml version="1.0" encoding="windows-1256"?><t>{persian}</t>'
    recorder = Recorder(
        lambda request: httpx.Response(
            200, content=body.encode("cp1256"), headers={"content-type": "text/xml"}
        )
    )
    result = safefetch.fetch(
        URL,
        allowed_hosts=ALLOWED,
        policy=FAST,
        resolver=public_resolver,
        transport=recorder.transport,
    )
    assert persian in result.text
    assert result.charset == "windows-1256"


# --- HTTP behaviour ----------------------------------------------------------


def test_timeout_maps_to_fetchtimeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    recorder = Recorder(handler)
    with pytest.raises(FetchTimeout):
        safefetch.fetch(
            URL,
            allowed_hosts=ALLOWED,
            policy=FAST,
            resolver=public_resolver,
            transport=recorder.transport,
        )


def test_auth_wall_is_not_retried():
    recorder = Recorder(lambda request: httpx.Response(403))
    policy = replace(FAST, max_attempts=3)
    with pytest.raises(FetchHTTPError) as excinfo:
        safefetch.fetch(
            URL,
            allowed_hosts=ALLOWED,
            policy=policy,
            resolver=public_resolver,
            transport=recorder.transport,
        )
    assert excinfo.value.status_code == 403
    assert excinfo.value.retryable is False
    assert len(recorder.urls) == 1


def test_transient_status_uses_the_retry_budget():
    recorder = Recorder(lambda request: httpx.Response(429))
    policy = replace(FAST, max_attempts=2)
    with pytest.raises(FetchHTTPError) as excinfo:
        safefetch.fetch(
            URL,
            allowed_hosts=ALLOWED,
            policy=policy,
            resolver=public_resolver,
            transport=recorder.transport,
        )
    assert excinfo.value.retryable is True
    assert len(recorder.urls) == 2


def test_client_error_is_reported_with_its_status():
    recorder = Recorder(lambda request: httpx.Response(404))
    with pytest.raises(FetchHTTPError) as excinfo:
        safefetch.fetch(
            URL,
            allowed_hosts=ALLOWED,
            policy=FAST,
            resolver=public_resolver,
            transport=recorder.transport,
        )
    assert excinfo.value.status_code == 404


# --- secrets -----------------------------------------------------------------


def test_api_key_is_redacted_from_error_messages():
    recorder = Recorder(lambda request: httpx.Response(500))
    with pytest.raises(FetchHTTPError) as excinfo:
        safefetch.fetch(
            f"https://{HOST}/feed.xml?api_key=SUPERSECRET123&format=rss",
            allowed_hosts=ALLOWED,
            policy=FAST,
            resolver=public_resolver,
            transport=recorder.transport,
        )
    message = str(excinfo.value)
    assert "SUPERSECRET123" not in message
    assert "REDACTED" in message


@pytest.mark.parametrize(
    "raw, secret",
    [
        ("https://x.example/f?token=abc123def", "abc123def"),
        ("https://user:hunter2@x.example/f", "hunter2"),
        ("Authorization: Bearer eyJhbGciOi.payload", "eyJhbGciOi.payload"),
        ("failed with api_key=zzz9999", "zzz9999"),
    ],
)
def test_redact_removes_credentials(raw, secret):
    cleaned = safefetch.redact(raw)
    assert secret not in cleaned
    assert "REDACTED" in cleaned


# --- happy path --------------------------------------------------------------


def test_successful_fetch_carries_its_own_evidence():
    body = load_fixture_text("fed_press.xml")
    recorder = Recorder(
        lambda request: httpx.Response(
            200,
            content=body.encode("utf-8"),
            headers={"content-type": "application/rss+xml; charset=utf-8"},
        )
    )
    result = safefetch.fetch(
        URL,
        allowed_hosts=ALLOWED,
        policy=FAST,
        resolver=public_resolver,
        transport=recorder.transport,
    )
    assert result.status_code == 200
    assert result.content_type == "application/rss+xml"
    assert result.resolved_ips == (PUBLIC_IP,)
    assert result.bytes_received > 0
    # fetched_at is our clock and must be usable as an ingest timestamp: aware
    # UTC, never a stand-in for the source's publication time.
    assert result.fetched_at.tzinfo is not None
    assert result.fetched_at.utcoffset().total_seconds() == 0

    root = safefetch.parse_xml_safely(result.content)
    assert root.tag == "rss"
    assert root.findall("./channel/item")


def test_default_user_agent_is_the_honest_project_string():
    recorder = Recorder(lambda request: xml_response())
    safefetch.fetch(
        URL,
        allowed_hosts=ALLOWED,
        policy=FAST,
        resolver=public_resolver,
        transport=recorder.transport,
    )
    assert recorder.headers[0]["user-agent"] == FAST.user_agent
    assert "IranGoldPredictor" in FAST.user_agent


def test_not_modified_is_an_empty_result_not_an_error():
    """A conditional GET answered 304 has no body and no Content-Type."""
    recorder = Recorder(lambda request: httpx.Response(304))
    result = safefetch.fetch(
        URL,
        allowed_hosts=ALLOWED,
        policy=FAST,
        headers={"If-None-Match": '"etag-1"'},
        resolver=public_resolver,
        transport=recorder.transport,
    )
    assert result.status_code == 304
    assert result.content == b""
    assert result.text == ""


def test_fetch_without_an_allowlist_is_refused():
    with pytest.raises(FetchBlocked, match="no allowed hosts"):
        safefetch.fetch(URL, allowed_hosts=(), policy=FAST, resolver=public_resolver)


# --- XML parsing -------------------------------------------------------------


def test_xxe_entity_is_not_expanded(tmp_path):
    """An external entity must not read a local file, expanded or reported."""
    secret_file = tmp_path / "canary.txt"
    secret_file.write_text("CANARY-XXE-7F3A", encoding="utf-8")
    payload = (
        '<?xml version="1.0"?>\n'
        f'<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file://{secret_file}">]>\n'
        "<rss><channel><title>&xxe;</title></channel></rss>"
    )
    with pytest.raises(FetchBlocked) as excinfo:
        safefetch.parse_xml_safely(payload)
    assert "DTD" in str(excinfo.value)
    assert "CANARY-XXE-7F3A" not in str(excinfo.value)


def test_billion_laughs_is_refused_before_parsing():
    payload = (
        '<?xml version="1.0"?>\n'
        "<!DOCTYPE lolz [\n"
        '  <!ENTITY lol "lol">\n'
        '  <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">\n'
        '  <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">\n'
        "]>\n<lolz>&lol2;</lolz>"
    )
    with pytest.raises(FetchBlocked, match="DTD"):
        safefetch.parse_xml_safely(payload)


def test_doctype_inside_element_text_does_not_trip_the_guard():
    """The guard reads the prolog only, so an article about XML still parses."""
    payload = "<rss><channel><title>How &lt;!DOCTYPE&gt; works</title></channel></rss>"
    root = safefetch.parse_xml_safely(payload)
    assert root.findtext("./channel/title") == "How <!DOCTYPE> works"


def test_undefined_entity_without_a_dtd_is_a_parse_error():
    with pytest.raises(FetchParseError):
        safefetch.parse_xml_safely("<rss><item>&nope;</item></rss>")


def test_malformed_xml_raises_a_parse_error():
    with pytest.raises(FetchParseError):
        safefetch.parse_xml_safely("<rss><item></rss>")


def test_oversize_xml_is_refused():
    with pytest.raises(FetchTooLarge):
        safefetch.parse_xml_safely("<a/>" * 100, max_bytes=16)


def test_xml_declared_in_another_charset_is_parsed_from_str():
    persian = "قيمت طلا"
    payload = f'<?xml version="1.0" encoding="windows-1256"?><t>{persian}</t>'
    root = safefetch.parse_xml_safely(payload)
    assert root.text == persian


# --- JSON parsing ------------------------------------------------------------


def test_json_parses_normally():
    assert safefetch.parse_json_safely('{"items": [1, 2]}') == {"items": [1, 2]}


def test_json_depth_limit():
    payload = "[" * 200 + "]" * 200
    with pytest.raises(FetchBlocked, match="max_depth"):
        safefetch.parse_json_safely(payload, max_depth=64)


def test_json_depth_counts_only_real_brackets():
    payload = '{"title": "[[[[[[ gold [[[["}'
    assert safefetch.parse_json_safely(payload, max_depth=2)["title"].startswith("[[")


def test_json_size_limit():
    with pytest.raises(FetchTooLarge):
        safefetch.parse_json_safely('{"a": 1}', max_bytes=4)


def test_json_rejects_nan_and_infinity():
    for payload in ('{"v": NaN}', '{"v": Infinity}', '{"v": -Infinity}'):
        with pytest.raises(FetchBlocked, match="constant"):
            safefetch.parse_json_safely(payload)


def test_malformed_json_raises_a_parse_error():
    with pytest.raises(FetchParseError):
        safefetch.parse_json_safely('{"a": ')
