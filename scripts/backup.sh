#!/bin/sh
# Model-aware backup BUNDLE for the tala-pala stack.
#
# WHY a bundle and not a bare pg_dump: predictions are served by loading the
# joblib artifact recorded in the ACTIVE model_versions row (see
# app/models/predicting.py — it refuses to predict when that file is gone).
# The artifacts live in the `models` Docker volume, which no database dump
# touches. Restoring a dump alone therefore produces a database that points at
# artifacts nobody kept: the API comes up, the dashboard renders, and every
# horizon fails to predict until a full retrain. This script backs up both,
# together, with a manifest that ties them to each other and to the commits
# that produced them.
#
# Usage (from the repo root, typically via cron):
#   scripts/backup.sh [BACKUP_DIR]        create a bundle (default ./backups)
#   scripts/backup.sh verify BUNDLE_DIR   re-check an existing bundle's checksums
#
# Bundle layout (BACKUP_DIR/goldpred-<UTC YYYYMMDD-HHMMSS>/):
#   db.dump                  pg_dump --format=custom
#   artifacts/<sha256>.joblib  copies of the ACTIVE artifacts, content-addressed
#   manifest.json            schema_version 1; see docs/backup-restore.md
#
# Environment:
#   BACKUP_KEEP=14           bundles kept locally (the newest is never pruned)
#   BACKUP_RSYNC_TARGET      optional offsite archive, e.g. user@host:/srv/bk
#
# Exit status is non-zero for ANY problem and nothing is promoted into place:
# a backup that half-succeeded is worse than one that loudly failed, because
# only the second gets fixed.
#
# Suggested cron (as root, on the deployment host):
#   /etc/cron.d/tala-pala-backup:
#   30 3 * * * root cd /opt/tala-pala && sh scripts/backup.sh >> /var/log/tala-pala-backup.log 2>&1
set -eu

cd "$(dirname "$0")/.."

TAB=$(printf '\t')
STAGE=""
WORK=""

cleanup() {
  [ -n "$STAGE" ] && [ -d "$STAGE" ] && rm -rf "$STAGE"
  [ -n "$WORK" ] && [ -d "$WORK" ] && rm -rf "$WORK"
  return 0
}
trap cleanup EXIT INT TERM

fail() {
  echo "backup FAILED: $*" >&2
  exit 1
}

# sha256sum (coreutils) on Linux hosts, shasum on macOS.
if command -v sha256sum >/dev/null 2>&1; then
  sha256_of() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
  sha256_of() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
  fail "need sha256sum or shasum to checksum the bundle"
fi

size_of() { wc -c < "$1" | tr -d ' '; }

# Only backslash and double quote can realistically appear in the DB text
# columns that reach the manifest (model names, versions, artifact paths).
json_escape() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }

# SQL travels in an environment variable so the quoting survives the two shells
# it passes through (host sh -> container sh) without any escaping games.
psql_query() {
  docker compose exec -T -e SQL="$1" postgres \
    sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "$SQL"'
}

# artifact_path is recorded by the prediction service and names a path inside
# ITS filesystem (/app/models is a named volume with no host path). Prefer a
# host-visible file when the models directory happens to be bind-mounted, and
# otherwise read through the container.
artifact_size() {
  if [ -f "$1" ]; then
    size_of "$1"
    return 0
  fi
  as_out=$(docker compose exec -T -e P="$1" prediction-service \
    sh -c 'test -f "$P" && wc -c < "$P"' 2>/dev/null) || return 1
  printf '%s' "$as_out" | tr -d ' \r\n'
}

artifact_copy() {  # $1 source path, $2 destination on the host
  if [ -f "$1" ]; then
    cat "$1" > "$2"
    return 0
  fi
  docker compose exec -T -e P="$1" prediction-service sh -c 'cat "$P"' > "$2"
}

# ---------------------------------------------------------------- verify ----
# Parses the manifest this script writes (stable key order) and re-hashes every
# file it names. Deliberately dependency-free: verification has to work on a
# rescue host that has nothing but a shell, and on the offsite copy where the
# stack is not running. app/core/backup_manifest.py is the same contract in
# Python, pinned by the test-suite.
verify_bundle() {
  vb_dir="$1"
  vb_manifest="$vb_dir/manifest.json"
  vb_bad=0

  [ -d "$vb_dir" ] || { echo "verify FAILED: no such bundle $vb_dir" >&2; return 1; }
  [ -f "$vb_manifest" ] || { echo "verify FAILED: no manifest.json in $vb_dir" >&2; return 1; }

  grep -Eq '"schema_version"[[:space:]]*:[[:space:]]*1[[:space:]]*,' "$vb_manifest" \
    || { echo "verify: unsupported or missing schema_version" >&2; vb_bad=1; }

  # A manifest is copied off-host by design; a credential in one is published.
  if grep -Eiq '"[^"]*(password|secret|token|api_?key|credential|database_url)[^"]*"[[:space:]]*:' "$vb_manifest"; then
    echo "verify: manifest contains a secret-looking key" >&2
    vb_bad=1
  fi

  awk '
    function value(line) {
      sub(/^[ \t]*"[^"]*"[ \t]*:[ \t]*/, "", line)
      sub(/,[ \t]*$/, "", line)
      gsub(/^"|"$/, "", line)
      return line
    }
    /"file"[ \t]*:/        { name = value($0); prefix = "";           next }
    /"stored_name"[ \t]*:/ { name = value($0); prefix = "artifacts/"; next }
    /"size_bytes"[ \t]*:/  { size = value($0);                        next }
    /"sha256"[ \t]*:/ {
      if (name != "") { printf "%s%s\t%s\t%s\n", prefix, name, size, value($0) }
      name = ""; size = ""; next
    }
  ' "$vb_manifest" > "$WORK/expected.tsv"

  [ -s "$WORK/expected.tsv" ] || { echo "verify: manifest lists no files" >&2; return 1; }

  vb_files=0
  while IFS="$TAB" read -r vb_rel vb_size vb_sha; do
    [ -n "$vb_rel" ] || continue
    vb_files=$((vb_files + 1))
    case "$vb_rel" in
      *..*|/*) echo "verify: unsafe path in manifest: $vb_rel" >&2; vb_bad=1; continue ;;
    esac
    vb_file="$vb_dir/$vb_rel"
    if [ ! -f "$vb_file" ]; then
      echo "verify: missing file $vb_rel" >&2
      vb_bad=1
      continue
    fi
    vb_actual_size=$(size_of "$vb_file")
    if [ "$vb_actual_size" = "0" ]; then
      echo "verify: empty file $vb_rel" >&2
      vb_bad=1
      continue
    fi
    if [ "$vb_actual_size" != "$vb_size" ]; then
      echo "verify: size mismatch $vb_rel ($vb_actual_size != $vb_size)" >&2
      vb_bad=1
    fi
    vb_actual_sha=$(sha256_of "$vb_file")
    if [ "$vb_actual_sha" != "$vb_sha" ]; then
      echo "verify: CHECKSUM MISMATCH $vb_rel" >&2
      vb_bad=1
    fi
  done < "$WORK/expected.tsv"

  [ "$vb_bad" -eq 0 ] || { echo "verify FAILED: $vb_dir" >&2; return 1; }
  echo "verify OK: $vb_dir ($vb_files files checksummed)"
  return 0
}

WORK=$(mktemp -d)

if [ "${1:-}" = "verify" ]; then
  [ -n "${2:-}" ] || fail "usage: scripts/backup.sh verify BUNDLE_DIR"
  verify_bundle "$2"
  exit $?
fi

# ---------------------------------------------------------------- create ----
BACKUP_DIR="${1:-./backups}"
KEEP="${BACKUP_KEEP:-14}"
case "$KEEP" in
  ''|*[!0-9]*) fail "BACKUP_KEEP must be a whole number (got '$KEEP')" ;;
esac
# Keeping zero bundles would mean deleting the one just written.
[ "$KEEP" -ge 1 ] || KEEP=1

STAMP=$(date -u +%Y%m%d-%H%M%S)
BUNDLE="$BACKUP_DIR/goldpred-$STAMP"
# Staged inside BACKUP_DIR so the final promotion is a same-filesystem rename,
# which is atomic: a reader either sees a complete bundle or no bundle at all.
STAGE="$BACKUP_DIR/.staging-$STAMP-$$"

[ -e "$BUNDLE" ] && fail "bundle $BUNDLE already exists"
mkdir -p "$STAGE/artifacts"

docker compose exec -T postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --compress=6' \
  > "$STAGE/db.dump" || fail "pg_dump failed"
# A zero-byte dump means the exec failed silently — the classic backup that is
# only discovered to be empty on the day it is needed.
[ -s "$STAGE/db.dump" ] || fail "pg_dump produced an empty dump"

MIGRATION=$(psql_query \
  "SELECT version::text || CASE WHEN dirty THEN '-dirty' ELSE '' END FROM schema_migrations LIMIT 1" \
  2>/dev/null | tr -d ' \r\n') || MIGRATION=""
[ -n "$MIGRATION" ] || MIGRATION="unknown"
case "$MIGRATION" in
  *-dirty) echo "WARNING: schema_migrations is dirty at $MIGRATION" >&2 ;;
esac

APP_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
PRED_COMMIT=$(docker compose exec -T prediction-service sh -c 'echo "${BUILD_COMMIT:-}"' \
  2>/dev/null | tr -d ' \r\n')
[ -n "$PRED_COMMIT" ] || PRED_COMMIT="unknown"
API_COMMIT=$(docker compose exec -T api sh -c 'echo "${BUILD_COMMIT:-}"' \
  2>/dev/null | tr -d ' \r\n')
if [ -z "$API_COMMIT" ]; then
  # The Go image carries no commit stamp; its image ID still identifies the
  # exact binary whose migrations produced the schema in this dump.
  API_IMAGE=$(docker compose images -q api 2>/dev/null | head -n1 | tr -d ' \r\n')
  API_COMMIT="unknown"
  if [ -n "$API_IMAGE" ]; then API_COMMIT="image:$API_IMAGE"; fi
fi

psql_query \
  "SELECT symbol, horizon, model_name, version, coalesce(artifact_path, '') FROM model_versions WHERE is_active ORDER BY symbol, horizon" \
  > "$WORK/active.psv" || fail "could not query active model_versions"

ARTIFACT_COUNT=0
: > "$WORK/artifacts.json"

while IFS='|' read -r SYMBOL HORIZON MODEL VERSION APATH; do
  # psql -tA emits a trailing blank line.
  [ -n "$SYMBOL" ] || continue
  LABEL="$SYMBOL/$HORIZON/$MODEL"

  [ -n "$APATH" ] || fail "$LABEL is ACTIVE but has no artifact_path — it cannot be restored"

  # A partial/temp name means we would be copying a file that is still being
  # written (or a leftover from a crashed training run); either way its bytes
  # are not the model that is serving predictions.
  BASE=${APATH##*/}
  case "$BASE" in
    *.tmp|*.part|.*) fail "$LABEL points at a partial/temp artifact: $APATH" ;;
  esac

  REMOTE_SIZE=$(artifact_size "$APATH") || fail "$LABEL artifact unreadable: $APATH"
  case "$REMOTE_SIZE" in
    ''|*[!0-9]*) fail "$LABEL artifact size unreadable: $APATH" ;;
  esac
  [ "$REMOTE_SIZE" -gt 0 ] || fail "$LABEL artifact is empty: $APATH"

  artifact_copy "$APATH" "$STAGE/artifacts/.incoming" \
    || fail "$LABEL artifact copy failed: $APATH"
  LOCAL_SIZE=$(size_of "$STAGE/artifacts/.incoming")
  # A stream truncated mid-copy is otherwise indistinguishable from a small
  # model; compare against the size measured at the source.
  [ "$LOCAL_SIZE" = "$REMOTE_SIZE" ] \
    || fail "$LABEL short copy: got $LOCAL_SIZE of $REMOTE_SIZE bytes"

  SHA=$(sha256_of "$STAGE/artifacts/.incoming")
  STORED="$SHA.joblib"
  mv "$STAGE/artifacts/.incoming" "$STAGE/artifacts/$STORED"

  if [ "$ARTIFACT_COUNT" -gt 0 ]; then printf ',\n' >> "$WORK/artifacts.json"; fi
  {
    printf '    {\n'
    printf '      "symbol": "%s",\n' "$(json_escape "$SYMBOL")"
    printf '      "horizon": "%s",\n' "$(json_escape "$HORIZON")"
    printf '      "model_name": "%s",\n' "$(json_escape "$MODEL")"
    printf '      "version": "%s",\n' "$(json_escape "$VERSION")"
    printf '      "source_path": "%s",\n' "$(json_escape "$APATH")"
    printf '      "stored_name": "%s",\n' "$STORED"
    printf '      "size_bytes": %s,\n' "$LOCAL_SIZE"
    printf '      "sha256": "%s"\n' "$SHA"
    printf '    }'
  } >> "$WORK/artifacts.json"
  ARTIFACT_COUNT=$((ARTIFACT_COUNT + 1))
done < "$WORK/active.psv"

if [ "$ARTIFACT_COUNT" -gt 0 ]; then printf '\n' >> "$WORK/artifacts.json"; fi
[ "$ARTIFACT_COUNT" -gt 0 ] || echo "WARNING: no ACTIVE model artifacts to back up" >&2

DUMP_SIZE=$(size_of "$STAGE/db.dump")
DUMP_SHA=$(sha256_of "$STAGE/db.dump")
CREATED=$(date -u +%Y-%m-%dT%H:%M:%SZ)
RESTORE_TEXT="Verify: sh scripts/backup.sh verify <bundle>. Preview: sh scripts/restore.sh --dry-run <bundle>. Apply: sh scripts/restore.sh --confirm <bundle> (pg_restore --clean --if-exists --no-owner of db.dump, then every artifacts/<stored_name> back to its source_path). See docs/backup-restore.md."

# Identifiers only: no DATABASE_URL, no POSTGRES_PASSWORD, no tokens. The
# bundle travels off-host, so anything written here is effectively published.
{
  printf '{\n'
  printf '  "schema_version": 1,\n'
  printf '  "created_at": "%s",\n' "$CREATED"
  printf '  "app_git_commit": "%s",\n' "$(json_escape "$APP_COMMIT")"
  printf '  "api_build_commit": "%s",\n' "$(json_escape "$API_COMMIT")"
  printf '  "prediction_build_commit": "%s",\n' "$(json_escape "$PRED_COMMIT")"
  printf '  "db_migration_version": "%s",\n' "$(json_escape "$MIGRATION")"
  printf '  "db": {\n'
  printf '    "file": "db.dump",\n'
  printf '    "size_bytes": %s,\n' "$DUMP_SIZE"
  printf '    "sha256": "%s"\n' "$DUMP_SHA"
  printf '  },\n'
  printf '  "artifacts": [\n'
  cat "$WORK/artifacts.json"
  printf '  ],\n'
  printf '  "restore_instructions": "%s"\n' "$(json_escape "$RESTORE_TEXT")"
  printf '}\n'
} > "$STAGE/manifest.json"

if command -v python3 >/dev/null 2>&1; then
  python3 -m json.tool "$STAGE/manifest.json" >/dev/null \
    || fail "manifest.json is not valid JSON"
fi

# Re-verify the staged bundle before promoting it. This costs a second read of
# the dump, and buys two things: nothing is promoted that cannot be verified,
# and the verify path itself is exercised every night — so it cannot rot
# unnoticed and then fail on the one day it matters.
verify_bundle "$STAGE" >/dev/null || fail "staged bundle failed verification"

mv "$STAGE" "$BUNDLE"
STAGE=""

# Retention. Bundle names are UTC timestamps, so a reverse lexical sort is a
# reverse chronological sort. The bundle just written is passed in as
# protected: whatever KEEP says, the newest known-good bundle is never the one
# that gets deleted.
find "$BACKUP_DIR" -maxdepth 1 -type d -name 'goldpred-*' 2>/dev/null \
  | sort -r > "$WORK/bundles"
KEPT=0
while read -r OLD; do
  [ -n "$OLD" ] || continue
  KEPT=$((KEPT + 1))
  [ "$KEPT" -le "$KEEP" ] && continue
  [ "$OLD" = "$BUNDLE" ] && continue
  rm -rf "$OLD"
  echo "pruned $OLD"
done < "$WORK/bundles"

# Staging directories from a run that was killed mid-backup (>1 day old, so
# never a concurrent run's).
find "$BACKUP_DIR" -maxdepth 1 -type d -name '.staging-*' -mtime +0 \
  -exec rm -rf {} + 2>/dev/null || true

echo "backup OK: $BUNDLE (db $(du -h "$BUNDLE/db.dump" | cut -f1), $ARTIFACT_COUNT artifact(s), migration $MIGRATION)"

if [ -n "${BACKUP_RSYNC_TARGET:-}" ]; then
  # NO --delete, deliberately. The offsite copy is an ARCHIVE, not a mirror:
  # with --delete, local pruning — or a wiped BACKUP_DIR, a failed disk, an
  # attacker with shell on this host — replicates the deletion offsite and
  # destroys the last remaining copy. Growth is bounded offsite by the
  # retention policy of that host, not by this one.
  rsync -az "$BACKUP_DIR"/ "$BACKUP_RSYNC_TARGET"/ \
    || { echo "offsite rsync FAILED (local bundle $BUNDLE is valid)" >&2; exit 1; }
  echo "offsite OK: $BACKUP_RSYNC_TARGET"
fi
