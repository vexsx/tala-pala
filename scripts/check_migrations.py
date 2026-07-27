#!/usr/bin/env python3
"""Static hygiene checks over the golang-migrate migration set.

Why this exists: migrations are applied once, by number, at deploy time. A
defect in the FILE SET rather than in the SQL - a duplicated number, an
.up.sql with no .down.sql, a hole in the numbering, an accidentally truncated
file - raises nothing when it is written. It surfaces months later, either on
the next fresh database (golang-migrate refuses a duplicated version) or at the
worst possible moment, when a rollback is attempted and the down file is not
there. The checks below need no database, so they are cheap enough to run in
the ordinary pytest suite as well as in CI.

The same parse also answers "what must a fully migrated database contain?".
CI asserts the live schema against `--list tables` / `--list indexes` instead
of a hand-maintained list, so the assertion cannot drift away from the
migrations it is supposed to guard. `--list up-files` emits the apply order
(numeric, not lexicographic) and takes --min-version/--max-version so an
upgrade path can be replayed in stages.

Usage:
    python3 scripts/check_migrations.py                       # exit 1 on any problem
    python3 scripts/check_migrations.py --list tables
    python3 scripts/check_migrations.py --list indexes
    python3 scripts/check_migrations.py --list up-files --max-version 16
    python3 scripts/check_migrations.py --list versions
"""
from __future__ import annotations

import argparse
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MIGRATIONS_DIR = os.path.join(REPO_ROOT, "database", "migrations")

# golang-migrate file convention: NNNN_snake_name.up.sql / NNNN_snake_name.down.sql
FILENAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.(up|down)\.sql$")

_IDENT = r'[\w."]+'
CREATE_TABLE_RE = re.compile(
    r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(" + _IDENT + r")", re.IGNORECASE)
DROP_TABLE_RE = re.compile(
    r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w.\"\s,]+)", re.IGNORECASE)
CREATE_INDEX_RE = re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(" + _IDENT + r")\s+ON\b", re.IGNORECASE)
DROP_INDEX_RE = re.compile(
    r"\bDROP\s+INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+EXISTS\s+)?([\w.\"\s,]+)",
    re.IGNORECASE)


class Migration:
    """One numbered migration and the files claiming that number."""

    def __init__(self, version: int) -> None:
        self.version = version
        self.up: list[str] = []      # file names, plural so duplicates are reportable
        self.down: list[str] = []
        self.names: set[str] = set()  # descriptive part, e.g. 'hot_path_indexes'

    @property
    def label(self) -> str:
        return f"{self.version:04d}"


DOLLAR_TAG_RE = re.compile(r"\$[A-Za-z_]\w*\$|\$\$")


def _executable_sql(sql: str) -> str:
    """Statement text only: comments removed, string-literal bodies blanked.

    Migration files carry long explanatory headers that name tables in prose
    and quote superseded DDL, and they seed rows whose values are free text.
    Without this, a commented-out statement or a table name inside a quoted
    value would be read as an object the database actually creates. Quotes and
    delimiters are kept so an emptiness check can still tell a file that runs
    nothing from a file that runs a statement with an empty literal in it.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    line_comment = block_comment = in_string = False
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if line_comment:
            if ch == "\n":
                line_comment = False
                out.append(ch)
        elif block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 1
        elif in_string:
            if ch == "'":
                if nxt == "'":          # escaped quote inside the literal
                    i += 1
                else:
                    in_string = False
                    out.append(ch)
        elif ch == "-" and nxt == "-":
            line_comment = True
            i += 1
        elif ch == "/" and nxt == "*":
            block_comment = True
            i += 1
        elif ch == "'":
            in_string = True
            out.append(ch)
        elif ch == "$" and DOLLAR_TAG_RE.match(sql, i):
            # Dollar-quoted body (function/procedure source): opaque, and it
            # may legitimately contain DDL that is not run at migration time.
            tag = DOLLAR_TAG_RE.match(sql, i).group(0)
            end = sql.find(tag, i + len(tag))
            i = (end + len(tag) - 1) if end != -1 else n
            out.append(tag + tag)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _unqualify(name: str) -> str:
    """'public."idx_foo"' -> 'idx_foo' so derived names compare to pg catalogs."""
    return name.strip().strip(",").replace('"', "").split(".")[-1]


def _name_list(blob: str) -> list[str]:
    """Object names from a DROP that may target several comma-separated objects."""
    names = []
    for part in blob.split(","):
        token = part.strip().split()[0] if part.strip() else ""
        if token:
            names.append(_unqualify(token))
    return names


def scan(directory: str) -> tuple[dict[int, Migration], list[str]]:
    """Group migration files by version. Returns (migrations, filename problems)."""
    problems: list[str] = []
    migrations: dict[int, Migration] = {}
    if not os.path.isdir(directory):
        return migrations, [f"migrations directory not found: {directory}"]

    for filename in sorted(os.listdir(directory)):
        path = os.path.join(directory, filename)
        if not os.path.isfile(path) or not filename.endswith(".sql"):
            continue
        match = FILENAME_RE.match(filename)
        if not match:
            problems.append(
                f"unrecognized migration filename {filename!r}: expected "
                f"NNNN_snake_name.up.sql or NNNN_snake_name.down.sql")
            continue
        version = int(match.group(1))
        migration = migrations.setdefault(version, Migration(version))
        migration.names.add(match.group(2))
        getattr(migration, match.group(3)).append(filename)
    return migrations, problems


def check_migrations(directory: str = DEFAULT_MIGRATIONS_DIR) -> list[str]:
    """Every problem found in the migration file set, one diagnostic each."""
    migrations, problems = scan(directory)

    for version in sorted(migrations):
        migration = migrations[version]
        for direction in ("up", "down"):
            files = getattr(migration, direction)
            if len(files) > 1:
                problems.append(
                    f"duplicate migration number {migration.label}: "
                    f"{', '.join(sorted(files))} all claim the same version "
                    f"({direction} direction)")
        if not migration.up:
            problems.append(
                f"migration {migration.label} has a .down.sql "
                f"({', '.join(sorted(migration.down))}) but no .up.sql")
        if not migration.down:
            problems.append(
                f"migration {migration.label} has no .down.sql to match "
                f"{', '.join(sorted(migration.up))} — the deploy cannot be rolled back")
        if len(migration.names) > 1:
            problems.append(
                f"migration {migration.label} mixes descriptive names "
                f"({', '.join(sorted(migration.names))}); golang-migrate pairs "
                f"up/down by number AND name")

    if migrations:
        if 0 in migrations:
            problems.append(
                "migration numbered 0000: numbering must start at 0001 "
                "(golang-migrate treats version 0 as 'no migrations applied')")
        highest = max(migrations)
        for missing in range(1, highest + 1):
            if missing not in migrations:
                problems.append(
                    f"gap in migration numbering: {missing:04d} is missing "
                    f"(versions present run up to {highest:04d})")

    for version in sorted(migrations):
        for filename in sorted(migrations[version].up + migrations[version].down):
            path = os.path.join(directory, filename)
            with open(path, "r", encoding="utf-8") as handle:
                content = handle.read()
            if not content.strip():
                problems.append(
                    f"{filename} is empty — it would apply cleanly and change nothing, "
                    f"silently marking the version as done")
            elif not _strip_sql_comments(content).strip():
                problems.append(
                    f"{filename} contains only comments — no SQL statement to run")

    return problems


def up_files(directory: str = DEFAULT_MIGRATIONS_DIR, min_version: int = 0,
             max_version: int | None = None) -> list[str]:
    """Paths of the .up.sql files in numeric apply order, optionally bounded."""
    migrations, _ = scan(directory)
    paths = []
    for version in sorted(migrations):
        if version < min_version or (max_version is not None and version > max_version):
            continue
        for filename in sorted(migrations[version].up):
            paths.append(os.path.join(directory, filename))
    return paths


def derive_schema(directory: str = DEFAULT_MIGRATIONS_DIR) -> tuple[set[str], set[str]]:
    """(tables, indexes) a database has after every .up.sql has been applied.

    Replayed in apply order so a later migration that drops an object is
    reflected, and named indexes only - indexes that Postgres creates behind
    PRIMARY KEY / UNIQUE constraints are not written as CREATE INDEX and are
    therefore not claimed here.
    """
    tables: set[str] = set()
    indexes: set[str] = set()
    for path in up_files(directory):
        with open(path, "r", encoding="utf-8") as handle:
            sql = _strip_sql_comments(handle.read())
        for match in CREATE_TABLE_RE.finditer(sql):
            tables.add(_unqualify(match.group(1)))
        for match in CREATE_INDEX_RE.finditer(sql):
            indexes.add(_unqualify(match.group(1)))
        for match in DROP_TABLE_RE.finditer(sql):
            tables.difference_update(_name_list(match.group(1)))
        for match in DROP_INDEX_RE.finditer(sql):
            indexes.difference_update(_name_list(match.group(1)))
    return tables, indexes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", default=DEFAULT_MIGRATIONS_DIR,
                        help="migrations directory (default: database/migrations)")
    parser.add_argument("--list", dest="listing",
                        choices=("tables", "indexes", "up-files", "versions"),
                        help="print derived facts instead of running the checks")
    parser.add_argument("--min-version", type=int, default=0,
                        help="with --list up-files/versions: skip lower versions")
    parser.add_argument("--max-version", type=int, default=None,
                        help="with --list up-files/versions: skip higher versions")
    args = parser.parse_args(argv)

    if args.listing == "tables":
        tables, _ = derive_schema(args.dir)
        print("\n".join(sorted(tables)))
        return 0
    if args.listing == "indexes":
        _, indexes = derive_schema(args.dir)
        print("\n".join(sorted(indexes)))
        return 0
    if args.listing == "up-files":
        print("\n".join(up_files(args.dir, args.min_version, args.max_version)))
        return 0
    if args.listing == "versions":
        migrations, _ = scan(args.dir)
        print("\n".join(
            f"{v:04d}" for v in sorted(migrations)
            if v >= args.min_version
            and (args.max_version is None or v <= args.max_version)))
        return 0

    problems = check_migrations(args.dir)
    if problems:
        print(f"{len(problems)} problem(s) in {args.dir}:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    migrations, _ = scan(args.dir)
    tables, indexes = derive_schema(args.dir)
    print(f"migrations OK: {len(migrations)} version(s) in {args.dir}, "
          f"every up/down pair present, no gaps, no empty files")
    print(f"  fully migrated schema: {len(tables)} tables, {len(indexes)} named indexes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
