# Backup & Restore

## Why a bundle and not a dump

Predictions are served by loading the joblib artifact recorded in the ACTIVE `model_versions` row. The prediction service refuses to forecast when that file is missing (`app/models/predicting.py` returns `artifact missing at ...`), and the artifacts live in the `models` Docker volume — a place no database dump touches.

Restoring a bare `pg_dump` therefore gives you a database that points at artifacts nobody kept: the stack comes up healthy, the dashboard renders, and every horizon fails to predict until a full retrain (which needs history the dump does contain, but takes a training cycle and produces different models than the ones your recorded predictions were made with).

`scripts/backup.sh` writes both halves together, plus a manifest that ties them to each other and to the commits that produced them.

## Bundle layout

```
backups/goldpred-20260727-031500/
├── db.dump                        pg_dump --format=custom --compress=6
├── artifacts/
│   └── <sha256>.joblib            one copy per ACTIVE model artifact
└── manifest.json                  schema_version 1
```

Artifacts are content-addressed: the stored name *is* the checksum, so a bundle cannot disagree with itself about which bytes belong to which model, and two active rows pointing at the same file are stored once.

### manifest.json (schema_version 1)

```json
{
  "schema_version": 1,
  "created_at": "2026-07-27T03:15:00Z",
  "app_git_commit": "3e97412",
  "api_build_commit": "image:deadbeef1234",
  "prediction_build_commit": "3e97412",
  "db_migration_version": "16",
  "db": { "file": "db.dump", "size_bytes": 40213931, "sha256": "…" },
  "artifacts": [
    {
      "symbol": "IR_GOLD_18K",
      "horizon": "1d",
      "model_name": "ensemble",
      "version": "2026-07-27T02:30:00",
      "source_path": "/app/models/IR_GOLD_18K/1d/ensemble-20260727T023000.joblib",
      "stored_name": "<sha256>.joblib",
      "size_bytes": 1841320,
      "sha256": "…"
    }
  ],
  "restore_instructions": "Verify: … Preview: … Apply: …"
}
```

| Field | Meaning |
|---|---|
| `app_git_commit` | `git rev-parse --short HEAD` of the checkout that ran the backup |
| `api_build_commit` | `$BUILD_COMMIT` from the api container; the Go image carries no stamp, so this normally falls back to `image:<docker image id>` — still an exact identifier of the binary whose migrations produced this schema |
| `prediction_build_commit` | `$BUILD_COMMIT` from the prediction-service image (set from `GIT_COMMIT` at build time) |
| `db_migration_version` | `schema_migrations.version`, suffixed `-dirty` when golang-migrate left the dirty flag set |
| `source_path` | where the artifact must land again on restore |

**No secrets.** The manifest carries identifiers only — never `DATABASE_URL`, `POSTGRES_PASSWORD` or any token. A bundle is copied off-host by design, so anything written into it is effectively published. Both `scripts/backup.sh verify` and `app/core/backup_manifest.verify_manifest` fail a bundle whose manifest contains a secret-looking key.

## Taking a backup

```bash
make backup                        # = sh scripts/backup.sh  -> ./backups
sh scripts/backup.sh /srv/backups  # explicit destination
```

| Variable | Default | Effect |
|---|---|---|
| `BACKUP_KEEP` | `14` | bundles kept locally; the newest is never pruned, whatever this says |
| `BACKUP_RSYNC_TARGET` | unset | offsite archive, e.g. `user@host:/srv/backups/tala-pala` |

Cron on the deployment host (`/etc/cron.d/tala-pala-backup`):

```
30 3 * * * root cd /opt/tala-pala && sh scripts/backup.sh >> /var/log/tala-pala-backup.log 2>&1
```

### What makes the run fail (nothing is promoted)

The bundle is staged in a hidden directory and moved into place with a single `mv` — an atomic same-filesystem rename — only after every check passes. A reader of `backups/` therefore sees a complete bundle or no bundle at all. The run aborts on:

- a `pg_dump` that fails or produces zero bytes;
- an ACTIVE `model_versions` row with no `artifact_path` (it could never be restored);
- an artifact file that is missing, unreadable, or empty;
- an artifact whose name looks partial or temporary (`*.tmp`, `*.part`, dotfiles) — an active model pointing at a half-written file is a real problem, not something to quietly copy;
- a short copy (bytes read out ≠ bytes measured at the source);
- a manifest that is not valid JSON, or a staged bundle that fails its own verification.

The staged directory is removed on any failure, including SIGINT/SIGTERM.

### Retention and the offsite copy

Local pruning keeps the `BACKUP_KEEP` newest bundles by timestamp and never deletes the bundle just written (`BACKUP_KEEP=0` is clamped to 1). Leftover staging directories older than a day — from a run that was killed mid-backup — are swept as well.

The offsite rsync deliberately runs **without `--delete`**: the remote is an *archive*, not a mirror. With `--delete`, local pruning — or a wiped `backups/`, a failed disk, or an attacker with shell on the host — replicates the deletion offsite and destroys the last remaining copy. Growth offsite is bounded by that host's own retention policy.

Bundles from before this tooling (flat `goldpred-*.dump` files) are left untouched; they are still valid `pg_dump` archives and can be deleted by hand once they age out.

## Verifying

```bash
sh scripts/backup.sh verify backups/goldpred-20260727-031500
```

Re-hashes every file the manifest names and exits non-zero on any missing file, size mismatch or checksum mismatch. It needs nothing but a shell and `sha256sum`/`shasum`, so it also works on a rescue host and on the offsite copy where the stack is not running. The create path runs it against the staged bundle before promoting, so this code is exercised every night rather than for the first time during an incident.

`app/core/backup_manifest.py` is the same contract in Python (`build_manifest`, `verify_manifest`, `sha256_file`), pinned by `tests/test_backup_manifest.py` and importable by future tooling.

## Restoring

**Always dry-run first.** It writes nothing and works without the stack running:

```bash
sh scripts/restore.sh --dry-run backups/goldpred-20260727-031500
```

It prints the bundle's provenance, validates every checksum, then lists exactly what a real restore would do: the database it would replace, and for each artifact the source in the bundle, the target path, and whether that target would be created or overwritten.

The real restore is destructive and requires an explicit flag:

```bash
sh scripts/restore.sh --confirm backups/goldpred-20260727-031500
```

In order:

1. verify every checksum in the bundle — abort on any mismatch;
2. `docker compose stop api`, so the scheduler cannot train or predict mid-restore;
3. dump the **current** database to `./backups/pre-restore-<stamp>.dump` — restoring the wrong bundle is otherwise unrecoverable;
4. write each artifact into the prediction service under a `.part` name, re-hash it *inside the container*, and `mv` it into place only when it matches. Artifacts go first, so no restored database row ever references a file that is not on disk yet. `.part` is one of the suffixes the backup script refuses to copy, so an interrupted restore cannot leave a file a later backup would mistake for a model;
5. `pg_restore --clean --if-exists --no-owner` of `db.dump`;
6. `docker compose restart prediction-service` and `docker compose start api`.

Artifacts are written through the prediction-service container (they land in the `models` volume with the container user's ownership), so that service must be running for a restore that carries artifacts.

Afterwards:

```bash
make ps                    # api re-runs forward-only migrations at startup
make predict               # proves the restored artifacts actually load
```

If the bundle predates the current checkout, the restore prints a note. That is normal: migrations are forward-only, so newer code reads an older dump and migrates it on the next api start.

## What is NOT in a bundle

- **`.env`** — secrets are deliberately excluded. Back it up separately, encrypted, or regenerate it: `POSTGRES_PASSWORD` must match the restored database's role password.
- **Redis** — only cache and scheduler locks; it rebuilds itself.
- **Built images** — rebuild from the commit recorded in the manifest (`make update`).

## Restore drill

A backup nobody has restored is a rumour. Once a quarter, on a scratch host:

```bash
git checkout <app_git_commit from the manifest>
cp .env.example .env && edit          # set POSTGRES_PASSWORD etc.
docker compose up -d postgres prediction-service
sh scripts/restore.sh --dry-run  /path/to/bundle
sh scripts/restore.sh --confirm  /path/to/bundle
docker compose up -d && make predict
```

Predictions that come back with intervals and no `artifact missing` warning are the actual proof.
