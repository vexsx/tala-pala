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

Usage:
    python3 scripts/tsetmc_fetch.py --dry-run              # roster + download only
    python3 scripts/tsetmc_fetch.py                        # ...then upload and ingest
    python3 scripts/tsetmc_fetch.py --ins-code 46348559193224090 --dry-run
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
    proc = subprocess.run(
        ["curl", "--fail", "--location", "--silent", "--show-error",
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


def fetch_roster(host: str) -> list[dict]:
    """Read the enabled roster from the prediction service, over SSH.

    A read, so --dry-run does it too: knowing what the deployment would ingest
    is exactly what a dry run is for.
    """
    inner = (
        'wget -qO- --header="X-Internal-Token: $TOKEN" ' + shlex.quote(ROSTER_URL)
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
        payload = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"the roster endpoint did not return JSON: {exc}\n"
            f"first 200 bytes: {raw[:200]!r}"
        ) from exc
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise SystemExit(
            "the roster is empty. equity_instruments is seeded by migration 0028; "
            "either it has not been applied or every row is disabled."
        )
    for item in items:
        _assert_safe_ins_code(str(item.get("ins_code", "")))
    return items


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="read the roster and download only; do not touch the server")
    ap.add_argument("--host", default=DEFAULT_HOST, help="ssh target for the server")
    ap.add_argument("--out", default=None,
                    help="local directory for the payloads (default: ./tsetmc-bars)")
    ap.add_argument("--ins-code", action="append", default=[],
                    help="fetch only these insCodes instead of the server's roster "
                         "(repeatable); skips the roster read entirely")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="courtesy delay between downloads, seconds (default 0.5)")
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

    out_dir = Path(args.out or "tsetmc-bars")
    # 0700, never 0777: these payloads are the physical evidence behind every
    # adjusted price this system will serve, and a directory anyone can write
    # cannot back that. Same rule as the SCI archive directory.
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o700)

    downloaded: list[Path] = []
    for ins_code in codes:
        path = download(ins_code, out_dir, args.delay)
        print(f"downloaded : {ins_code}  {path.stat().st_size:,} bytes")
        downloaded.append(path)

    if args.dry_run:
        print(f"\ndry run: {len(downloaded)} payload(s) in {out_dir}; server untouched.")
        return 0

    # One directory per run, named by the wall clock, so a run never overwrites
    # the archive of an earlier one and a failed transfer is diagnosable
    # afterwards. The name is generated here and never comes from a response.
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    remote_dir = f"{REMOTE_DIR}/{run_id}"
    container_dir = f"/tmp/tsetmc-{run_id}"

    print(f"\nuploading to {args.host}:{remote_dir}/ ...")
    quoted_remote = shlex.quote(remote_dir)
    prepare = (
        f"sudo mkdir -p {quoted_remote} && "
        f'sudo chown "$(id -un)":"$(id -gn)" {quoted_remote} && '
        f"sudo chmod 700 {quoted_remote}"
    )
    subprocess.run(["ssh", args.host, prepare], check=True)
    for path in downloaded:
        # path.name is "<insCode>.json" and the insCode was validated above.
        subprocess.run(
            ["scp", "-q", str(path), f"{args.host}:{remote_dir}/{path.name}"],
            check=True,
        )

    # The ingest body names PATHS, not payloads: twenty histories are ~28 MB and
    # `wget --post-data` puts its argument in argv, which cannot carry that.
    # The files go in with `docker compose cp`, the same way the SCI workbook
    # does, and the body stays a few hundred bytes.
    container_paths = [f"{container_dir}/{path.name}" for path in downloaded]
    body = shlex.quote(json.dumps({"paths": container_paths}))
    inner = (
        'wget -qO- --header="X-Internal-Token: $TOKEN" '
        '--header="Content-Type: application/json" '
        f"--post-data={body} "
        f"{shlex.quote(INGEST_URL)}"
    )
    remote = _remote_token_prelude() + (
        # The target directory is created FIRST and the source ends in "/." so
        # the CONTENTS are copied into it.
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
        # -e TOKEN="$TOKEN" again: the inner command is shlex-quoted so the host
        # shell cannot expand $TOKEN inside it, and without passing it into the
        # container the request goes out with the literal header
        # "X-Internal-Token: $TOKEN" and earns a 401. That exact bug is what the
        # first SCI run hit.
        f'sudo docker compose exec -T -e TOKEN="$TOKEN" api sh -c {shlex.quote(inner)}'
    )
    raw = _ssh(args.host, remote, capture=True)
    text = raw.decode("utf-8", "replace").strip()
    try:
        report = json.loads(text)
    except json.JSONDecodeError:
        print(text)
        raise SystemExit("the ingest did not return JSON; output above")

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
    # A pass in which every payload failed answers 502 and wget exits non-zero,
    # so this is reached only on a full or partial success; a partial one is
    # still worth a non-zero exit so a caller notices.
    return 1 if report.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
