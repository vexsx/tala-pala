#!/bin/sh
# Restore a backup bundle produced by scripts/backup.sh.
#
# WHY this exists: an untested restore is a rumour. The dump and the model
# artifacts have to land together — the database rows in model_versions point
# at artifact files by absolute path, and the prediction service refuses to
# predict when the file named by the ACTIVE row is missing. This script does
# both halves, in an order that never leaves the database pointing at bytes
# that were never written, and it defaults to a DRY RUN so the plan can be
# read before anything is destroyed.
#
# Usage (from the repo root):
#   scripts/restore.sh BUNDLE_DIR              dry run (default): print the plan
#   scripts/restore.sh --dry-run BUNDLE_DIR    same, explicitly
#   scripts/restore.sh --confirm BUNDLE_DIR    actually restore (destructive)
#
# The dry run writes nothing at all and works without the stack running, so it
# is also the way to audit an offsite copy.
#
# --confirm performs, in this order:
#   1. verify every checksum in the bundle (abort on any mismatch);
#   2. stop `api` so the scheduler cannot train/predict mid-restore;
#   3. dump the CURRENT database to ./backups/pre-restore-<stamp>.dump —
#      restoring the wrong bundle is otherwise unrecoverable;
#   4. write each artifact into the prediction service (temp name, checksum,
#      then atomic mv) — done BEFORE the database so no restored row can ever
#      reference a file that is not on disk yet;
#   5. pg_restore --clean --if-exists --no-owner of db.dump;
#   6. restart prediction-service and start api.
set -eu

# Bundle paths are resolved against the caller's directory, not the repo root
# we are about to move into — a bundle is usually somewhere else entirely.
ORIG_PWD=$(pwd)
cd "$(dirname "$0")/.."

TAB=$(printf '\t')
WORK=""

cleanup() {
  [ -n "$WORK" ] && [ -d "$WORK" ] && rm -rf "$WORK"
  return 0
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

fail() {
  echo "restore FAILED: $*" >&2
  exit 1
}

usage() {
  echo "usage: scripts/restore.sh [--dry-run|--confirm] BUNDLE_DIR"
}

MODE="dry-run"
BUNDLE=""
for ARG in "$@"; do
  case "$ARG" in
    --dry-run) MODE="dry-run" ;;
    --confirm) MODE="confirm" ;;
    -h|--help) usage; exit 0 ;;
    -*) usage >&2; fail "unknown option: $ARG" ;;
    *) BUNDLE="$ARG" ;;
  esac
done

[ -n "$BUNDLE" ] || { usage >&2; exit 2; }
BUNDLE=${BUNDLE%/}
case "$BUNDLE" in /*) ;; *) BUNDLE="$ORIG_PWD/$BUNDLE" ;; esac
MANIFEST="$BUNDLE/manifest.json"
[ -f "$MANIFEST" ] || fail "no manifest.json in $BUNDLE"

WORK=$(mktemp -d)

manifest_field() {
  sed -n "s/^  \"$1\": \"\{0,1\}\([^\"]*\)\"\{0,1\},\{0,1\}\$/\1/p" "$MANIFEST" | head -n1
}

service_running() {
  docker compose ps --status running --services 2>/dev/null | grep -qx "$1"
}

# Re-hash a file inside the prediction service so a restored artifact is proven
# byte-identical to the bundle, not merely reported as copied.
container_sha() {
  cs_out=$(docker compose exec -T -e P="$1" prediction-service \
    sh -c 'sha256sum "$P"' 2>/dev/null) \
    || cs_out=$(docker compose exec -T -e P="$1" prediction-service python -c \
      'import hashlib,os;print(hashlib.sha256(open(os.environ["P"],"rb").read()).hexdigest())' \
      2>/dev/null) \
    || return 1
  printf '%s' "${cs_out%% *}" | tr -d ' \r\n'
}

# Artifact rows out of the manifest, in the key order scripts/backup.sh writes.
# The db section is skipped because it has no source_path.
awk '
  function value(line) {
    sub(/^[ \t]*"[^"]*"[ \t]*:[ \t]*/, "", line)
    sub(/,[ \t]*$/, "", line)
    gsub(/^"|"$/, "", line)
    return line
  }
  /"symbol"[ \t]*:/      { sym = value($0);    next }
  /"horizon"[ \t]*:/     { hz = value($0);     next }
  /"model_name"[ \t]*:/  { mdl = value($0);    next }
  /"version"[ \t]*:/     { ver = value($0);    next }
  /"source_path"[ \t]*:/ { src = value($0);    next }
  /"stored_name"[ \t]*:/ { stored = value($0); next }
  /"size_bytes"[ \t]*:/  { size = value($0);   next }
  /"sha256"[ \t]*:/ {
    if (src != "" && stored != "")
      printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n", sym, hz, mdl, ver, src, stored, size, value($0)
    src = ""; stored = ""; next
  }
' "$MANIFEST" > "$WORK/artifacts.tsv"

CREATED=$(manifest_field created_at)
APP_COMMIT=$(manifest_field app_git_commit)
API_COMMIT=$(manifest_field api_build_commit)
PRED_COMMIT=$(manifest_field prediction_build_commit)
MIGRATION=$(manifest_field db_migration_version)
ARTIFACT_COUNT=$(grep -c . "$WORK/artifacts.tsv" || true)

echo "bundle:            $BUNDLE"
echo "created_at:        $CREATED"
echo "app_git_commit:    $APP_COMMIT"
echo "api_build_commit:  $API_COMMIT"
echo "prediction_commit: $PRED_COMMIT"
echo "db_migration:      $MIGRATION"
echo "artifacts:         $ARTIFACT_COUNT"
echo

HEAD_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
if [ "$HEAD_COMMIT" != "unknown" ] && [ "$APP_COMMIT" != "unknown" ] \
   && [ "$HEAD_COMMIT" != "$APP_COMMIT" ]; then
  # Not fatal: migrations are forward-only, so newer code can read an older
  # dump. It IS worth knowing before predictions look strange.
  echo "NOTE: bundle was taken at $APP_COMMIT, this checkout is $HEAD_COMMIT" >&2
fi

echo "== verifying bundle checksums"
sh scripts/backup.sh verify "$BUNDLE" || fail "bundle failed verification — refusing to restore"
echo

DB_NAME="(unknown: postgres not running)"
if service_running postgres; then
  DB_NAME=$(docker compose exec -T postgres sh -c 'echo "$POSTGRES_DB"' 2>/dev/null \
    | tr -d ' \r\n')
  [ -n "$DB_NAME" ] || DB_NAME="(unknown)"
fi

[ -f "$BUNDLE/db.dump" ] || fail "bundle has no db.dump (unsupported layout)"
DUMP_SIZE=$(wc -c < "$BUNDLE/db.dump" | tr -d ' ')

if [ "$MODE" = "dry-run" ]; then
  echo "== DRY RUN — nothing is written"
  echo
  echo "database:"
  echo "  would pg_restore --clean --if-exists --no-owner"
  echo "    from: $BUNDLE/db.dump ($DUMP_SIZE bytes)"
  echo "    into: database '$DB_NAME' in the postgres service"
  echo "  every existing table, index and row in that database would be replaced."
  echo
  echo "artifacts (written into the prediction-service container):"
  if [ "$ARTIFACT_COUNT" -eq 0 ]; then
    echo "  (none — this bundle has no active model artifacts)"
  fi
  while IFS="$TAB" read -r SYM HZ MDL VER SRC STORED SIZE SHA; do
    [ -n "$SRC" ] || continue
    STATE="target state unknown (prediction-service not running)"
    # Host-visible first (bind-mounted models dir), then through the container
    # — the same order scripts/backup.sh reads them in.
    if [ -f "$SRC" ]; then
      STATE="would OVERWRITE the existing file"
    elif service_running prediction-service; then
      if docker compose exec -T -e P="$SRC" prediction-service \
           sh -c 'test -f "$P"' >/dev/null 2>&1; then
        STATE="would OVERWRITE the existing file"
      else
        STATE="would CREATE (target does not exist)"
      fi
    fi
    echo "  $SYM $HZ $MDL ($VER)"
    echo "    from: artifacts/$STORED ($SIZE bytes, sha256 $(printf '%s' "$SHA" | cut -c1-16)...)"
    echo "    to:   $SRC"
    echo "    $STATE"
  done < "$WORK/artifacts.tsv"
  echo
  echo "services: api would be stopped, prediction-service restarted afterwards."
  echo "safety:   the current database would first be dumped to ./backups/pre-restore-<stamp>.dump"
  echo
  echo "DRY RUN complete — nothing was written. Re-run with --confirm to apply."
  exit 0
fi

# ---------------------------------------------------------------- confirm ---
service_running postgres || fail "postgres is not running (docker compose up -d postgres)"
if [ "$ARTIFACT_COUNT" -gt 0 ]; then
  service_running prediction-service \
    || fail "prediction-service is not running; artifacts are written through it"
fi

echo "== stopping api (scheduler) so nothing writes during the restore"
docker compose stop api || fail "could not stop api"

STAMP=$(date -u +%Y%m%d-%H%M%S)
mkdir -p ./backups
PRE="./backups/pre-restore-$STAMP.dump"
echo "== dumping the CURRENT database to $PRE"
docker compose exec -T postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --compress=6' \
  > "$PRE" || fail "pre-restore dump failed (nothing has been changed)"
[ -s "$PRE" ] || fail "pre-restore dump is empty (nothing has been changed)"

echo "== restoring $ARTIFACT_COUNT artifact(s)"
while IFS="$TAB" read -r SYM HZ MDL VER SRC STORED SIZE SHA; do
  [ -n "$SRC" ] || continue
  LOCAL="$BUNDLE/artifacts/$STORED"
  # .part is one of the suffixes scripts/backup.sh refuses to back up, so an
  # interrupted restore can never leave a file that a later backup would
  # mistake for a model.
  docker compose exec -T -e P="$SRC" prediction-service \
    sh -c 'mkdir -p "$(dirname "$P")"' || fail "could not create directory for $SRC"
  docker compose exec -T -e P="$SRC" prediction-service \
    sh -c 'cat > "$P.part"' < "$LOCAL" || fail "could not write $SRC.part"
  ACTUAL=$(container_sha "$SRC.part") || fail "could not checksum restored $SRC.part"
  [ "$ACTUAL" = "$SHA" ] || fail "checksum mismatch after writing $SRC (got $ACTUAL)"
  docker compose exec -T -e P="$SRC" prediction-service \
    sh -c 'mv "$P.part" "$P"' || fail "could not move $SRC into place"
  echo "  restored $SYM $HZ $MDL -> $SRC"
done < "$WORK/artifacts.tsv"

echo "== restoring the database into '$DB_NAME'"
docker compose exec -T postgres sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' \
  < "$BUNDLE/db.dump" || fail "pg_restore failed — the pre-restore dump is at $PRE"

echo "== restarting services"
docker compose restart prediction-service || fail "could not restart prediction-service"
docker compose start api || fail "could not start api"

echo
echo "restore OK from $BUNDLE"
echo "  pre-restore dump kept at $PRE"
echo "  api re-runs forward-only migrations at startup; check: make ps"
echo "  then confirm predictions load their artifacts: make predict"
