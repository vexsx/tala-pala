"""Backup manifest: the bundle contract that makes a restore provable.

These tests pin the properties an operator relies on during an incident — the
schema is versioned, every byte is checksummed, a truncated or vanished
artifact is reported rather than silently restored, and the manifest carries
no credential to whatever off-host location the bundle is copied to.
"""
from __future__ import annotations

import json
import re

import pytest

from app.core.backup_manifest import (ARTIFACTS_DIRNAME, DB_DUMP_NAME,
                                      REQUIRED_TOP_LEVEL, SCHEMA_VERSION,
                                      artifact_stored_name, build_manifest,
                                      secret_like_findings, sha256_file,
                                      verify_manifest)

ARTIFACT_BYTES = b"joblib-artifact-payload"

# Deliberately independent of the module's own pattern: this asserts what a
# reviewer would grep for in a bundle that left the host, not what the code
# happens to consider secret.
SECRET_HINT = re.compile(r"password|secret|token|key|credential|url|dsn|auth", re.IGNORECASE)


def _bundle(tmp_path, artifact_payload: bytes = ARTIFACT_BYTES, dump=b"PGDMP-fake-dump"):
    """Stage a bundle directory the way scripts/backup.sh does."""
    bundle = tmp_path / "goldpred-20260727-031500"
    (bundle / ARTIFACTS_DIRNAME).mkdir(parents=True)
    (bundle / DB_DUMP_NAME).write_bytes(dump)
    stored = artifact_stored_name(
        sha256_file(str(_write(bundle / ARTIFACTS_DIRNAME / "staged.bin", artifact_payload)))
    )
    (bundle / ARTIFACTS_DIRNAME / "staged.bin").rename(bundle / ARTIFACTS_DIRNAME / stored)
    spec = {
        "symbol": "IR_GOLD_18K",
        "horizon": "1d",
        "model_name": "ensemble",
        "version": "2026-07-27T02:30:00",
        "source_path": "/app/models/IR_GOLD_18K/1d/ensemble-20260727T023000.joblib",
        "stored_name": stored,
    }
    return bundle, spec


def _write(path, payload: bytes):
    path.write_bytes(payload)
    return path


def test_manifest_shape_and_schema_version(tmp_path):
    bundle, spec = _bundle(tmp_path)
    manifest = build_manifest(
        str(bundle),
        artifacts=[spec],
        created_at="2026-07-27T03:15:00Z",
        app_git_commit="3e97412",
        api_build_commit="image:abc123def456",
        prediction_build_commit="3e97412",
        db_migration_version="16",
    )

    assert manifest["schema_version"] == SCHEMA_VERSION
    for field in REQUIRED_TOP_LEVEL:
        assert field in manifest
    assert manifest["created_at"].endswith("Z")
    assert manifest["db"]["size_bytes"] == len(b"PGDMP-fake-dump")
    assert manifest["db"]["sha256"] == sha256_file(str(bundle / DB_DUMP_NAME))
    assert "restore.sh" in manifest["restore_instructions"]

    (entry,) = manifest["artifacts"]
    assert entry["symbol"] == "IR_GOLD_18K"
    assert entry["horizon"] == "1d"
    assert entry["model_name"] == "ensemble"
    assert entry["source_path"].endswith(".joblib")
    # Content-addressed: the stored name IS the checksum, so a bundle cannot
    # disagree with itself about which bytes belong to which model.
    assert entry["stored_name"] == f"{entry['sha256']}.joblib"
    assert entry["size_bytes"] == len(ARTIFACT_BYTES)

    # The whole manifest must survive a JSON round-trip: the shell writer and
    # this module have to agree on a format an operator can read.
    assert json.loads(json.dumps(manifest)) == manifest
    assert verify_manifest(manifest, str(bundle)) == []


def test_checksum_mismatch_detected(tmp_path):
    bundle, spec = _bundle(tmp_path)
    manifest = build_manifest(str(bundle), artifacts=[spec])

    stored = bundle / ARTIFACTS_DIRNAME / spec["stored_name"]
    stored.write_bytes(b"tampered-artifact-bytes")
    problems = verify_manifest(manifest, str(bundle))
    assert any("sha256" in p for p in problems)

    # And the same for the dump — bit-rot on the big file is the likelier one.
    (bundle / DB_DUMP_NAME).write_bytes(b"PGDMP-fake-dumq")
    problems = verify_manifest(manifest, str(bundle))
    assert any(p.startswith("db dump: sha256") for p in problems)


def test_missing_artifact_file_detected(tmp_path):
    bundle, spec = _bundle(tmp_path)
    manifest = build_manifest(str(bundle), artifacts=[spec])

    (bundle / ARTIFACTS_DIRNAME / spec["stored_name"]).unlink()
    problems = verify_manifest(manifest, str(bundle))
    assert any("file missing" in p for p in problems)

    # Building over a bundle whose artifact was never staged must fail loudly
    # instead of writing a manifest that describes nothing.
    with pytest.raises(ValueError, match="file missing"):
        build_manifest(str(bundle), artifacts=[spec])


def test_zero_byte_artifact_rejected(tmp_path):
    bundle, spec = _bundle(tmp_path)
    (bundle / ARTIFACTS_DIRNAME / spec["stored_name"]).write_bytes(b"")

    with pytest.raises(ValueError, match="empty"):
        build_manifest(str(bundle), artifacts=[spec])


def test_zero_byte_dump_rejected(tmp_path):
    bundle, spec = _bundle(tmp_path, dump=b"")
    with pytest.raises(ValueError, match="db dump: file is empty"):
        build_manifest(str(bundle), artifacts=[spec])


def test_stored_name_cannot_escape_the_bundle(tmp_path):
    bundle, spec = _bundle(tmp_path)
    manifest = build_manifest(str(bundle), artifacts=[spec])
    manifest["artifacts"][0]["stored_name"] = "../../etc/passwd"

    problems = verify_manifest(manifest, str(bundle))
    assert any("unsafe stored_name" in p for p in problems)

    with pytest.raises(ValueError, match="unsafe stored_name"):
        build_manifest(str(bundle), artifacts=[{**spec, "stored_name": "../evil.joblib"}])


def test_manifest_contains_no_secret_looking_keys(tmp_path):
    bundle, spec = _bundle(tmp_path)
    manifest = build_manifest(str(bundle), artifacts=[spec], db_migration_version="16")

    def keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from keys(value)
        elif isinstance(node, list):
            for value in node:
                yield from keys(value)

    assert not [k for k in keys(manifest) if SECRET_HINT.search(k)]
    assert secret_like_findings(manifest) == []
    assert "postgresql://" not in json.dumps(manifest)


def test_secret_smuggled_into_a_manifest_fails_verification(tmp_path):
    bundle, spec = _bundle(tmp_path)
    manifest = build_manifest(str(bundle), artifacts=[spec])
    manifest["database_url"] = "postgresql+psycopg://goldpred:hunter2@postgres:5432/goldpred"

    problems = verify_manifest(manifest, str(bundle))
    assert any("secret-looking key" in p for p in problems)
    assert any("embeds credentials" in p for p in problems)


def test_unsupported_schema_version_is_reported(tmp_path):
    bundle, spec = _bundle(tmp_path)
    manifest = build_manifest(str(bundle), artifacts=[spec])
    manifest["schema_version"] = SCHEMA_VERSION + 1

    problems = verify_manifest(manifest, str(bundle))
    assert any("schema_version" in p for p in problems)


def test_incomplete_artifact_spec_is_rejected(tmp_path):
    bundle, spec = _bundle(tmp_path)
    # An ACTIVE model_versions row with no artifact_path cannot be restored;
    # the backup must refuse rather than record a half-entry.
    with pytest.raises(ValueError, match="source_path"):
        build_manifest(str(bundle), artifacts=[{**spec, "source_path": ""}])
