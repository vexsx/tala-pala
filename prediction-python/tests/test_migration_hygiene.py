"""The migration file set is guarded by the ordinary test suite, not only by CI.

Why here, in the Python suite: a broken file set (a duplicated number, an
.up.sql with no .down.sql, a hole in the numbering, an empty file) costs
nothing to detect and is otherwise invisible until a deploy applies it against
a real database. scripts/check_migrations.py needs no database, so the suite
every change already runs is the cheapest place to catch it. CI's `migrations`
job runs the same checker before it touches Postgres, and derives its schema
assertions from the same parse — these tests keep that derivation honest, since
a regression there would silently weaken the CI assertions rather than fail
them.

The checker lives at the repo root because CI invokes it standalone, so it is
loaded by path rather than imported. Under scripts/pytest_docker.sh only
prediction-python/ is mounted into the container; there the repo root is not
reachable and these tests skip loudly instead of passing vacuously. Both CI
jobs that matter (`python` and `migrations`) run against a full checkout.
"""
from __future__ import annotations

import importlib.util
import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHECKER_PATH = os.path.join(REPO_ROOT, "scripts", "check_migrations.py")
MIGRATIONS_DIR = os.path.join(REPO_ROOT, "database", "migrations")

# Created by 0014 for query paths that previously scanned the two
# fastest-growing tables end to end.
HOT_PATH_INDEXES = {
    "idx_raw_obs_provider_time",
    "idx_raw_obs_collected_at",
    "idx_predictions_symbol_horizon_predicted",
    "idx_predictions_symbol_horizon_target",
}


def _load_checker():
    if not os.path.isfile(CHECKER_PATH) or not os.path.isdir(MIGRATIONS_DIR):
        pytest.skip(
            "repo root not mounted (scripts/check_migrations.py and "
            "database/migrations unreachable) — run from a full checkout")
    spec = importlib.util.spec_from_file_location("check_migrations", CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    return _load_checker()


def test_migration_file_set_is_clean(checker):
    problems = checker.check_migrations(MIGRATIONS_DIR)
    assert problems == [], "migration file-set problems:\n" + "\n".join(
        f"  - {p}" for p in problems)


def test_every_version_has_both_directions(checker):
    migrations, problems = checker.scan(MIGRATIONS_DIR)
    assert problems == []
    assert migrations, "no migrations found — the directory or naming changed"
    for version, migration in sorted(migrations.items()):
        assert len(migration.up) == 1, f"{version:04d}: {migration.up}"
        assert len(migration.down) == 1, f"{version:04d}: {migration.down}"
    assert sorted(migrations) == list(range(1, max(migrations) + 1))


def test_derived_schema_covers_the_core_tables(checker):
    """Guards the derivation CI asserts against: an empty or partial parse
    would turn the CI schema check into a check of nothing."""
    tables, indexes = checker.derive_schema(MIGRATIONS_DIR)
    for table in ("prices", "raw_observations", "predictions", "model_versions",
                  "users", "app_settings", "app_issues", "news_events"):
        assert table in tables, f"{table} missing from the derived table set"
    assert HOT_PATH_INDEXES <= indexes, HOT_PATH_INDEXES - indexes


def _write(directory, name, body="SELECT 1;\n"):
    with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
        handle.write(body)


def test_commented_out_ddl_is_not_counted(checker, tmp_path):
    """Migration headers describe tables in prose and show example DDL; only
    statements the database actually runs may enter the expected schema."""
    _write(tmp_path, "0001_a.up.sql",
           "-- Superseded: CREATE TABLE ghost (id INT);\n"
           "/* CREATE INDEX idx_ghost ON ghost (id); */\n"
           "CREATE TABLE real_one (note TEXT NOT NULL DEFAULT 'CREATE TABLE quoted');\n")
    _write(tmp_path, "0001_a.down.sql", "DROP TABLE real_one;\n")
    tables, indexes = checker.derive_schema(str(tmp_path))
    assert tables == {"real_one"}
    assert indexes == set()


def test_detects_a_missing_down_file(checker, tmp_path):
    _write(tmp_path, "0001_a.up.sql")
    _write(tmp_path, "0001_a.down.sql")
    _write(tmp_path, "0002_b.up.sql")
    problems = checker.check_migrations(str(tmp_path))
    assert len(problems) == 1
    assert "no .down.sql" in problems[0] and "0002" in problems[0]


def test_detects_a_duplicated_number(checker, tmp_path):
    for name in ("0001_a", "0002_b", "0002_c"):
        _write(tmp_path, f"{name}.up.sql")
        _write(tmp_path, f"{name}.down.sql")
    problems = checker.check_migrations(str(tmp_path))
    assert any("duplicate migration number 0002" in p for p in problems)


def test_detects_a_numbering_gap(checker, tmp_path):
    for name in ("0001_a", "0003_c"):
        _write(tmp_path, f"{name}.up.sql")
        _write(tmp_path, f"{name}.down.sql")
    problems = checker.check_migrations(str(tmp_path))
    assert len(problems) == 1
    assert "gap in migration numbering: 0002" in problems[0]


def test_detects_empty_and_comment_only_files(checker, tmp_path):
    _write(tmp_path, "0001_a.up.sql", "")
    _write(tmp_path, "0001_a.down.sql", "-- nothing to undo\n")
    problems = checker.check_migrations(str(tmp_path))
    assert len(problems) == 2
    assert any("0001_a.up.sql is empty" in p for p in problems)
    assert any("0001_a.down.sql contains only comments" in p for p in problems)


def test_detects_an_unrecognized_filename(checker, tmp_path):
    _write(tmp_path, "0001_a.up.sql")
    _write(tmp_path, "0001_a.down.sql")
    _write(tmp_path, "17_news.up.sql")
    problems = checker.check_migrations(str(tmp_path))
    assert len(problems) == 1
    assert "unrecognized migration filename" in problems[0]


def test_apply_order_is_numeric(checker, tmp_path):
    for number in (2, 10, 1):
        _write(tmp_path, f"{number:04d}_m.up.sql")
        _write(tmp_path, f"{number:04d}_m.down.sql")
    order = [os.path.basename(p) for p in checker.up_files(str(tmp_path))]
    assert order == ["0001_m.up.sql", "0002_m.up.sql", "0010_m.up.sql"]
    assert [os.path.basename(p) for p in
            checker.up_files(str(tmp_path), min_version=2, max_version=9)] == \
        ["0002_m.up.sql"]


def test_alter_added_columns_reach_the_sqlalchemy_mirror(checker):
    """A column added by ALTER TABLE must appear in the Python mirror too.

    CREATE TABLE bodies are easy to mirror because the whole table is written
    at once; the columns a LATER migration bolts on are the ones that get
    missed, because nothing points at the mirror when the ALTER is written.
    That is exactly what happened to 0017: ten columns on ``news_articles``
    (which made the collectors log a mirror-drift warning every pass) and six
    on ``news_events`` (which are precisely the keys
    ``consolidate.event_consolidation_fields`` returns, so the consolidation
    result had nowhere to be written at all).

    Only tables the mirror actually declares are checked — Python deliberately
    does not mirror the whole schema (users, alerts and the rest are the Go
    service's).
    """
    import app.news  # noqa: F401  registers the news tables on db.metadata
    from app.db import metadata

    alter_add = re.compile(
        r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w.]+)\s+"
        r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
        re.IGNORECASE,
    )
    missing = []
    checked = 0
    for path in checker.up_files(MIGRATIONS_DIR):
        with open(path, "r", encoding="utf-8") as handle:
            sql = checker._executable_sql(handle.read())
        for raw_table, column in alter_add.findall(sql):
            table = raw_table.split(".")[-1]
            if table not in metadata.tables:
                continue  # not mirrored in Python at all, by design
            checked += 1
            if column not in metadata.tables[table].c:
                missing.append(f"{table}.{column} ({os.path.basename(path)})")

    assert checked, "no ALTER ... ADD COLUMN found — the parse or the naming changed"
    assert missing == [], (
        "columns added by a migration but absent from the SQLAlchemy mirror:\n"
        + "\n".join(f"  - {m}" for m in missing))


def test_drop_in_a_later_migration_is_reflected(checker, tmp_path):
    _write(tmp_path, "0001_a.up.sql",
           "CREATE TABLE keep (id INT);\nCREATE TABLE gone (id INT);\n"
           "CREATE INDEX idx_gone ON gone (id);\n")
    _write(tmp_path, "0001_a.down.sql", "DROP TABLE keep;\n")
    _write(tmp_path, "0002_b.up.sql",
           "DROP INDEX IF EXISTS idx_gone;\nDROP TABLE IF EXISTS gone;\n")
    _write(tmp_path, "0002_b.down.sql", "SELECT 1;\n")
    tables, indexes = checker.derive_schema(str(tmp_path))
    assert tables == {"keep"}
    assert indexes == set()
