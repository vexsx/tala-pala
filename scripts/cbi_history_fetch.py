#!/usr/bin/env python3
"""Fetch the CBI time-series viewer's quarterly history of the monetary
aggregates and hand it to the prediction service for ingestion.

WHY THIS RUNS HERE AND NOT ON THE SERVER
----------------------------------------
tsdview.cis.cbi.ir -- the Central Bank of Iran's time-series viewer, the only
machine-readable source of base money, M2, M1 and quasi-money before the
monthly Excel editions begin in Tir 1399 -- answers from outside Iran and NOT
from the production host. Measured 2026-10-11 rather than assumed: the host's
configured resolver fails, and with 1.1.1.1 the name resolves to 80.191.60.23
and the connection times out. The monthly workbooks on www.cbi.ir DO answer
there, which is why they have a scheduled job (cbi-monetary) and this does
not.

So, the scripts/sci_fetch.py pattern: this script runs somewhere that can
reach the viewer, verifies what it got, copies it to the server over the
existing SSH access and asks the prediction service to ingest it from disk
inside the compose network. Nothing new is exposed publicly.

WHAT IT ASKS FOR
----------------
One POST to the viewer's own JSON API (the request its front end makes, read
from its JavaScript): the four series' category ids, the quarterly
sub-frequency, the Jalali calendar, every quarter from 1350 to the end of next
year. No login, no cookie, the project's honest User-Agent. The monthly
sub-frequency answers empty values for these categories, so quarterly is what
exists. The history changes only when CBI restates it, so this is a one-off
that is worth re-running a few times a year, not a refresh.

The server re-checks everything (app/economic/cbi_history.py): the unit, the
frequency, every value, and agreement with the Excel editions already stored.
The checks here only stop an error page or an empty answer from travelling.

Usage:
    python3 scripts/cbi_history_fetch.py --dry-run          # fetch + verify only
    python3 scripts/cbi_history_fetch.py                    # ...then upload and ingest
    python3 scripts/cbi_history_fetch.py --from-file saved.json --dry-run
    python3 scripts/cbi_history_fetch.py --host ubuntu@1.2.3.4
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GRAPH_URL = "https://tsdview.cis.cbi.ir/backend/api/value/graph"
# The project's honest User-Agent, same as every provider in this repo.
USER_AGENT = "IranGoldPredictor/1.0 (+self-hosted analytics)"

# Category ids from the viewer's own category tree (2026-10-11). The same four
# app/economic/cbi_history.py maps to the catalog's CBI_* codes.
CATEGORIES = {
    "CBI_LIQUIDITY": "3505fc25-6097-8f09-e063-4ceaa8c0b336",
    "CBI_BASE_MONEY": "3505fc25-6014-8f09-e063-4ceaa8c0b336",
    "CBI_MONEY_M1": "3505fc25-6098-8f09-e063-4ceaa8c0b336",
    "CBI_QUASI_MONEY": "3505fc25-6031-8f09-e063-4ceaa8c0b336",
}
QUARTERLY = "9848C04378D9D268F987140AB9D87A3BF71A53B447D059BB26A52B9DD0F58732"
FIRST_YEAR = 1350

DEFAULT_HOST = "ubuntu@85.137.30.142"
REMOTE_DIR = "/opt/tala-pala/backups/cbi"
INGEST_URL = "http://prediction-service:8500/internal/economic/cbi-history"

# A minute per attempt and four attempts 5/10/20 s apart: the viewer is slow
# to answer a forty-year query, and a resolver that fails once should not cost
# the run.
TIMEOUT_SECONDS = 60
ATTEMPTS = 4
BACKOFF_SECONDS = 5.0

# The payload name is built HERE, from the date and the hash, never scraped:
# it travels into a command the production host runs under sudo. It is still
# re-validated at the point of use.
SAFE_NAME_RE = re.compile(r"\Atsdview_quarterly_\d{8}_[0-9a-f]{12}\.json\Z")


def current_jalali_year(today: datetime | None = None) -> int:
    """The Jalali year ``today`` falls in (approximate at Nowruz, which is
    harmless: the request runs to the end of the FOLLOWING year anyway)."""
    today = today or datetime.now(timezone.utc)
    return today.year - 621 - (1 if (today.month, today.day) < (3, 21) else 0)


def request_body(today: datetime | None = None) -> dict:
    """The viewer's ``value/graph`` request: the four series, quarterly, Jalali.

    ``toDateDate`` is the API's own spelling.
    """
    return {
        "fromDate": f"{FIRST_YEAR}0101",
        "toDateDate": f"{current_jalali_year(today) + 1}1230",
        "categoryIds": list(CATEGORIES.values()),
        "calender": "FA",
        "subFrequencyId": QUARTERLY,
    }


def fetch(body: dict) -> bytes:
    """POST the request; return the raw answer, after bounded retries.

    Certificate verification is the default and is never turned off. A 401 or
    403 is an access decision by the viewer: it is reported, not retried with
    other headers.
    """
    data = json.dumps(body).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(ATTEMPTS):
        request = urllib.request.Request(
            GRAPH_URL,
            data=data,
            method="POST",
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise SystemExit(
                    f"the viewer refused the request (HTTP {exc.code}); not retrying "
                    "and not working around it"
                ) from exc
            last_error = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        if attempt < ATTEMPTS - 1:
            time.sleep(BACKOFF_SECONDS * (2 ** attempt))
    raise SystemExit(f"fetch failed after {ATTEMPTS} attempts: {last_error}")


def verify(raw: bytes) -> dict:
    """Refuse anything that is not the viewer's answer for the four series.

    Returns a summary (rows, values per series, newest quarter). The server
    performs the full checks; this only keeps an error page, an empty answer
    or a payload missing a series from being shipped and archived.
    """
    if raw.lstrip()[:1] == b"<":
        raise SystemExit("the viewer answered an HTML page, not JSON -- an error or a challenge page")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"the answer is not JSON: {exc}") from exc
    if not isinstance(payload, list) or not payload:
        raise SystemExit("the answer is not a non-empty list of value rows")
    codes = {category: code for code, category in CATEGORIES.items()}
    values: dict[str, int] = {code: 0 for code in CATEGORIES}
    newest = ""
    for row in payload:
        if not isinstance(row, dict):
            raise SystemExit("the answer contains a row that is not an object")
        code = codes.get(row.get("CATEGORYID"))
        if code is None or row.get("amount") in (None, ""):
            continue
        values[code] += 1
        newest = max(newest, str(row.get("khDate") or ""))
    missing = sorted(code for code, count in values.items() if count == 0)
    if missing:
        raise SystemExit(f"the answer has no values for {', '.join(missing)}")
    return {"rows": len(payload), "values": values, "newest": newest}


def payload_name(raw: bytes, today: datetime | None = None) -> str:
    today = today or datetime.now(timezone.utc)
    return f"tsdview_quarterly_{today:%Y%m%d}_{hashlib.sha256(raw).hexdigest()[:12]}.json"


def _assert_safe_name(name: str) -> str:
    if not SAFE_NAME_RE.match(name):
        raise SystemExit(f"refusing the payload name {name!r}: it is not one this script builds")
    return name


def remote_ingest_command(name: str) -> str:
    """The shell command the server runs to copy the payload in and ingest it.

    ``ssh host "..."`` runs its argument in a remote SHELL, so every value here
    is shell input on the production box: the name is validated and
    shlex-quoted, and the JSON body is built by json.dumps and quoted as one
    unit -- the same construction as scripts/sci_fetch.py, for the same
    reasons.
    """
    _assert_safe_name(name)
    remote_path = shlex.quote(f"{REMOTE_DIR}/{name}")
    container_path = shlex.quote(f"/tmp/{name}")
    body = shlex.quote(json.dumps({"path": f"/tmp/{name}"}))
    inner = (
        "wget -qO- --header=\"X-Internal-Token: $TOKEN\" "
        "--header=\"Content-Type: application/json\" "
        f"--post-data={body} "
        f"{INGEST_URL}"
    )
    return (
        "cd /opt/tala-pala && "
        "TOKEN=$(sudo grep -E '^INTERNAL_API_TOKEN=' .env | cut -d= -f2-) && "
        f"sudo docker compose cp {remote_path} prediction-service:{container_path} && "
        # -e TOKEN="$TOKEN" is load-bearing: the inner command is quoted so the
        # HOST shell expands nothing in it, so $TOKEN must come from the
        # container's environment. It is read from the server's own .env by
        # the server's own shell and never appears on this machine.
        f'sudo docker compose exec -T -e TOKEN="$TOKEN" api sh -c {shlex.quote(inner)}'
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch (or read) and verify only; do not touch the server")
    ap.add_argument("--from-file", default=None,
                    help="use a payload saved earlier instead of fetching one")
    ap.add_argument("--host", default=DEFAULT_HOST, help="ssh target for the server")
    ap.add_argument("--out-dir", default=".", help="where to save the payload locally")
    args = ap.parse_args(argv)

    if args.from_file:
        raw = Path(args.from_file).read_bytes()
        print(f"read       : {args.from_file}")
    else:
        body = request_body()
        print(f"requesting : {GRAPH_URL} ({body['fromDate']}..{body['toDateDate']}, quarterly)")
        raw = fetch(body)
    summary = verify(raw)
    name = payload_name(raw)
    out = Path(args.out_dir) / name
    out.write_bytes(raw)
    print(f"rows       : {summary['rows']:,}")
    print("values     : " + ", ".join(f"{code} {count}" for code, count in summary["values"].items()))
    print(f"newest     : {summary['newest']}")
    print(f"sha256     : {hashlib.sha256(raw).hexdigest()}")
    print(f"saved      : {out}")

    if args.dry_run:
        print("\ndry run: server untouched.")
        return 0

    _assert_safe_name(name)
    print(f"\nuploading to {args.host}:{REMOTE_DIR}/ ...")
    # Owned by the invoking user and readable by nobody else: this directory is
    # the physical half of the provenance claim source_documents makes.
    quoted_dir = shlex.quote(REMOTE_DIR)
    prepare = (
        f"sudo mkdir -p {quoted_dir} && "
        f'sudo chown "$(id -un)":"$(id -gn)" {quoted_dir} && '
        f"sudo chmod 700 {quoted_dir}"
    )
    subprocess.run(["ssh", args.host, prepare], check=True)
    subprocess.run(["scp", "-q", str(out), f"{args.host}:{REMOTE_DIR}/{name}"], check=True)
    subprocess.run(["ssh", args.host, remote_ingest_command(name)], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
