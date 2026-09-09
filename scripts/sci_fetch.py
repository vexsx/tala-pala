#!/usr/bin/env python3
"""Fetch the Statistical Centre of Iran's CPI workbook and hand it to the
prediction service for ingestion.

WHY THIS RUNS HERE AND NOT ON THE SERVER
----------------------------------------
amar.org.ir is not reachable from the production host. Diagnosed 2026-09-09
rather than assumed: DNS resolves (217.218.11.77), TCP connects on BOTH 443 and
80, and then nothing answers -- no TLS handshake completes at 1.2 or 1.3, and
plain HTTP returns nothing either. The connection is accepted and the payload
dropped, which is application-layer filtering, not a TLS misconfiguration. The
same host fetches the file fine from outside that network.

So SCI cannot be a server-side cron. This script runs somewhere that can reach
SCI, verifies what it downloaded, copies it to the server, and asks the
prediction service to ingest it from disk. Nothing new is exposed publicly: the
transfer is scp over the existing SSH access and the ingest call happens inside
the compose network, exactly like every other operational task in this repo.

WHY THE URL IS DISCOVERED AND NEVER HARDCODED
---------------------------------------------
SCI embeds the reference period AND a publish timestamp in every filename --
`ts_urban_140505-14050618165804.xlsx` is the Mordad 1405 edition published
1405/06/18 16:58:04. The URL therefore changes on every release, and older
paths rot. Hardcoding one silently freezes the series at whatever was current
when the code was written, so the listing page is scraped each run.

That timestamp is also the reason this series can carry a real `published_at`
where the World Bank series cannot: it is the publication moment, stated by the
publisher, in the filename.

Usage:
    python3 scripts/sci_fetch.py --dry-run          # discover + download only
    python3 scripts/sci_fetch.py                    # ...then upload and ingest
    python3 scripts/sci_fetch.py --host ubuntu@1.2.3.4
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

LISTING_URL = "https://amar.org.ir/prices"
BASE = "https://amar.org.ir"
# The project's honest User-Agent, same as every provider in this repo.
USER_AGENT = "IranGoldPredictor/1.0 (+self-hosted analytics)"

# Urban CPI time-series workbook. 'ts_urban' is the urban-household series --
# the one whose headline, housing, rent and vehicle rows we ingest. Its siblings
# (ts_rural, ts_national, ts_decile) are deliberately NOT fetched: each is a
# different population and would need its own series codes rather than being
# folded into these.
# The capture is deliberately NARROW. `[^"]*` would have matched anything up to
# the closing quote -- including ';', '$(', '`', spaces and '/' -- and this
# filename is scraped from a REMOTE PAGE and then travels into a command the
# production host runs under sudo. That is attacker-controlled input reaching a
# root shell, and no amount of quoting downstream makes a permissive pattern
# here the right choice: the value is a filename, so only filename characters
# are accepted.
WORKBOOK_RE = re.compile(
    r'href="(/Portals/0/Statistics/(ts_urban[A-Za-z0-9._-]*\.xlsx))"', re.IGNORECASE
)

# Belt and braces: even a narrowed regex is one careless edit away from being
# widened again, so the value is re-validated at the point of use.
SAFE_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.xlsx\Z")


def _assert_safe_name(name: str) -> str:
    """Refuse any filename that is not plainly a filename.

    Nothing scraped from a remote page is trusted into a command line. If SCI's
    markup ever changes shape, the right outcome is a loud refusal here rather
    than a surprising string reaching the server's shell.
    """
    if not SAFE_NAME_RE.match(name) or "/" in name or ".." in name:
        raise SystemExit(
            f"refusing the scraped filename {name!r}: it is not a plain "
            "[A-Za-z0-9._-] .xlsx name. This value is executed on the "
            "production host, so anything unexpected is refused rather than "
            "quoted and hoped for."
        )
    return name

DEFAULT_HOST = "ubuntu@85.137.30.142"
REMOTE_DIR = "/opt/tala-pala/backups/sci"
MIN_PLAUSIBLE_BYTES = 100_000     # the real workbook is ~1.2 MB; a WAF error page is not


def _get(url: str, timeout: int = 120) -> bytes:
    """Fetch over TLS, WITH full certificate verification, via curl.

    SCI serves an incomplete certificate chain. Diagnosed 2026-09-09 rather
    than worked around blindly: the leaf CN=amar.org.ir is issued by
    "Certum DV TLS G2 R39 CA", and that intermediate is NOT among the two
    certificates the server sends (it offers "Certum Trusted Network CA" and
    "Certum Domain Validation CA SHA2", neither of which signed the leaf), so
    openssl reports "Verify return code: 21 (unable to verify the first
    certificate)".

    Python's path builder refuses it -- with the system store, with certifi,
    and with VERIFY_X509_STRICT cleared -- while curl completes the path
    against the same certifi bundle and returns 200. So this shells out to curl
    instead of using urllib.

    What this is NOT: verification is never disabled. There is no -k and no
    ssl._create_unverified_context anywhere here. A server that misconfigures
    its chain is a reason to use a client that can still verify it, never a
    reason to stop verifying -- this file downloads data that becomes official
    inflation statistics in a research platform.
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
            f"curl exit {proc.returncode}: {proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout


def discover() -> tuple[str, str]:
    """Return (absolute_url, filename) of the current urban CPI workbook."""
    html = _get(LISTING_URL).decode("utf-8", "replace")
    matches = WORKBOOK_RE.findall(html)
    if not matches:
        raise SystemExit(
            "no ts_urban*.xlsx link found on the listing page. SCI reorganised "
            "the page; re-derive the pattern rather than hardcoding a URL."
        )
    # Several links can point at the same file; take the first and confirm the
    # rest agree, so a page carrying two editions is a visible error, not a
    # coin flip over which one gets ingested.
    paths = {p for p, _ in matches}
    if len(paths) > 1:
        raise SystemExit(
            "the listing offers more than one urban CPI workbook:\n  "
            + "\n  ".join(sorted(paths))
            + "\nRefusing to guess which edition is current."
        )
    path, name = matches[0]
    return BASE + path, name


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="discover and download only; do not touch the server")
    ap.add_argument("--host", default=DEFAULT_HOST, help="ssh target for the server")
    ap.add_argument("--out", default=None, help="where to write the workbook locally")
    args = ap.parse_args()

    url, name = discover()
    print(f"discovered : {name}")
    print(f"url        : {url}")

    blob = _get(url)
    if len(blob) < MIN_PLAUSIBLE_BYTES:
        raise SystemExit(
            f"downloaded only {len(blob)} bytes -- that is an error page, not the "
            "workbook. Refusing to hand it on."
        )
    if blob[:2] != b"PK":
        raise SystemExit("downloaded file is not a zip container, so not an .xlsx")

    digest = hashlib.sha256(blob).hexdigest()
    out = Path(args.out or name)
    out.write_bytes(blob)
    print(f"bytes      : {len(blob):,}")
    print(f"sha256     : {digest}")
    print(f"saved      : {out}")

    if args.dry_run:
        print("\ndry run: server untouched.")
        return 0

    _assert_safe_name(name)
    print(f"\nuploading to {args.host}:{REMOTE_DIR}/ ...")
    # 0777 would make the archive world-writable, and this directory is the
    # physical half of the provenance claim: `source_documents` says a value came
    # from a file with a given sha256, and a workbook anyone on the box can
    # overwrite cannot back that claim. Owned by the invoking user, readable by
    # nobody else.
    quoted_dir = shlex.quote(REMOTE_DIR)
    prepare = (
        f"sudo mkdir -p {quoted_dir} && "
        f'sudo chown "$(id -un)":"$(id -gn)" {quoted_dir} && '
        f"sudo chmod 700 {quoted_dir}"
    )
    subprocess.run(["ssh", args.host, prepare], check=True)
    subprocess.run(["scp", "-q", str(out), f"{args.host}:{REMOTE_DIR}/{name}"], check=True)  # name validated above

    # The prediction service reads the file from a mounted path; the ingest call
    # runs inside the compose network so nothing is published to the internet.
    # `ssh host "..."` runs its argument in a remote SHELL, so every value
    # interpolated here is shell input on the production box. The filename is
    # validated above AND shlex-quoted here; the JSON body is built by json.dumps
    # and quoted as one unit rather than assembled with nested escapes, which is
    # how the previous version smuggled quote characters through three layers.
    remote_path = shlex.quote(f"{REMOTE_DIR}/{name}")
    container_path = shlex.quote(f"/tmp/{name}")
    body = shlex.quote(json.dumps({"path": f"/tmp/{name}", "filename": name}))
    inner = (
        "wget -qO- --header=\"X-Internal-Token: $TOKEN\" "
        "--header=\"Content-Type: application/json\" "
        f"--post-data={body} "
        "http://prediction-service:8500/internal/economic/sci-cpi"
    )
    remote = (
        "cd /opt/tala-pala && "
        "TOKEN=$(sudo grep -E '^INTERNAL_API_TOKEN=' .env | cut -d= -f2-) && "
        f"sudo docker compose cp {remote_path} prediction-service:{container_path} && "
        f"sudo docker compose exec -T api sh -c {shlex.quote(inner)}"
    )
    subprocess.run(["ssh", args.host, remote], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
