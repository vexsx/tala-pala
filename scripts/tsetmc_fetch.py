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
(GetMarketValueByFlow), the حقیقی/حقوقی money flow of every roster instrument
(GetClientTypeHistory), and the market overview and sector breadth as they
stand at that moment. They go up in the same directory and are posted to
POST /internal/bourse/ingest after the bars. --bars-only restores the old
behaviour; --market-only skips the bars.

Usage:
    python3 scripts/tsetmc_fetch.py --dry-run              # rosters + download only
    python3 scripts/tsetmc_fetch.py                        # ...then upload and ingest
    python3 scripts/tsetmc_fetch.py --ins-code 46348559193224090 --dry-run
    python3 scripts/tsetmc_fetch.py --bars-only
    python3 scripts/tsetmc_fetch.py --host ubuntu@1.2.3.4
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

BASE = "https://cdn.tsetmc.com"
# GetClosingPriceDailyList/<insCode>/0 — the trailing 0 asks for the whole
# history rather than a window. فولاد returns 4,636 bars, 2007-03-11 onward.
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
    """
    # Retried, because TSETMC's CDN is intermittently unwell: on 2026-09-29 the
    # same URL answered 200, 200, 200, 502, 200 two seconds apart, and a run of
    # ~120 files died on the first one. --retry-all-errors and not plain
    # --retry, measured rather than assumed: curl 8.7 reports TSETMC's 502 as
    # exit 56 (a receive error, not an HTTP one) and plain --retry does not
    # treat that as transient — it gave up after 0 seconds. The cost of
    # retrying everything is that a genuine 404 takes ~15 s to fail instead of
    # none, which a manual weekly run can afford.
    proc = subprocess.run(
        ["curl", "--fail", "--location", "--silent", "--show-error",
         "--retry", "5", "--retry-delay", "3", "--retry-all-errors",
         "--max-time", str(timeout), "--user-agent", USER_AGENT, url],
        capture_output=True,
        check=False,   # the return code is inspected below, with the stderr
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"fetch failed for {url}\n"
            f"curl exit {proc.returncode}: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}\n"
            "If this is the production host, that is expected: cdn.tsetmc.com "
            "refuses TCP 443 from there. Run this script somewhere else."
        )
    return proc.stdout


def _ssh(host: str, command: str, capture: bool = False) -> bytes:
    proc = subprocess.run(
        ["ssh", host, command],
        capture_output=capture,
        check=not capture,
    )
    if capture and proc.returncode != 0:
        raise SystemExit(
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
    raw = _ssh(host, remote, capture=True)
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


def download_payload(url_path: str, out: Path, key: str, min_bytes: int,
                     courtesy_delay: float, ins_code: str = "") -> Path:
    """Download one market-level payload and verify its shape before keeping it.

    The same three refusals as a bar download: too small to be real, not JSON,
    or missing the one top-level key the service parses. When ``ins_code`` is
    given, every row must carry it — a file named for one index and holding
    another's values is invisible after the transfer.
    """
    blob = _curl(BASE + url_path)
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
    time.sleep(courtesy_delay)
    return out


def download(ins_code: str, out_dir: Path, courtesy_delay: float) -> Path:
    """Download one instrument's whole daily-bar history and verify it."""
    _assert_safe_ins_code(ins_code)
    blob = _curl(BASE + BARS_PATH.format(ins_code=ins_code))
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
    time.sleep(courtesy_delay)
    return out


def _post_ingest(host: str, url: str, body: dict, remote_dir: str,
                 container_dir: str, copy_first: bool) -> dict:
    """Copy the run directory into the prediction container (once) and POST an
    ingest body naming files inside it. Returns the service's JSON report."""
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
    except json.JSONDecodeError:
        print(text)
        raise SystemExit("the ingest did not return JSON; output above")


def _print_bars_report(report: dict) -> None:
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
    for error in report.get("errors") or []:
        print(f"  ERROR {error['path']}: {error['error']}: {error['message']}")


def _print_market_report(report: dict) -> None:
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
    for market, r in sorted((report.get("market_values") or {}).items()):
        print(f"market val : {market:<10} {r['values_inserted']:>5} inserted, "
              f"{r['first_date']}..{r['last_date']}")
    flows = report.get("client_flows") or {}
    print(f"money flow : {len(flows)} symbol(s), "
          f"{sum(r['sessions_inserted'] for r in flows.values())} session(s) inserted")
    snap = report.get("snapshot") or {}
    for market in ("bourse", "farabourse"):
        if market in snap:
            print(f"snapshot   : {market:<10} {snap[market]['activity_at']} "
                  f"({snap[market]['stored']})")
    if "sectors" in snap:
        print(f"sectors    : {snap['sectors']['rows']} rows, "
              f"{snap['sectors']['inserted']} inserted")
    for error in report.get("errors") or []:
        print(f"  ERROR [{error['kind']}] {error['path']}: {error['error']}: "
              f"{error['message']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="read the rosters and download only; do not touch the server")
    ap.add_argument("--host", default=DEFAULT_HOST, help="ssh target for the server")
    ap.add_argument("--out", default=None,
                    help="local directory for the payloads (default: ./tsetmc-bars)")
    ap.add_argument("--ins-code", action="append", default=[],
                    help="fetch only these insCodes instead of the server's roster "
                         "(repeatable); skips the roster read entirely")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="courtesy delay between downloads, seconds (default 0.5)")
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--bars-only", action="store_true",
                       help="fetch equity bars only, as before migration 0029")
    scope.add_argument("--market-only", action="store_true",
                       help="skip the equity bars; fetch indices, market value, "
                            "money flow and the snapshot")
    args = ap.parse_args()

    if args.ins_code:
        codes = [_assert_safe_ins_code(str(c).strip()) for c in args.ins_code]
        print(f"roster     : {len(codes)} insCode(s) given on the command line")
    else:
        items = fetch_roster(args.host)
        codes = [str(item["ins_code"]) for item in items]
        print(f"roster     : {len(codes)} enabled instrument(s) from {args.host}")
        for item in items:
            print(f"             {item['symbol_fa']}  {item['ins_code']}  "
                  f"bars={item.get('bar_count', 0)}  last={item.get('last_bar')}")

    index_codes: list[str] = []
    if not args.bars_only:
        index_items = fetch_index_roster(args.host)
        index_codes = [str(item["ins_code"]) for item in index_items]
        print(f"indices    : {len(index_codes)} enabled index(es) from {args.host}")

    out_dir = Path(args.out or "tsetmc-bars")
    # 0700, never 0777: these payloads are the physical evidence behind every
    # adjusted price this system will serve, and a directory anyone can write
    # cannot back that. Same rule as the SCI archive directory.
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o700)

    bar_files: list[Path] = []
    if not args.market_only:
        for ins_code in codes:
            path = download(ins_code, out_dir, args.delay)
            print(f"downloaded : {ins_code}  {path.stat().st_size:,} bytes")
            bar_files.append(path)

    market_files: dict[str, Path] = {}
    index_files: list[Path] = []
    flow_files: list[Path] = []
    if not args.bars_only:
        for name, (url_path, key, min_bytes) in MARKET_PATHS.items():
            market_files[name] = download_payload(
                url_path, out_dir / name, key, min_bytes, args.delay)
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
                args.delay, ins_code=ins_code)
            index_files.append(path)
        print(f"downloaded : {len(index_files)} index histories, "
              f"{sum(p.stat().st_size for p in index_files):,} bytes")
        for ins_code in codes:
            path = download_payload(
                CLIENT_TYPE_PATH.format(ins_code=ins_code),
                out_dir / f"clienttype-{ins_code}.json", "clientType",
                MIN_CLIENT_TYPE_BYTES, args.delay, ins_code=ins_code)
            flow_files.append(path)
        print(f"downloaded : {len(flow_files)} money-flow histories, "
              f"{sum(p.stat().st_size for p in flow_files):,} bytes")

    downloaded = bar_files + list(market_files.values()) + index_files + flow_files
    if args.dry_run:
        print(f"\ndry run: {len(downloaded)} payload(s) in {out_dir}; server untouched.")
        return 0

    # One directory per run, named by the wall clock, so a run never overwrites
    # the archive of an earlier one and a failed transfer is diagnosable
    # afterwards. The name is generated here and never comes from a response.
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    remote_dir = f"{REMOTE_DIR}/{run_id}"
    container_dir = f"/tmp/tsetmc-{run_id}"

    print(f"\nuploading {len(downloaded)} file(s) to {args.host}:{remote_dir}/ ...")
    quoted_remote = shlex.quote(remote_dir)
    prepare = (
        f"sudo mkdir -p {quoted_remote} && "
        f'sudo chown "$(id -un)":"$(id -gn)" {quoted_remote} && '
        f"sudo chmod 700 {quoted_remote}"
    )
    subprocess.run(["ssh", args.host, prepare], check=True)
    # One scp for the whole run rather than one connection per file: a full run
    # is ~120 files. Every name is generated here from a validated insCode or a
    # fixed table, never from a response.
    subprocess.run(
        ["scp", "-q", *[str(p) for p in downloaded], f"{args.host}:{remote_dir}/"],
        check=True,
    )

    def inside(path: Path) -> str:
        return f"{container_dir}/{path.name}"

    exit_code = 0
    copied = False
    if bar_files:
        # The ingest body names PATHS, not payloads: twenty histories are ~28 MB
        # and `wget --post-data` puts its argument in argv, which cannot carry
        # that. The files go in with `docker compose cp`, the same way the SCI
        # workbook does, and the body stays a few hundred bytes.
        report = _post_ingest(args.host, INGEST_URL,
                              {"paths": [inside(p) for p in bar_files]},
                              remote_dir, container_dir, copy_first=True)
        copied = True
        _print_bars_report(report)
        # A pass in which every payload failed answers 502 and wget exits
        # non-zero, so this is reached only on a full or partial success; a
        # partial one is still worth a non-zero exit so a caller notices.
        if report.get("failed"):
            exit_code = 1

    if not args.bars_only:
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
        report = _post_ingest(args.host, MARKET_INGEST_URL, body, remote_dir,
                              container_dir, copy_first=not copied)
        _print_market_report(report)
        if report.get("failed"):
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
