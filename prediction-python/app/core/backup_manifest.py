"""Backup-bundle manifest: build it, and prove a bundle is still restorable.

A dump alone does not restore this system. Predictions are served by loading
the joblib artifact recorded in the ACTIVE ``model_versions`` row
(``models/predicting.py`` refuses to predict when that file is missing), so a
database-only backup restores a schema that points at artifacts nobody kept.
The bundle therefore pairs the dump with the active artifacts, and the
manifest is the contract that ties the two together: which artifact belongs to
which symbol/horizon/model, where it has to land on restore, and what its
bytes must hash to.

WHY this lives in Python as well as in ``scripts/backup.sh``: the shell script
is the operational writer — it must run on a host that has nothing but docker
and coreutils, so it emits the JSON itself. This module is the executable
specification of the same contract: the test-suite pins the schema here, and
in-process tooling (an ops endpoint that audits the newest bundle, a restore
harness) can import it instead of re-deriving the rules.

Nothing here shells out, and nothing here reads configuration — a manifest
carries identifiers only. The bundle leaves the host by design (rsync, object
storage, a laptop during an incident), so a credential written into it would
be a credential published; ``verify_manifest`` fails a bundle that contains
one.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = 1
DB_DUMP_NAME = "db.dump"
ARTIFACTS_DIRNAME = "artifacts"
MANIFEST_NAME = "manifest.json"

# 1 MiB: artifacts are single-digit MB and the dump can be hundreds, so hash
# in chunks rather than reading a bundle into memory during a nightly cron.
_CHUNK_BYTES = 1 << 20

REQUIRED_TOP_LEVEL = (
    "schema_version",
    "created_at",
    "app_git_commit",
    "api_build_commit",
    "prediction_build_commit",
    "db_migration_version",
    "db",
    "artifacts",
    "restore_instructions",
)

REQUIRED_ARTIFACT_FIELDS = (
    "symbol",
    "horizon",
    "model_name",
    "version",
    "source_path",
    "stored_name",
    "size_bytes",
    "sha256",
)

# Kept byte-identical to RESTORE_TEXT in scripts/backup.sh: a bundle found by
# an operator who has never read the docs must still say how to restore it.
RESTORE_INSTRUCTIONS = (
    "Verify: sh scripts/backup.sh verify <bundle>. "
    "Preview: sh scripts/restore.sh --dry-run <bundle>. "
    "Apply: sh scripts/restore.sh --confirm <bundle> "
    "(pg_restore --clean --if-exists --no-owner of db.dump, then every "
    "artifacts/<stored_name> back to its source_path). "
    "See docs/backup-restore.md."
)

# Key names that must never appear in a manifest, at any depth.
_SECRET_KEY_RE = re.compile(
    r"password|passwd|secret|token|api[_-]?key|credential|database_url|"
    r"\bdsn\b|private[_-]?key",
    re.IGNORECASE,
)
# A value carrying inline credentials, e.g. postgresql://user:pw@host/db.
_SECRET_VALUE_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")


def utc_now_iso() -> str:
    """Second-resolution UTC stamp, matching ``date -u +%Y-%m-%dT%H:%M:%SZ``."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def sha256_file(path: str) -> str:
    """Hex sha256 of a file, read in bounded chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_stored_name(sha256: str) -> str:
    """Content-addressed name inside the bundle.

    Two active rows can point at the same file (a model reactivated without
    retraining); hashing the content deduplicates them and makes the stored
    name self-verifying.
    """
    return f"{sha256}.joblib"


def _safe_stored_name(name: str) -> bool:
    """Reject anything that could escape the bundle when joined to a path."""
    return bool(name) and name == os.path.basename(name) and not name.startswith(".")


def _measure(path: str, label: str) -> tuple[int, str]:
    if not os.path.isfile(path):
        raise ValueError(f"{label}: file missing at {path}")
    size = os.path.getsize(path)
    if size == 0:
        # A zero-byte file is the signature of a truncated copy or a silently
        # failed `docker exec`; promoting it would hide the loss until restore.
        raise ValueError(f"{label}: file is empty at {path}")
    return size, sha256_file(path)


def build_manifest(
    bundle_dir: str,
    *,
    artifacts: Iterable[Mapping[str, Any]],
    created_at: str | None = None,
    app_git_commit: str = "unknown",
    api_build_commit: str = "unknown",
    prediction_build_commit: str = "unknown",
    db_migration_version: str = "unknown",
    db_dump_name: str = DB_DUMP_NAME,
) -> dict:
    """Build the manifest for an already-staged bundle directory.

    ``bundle_dir`` must already contain ``db_dump_name`` and
    ``artifacts/<stored_name>`` for every entry; sizes and checksums are read
    from those files so the manifest can never describe bytes that were not
    written. Raises ``ValueError`` for a missing, empty or unsafely named
    file — the caller must abort the backup rather than promote it.
    """
    db_size, db_sha = _measure(os.path.join(bundle_dir, db_dump_name), "db dump")

    entries: list[dict] = []
    for spec in artifacts:
        missing = [f for f in ("symbol", "horizon", "model_name", "version",
                               "source_path", "stored_name") if not spec.get(f)]
        if missing:
            raise ValueError(f"artifact spec missing {', '.join(missing)}: {dict(spec)!r}")
        stored = str(spec["stored_name"])
        if not _safe_stored_name(stored):
            raise ValueError(f"unsafe stored_name: {stored!r}")
        size, sha = _measure(
            os.path.join(bundle_dir, ARTIFACTS_DIRNAME, stored),
            f"artifact {spec['symbol']}/{spec['horizon']}",
        )
        entries.append({
            "symbol": str(spec["symbol"]),
            "horizon": str(spec["horizon"]),
            "model_name": str(spec["model_name"]),
            "version": str(spec["version"]),
            "source_path": str(spec["source_path"]),
            "stored_name": stored,
            "size_bytes": size,
            "sha256": sha,
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": created_at or utc_now_iso(),
        "app_git_commit": app_git_commit,
        "api_build_commit": api_build_commit,
        "prediction_build_commit": prediction_build_commit,
        "db_migration_version": db_migration_version,
        "db": {"file": db_dump_name, "size_bytes": db_size, "sha256": db_sha},
        "artifacts": entries,
        "restore_instructions": RESTORE_INSTRUCTIONS,
    }


def secret_like_findings(node: Any, path: str = "manifest") -> list[str]:
    """Report keys that name a secret and values that embed credentials."""
    problems: list[str] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            here = f"{path}.{key}"
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                problems.append(f"{here}: secret-looking key")
            problems.extend(secret_like_findings(value, here))
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            problems.extend(secret_like_findings(value, f"{path}[{index}]"))
    elif isinstance(node, str) and _SECRET_VALUE_RE.search(node):
        problems.append(f"{path}: value embeds credentials")
    return problems


def _check_file(directory: str, name: str, meta: Mapping[str, Any], label: str) -> list[str]:
    problems: list[str] = []
    path = os.path.join(directory, name)
    if not os.path.isfile(path):
        return [f"{label}: file missing at {path}"]
    size = os.path.getsize(path)
    if size == 0:
        problems.append(f"{label}: file is empty at {path}")
    expected_size = meta.get("size_bytes")
    if isinstance(expected_size, int) and expected_size != size:
        problems.append(f"{label}: size {size} != manifest {expected_size}")
    expected_sha = meta.get("sha256")
    if not isinstance(expected_sha, str) or not expected_sha:
        problems.append(f"{label}: manifest has no sha256")
    else:
        actual = sha256_file(path)
        if actual != expected_sha:
            problems.append(f"{label}: sha256 {actual} != manifest {expected_sha}")
    return problems


def verify_manifest(manifest: Mapping[str, Any], base_dir: str) -> list[str]:
    """Re-check a bundle on disk against its manifest.

    Returns a list of human-readable problems; empty means the bundle is
    intact and safe to restore from. Never raises for a corrupt bundle — the
    whole point is to enumerate what is wrong.
    """
    problems: list[str] = []

    version = manifest.get("schema_version")
    if version != SCHEMA_VERSION:
        problems.append(
            f"manifest schema_version {version!r} != supported {SCHEMA_VERSION}"
        )
    problems.extend(
        f"missing manifest field: {field}"
        for field in REQUIRED_TOP_LEVEL
        if field not in manifest
    )
    problems.extend(secret_like_findings(manifest))

    db = manifest.get("db")
    if not isinstance(db, Mapping):
        problems.append("db section missing or malformed")
    else:
        db_name = db.get("file") or DB_DUMP_NAME
        if not _safe_stored_name(str(db_name)):
            problems.append(f"db: unsafe file name {db_name!r}")
        else:
            problems.extend(_check_file(base_dir, str(db_name), db, "db dump"))

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Sequence) or isinstance(artifacts, (str, bytes)):
        problems.append("artifacts section missing or malformed")
        return problems

    for index, entry in enumerate(artifacts):
        label = f"artifact[{index}]"
        if not isinstance(entry, Mapping):
            problems.append(f"{label}: not an object")
            continue
        label = (
            f"artifact[{index}] {entry.get('symbol')}/{entry.get('horizon')}"
            f"/{entry.get('model_name')}"
        )
        problems.extend(
            f"{label}: missing {field}"
            for field in REQUIRED_ARTIFACT_FIELDS
            if entry.get(field) in (None, "")
        )
        stored = str(entry.get("stored_name") or "")
        if not _safe_stored_name(stored):
            problems.append(f"{label}: unsafe stored_name {stored!r}")
            continue
        problems.extend(
            _check_file(os.path.join(base_dir, ARTIFACTS_DIRNAME), stored, entry, label)
        )

    return problems
