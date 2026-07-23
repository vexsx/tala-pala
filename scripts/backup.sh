#!/bin/sh
# Nightly Postgres backup for the tala-pala stack.
#
# Usage (from the repo root, typically via cron):
#   scripts/backup.sh [BACKUP_DIR]
#
# Behavior:
#   * pg_dump (custom format, compressed) via the running postgres container;
#   * writes BACKUP_DIR/goldpred-YYYYMMDD-HHMMSS.dump (default ./backups);
#   * prunes local dumps older than $BACKUP_KEEP_DAYS (default 14);
#   * if $BACKUP_RSYNC_TARGET is set (e.g. user@host:/srv/backups/tala-pala),
#     mirrors the directory off-host with rsync — a backup that lives only on
#     the database host does not survive the host.
#
# Suggested cron (as root, on the deployment host):
#   /etc/cron.d/tala-pala-backup:
#   30 3 * * * root cd /opt/tala-pala && sh scripts/backup.sh >> /var/log/tala-pala-backup.log 2>&1
set -eu

cd "$(dirname "$0")/.."
BACKUP_DIR="${1:-./backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"

mkdir -p "$BACKUP_DIR"
OUT="$BACKUP_DIR/goldpred-$STAMP.dump"

# -T: no TTY (cron); custom format dumps restore selectively via pg_restore.
docker compose exec -T postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --compress=6' \
  > "$OUT"

# A zero-byte dump means the exec failed silently — fail loudly instead.
[ -s "$OUT" ] || { echo "backup FAILED: empty dump $OUT" >&2; rm -f "$OUT"; exit 1; }

find "$BACKUP_DIR" -name 'goldpred-*.dump' -mtime "+$KEEP_DAYS" -delete

if [ -n "${BACKUP_RSYNC_TARGET:-}" ]; then
  rsync -az --delete "$BACKUP_DIR"/ "$BACKUP_RSYNC_TARGET"/
fi

echo "backup OK: $OUT ($(du -h "$OUT" | cut -f1))"
