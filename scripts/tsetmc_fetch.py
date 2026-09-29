#!/usr/bin/env python3
"""Fetch Tehran equity daily bars from TSETMC and hand them to the prediction
service for ingestion.

WHY THIS RUNS HERE AND NOT ON THE SERVER
----------------------------------------
cdn.tsetmc.com is not reachable from the production host. Diagnosed 2026-09-10
rather than assumed: DNS resolves to three addresses (94.182.113.115,
46.102.143.218, 212.16.75.245) and TCP 443 FAILS OUTRIGHT — every endpoint
returns http=000. That is a harder block than the SCI one scripts/sci_fetch.py
documents, where the connection was accepted and the payload dropped. The same
endpoints answer 200 from outside that network, 1.4 MB for one symbol.

So TSETMC cannot be a server-side cron. This script runs somewhere that can
reach it, verifies what it downloaded, copies the payloads to the server, and
asks the prediction service to ingest them from disk. Nothing new is exposed
publicly: the transfer is scp over the existing SSH access and the ingest call
happens inside the compose network, exactly like scripts/sci_fetch.py and every
other operational task in this repo.

THE USER-AGENT, MEASURED RATHER THAN ASSUMED
---------------------------------------------
The brief this was built from said TSETMC needs a browser User-Agent. It does
not, and the difference matters because this project does not impersonate
browsers. Measured 2026-09-10 against
GetInstrumentInfo/46348559193224090:

    curl's own default UA (curl/8.x)                     -> 403
    'IranGoldPredictor/1.0 (+self-hosted analytics)'      -> 200
    a Chrome UA string                                    -> 200

So what TSETMC refuses is the literal `curl/*` token, not "anything that is not
a browser". This script therefore sends the SAME honest User-Agent every
provider in this repo sends, and identifies itself truthfully. A browser UA
would have worked too; it would also have been a lie, and there was never a
reason to tell it.

WHY THE ROSTER IS FETCHED FROM THE SERVER
------------------------------------------
The set of instruments this deployment ingests is data — rows in
`equity_instruments`, seeded by migration 0028 — not a list in this file. So
widening it is an INSERT, and this script reads what to fetch from
GET /internal/equities/roster over the same SSH path it uses for everything
else. A hardcoded list here would silently drift from the database that has to
accept what it sends, and the drift would only show up as a refused ingest.

THE MARKET ITSELF, SINCE MIGRATION 0029
----------------------------------------
The same run also fetches what the market as a whole did: every index the
server's index roster names (GetIndexB2History, 71 of them, ~31 MB), TSETMC's
live value for each (GetIndexB1LastAll, the cross-check the index correction
is verified against), the total market value of both markets
(GetMarketValueByFlow), and the market overview and sector breadth as they
stand at that moment. They go up in the same directory and are posted to
POST /internal/bourse/ingest after the bars. --bars-only restores the old
behaviour; --market-only skips the bars.

EVERY SHARE, AND THE COMMODITY FUNDS, SINCE MIGRATION 0030
-----------------------------------------------------------
The default run also fetches the whole market's money flow, not just the
roster's:

  (a) the market watch (GetMarketWatch, every instrument; the ~1,160 SHARES
      are picked out by insID prefix IRO1/IRO3/IRO5/IRO7) and TSETMC's
      industrial-group names (GetStaticData);
  (b) GetClientTypeHistory for every share, TRIMMED before it is shipped to
      the rows the server does not have plus an overlap of the newest rows it
      does (so a restatement is still caught), and for a share outside the
      roster never earlier than the floor the server states (2025-03-21);
  (c) GetInstrmentsHistoryInDay for every market session since that floor
      that the server has not ingested, plus the newest two it has and any
      it has that shares joined the universe after (the server names them) —
      the sessions being the dates on which at least 200 shares have a flow
      row in the histories just downloaded, which is the market's own
      calendar rather than one this script would have to maintain;
  (d) the whole daily list of every commodity fund the server's fund roster
      names (GetClosingPriceDailyList/{insCode}/0).

They travel in the same single scp (compressed, -C) and are ingested through
POST /internal/bourse/shares/ingest in chunks and POST /internal/bourse/funds/
ingest. --no-shares skips (a)-(c) and fetches the roster's money flow the
0029 way instead.

About 1,160 histories and, on the first run, ~305 day files (TEDPIX has 305
distinct sessions from the floor to 2026-09-28) is ~1,500 requests: this is
the one loop here that is large, so it is the one that is built to lose a
request rather than the run. A failed download is reported against its
insCode or date and the loop carries on; the run exits 1 at the end if
anything failed. Requests start 0.5 s apart by default (--delay; never closer
than a third of a second), about two a second, which puts a full run at
roughly 12-20 minutes — ~12 for a weekly refresh, ~20 for the first one.

TSETMC's CDN answers with two copies of the same histories (measured
2026-09-29: five minutes apart, one copy printed indices to six significant
digits, lacked the whole 2023-03-27 session and differed by a trade or two on
a handful of bars and day-file rows). A stored row is never overwritten; one
the other copy states differently is kept and listed at the end under
RESTATED, which does not fail the run. What fails it is listed under FAILED
and CONTRADICTIONS: every per-item error of every ingest call, the bars and
the market ingest included.

The run's copy inside the prediction container (/tmp/tsetmc-<run>) is removed
once the ingest calls are done; the host archive under backups/tsetmc/<run>
is what a re-ingest or a diagnosis reads, and it is kept.

Usage:
    python3 scripts/tsetmc_fetch.py --dry-run              # plan; all but the bulk loop (~104 requests)
    python3 scripts/tsetmc_fetch.py                        # ...then upload and ingest
    python3 scripts/tsetmc_fetch.py --ins-code 46348559193224090 --dry-run
    python3 scripts/tsetmc_fetch.py --bars-only
    python3 scripts/tsetmc_fetch.py --no-shares            # roster money flow only
    python3 scripts/tsetmc_fetch.py --flows-since 2026-01-01
    python3 scripts/tsetmc_fetch.py --host ubuntu@1.2.3.4
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import shlex
import subprocess
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

BASE = "https://cdn.tsetmc.com"
# GetClosingPriceDailyList/<insCode>/0 — the trailing 0 asks for the whole
# history rather than a window. فولاد returns 4,636 bars, 2007-03-11 onward.
# The commodity funds are fetched from the same endpoint (migration 0030).
BARS_PATH = "/api/ClosingPrice/GetClosingPriceDailyList/{ins_code}/0"

# The project's honest User-Agent, same as every provider in this repo. See the
# module docstring for the measurement that says this is sufficient.
USER_AGENT = "IranGoldPredictor/1.0 (+self-hosted analytics)"

DEFAULT_HOST = "ubuntu@85.137.30.142"
REMOTE_DIR = "/opt/tala-pala/backups/tsetmc"
COMPOSE_DIR = "/opt/tala-pala"
INGEST_URL = "http://prediction-service:8500/internal/equities/bars"
ROSTER_URL = "http://prediction-service:8500/internal/equities/roster"
MARKET_INGEST_URL = "http://prediction-service:8500/internal/bourse/ingest"
INDEX_ROSTER_URL = "http://prediction-service:8500/internal/bourse/indices/roster"
SHARE_STATE_URL = "http://prediction-service:8500/internal/bourse/shares/state"
SHARE_INGEST_URL = "http://prediction-service:8500/internal/bourse/shares/ingest"
FUND_ROSTER_URL = "http://prediction-service:8500/internal/bourse/funds/roster"
FUND_INGEST_URL = "http://prediction-service:8500/internal/bourse/funds/ingest"

# The market-level endpoints (migration 0029). Flow 1 is the bourse, 2 the
# Farabourse. Each entry: local file name -> (path, top-level key it must carry,
# smallest plausible size in bytes). The sizes are an order of magnitude under
# what was measured on 2026-09-29 and far above an error page.
MARKET_PATHS = {
    "indexlive-1.json": ("/api/Index/GetIndexB1LastAll/All/1", "indexB1", 2_000),
    "indexlive-2.json": ("/api/Index/GetIndexB1LastAll/All/2", "indexB1", 500),
    "marketvalue-1.json": ("/api/MarketData/GetMarketValueByFlow/1/9999", "marketValue", 5_000),
    "marketvalue-2.json": ("/api/MarketData/GetMarketValueByFlow/2/9999", "marketValue", 5_000),
    "overview-1.json": ("/api/MarketData/GetMarketOverview/1", "marketOverview", 200),
    "overview-2.json": ("/api/MarketData/GetMarketOverview/2", "marketOverview", 200),
    "sectors.json": ("/api/MarketData/GetSectorsSummary", "sectorSummeries", 1_000),
}
INDEX_HISTORY_PATH = "/api/Index/GetIndexB2History/{ins_code}"
CLIENT_TYPE_PATH = "/api/ClientType/GetClientTypeHistory/{ins_code}"
# The smallest index history on 2026-09-29 (a Farabourse sector listed in
# January 2026) is ~20 KB; the smallest roster money-flow history is ~370 KB.
MIN_INDEX_BYTES = 2_000
MIN_CLIENT_TYPE_BYTES = 5_000

# The whole market (migration 0030). The market watch asks for every paper
# type and untraded instruments too (showTraded=false): 3,786 rows, 2.25 MB,
# on 2026-09-29, against 2,184 traded-only.
MARKET_WATCH_PATH = (
    "/api/ClosingPrice/GetMarketWatch?market=0&industrialGroup="
    "&paperTypes%5B0%5D=1&paperTypes%5B1%5D=2&paperTypes%5B2%5D=3&paperTypes%5B3%5D=4"
    "&paperTypes%5B4%5D=5&paperTypes%5B5%5D=6&paperTypes%5B6%5D=7&paperTypes%5B7%5D=8"
    "&paperTypes%5B8%5D=9&showTraded=false&withBestLimits=false"
)
STATIC_DATA_PATH = "/api/StaticData/GetStaticData"
DAY_FILE_PATH = "/api/ClosingPrice/GetInstrmentsHistoryInDay/{yyyymmdd}"
# The insID prefixes that are shares — app.bourse.parse.SHARE_MARKETS, which
# the ingest applies again; this copy only decides what to download.
SHARE_PREFIXES = ("IRO1", "IRO3", "IRO5", "IRO7")
# The market watch listed 1,162 shares on 2026-09-29. A response with fewer
# than this is truncated, and shipping it would ask the server to delist
# hundreds of shares (which the ingest refuses anyway).
MIN_MARKET_WATCH_SHARES = 500

# How many of the newest STORED flow rows are re-shipped with a share's new
# ones: compared, never written, so a TSETMC restatement of recent history
# still fails that share instead of passing unseen.
FLOW_OVERLAP_ROWS = 10
# The newest stored day files re-fetched each run for the same reason.
DAY_OVERLAP = 2
# A date on which fewer shares than this have a flow row is not a market
# session (a closure, a holiday, one thinly traded board). A normal session
# has ~1,000 of the ~1,160.
SESSION_MIN_SHARES = 200
# Files per ingest call. A flow file is one share's trimmed history; a day
# file is ~1,100 share rows. Both keep a call well inside busybox wget's read
# timeout on the server.
FLOW_CHUNK = 200
SESSION_CHUNK = 20
# Tehran has kept UTC+03:30 without daylight saving since 2022; the fetch
# only needs "which Tehran date is it", so a fixed offset is exact enough and
# needs no tz database on the laptop. The ingest applies the real rule.
TEHRAN_OFFSET = timedelta(hours=3, minutes=30)
SETTLED_AFTER_HOUR = 15   # app.bourse.ingest.SETTLED_AFTER_HOUR
EARLIEST_FLOOR = date(2008, 1, 1)

# Politeness: about two requests a second by default, and never more than
# three. --delay is the gap between the STARTS of consecutive requests, so a
# slow response eats into it rather than adding to it. 0.5 s is the spacing
# every other TSETMC and TGJU request in this repo keeps; the floor exists
# for an operator who knows why they need a faster run.
MIN_REQUEST_INTERVAL = 1 / 3
DEFAULT_DELAY = 0.5

# The smallest history in the seeded roster (ذوب, listed 2021-12) is 361 KB;
# فولاد's is 1.4 MB. A WAF page, an empty result or a truncated transfer is
# orders of magnitude smaller, and handing one on would look like an
# instrument that simply has little history.
MIN_PLAUSIBLE_BYTES = 20_000

# TSETMC's insCode is an opaque numeric identifier of up to ~17 digits. This
# value is interpolated into a URL and then into a FILENAME that travels into a
# command the production host runs under sudo, so only digits are accepted.
#
# The reasoning is the same one scripts/sci_fetch.py states for the scraped
# workbook name, and it applies with more force here because the value can
# arrive from --ins-code on a command line OR from the roster the server
# returned: `[^/]*` would admit ';', '$(', '`', spaces and '..'. The value is a
# number, so only a number is accepted.
INS_CODE_RE = re.compile(r"\A[0-9]{1,20}\Z")
# The generated names this script writes into its own subdirectories — and
# the only names it ever deletes from them.
FLOW_NAME_RE = re.compile(r"\Aflow-[0-9]{1,20}\.json\Z")
DAY_NAME_RE = re.compile(r"\Aday-[0-9]{8}\.json\Z")
FUND_NAME_RE = re.compile(r"\Afund-[0-9]{1,20}\.json\Z")
SHARE_FIXED_NAMES = ("manifest.json", "market-watch.json", "static-data.json")


class FetchError(Exception):
    """One download failed: curl gave up, or the body is not the payload asked
    for.

    A plain Exception — unlike the SystemExit this script used to raise from
    `_curl` — because SystemExit is a BaseException: an `except Exception`
    around one share in a loop of 1,160 does not catch it, and the first 502
    would have ended the whole run. The roster downloads, which should stop the
    run, turn it back into SystemExit at their call sites.
    """


class RemoteError(Exception):
    """A command on the server failed (ssh, docker compose, or the ingest
    call itself answering an HTTP error, which wget reports as a non-zero
    exit). A chunk of the bulk ingest catches it and carries on."""


class Pacer:
    """Spaces the starts of consecutive requests by at least `interval` s."""

    def __init__(self, interval: float = DEFAULT_DELAY) -> None:
        self.interval = interval
        self._last: Optional[float] = None
        self.requests = 0

    def wait(self) -> None:
        now = time.monotonic()
        if self._last is not None:
            remaining = self._last + self.interval - now
            if remaining > 0:
                time.sleep(remaining)
        self._last = time.monotonic()
        self.requests += 1


PACER = Pacer()


def _assert_safe_ins_code(ins_code: str) -> str:
    """Refuse any insCode that is not plainly a number.

    Belt and braces: validated where it enters (from --ins-code or from the
    roster response) and again at the point of use, because the roster is a
    REMOTE RESPONSE — trusted more than a scraped page, but still not trusted
    into a root shell. If the endpoint ever changes shape, the right outcome is
    a loud refusal here rather than a surprising string reaching the server.
    """
    if not INS_CODE_RE.match(ins_code):
        raise SystemExit(
            f"refusing the insCode {ins_code!r}: it is not a plain sequence of "
            "digits. This value becomes a filename in a command executed on the "
            "production host, so anything unexpected is refused rather than "
            "quoted and hoped for."
        )
    return ins_code


def _curl(url: str, timeout: int = 120) -> bytes:
    """Fetch over TLS with full certificate verification, via curl.

    Same shape as scripts/sci_fetch.py, and for a plainer reason: curl is
    already the repo's answer for an off-server fetch, so there is one way of
    doing this rather than two. Verification is never disabled — there is no -k
    and no unverified context here. TSETMC's chain verifies normally; the only
    thing it objects to is curl's own default User-Agent, which is why one is
    always passed.

    --compressed since 0030: TSETMC gzips on request, and a money-flow history
    is 5.1x smaller on the wire (فولاد: 259,721 bytes against 1,319,545,
    0.13 s against 0.33 s). curl decompresses, so every size check below still
    sees the JSON itself.

    Raises FetchError, never SystemExit; see that class for why.
    """
    # Retried, because TSETMC's CDN is intermittently unwell: on 2026-09-29 the
    # same URL answered 200, 200, 200, 502, 200 two seconds apart, and a run of
    # ~120 files died on the first one. --retry-all-errors and not plain
    # --retry, measured rather than assumed: curl 8.7 reports TSETMC's 502 as
    # exit 56 (a receive error, not an HTTP one) and plain --retry does not
    # treat that as transient — it gave up after 0 seconds. The cost of
    # retrying everything is that a genuine 404 takes ~15 s to fail instead of
    # none, which a manual weekly run can afford.
    PACER.wait()
    proc = subprocess.run(
        ["curl", "--fail", "--location", "--silent", "--show-error", "--compressed",
         "--retry", "5", "--retry-delay", "3", "--retry-all-errors",
         "--max-time", str(timeout), "--user-agent", USER_AGENT, url],
        capture_output=True,
        check=False,   # the return code is inspected below, with the stderr
    )
    if proc.returncode != 0:
        raise FetchError(
            f"fetch failed for {url}\n"
            f"curl exit {proc.returncode}: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}\n"
            "If this is the production host, that is expected: cdn.tsetmc.com "
            "refuses TCP 443 from there. Run this script somewhere else."
        )
    return proc.stdout


def _curl_or_exit(url: str) -> bytes:
    """The roster downloads stop the run on a failed fetch, as they always have."""
    try:
        return _curl(url)
    except FetchError as exc:
        raise SystemExit(str(exc)) from exc


def _ssh(host: str, command: str, capture: bool = False) -> bytes:
    proc = subprocess.run(
        ["ssh", host, command],
        capture_output=capture,
        check=not capture,
    )
    if capture and proc.returncode != 0:
        raise RemoteError(
            f"ssh command failed on {host} (exit {proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout if capture else b""


def _remote_token_prelude() -> str:
    """Read INTERNAL_API_TOKEN from the server's own .env, on the server.

    The secret is never a local process argument: it is read by the server's
    shell out of the server's file and handed to the container through
    ``docker compose exec -e``. See the comment at the call sites for why the
    ``-e TOKEN="$TOKEN"`` form is load-bearing.
    """
    return (
        f"cd {shlex.quote(COMPOSE_DIR)} && "
        "TOKEN=$(sudo grep -E '^INTERNAL_API_TOKEN=' .env | cut -d= -f2-) && "
    )


def _internal_get(host: str, url: str, what: str) -> dict:
    """GET an internal endpoint from inside the compose network, over SSH.

    A read, so --dry-run does it too: knowing what the deployment would ingest
    is exactly what a dry run is for.
    """
    inner = (
        'wget -qO- --header="X-Internal-Token: $TOKEN" ' + shlex.quote(url)
    )
    remote = _remote_token_prelude() + (
        # -e TOKEN="$TOKEN" is load-bearing, and this is the exact bug that
        # returned 401 on the first SCI run. The inner command is shlex-quoted
        # so the HOST shell cannot expand anything inside it — that is the
        # whole point of the quoting — which also means $TOKEN stays literal
        # unless the CONTAINER has it in its environment.
        f'sudo docker compose exec -T -e TOKEN="$TOKEN" api sh -c {shlex.quote(inner)}'
    )
    try:
        raw = _ssh(host, remote, capture=True)
    except RemoteError as exc:
        raise SystemExit(f"reading the {what} failed: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"the {what} endpoint did not return JSON: {exc}\n"
            f"first 200 bytes: {raw[:200]!r}"
        ) from exc


def fetch_roster(host: str) -> list[dict]:
    """Read the enabled equity roster from the prediction service."""
    items = _internal_get(host, ROSTER_URL, "roster").get("items")
    if not isinstance(items, list) or not items:
        raise SystemExit(
            "the roster is empty. equity_instruments is seeded by migration 0028; "
            "either it has not been applied or every row is disabled."
        )
    for item in items:
        _assert_safe_ins_code(str(item.get("ins_code", "")))
    return items


def fetch_index_roster(host: str) -> list[dict]:
    """Read the enabled index roster (migration 0029 seeds 71)."""
    items = _internal_get(host, INDEX_ROSTER_URL, "index roster").get("items")
    if not isinstance(items, list) or not items:
        raise SystemExit(
            "the index roster is empty. market_indices is seeded by migration 0029; "
            "either it has not been applied (deploy first) or every row is disabled."
        )
    for item in items:
        _assert_safe_ins_code(str(item.get("ins_code", "")))
    return items


def fetch_share_state(host: str) -> dict:
    """Read what the server holds for every share (migration 0030): its flow
    coverage, its roster flag, the ingested session dates and the floor."""
    state = _internal_get(host, SHARE_STATE_URL, "share state")
    items = state.get("items")
    if not isinstance(items, list) or not isinstance(state.get("session_dates"), list):
        raise SystemExit(
            "the share state is not the shape this script expects; is migration 0030 "
            "deployed? (GET /internal/bourse/shares/state)"
        )
    for item in items:
        _assert_safe_ins_code(str(item.get("ins_code", "")))
    try:
        date.fromisoformat(str(state.get("floor")))
    except ValueError as exc:
        raise SystemExit(f"the share state carries no usable floor: {exc}") from exc
    return state


def fetch_fund_roster(host: str) -> list[dict]:
    """Read the enabled commodity-fund roster (migration 0030 seeds five)."""
    items = _internal_get(host, FUND_ROSTER_URL, "fund roster").get("items")
    if not isinstance(items, list):
        raise SystemExit(
            "the fund roster is not a list; is migration 0030 deployed? "
            "(GET /internal/bourse/funds/roster)"
        )
    for item in items:
        _assert_safe_ins_code(str(item.get("ins_code", "")))
    return items


def download_payload(url_path: str, out: Path, key: str, min_bytes: int,
                     ins_code: str = "") -> Path:
    """Download one market-level payload and verify its shape before keeping it.

    The same three refusals as a bar download: too small to be real, not JSON,
    or missing the one top-level key the service parses. When ``ins_code`` is
    given, every row must carry it — a file named for one index and holding
    another's values is invisible after the transfer.
    """
    blob = _curl_or_exit(BASE + url_path)
    if len(blob) < min_bytes:
        raise SystemExit(
            f"{url_path}: downloaded only {len(blob)} bytes (at least {min_bytes} "
            "expected) — an error page or an empty result. Refusing to hand it on."
        )
    try:
        payload = json.loads(blob.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{url_path}: response is not JSON: {exc}") from exc
    if not isinstance(payload, dict) or key not in payload or not payload[key]:
        keys = sorted(payload)[:8] if isinstance(payload, dict) else type(payload).__name__
        raise SystemExit(
            f"{url_path}: response carries no {key!r}. The endpoint shape changed; "
            f"keys present: {keys}"
        )
    if ins_code:
        codes = {str(row.get("insCode", "")).strip() for row in payload[key]}
        if codes != {ins_code}:
            raise SystemExit(
                f"{url_path}: payload carries insCode(s) {sorted(codes)[:4]}, not "
                f"{ins_code}. Refusing to store one series under another's code."
            )
    out.write_bytes(blob)
    return out


def download(ins_code: str, out_dir: Path) -> Path:
    """Download one instrument's whole daily-bar history and verify it."""
    _assert_safe_ins_code(ins_code)
    blob = _curl_or_exit(BASE + BARS_PATH.format(ins_code=ins_code))
    if len(blob) < MIN_PLAUSIBLE_BYTES:
        raise SystemExit(
            f"{ins_code}: downloaded only {len(blob)} bytes — that is an error "
            "page or an empty result, not an instrument history. Refusing to "
            "hand it on."
        )
    try:
        payload = json.loads(blob.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{ins_code}: response is not JSON: {exc}") from exc
    rows = payload.get("closingPriceDaily")
    if not isinstance(rows, list) or not rows:
        raise SystemExit(
            f"{ins_code}: response carries no closingPriceDaily list. The endpoint "
            f"shape changed; keys present: {sorted(payload)[:8]}"
        )
    # Verified HERE as well as in the service: the insCode in the payload must
    # be the one asked for. A file named for one instrument and carrying
    # another's bars is invisible after the transfer.
    codes = {str(row.get("insCode", "")).strip() for row in rows}
    if codes != {ins_code}:
        raise SystemExit(
            f"{ins_code}: payload carries insCode(s) {sorted(codes)}. Refusing to "
            "store one company's bars under another's code."
        )
    out = out_dir / f"{ins_code}.json"
    out.write_bytes(blob)
    return out


# --- the bulk loop: pure pieces ------------------------------------------------
#
# Everything the bulk loop DECIDES is a pure function of what it downloaded and
# what the server said it holds, so it can be tested without a network.


def fetch_json(url_path: str, key: str) -> tuple[bytes, dict]:
    """Download one payload for the bulk loop: JSON, an object, `key` a list.
    Raises FetchError. An EMPTY list is returned, not refused — for a share
    that has never traded it is the honest answer, and the caller decides."""
    blob = _curl(BASE + url_path)
    try:
        payload = json.loads(blob.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise FetchError(f"{url_path}: response is not JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        keys = sorted(payload)[:8] if isinstance(payload, dict) else type(payload).__name__
        raise FetchError(
            f"{url_path}: response carries no {key!r} list. The endpoint shape changed; "
            f"keys present: {keys}"
        )
    return blob, payload


def _rec_date(value: Any) -> date:
    """TSETMC's integer Gregorian YYYYMMDD (recDate/dEven) as a date."""
    if isinstance(value, bool) or not isinstance(value, int) or not 19000101 <= value <= 21001231:
        raise FetchError(f"date {value!r} is not an integer YYYYMMDD")
    try:
        return date(value // 10000, (value // 100) % 100, value % 100)
    except ValueError as exc:
        raise FetchError(f"date {value} is not a calendar date") from exc


def check_client_types(payload: dict, ins_code: str) -> list[dict]:
    """The shape checks that replace the old 5,000-byte floor for this loop.

    A new listing's history is a few hundred bytes and a secondary board's a
    few KB, so a size floor refuses real payloads; what matters is that every
    row is an object for the share asked for, with a real date, and no
    session twice. Returns the rows, possibly none.

    The repeat check is the ingest's own (it refuses a history with two rows
    for one session) made HERE because trimming keys rows by date: a repeat
    that reached trim_client_types would be collapsed to one row silently,
    and the service would never see the evidence it refuses on.
    """
    rows = payload["clientType"]
    seen: set[date] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise FetchError(f"{ins_code}: a clientType row is not an object")
        if str(row.get("insCode", "")).strip() != ins_code:
            raise FetchError(
                f"{ins_code}: a clientType row carries insCode {row.get('insCode')!r}. "
                "Refusing to ship one share's flows under another's code."
            )
        session = _rec_date(row.get("recDate"))
        if session in seen:
            raise FetchError(
                f"{ins_code}: the history carries {session.isoformat()} twice; one session "
                "is one row, and which of the two is true is not this script's to decide"
            )
        seen.add(session)
    return rows


def trim_client_types(
    rows: list[dict],
    *,
    last_stored: Optional[date],
    first_stored: Optional[date],
    overlap: int,
    floor: Optional[date],
) -> Optional[list[dict]]:
    """What of one share's downloaded history is worth shipping. Pure.

    - Nothing stored: everything, or, for a share outside the roster
      (``floor`` set), everything from the floor on.
    - Something stored: every row NEWER than the newest stored one, plus the
      ``overlap`` newest rows at or before it — which the server compares and
      never writes, so a restated recent session still fails the share.
    - Also every row OLDER than the oldest stored one — for the roster
      (``floor`` None) all of them, so a share that joins the roster after
      being floored gets the history the roster keeps; for any other share
      those from the floor on, so a run whose --flows-since was later than the
      server's floor leaves no stretch between the two missing for good.

    Returns the rows newest-first, as TSETMC serves them, or None when there is
    nothing the server does not already have.
    """
    dated = sorted(((_rec_date(r["recDate"]), r) for r in rows), key=lambda item: item[0])
    if last_stored is None:
        keep = [(d, r) for d, r in dated if floor is None or d >= floor]
        return [r for _, r in reversed(keep)] or None
    newer = [(d, r) for d, r in dated if d > last_stored]
    older = [
        (d, r) for d, r in dated
        if first_stored is not None and d < first_stored and (floor is None or d >= floor)
    ]
    if not newer and not older:
        return None
    upto = [(d, r) for d, r in dated if d <= last_stored]
    chosen = {d: r for d, r in older}
    chosen.update({d: r for d, r in upto[-overlap:]} if overlap > 0 else {})
    chosen.update({d: r for d, r in newer})
    if floor is not None:
        chosen = {d: r for d, r in chosen.items() if d >= floor}
    return [chosen[d] for d in sorted(chosen, reverse=True)] or None


def settled_cutoff(now: datetime) -> date:
    """The first date that is NOT a settled session at ``now`` (UTC-aware):
    the Tehran date until 15:00 Tehran, the next day after. The same rule as
    app.bourse.ingest.settled_cutoff, which the ingest applies again."""
    local = now.astimezone(timezone.utc) + TEHRAN_OFFSET
    return local.date() + timedelta(days=1) if local.hour >= SETTLED_AFTER_HOUR else local.date()


def market_sessions(
    dates_by_share: Mapping[str, Iterable[date]],
    *,
    floor: date,
    before: date,
    min_shares: int = SESSION_MIN_SHARES,
) -> list[date]:
    """The market's session calendar, read off the flow histories. Pure.

    A date is a session when at least ``min_shares`` shares have a flow row
    on it; only settled sessions (before ``before``) from the floor on count.
    """
    counts: Counter = Counter()
    for dates in dates_by_share.values():
        counts.update(set(dates))
    return sorted(d for d, n in counts.items() if n >= min_shares and floor <= d < before)


def day_files_to_fetch(
    sessions: Iterable[date], stored: Iterable[date], overlap: int = DAY_OVERLAP,
    recheck: Iterable[date] = (),
) -> list[date]:
    """The sessions whose day file the server lacks, plus its ``overlap``
    newest stored ones (compared, never written), plus the stored ones the
    server asks for again (``recheck``: ingested before some of their shares
    were in the universe, see app.bourse.shares.share_state). Pure."""
    stored_set = set(stored)
    wanted = {d for d in sessions if d not in stored_set} | set(recheck)
    if overlap > 0:
        wanted |= set(sorted(stored_set)[-overlap:])
    return sorted(wanted)


def share_codes(market_watch: dict) -> list[tuple[str, str, str]]:
    """(insCode, insID, symbol) for every SHARE in a market watch. The prefix
    rule is the ingest's; the full parse is the ingest's too, and so is the
    refusal of a watch that lists one share twice: shipped anyway, that share's
    flow file would be named twice in the manifest, which the service refuses
    for every chunk of the flows part — the whole market's money flow for the
    run, for one repeated row."""
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for row in market_watch.get("marketwatch") or []:
        if not isinstance(row, dict):
            continue
        ins_id = str(row.get("insID") or "").strip()
        if ins_id[:4] not in SHARE_PREFIXES:
            continue
        code = str(row.get("insCode") or "").strip()
        if not INS_CODE_RE.match(code):
            raise FetchError(f"market-watch share {ins_id} carries insCode {code!r}")
        if code in seen:
            raise FetchError(f"the market watch lists share insCode {code} twice")
        seen.add(code)
        out.append((code, ins_id, str(row.get("lva") or "").strip()))
    return out


def build_manifest(flow_files: Iterable[str], day_files: Iterable[str], *,
                   floor: date, has_watch: bool, has_static: bool) -> dict:
    """The manifest the ingest reads: fixed names, generated here, nothing else."""
    return {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "floor": floor.isoformat(),
        "market_watch": "market-watch.json" if has_watch else None,
        "static_data": "static-data.json" if has_static else None,
        "flows": sorted(flow_files),
        "days": sorted(day_files),
    }


def _clear_generated(directory: Path, pattern: Optional[re.Pattern] = None,
                     fixed: Iterable[str] = ()) -> None:
    """Remove this script's own generated files from a previous run, and only
    those: a stale flow file must not ship twice, and --out may point anywhere,
    so nothing whose name this script did not generate is ever touched."""
    if not directory.is_dir():
        return
    fixed = set(fixed)
    for path in directory.iterdir():
        if path.is_file() and (
            (pattern is not None and pattern.match(path.name)) or path.name in fixed
        ):
            path.unlink()


# --- the bulk loop: the downloads ------------------------------------------------


def _brief(message: str) -> str:
    """The first two lines of a failure — the URL and curl's own reason — and
    not the advice about the production host that follows them."""
    return " | ".join(line.strip() for line in message.splitlines()[:2] if line.strip())


class Failures:
    """Per-item failures of a run, grouped for the report."""

    def __init__(self) -> None:
        self.items: list[tuple[str, str, str]] = []   # (kind, key, message)

    def add(self, kind: str, key: str, message: str) -> None:
        self.items.append((kind, key, message))
        print(f"  FAILED {kind} {key}: {_brief(message)}")

    def __bool__(self) -> bool:
        return bool(self.items)


class Restated:
    """Stored rows TSETMC's current copy states differently: kept as stored,
    listed at the end, and NOT a failure of the run (see the module
    docstring). A gap in a served copy and a retired or superseded corporate
    action are listed here too — each is the store being more complete than
    one copy, not the run going wrong."""

    def __init__(self) -> None:
        self.items: list[tuple[str, str, str]] = []   # (kind, key, what)

    def add(self, kind: str, key: str, what: str) -> None:
        self.items.append((kind, key, what))

    def __bool__(self) -> bool:
        return bool(self.items)


def _progress(label: str, done: int, total: int, started: float) -> None:
    elapsed = time.monotonic() - started
    rate = done / elapsed if elapsed > 0 else 0.0
    eta = (total - done) / rate if rate > 0 else 0.0
    print(f"  {label}: {done}/{total}  {elapsed:,.0f} s elapsed, ~{eta:,.0f} s to go")


def download_share_flows(
    shares: list[tuple[str, str, str]],
    state: Mapping[str, dict],
    floor: date,
    flows_dir: Path,
    evidence_dir: Path,
    failures: Failures,
) -> tuple[list[str], dict[str, list[date]], dict[str, Any]]:
    """GetClientTypeHistory for every share, trimmed and written, each share
    isolated. Returns the files written, every share's full date list (the
    session calendar is read from it) and counts for the report."""
    written: list[str] = []
    dates: dict[str, list[date]] = {}
    counts = Counter()
    started = time.monotonic()
    for index, (code, ins_id, symbol) in enumerate(shares, 1):
        try:
            blob, payload = fetch_json(CLIENT_TYPE_PATH.format(ins_code=code), "clientType")
            rows = check_client_types(payload, code)
        except FetchError as exc:
            failures.add("flows", f"{code} {symbol}", str(exc))
            continue
        # The untrimmed history is the evidence; kept locally, compressed.
        (evidence_dir / f"clienttype-{code}.json.gz").write_bytes(gzip.compress(blob, 6))
        held = state.get(code) or {}
        stored_count = int(held.get("flow_count") or 0)
        if not rows and stored_count:
            # The honest answer for a listing that never traded, and a bad
            # response for a share the server already holds sessions of.
            failures.add("flows", f"{code} {symbol}",
                         f"{code}: TSETMC served an empty money-flow history for a share "
                         f"the server holds {stored_count} session(s) of; nothing shipped")
            continue
        if not rows:
            counts["empty"] += 1
            print(f"  NOTICE {code} {symbol} ({ins_id}): TSETMC serves no money-flow rows "
                  "for it — a listing that has not traded yet.")
            continue
        dates[code] = [_rec_date(r["recDate"]) for r in rows]
        trimmed = trim_client_types(
            rows,
            last_stored=date.fromisoformat(held["flow_last_date"]) if stored_count else None,
            first_stored=date.fromisoformat(held["flow_first_date"]) if stored_count else None,
            overlap=FLOW_OVERLAP_ROWS,
            floor=None if held.get("roster") else floor,
        )
        if trimmed is None:
            counts["nothing_new"] += 1
        else:
            name = f"flow-{code}.json"
            (flows_dir / name).write_text(
                json.dumps({"clientType": trimmed}, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            written.append(name)
            counts["rows_shipped"] += len(trimmed)
            counts["new_share" if not stored_count else "incremental"] += 1
        if index % 100 == 0:
            _progress("money flow", index, len(shares), started)
    counts["seconds"] = round(time.monotonic() - started)
    return written, dates, dict(counts)


def download_day_files(sessions: list[date], days_dir: Path, failures: Failures) -> list[str]:
    """GetInstrmentsHistoryInDay for each session, each isolated. The file is
    written byte-for-byte as served: its insCodes are bare JSON numbers above
    2^53, and re-serialising them through anything but an exact parser would
    be the one way to corrupt them."""
    written: list[str] = []
    started = time.monotonic()
    for index, session in enumerate(sessions, 1):
        stamp = session.strftime("%Y%m%d")
        try:
            blob, payload = fetch_json(
                DAY_FILE_PATH.format(yyyymmdd=stamp), "closingPriceDailyHistoryWithInstDetails")
            if not payload["closingPriceDailyHistoryWithInstDetails"]:
                raise FetchError(
                    f"{stamp}: TSETMC's day file is empty for a date on which at least "
                    f"{SESSION_MIN_SHARES} shares have money flow")
        except FetchError as exc:
            failures.add("session", session.isoformat(), str(exc))
            continue
        name = f"day-{stamp}.json"
        (days_dir / name).write_bytes(blob)
        written.append(name)
        if index % 50 == 0:
            _progress("day files", index, len(sessions), started)
    return written


def download_funds(funds: list[dict], funds_dir: Path, failures: Failures) -> list[Path]:
    """Each commodity fund's whole daily list, each isolated."""
    written: list[Path] = []
    for fund in funds:
        code = _assert_safe_ins_code(str(fund["ins_code"]))
        label = f"{code} {fund.get('symbol_fa', '')}".strip()
        try:
            blob, payload = fetch_json(BARS_PATH.format(ins_code=code), "closingPriceDaily")
            rows = payload["closingPriceDaily"]
            if rows and {str(r.get("insCode", "")).strip() for r in rows} != {code}:
                raise FetchError(f"{code}: the daily list carries another instrument's rows")
        except FetchError as exc:
            failures.add("fund", label, str(exc))
            continue
        if not rows and int(fund.get("close_count") or 0):
            failures.add("fund", label,
                         f"{code}: TSETMC served an empty daily list for a fund the server "
                         f"holds {fund['close_count']} close(s) of; nothing shipped")
            continue
        if not rows:
            print(f"  NOTICE fund {label}: TSETMC serves no daily rows for it.")
            continue
        path = funds_dir / f"fund-{code}.json"
        path.write_bytes(blob)
        written.append(path)
    return written


# --- the server side -----------------------------------------------------------


def _post_ingest(host: str, url: str, body: dict, remote_dir: str,
                 container_dir: str, copy_first: bool) -> dict:
    """Copy the run directory into the prediction container (once) and POST an
    ingest body naming files inside it. Returns the service's JSON report.
    Raises RemoteError when the call fails — including an ingest that answers
    502 because every item in it failed, which busybox wget reports only as a
    non-zero exit."""
    quoted_body = shlex.quote(json.dumps(body))
    inner = (
        'wget -qO- --header="X-Internal-Token: $TOKEN" '
        '--header="Content-Type: application/json" '
        f"--post-data={quoted_body} "
        f"{shlex.quote(url)}"
    )
    copy = ""
    if copy_first:
        copy = (
            # The target directory is created FIRST and the source ends in "/."
            # so the CONTENTS are copied into it.
            #
            # Without both halves this silently produces an empty directory:
            # `docker compose cp <dir> container:<newdir>` reported "Copied ..."
            # and created /tmp/tsetmc-<ts> with nothing in it, so all 20 symbols
            # failed to parse and the endpoint correctly answered 502 for a pass
            # that ingested nothing. The files were on the host the whole time --
            # 19 of them, right where scp put them. Measured 2026-09-10; copying
            # with a trailing "/." into a pre-made directory transfers all 19.
            f"sudo docker compose exec -T prediction-service "
            f"sh -c {shlex.quote('mkdir -p ' + shlex.quote(container_dir))} && "
            f"sudo docker compose cp {shlex.quote(remote_dir + '/.')} "
            f"prediction-service:{shlex.quote(container_dir)} && "
        )
    remote = _remote_token_prelude() + copy + (
        # -e TOKEN="$TOKEN" again: the inner command is shlex-quoted so the host
        # shell cannot expand $TOKEN inside it, and without passing it into the
        # container the request goes out with the literal header
        # "X-Internal-Token: $TOKEN" and earns a 401. That exact bug is what the
        # first SCI run hit.
        f'sudo docker compose exec -T -e TOKEN="$TOKEN" api sh -c {shlex.quote(inner)}'
    )
    raw = _ssh(host, remote, capture=True)
    text = raw.decode("utf-8", "replace").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RemoteError(f"the ingest did not return JSON: {text[:500]}") from exc


# The container copies this script makes, and the only thing it ever removes
# inside the container.
CONTAINER_DIR_RE = re.compile(r"\A/tmp/tsetmc-[0-9]{8}T[0-9]{6}Z\Z")


def _remove_container_copy(host: str, container_dir: str) -> None:
    """Remove this run's copy from the prediction container. The name is the
    one run() generated from the wall clock, checked again here because it
    goes into a root shell's ``rm -rf``."""
    if not CONTAINER_DIR_RE.match(container_dir):
        raise RemoteError(f"refusing to remove {container_dir!r}: not a run copy this "
                          "script made")
    inner = "rm -rf -- " + shlex.quote(container_dir)
    _ssh(host, f"cd {shlex.quote(COMPOSE_DIR)} && sudo docker compose exec -T "
               f"prediction-service sh -c {shlex.quote(inner)}", capture=True)


def _print_bars_report(report: dict, failures: "Failures", restated: "Restated") -> None:
    """The bars ingest's report. Every per-symbol error goes into `failures`,
    so it reaches the run's FAILED summary rather than scrolling past."""
    print(f"\nadjustment : {report.get('adjustment_version')}")
    print(f"validated  : {report.get('validated')}")
    print(f"refused    : {report.get('refused')}")
    print(f"failed     : {report.get('failed')}")
    print(f"bars       : {report.get('bars_inserted')} inserted")
    print(f"actions    : {report.get('actions_inserted')} inserted")
    for symbol, result in sorted((report.get("symbols") or {}).items()):
        print(f"  {symbol:<10} {result['status']:<9} "
              f"bars={result['bars_inserted']:>5} actions={result['actions_inserted']:>3} "
              f"adj=x{result['adjusted_ratio']:.2f}")
        if result.get("refusal_reason"):
            print(f"    {result['refusal_reason']}")
        if result.get("bars_restated"):
            restated.add("bars", symbol, f"{result['bars_restated']} stored bar(s) kept; e.g. "
                         f"{result.get('restated_examples', [])[:2]}")
        for key, what in (("actions_retired", "retired"), ("actions_superseded", "superseded")):
            if result.get(key):
                examples = result.get(f"{what}_examples", [])[:2]
                restated.add("actions", symbol, f"{result[key]} corporate action(s) {what}: the "
                             f"stored bars, and the copies served of them, no longer imply them "
                             f"as detected; {examples}")
        if result.get("contested_breaks"):
            restated.add("actions", symbol, f"{result['contested_breaks']} break(s) in the "
                         "reference chain contested — another copy TSETMC served chains them — "
                         f"and not read as actions; {result.get('contested_examples', [])[:2]}")
    for error in report.get("errors") or []:
        failures.add("ingest bars", error["path"], f"{error['error']}: {error['message']}")


def _print_market_report(report: dict, failures: "Failures", restated: "Restated",
                         contradictions: list) -> None:
    """The market ingest's report. Every per-item error goes into `failures`
    (a contradiction into `contradictions`), so it reaches the run's summary."""
    indices = report.get("indices") or {}
    print(f"\nindex check: {report.get('check_version')}")
    print(f"indices    : {len(indices)} ingested, {report.get('validated')} validated, "
          f"{report.get('refused')} refused")
    inserted = sum(r.get("values_inserted", 0) for r in indices.values())
    rescaled = [r for r in indices.values() if r.get("rows_rescaled")]
    print(f"values     : {inserted} inserted; {len(rescaled)} index(es) carry a "
          "power-of-ten correction")
    for r in sorted(rescaled, key=lambda r: -r["rows_rescaled"]):
        print(f"  {r['name_fa']:<32} breaks={r['scale_breaks']:>2} "
              f"rescaled={r['rows_rescaled']:>4} live_ratio={r.get('live_ratio')}")
    for r in indices.values():
        if r.get("refusal_reason"):
            print(f"  REFUSED {r['name_fa']}: {r['refusal_reason']}")
        if r.get("restated"):
            restated.add("index", f"{r['ins_code']} {r['name_fa']}",
                         f"{r['restated']} stored value(s) kept; e.g. "
                         f"{r.get('restated_examples', [])[:2]}")
    for market, r in sorted((report.get("market_values") or {}).items()):
        print(f"market val : {market:<10} {r['values_inserted']:>5} inserted, "
              f"{r.get('values_revised', 0)} restated by TSETMC, "
              f"{r['first_date']}..{r['last_date']}")
        if r.get("largest_revision"):
            lr = r["largest_revision"]
            print(f"             largest restatement {lr['date']}: {lr['pct']:+.4f}%")
    flows = report.get("client_flows") or {}
    if flows:
        print(f"money flow : {len(flows)} roster symbol(s), "
              f"{sum(r['sessions_inserted'] for r in flows.values())} session(s) inserted")
        for symbol, r in sorted(flows.items()):
            if r.get("restated"):
                restated.add("flows", f"{r['ins_code']} {symbol}",
                             f"{r['restated']} stored session(s) kept; e.g. "
                             f"{r.get('restated_examples', [])[:2]}")
    snap = report.get("snapshot") or {}
    for market in ("bourse", "farabourse"):
        if market in snap:
            print(f"snapshot   : {market:<10} {snap[market]['activity_at']} "
                  f"({snap[market]['stored']})")
    if "sectors" in snap:
        print(f"sectors    : {snap['sectors']['rows']} rows, "
              f"{snap['sectors']['inserted']} inserted")
    for error in report.get("errors") or []:
        if error["error"] in CONTRADICTIONS:
            contradictions.append((error["kind"], error["path"], error["message"]))
        else:
            failures.add(f"ingest {error['kind']}", error["path"],
                         f"{error['error']}: {error['message']}")


# The error types that mean "TSETMC's copy disagrees with most of what is
# stored", listed apart from the other failures.
CONTRADICTIONS = {"FlowContradiction", "SessionContradiction", "FundContradiction",
                  "IndexContradiction"}


def _print_universe_report(report: dict, failures: "Failures") -> None:
    """The universe part's report. A failed sector-names file or a refused
    market watch goes into `failures`, so it is in the run's FAILED summary."""
    u = report.get("universe") or {}
    print(f"\nuniverse   : {u.get('shares')} shares in {u.get('rows_total')} market-watch rows "
          f"{u.get('markets')} {u.get('boards')}")
    print(f"             inserted {u.get('inserted')}, renamed {u.get('renamed')}, "
          f"resectored {u.get('resectored')}, relisted {u.get('relisted')}, "
          f"delisted {u.get('delisted')}")
    if u.get("delisted_codes"):
        print(f"             no longer listed: {', '.join(u['delisted_codes'])}")
    unknown = u.get("unknown_sector") or {}
    if unknown.get("count"):
        print(f"             {unknown['count']} share(s) in a sector code market_sectors "
              f"does not carry: {unknown.get('codes')}")
    sectors = u.get("sectors")
    if sectors:
        print(f"sectors    : {sectors['listed']} TSETMC names; added {sectors['added'] or 'none'}, "
              f"renamed {sectors['renamed'] or 'none'}")
    for error in report.get("errors") or []:
        failures.add(f"ingest {error['kind']}", error["name"],
                     f"{error['error']}: {error['message']}")


def _print_chunk_errors(report: dict, contradictions: list, failures: Failures) -> None:
    for error in report.get("errors") or []:
        key = error.get("ins_code") or error.get("trade_date") or error.get("name")
        if error["error"] in CONTRADICTIONS:
            contradictions.append((error["kind"], key, error["message"]))
        else:
            failures.add(f"ingest {error['kind']}", str(key),
                         f"{error['error']}: {error['message']}")


def _note_restated(part: str, items: Mapping[str, dict], restated: "Restated") -> None:
    """A chunk's restated rows, into the run's RESTATED list."""
    for key, r in sorted(items.items()):
        count = r.get("restated") if part == "flows" else r.get("rows_restated")
        if count:
            restated.add(part, key, f"{count} stored row(s) kept; e.g. "
                         f"{r.get('restated_examples', [])[:2]}")


def _ingest_chunks(post, part: str, total: int, chunk: int, manifest: str,
                   failures: Failures, contradictions: list, restated: "Restated") -> dict:
    """Walk one part of the manifest in chunks. Every item a chunk failed on
    comes back in its report — the service answers 200 with the whole report
    even when every item of the chunk failed (``all_failed``), because wget
    drops the body of an error answer — and each is a failure of the run. A
    call that fails outright (ssh, or the service down) fails its chunk, and
    the walk continues with the next one."""
    totals = Counter()
    items: dict[str, dict] = {}
    offset = 0
    while offset < total:
        body = {"manifest": manifest, "part": part, "offset": offset, "limit": chunk}
        try:
            report = post(SHARE_INGEST_URL, body)
        except RemoteError as exc:
            failures.add(f"ingest {part}", f"[{offset}:{offset + chunk}]",
                         f"the call for this chunk failed ({exc})")
            offset += chunk
            continue
        items.update(report.get("items") or {})
        totals["succeeded"] += report.get("succeeded", 0)
        totals["failed"] += report.get("failed", 0)
        _print_chunk_errors(report, contradictions, failures)
        _note_restated(part, report.get("items") or {}, restated)
        next_offset = report.get("next_offset")
        offset = next_offset if isinstance(next_offset, int) and next_offset > offset else total
    return {"items": items, **totals}


def _positive_delay(text: str) -> float:
    value = float(text)
    if value < MIN_REQUEST_INTERVAL:
        raise argparse.ArgumentTypeError(
            f"--delay {value} would exceed three requests a second; the floor is "
            f"{MIN_REQUEST_INTERVAL:.3f}")
    return value


def _floor_date(text: str) -> date:
    try:
        value = date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--flows-since wants YYYY-MM-DD: {exc}") from exc
    if not EARLIEST_FLOOR <= value <= datetime.now(timezone.utc).date():
        raise argparse.ArgumentTypeError(
            f"--flows-since must fall between {EARLIEST_FLOOR} and today")
    return value


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="read the server's rosters and state, download everything but the "
                         "bulk loop (the roster's bars, the index histories, the market files "
                         "and watch, the fund lists: ~104 requests, ~60 MB on 2026-09-29), "
                         "print what the bulk loop would fetch; upload and ingest nothing")
    ap.add_argument("--host", default=DEFAULT_HOST, help="ssh target for the server")
    ap.add_argument("--out", default=None,
                    help="local directory for the payloads (default: ./tsetmc-bars)")
    ap.add_argument("--ins-code", action="append", default=[],
                    help="fetch bars and money flow only for these insCodes instead of the "
                         "server's roster and the whole market (repeatable)")
    ap.add_argument("--delay", type=_positive_delay, default=DEFAULT_DELAY,
                    help="seconds between the starts of consecutive TSETMC requests "
                         f"(default {DEFAULT_DELAY}; at least {MIN_REQUEST_INTERVAL:.3f}, "
                         "i.e. never more than three a second)")
    ap.add_argument("--flows-since", type=_floor_date, default=None,
                    help="floor for a non-roster share's money flow and for the day files "
                         "(YYYY-MM-DD; default: the server's, 2025-03-21)")
    ap.add_argument("--no-shares", action="store_true",
                    help="skip the whole-market money flow and day files (migration 0030); "
                         "fetch the roster's money flow the 0029 way instead")
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--bars-only", action="store_true",
                       help="fetch equity bars only, as before migration 0029")
    scope.add_argument("--market-only", action="store_true",
                       help="skip the equity bars; fetch indices, market value, money "
                            "flow, the snapshot and the commodity funds")
    args = ap.parse_args()
    PACER.interval = args.delay
    try:
        return run(args)
    except (FetchError, RemoteError) as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1


def run(args: argparse.Namespace) -> int:
    run_started = time.monotonic()
    do_market = not args.bars_only
    do_shares = do_market and not args.no_shares
    roster_flows = do_market and args.no_shares
    need_roster = not args.market_only or roster_flows

    codes: list[str] = []
    if args.ins_code:
        codes = [_assert_safe_ins_code(str(c).strip()) for c in args.ins_code]
        print(f"roster     : {len(codes)} insCode(s) given on the command line")
    elif need_roster:
        items = fetch_roster(args.host)
        codes = [str(item["ins_code"]) for item in items]
        print(f"roster     : {len(codes)} enabled instrument(s) from {args.host}")
        for item in items:
            print(f"             {item['symbol_fa']}  {item['ins_code']}  "
                  f"bars={item.get('bar_count', 0)}  last={item.get('last_bar')}")

    index_codes: list[str] = []
    state: dict = {}
    funds: list[dict] = []
    if do_market:
        index_items = fetch_index_roster(args.host)
        index_codes = [str(item["ins_code"]) for item in index_items]
        print(f"indices    : {len(index_codes)} enabled index(es) from {args.host}")
        funds = fetch_fund_roster(args.host)
        print(f"funds      : {len(funds)} commodity fund(s) from {args.host}: "
              f"{', '.join(str(f.get('symbol_fa')) for f in funds)}")
    if do_shares:
        state = fetch_share_state(args.host)
        print(f"shares     : the server holds {state.get('count')} share(s), "
              f"{len(state['session_dates'])} ingested session(s) "
              f"(newest {state['session_dates'][-1] if state['session_dates'] else 'none'}), "
              f"floor {state['floor']}")
    floor = args.flows_since or (date.fromisoformat(state["floor"]) if state else EARLIEST_FLOOR)

    out_dir = Path(args.out or "tsetmc-bars")
    # 0700, never 0777: these payloads are the physical evidence behind every
    # adjusted price this system will serve, and a directory anyone can write
    # cannot back that. Same rule as the SCI archive directory.
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o700)
    shares_dir = out_dir / "shares"
    flows_dir, days_dir = shares_dir / "flows", shares_dir / "days"
    funds_dir, evidence_dir = out_dir / "funds", out_dir / "evidence"
    for directory in (shares_dir, flows_dir, days_dir, funds_dir, evidence_dir):
        directory.mkdir(exist_ok=True)
        directory.chmod(0o700)
    _clear_generated(shares_dir, fixed=SHARE_FIXED_NAMES)
    _clear_generated(flows_dir, FLOW_NAME_RE)
    _clear_generated(days_dir, DAY_NAME_RE)
    _clear_generated(funds_dir, FUND_NAME_RE)

    failures = Failures()
    timings: dict[str, float] = {}

    bar_files: list[Path] = []
    if not args.market_only:
        started = time.monotonic()
        for ins_code in codes:
            path = download(ins_code, out_dir)
            print(f"downloaded : {ins_code}  {path.stat().st_size:,} bytes")
            bar_files.append(path)
        timings["bars"] = time.monotonic() - started

    market_files: dict[str, Path] = {}
    index_files: list[Path] = []
    flow_files: list[Path] = []
    if do_market:
        started = time.monotonic()
        for name, (url_path, key, min_bytes) in MARKET_PATHS.items():
            market_files[name] = download_payload(url_path, out_dir / name, key, min_bytes)
            print(f"downloaded : {name:<22} {market_files[name].stat().st_size:,} bytes")
        # What TSETMC lists against what the registry carries, both ways. A new
        # index is not ingested until it is INSERTed — the registry is data —
        # but it is named here so nobody has to notice its absence first.
        listed: set[str] = set()
        for name in ("indexlive-1.json", "indexlive-2.json"):
            rows = json.loads(market_files[name].read_text(encoding="utf-8"))["indexB1"]
            listed |= {str(r.get("insCode", "")).strip() for r in rows}
        unregistered = sorted(listed - set(index_codes))
        unlisted = sorted(set(index_codes) - listed)
        if unregistered:
            print(f"NOTICE     : TSETMC lists {len(unregistered)} index(es) this registry "
                  f"does not carry: {', '.join(unregistered)}. Add them to "
                  "market_indices with an INSERT to ingest them.")
        if unlisted:
            print(f"NOTICE     : {len(unlisted)} registered index(es) are not in TSETMC's "
                  f"live list today: {', '.join(unlisted)}.")
        for ins_code in index_codes:
            path = download_payload(
                INDEX_HISTORY_PATH.format(ins_code=ins_code),
                out_dir / f"index-{ins_code}.json", "indexB2", MIN_INDEX_BYTES,
                ins_code=ins_code)
            index_files.append(path)
        print(f"downloaded : {len(index_files)} index histories, "
              f"{sum(p.stat().st_size for p in index_files):,} bytes")
        if roster_flows:
            for ins_code in codes:
                path = download_payload(
                    CLIENT_TYPE_PATH.format(ins_code=ins_code),
                    out_dir / f"clienttype-{ins_code}.json", "clientType",
                    MIN_CLIENT_TYPE_BYTES, ins_code=ins_code)
                flow_files.append(path)
            print(f"downloaded : {len(flow_files)} roster money-flow histories, "
                  f"{sum(p.stat().st_size for p in flow_files):,} bytes")
        timings["market"] = time.monotonic() - started

    # --- the whole market (0030) ---------------------------------------------
    manifest_ready = False
    share_counts: dict[str, Any] = {}
    flow_names: list[str] = []
    day_names: list[str] = []
    if do_shares:
        started = time.monotonic()
        shares: list[tuple[str, str, str]] = []
        has_watch = has_static = False
        by_code = {item["ins_code"]: item for item in state.get("items") or []}
        try:
            blob, watch = fetch_json(MARKET_WATCH_PATH, "marketwatch")
            shares = share_codes(watch)
            if len(shares) < MIN_MARKET_WATCH_SHARES:
                raise FetchError(
                    f"the market watch lists only {len(shares)} shares in "
                    f"{len(watch['marketwatch'])} rows (1,162 on 2026-09-29): truncated")
            (shares_dir / "market-watch.json").write_bytes(blob)
            has_watch = True
        except FetchError as exc:
            failures.add("market watch", "GetMarketWatch", str(exc))
            # Without today's listing the universe is not refreshed, but the
            # shares the server already knows to be listed still get their
            # flows: one failed request should not cost 1,160 histories.
            shares = [(c, "", str(item.get("symbol_fa") or ""))
                      for c, item in sorted(by_code.items()) if item.get("listed")]
            print(f"NOTICE     : no market watch; fetching the {len(shares)} share(s) the "
                  "server already lists instead, and leaving the universe as it is.")
        # A roster share missing from the watch (suspended, or a watch that
        # skipped it) keeps its flow history current anyway, as it did before
        # 0030: the server already carries it in market_shares.
        listed_codes = {c for c, _, _ in shares}
        for c, item in sorted(by_code.items()):
            if item.get("roster") and c not in listed_codes:
                print(f"NOTICE     : roster share {item.get('symbol_fa')} ({c}) is not in "
                      "today's market watch; fetching its money flow anyway.")
                shares.append((c, "", str(item.get("symbol_fa") or "")))
        try:
            blob, static = fetch_json(STATIC_DATA_PATH, "staticData")
            if not any(isinstance(r, dict) and r.get("type") == "IndustrialGroup"
                       for r in static["staticData"]):
                raise FetchError("GetStaticData carries no IndustrialGroup rows")
            (shares_dir / "static-data.json").write_bytes(blob)
            has_static = True
        except FetchError as exc:
            # The names are a refresh of a seeded vocabulary; losing them costs
            # nothing but staleness, so the run goes on without them.
            failures.add("sector names", "GetStaticData", str(exc))
        if codes and args.ins_code:
            wanted = set(codes)
            missing = sorted(wanted - {c for c, _, _ in shares})
            if missing:
                print(f"NOTICE     : --ins-code {', '.join(missing)} is not a share in "
                      "today's market watch; its money flow is not fetched.")
            shares = [s for s in shares if s[0] in wanted]
        if shares:
            print(f"market     : {len(shares)} share(s) in the market watch; "
                  f"{sum(1 for c, _, _ in shares if c not in by_code)} new to the server")
        stored_sessions = [date.fromisoformat(d) for d in state.get("session_dates") or []]
        # Stored days ingested before some of their shares were in the
        # universe: fetched again so those shares get their session rows.
        recheck = [date.fromisoformat(d) for d in state.get("session_dates_incomplete") or []]
        if args.dry_run and shares:
            roster_n = sum(1 for c, _, _ in shares if (by_code.get(c) or {}).get("roster"))
            new_n = sum(1 for c, _, _ in shares if not (by_code.get(c) or {}).get("flow_count"))
            print(f"\nwould fetch: {len(shares)} money-flow histories "
                  f"({roster_n} roster, full history; {new_n} with nothing stored, "
                  f"from {floor}; the rest trimmed to what is new + {FLOW_OVERLAP_ROWS} "
                  "overlap rows)")
            print(f"would fetch: the day file of every session since {floor} on which "
                  f">= {SESSION_MIN_SHARES} shares traded, minus the {len(stored_sessions)} "
                  f"stored, plus the newest {DAY_OVERLAP} stored and the {len(recheck)} the "
                  "server asks for again — the calendar comes from those histories, so it is "
                  "known only after they are downloaded")
            print(f"estimate   : ~{len(shares) * max(args.delay, 0.4) / 60:.0f} minutes "
                  "for the histories alone at this --delay")
        elif shares:
            flow_names, dates, share_counts = download_share_flows(
                shares, by_code, floor, flows_dir, evidence_dir, failures)
            timings["money flow"] = time.monotonic() - started
            if args.ins_code:
                # The calendar is read off the whole market's histories; a
                # handful of shares cannot reach the 200-share threshold.
                print("NOTICE     : with --ins-code the session calendar is not derived; "
                      "no day file is fetched.")
                sessions, to_fetch = [], []
            else:
                cutoff = settled_cutoff(datetime.now(timezone.utc))
                sessions = market_sessions(dates, floor=floor, before=cutoff,
                                           min_shares=SESSION_MIN_SHARES)
                to_fetch = day_files_to_fetch(sessions, stored_sessions, overlap=DAY_OVERLAP,
                                              recheck=recheck)
            print(f"sessions   : {len(sessions)} market session(s) since {floor}; "
                  f"fetching {len(to_fetch)} day file(s)"
                  + (f", {len(recheck)} of them stored before some of their shares were "
                     "in the universe" if recheck and not args.ins_code else ""))
            day_started = time.monotonic()
            day_names = download_day_files(to_fetch, days_dir, failures)
            timings["day files"] = time.monotonic() - day_started
            print(f"downloaded : {len(flow_names)} trimmed money-flow file(s) "
                  f"({share_counts.get('rows_shipped', 0):,} rows; "
                  f"{share_counts.get('new_share', 0)} new share(s), "
                  f"{share_counts.get('incremental', 0)} incremental, "
                  f"{share_counts.get('nothing_new', 0)} with nothing new, "
                  f"{share_counts.get('empty', 0)} never traded), "
                  f"{len(day_names)} day file(s)")
        if not args.dry_run:
            manifest = build_manifest(flow_names, day_names, floor=floor,
                                      has_watch=has_watch, has_static=has_static)
            (shares_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
            manifest_ready = bool(has_watch or flow_names or day_names)

    fund_files: list[Path] = []
    if funds:
        started = time.monotonic()
        fund_files = download_funds(funds, funds_dir, failures)
        timings["funds"] = time.monotonic() - started
        print(f"downloaded : {len(fund_files)} commodity-fund daily list(s)")

    downloaded = bar_files + list(market_files.values()) + index_files + flow_files
    print(f"requests   : {PACER.requests} to TSETMC in {time.monotonic() - run_started:,.0f} s")
    if args.dry_run:
        print(f"\ndry run: {len(downloaded) + len(fund_files)} payload(s) in {out_dir}; "
              "server untouched.")
        for kind, key, message in failures.items:
            print(f"  FAILED {kind} {key}: {_brief(message)}")
        return 1 if failures else 0

    # One directory per run, named by the wall clock, so a run never overwrites
    # the archive of an earlier one and a failed transfer is diagnosable
    # afterwards. The name is generated here and never comes from a response.
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    remote_dir = f"{REMOTE_DIR}/{run_id}"
    container_dir = f"/tmp/tsetmc-{run_id}"

    ship_dirs = ([shares_dir] if manifest_ready else []) + ([funds_dir] if fund_files else [])
    print(f"\nuploading {len(downloaded)} file(s) and {len(ship_dirs)} directory(ies) to "
          f"{args.host}:{remote_dir}/ ...")
    quoted_remote = shlex.quote(remote_dir)
    prepare = (
        f"sudo mkdir -p {quoted_remote} && "
        f'sudo chown "$(id -un)":"$(id -gn)" {quoted_remote} && '
        f"sudo chmod 700 {quoted_remote}"
    )
    upload_started = time.monotonic()
    subprocess.run(["ssh", args.host, prepare], check=True)
    # One scp for the whole run rather than one connection per file: a full run
    # is ~1,500 files. -C because a first run is ~300 MB of JSON (the money
    # flow since the floor and ~305 day files) that compresses about five-fold. Every name is generated here from a
    # validated insCode, a date or a fixed table, never from a response.
    subprocess.run(
        ["scp", "-q", "-C", "-r", *[str(p) for p in downloaded],
         *[str(d) for d in ship_dirs], f"{args.host}:{remote_dir}/"],
        check=True,
    )
    timings["upload"] = time.monotonic() - upload_started

    def inside(path: Path) -> str:
        return f"{container_dir}/{path.name}"

    copied = False
    # Whether a copy into the container was attempted at all: a copy that
    # succeeded before its ingest call failed must still be removed.
    touched = False

    def post(url: str, body: dict) -> dict:
        nonlocal copied, touched
        touched = True
        report = _post_ingest(args.host, url, body, remote_dir, container_dir,
                              copy_first=not copied)
        copied = True
        return report

    exit_code = 1 if failures else 0
    restated = Restated()
    contradictions: list[tuple[str, str, str]] = []
    ingest_started = time.monotonic()
    if bar_files:
        # The ingest body names PATHS, not payloads: twenty histories are ~28 MB
        # and `wget --post-data` puts its argument in argv, which cannot carry
        # that. The files go in with `docker compose cp`, the same way the SCI
        # workbook does, and the body stays a few hundred bytes.
        #
        # A pass in which every payload failed answers 502 and wget exits
        # non-zero. That is one failed item of this run, not its end: the
        # market, the whole market's money flow and the funds after it are
        # independent of the bars, so it is reported and the run goes on. A
        # partial success is still worth a non-zero exit so a caller notices.
        try:
            report = post(INGEST_URL, {"paths": [inside(p) for p in bar_files]})
        except RemoteError as exc:
            failures.add("ingest bars", "all", str(exc))
        else:
            _print_bars_report(report, failures, restated)

    if do_market:
        body = {
            "index_live": [inside(market_files["indexlive-1.json"]),
                           inside(market_files["indexlive-2.json"])],
            "index_histories": [inside(p) for p in index_files],
            "market_values": {"bourse": inside(market_files["marketvalue-1.json"]),
                              "farabourse": inside(market_files["marketvalue-2.json"])},
            "client_flows": [inside(p) for p in flow_files],
            "overviews": {"bourse": inside(market_files["overview-1.json"]),
                          "farabourse": inside(market_files["overview-2.json"])},
            "sector_summary": inside(market_files["sectors.json"]),
        }
        try:
            report = post(MARKET_INGEST_URL, body)
        except RemoteError as exc:  # every market item failed; see the bars above
            failures.add("ingest market", "all", str(exc))
        else:
            _print_market_report(report, failures, restated, contradictions)

    if manifest_ready:
        manifest_path = f"{container_dir}/shares/manifest.json"
        manifest = json.loads((shares_dir / "manifest.json").read_text(encoding="utf-8"))
        # The universe first: a share new today must be in market_shares
        # before its flows or its session rows can be.
        if manifest["market_watch"]:
            try:
                report = post(SHARE_INGEST_URL, {"manifest": manifest_path, "part": "universe"})
                _print_universe_report(report, failures)
            except RemoteError as exc:
                failures.add("ingest universe", "market-watch.json", str(exc))
        if manifest["flows"]:
            flows = _ingest_chunks(post, "flows", len(manifest["flows"]), FLOW_CHUNK,
                                   manifest_path, failures, contradictions, restated)
            inserted = sum(r.get("sessions_inserted", 0) for r in flows["items"].values())
            print(f"money flow : {len(flows['items'])} share(s) ingested, {inserted:,} "
                  f"session row(s) inserted, {flows.get('failed', 0)} failed")
        if manifest["days"]:
            days = _ingest_chunks(post, "sessions", len(manifest["days"]), SESSION_CHUNK,
                                  manifest_path, failures, contradictions, restated)
            inserted = sum(r.get("rows_inserted", 0) for r in days["items"].values())
            unchecked = sorted(d for d, r in days["items"].items()
                               if (r.get("date_check") or {}).get("status") != "agreed")
            print(f"sessions   : {len(days['items'])} day file(s) ingested, {inserted:,} "
                  f"share-session row(s) inserted, {days.get('failed', 0)} failed")
            if unchecked:
                print(f"             date not cross-checked (too few stored flows): "
                      f"{', '.join(unchecked)}")

    if fund_files:
        try:
            report = post(FUND_INGEST_URL, {"paths": [f"{container_dir}/funds/{p.name}"
                                                      for p in fund_files]})
            for code, r in sorted((report.get("funds") or {}).items()):
                print(f"fund       : {r['symbol']:<6} {r['instrument_code']:<22} "
                      f"{r['closes_inserted']:>5} close(s) inserted, "
                      f"{r['carry_forward_skipped']} carry-forward and "
                      f"{r['unsettled_skipped']} unsettled skipped, "
                      f"{r['first_date']}..{r['last_date']}")
                if r.get("live_era_skipped"):
                    print(f"             {r['live_era_skipped']} session(s) from "
                          f"{r['live_era_from']} left to the live source")
                if r.get("restated"):
                    restated.add("fund", f"{code} {r['symbol']}",
                                 f"{r['restated']} stored close(s) kept; e.g. "
                                 f"{r.get('restated_examples', [])[:2]}")
                if r.get("served_gaps"):
                    restated.add("fund", f"{code} {r['symbol']}",
                                 f"the list served skips {r['served_gaps']} session(s) TEDPIX "
                                 f"records, not judged: {r.get('served_gap_examples')}")
            for error in report.get("errors") or []:
                if error["error"] in CONTRADICTIONS:
                    contradictions.append(("fund", error["path"], error["message"]))
                else:
                    failures.add("ingest fund", error["path"],
                                 f"{error['error']}: {error['message']}")
        except RemoteError as exc:
            failures.add("ingest funds", "all", str(exc))
    if touched:
        # The ingest is done with the container's copy; the host archive
        # stays. Left in /tmp, a first run is ~415 MB there and each weekly
        # one ~65 MB more, forever (measured on the 215-share rehearsal of
        # 2026-09-29 and scaled to the market).
        try:
            _remove_container_copy(args.host, container_dir)
            print(f"cleanup    : removed {container_dir} from the prediction container; "
                  f"the run stays archived in {remote_dir}")
        except RemoteError as exc:
            failures.add("cleanup", container_dir, str(exc))
    timings["ingest"] = time.monotonic() - ingest_started

    if restated:
        print(f"\nRESTATED ({len(restated.items)}): TSETMC's copy now states these "
              "differently from what is stored; the stored rows were kept and the run "
              "does not fail on them")
        for kind, key, what in restated.items:
            print(f"  [{kind}] {key}: {what}")
    if contradictions:
        print(f"\nCONTRADICTIONS ({len(contradictions)}): TSETMC now serves something "
              "unlike most of what is stored for these; nothing was overwritten and nothing "
              "was written for them")
        for kind, key, message in contradictions:
            print(f"  [{kind}] {key}: {message}")
    if failures:
        print(f"\nFAILED ({len(failures.items)}):")
        for kind, key, message in failures.items:
            print(f"  [{kind}] {key}: {_brief(message)}")
    print("\ntimings    : " + ", ".join(f"{k} {v:,.0f} s" for k, v in timings.items())
          + f"; total {time.monotonic() - run_started:,.0f} s")
    if contradictions or failures:
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
