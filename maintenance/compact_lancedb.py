"""Compact and prune every LanceDB store under ``data/``.

LanceDB is append-only / MVCC: every write (table create, batch append,
delete-then-re-add of changed content, FTS index build) creates an immutable
version, and nothing is garbage-collected automatically. Over time the
``_transactions/`` and ``_versions/`` history plus many small data fragments
bloat the committed stores. ``Table.optimize()`` is the PostgreSQL-``VACUUM``
equivalent: it compacts small fragments, prunes old versions, and folds new
rows into existing indices.

Workflow: let history accumulate on feature branches (so a PR diff shows what
data changed), then run this script before merging to ``main`` to collapse it.

Usage:
    python maintenance/compact_lancedb.py              # compact + prune all stores
    python maintenance/compact_lancedb.py --check      # read-only; exit 1 if uncompacted
    python maintenance/compact_lancedb.py --keep-days 7  # keep versions newer than 7 days

WARNING: ``--keep-days 0`` (the default) deletes every version except the
latest, and ``delete_unverified=True`` removes leftover files from interrupted
writes. Only run this when no other process (the MCP server or an ingest
script) is writing to the stores.
"""

import argparse
import glob
import os
import sys
import warnings
from datetime import timedelta

import lancedb

# Stay consistent with the ingest scripts and server.py, which all use
# db.table_names(); silence its cosmetic deprecation notice in 0.33.0.
warnings.filterwarnings("ignore", message="table_names\\(\\) is deprecated")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "data")


def safe_print(message):
    """Print without tripping PowerShell's default-encoding UnicodeEncodeError."""
    print(str(message).encode("ascii", "replace").decode("ascii"))


def dir_stats(path):
    """Return (file_count, total_bytes) for everything under ``path``."""
    file_count = 0
    total_bytes = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            fp = os.path.join(root, name)
            try:
                total_bytes += os.path.getsize(fp)
                file_count += 1
            except OSError:
                pass
    return file_count, total_bytes


def mb(num_bytes):
    return num_bytes / (1024 * 1024)


def iter_tables(store_paths):
    """Yield (store_name, table_name, db, table) for every table in every store."""
    for store_path in store_paths:
        store_name = os.path.basename(store_path)
        db = lancedb.connect(store_path)
        for table_name in db.table_names():
            yield store_name, table_name, db, db.open_table(table_name)


def find_stores():
    return sorted(glob.glob(os.path.join(DATA_DIR, "*_lancedb")))


def run_check(store_paths, max_versions):
    """Report version counts; return True if every table is within max_versions."""
    all_ok = True
    safe_print(f"Checking LanceDB stores (max-versions={max_versions})...")
    for store_name, table_name, _db, table in iter_tables(store_paths):
        versions = len(table.list_versions())
        status = "ok" if versions <= max_versions else "UNCOMPACTED"
        if versions > max_versions:
            all_ok = False
        safe_print(f"  [{status}] {store_name}/{table_name}: {versions} versions")
    return all_ok


def run_compact(store_paths, cleanup_older_than):
    """Optimize every table in place; print a before->after summary."""
    safe_print("Compacting LanceDB stores...")
    total_before_files = total_after_files = 0
    total_before_bytes = total_after_bytes = 0

    for store_name, table_name, _db, table in iter_tables(store_paths):
        store_path = os.path.join(DATA_DIR, store_name)
        versions_before = len(table.list_versions())
        files_before, bytes_before = dir_stats(store_path)

        table.optimize(cleanup_older_than=cleanup_older_than, delete_unverified=True)

        versions_after = len(table.list_versions())
        files_after, bytes_after = dir_stats(store_path)

        # dir_stats is per-store; attribute the store totals once (tables in the
        # same store share a directory, but every store here has a single table).
        total_before_files += files_before
        total_after_files += files_after
        total_before_bytes += bytes_before
        total_after_bytes += bytes_after

        safe_print(
            f"  {store_name}/{table_name}: "
            f"versions {versions_before}->{versions_after}, "
            f"files {files_before}->{files_after}, "
            f"{mb(bytes_before):.1f}MB->{mb(bytes_after):.1f}MB"
        )

    reclaimed = mb(total_before_bytes - total_after_bytes)
    safe_print(
        f"Done. Files {total_before_files}->{total_after_files}, "
        f"reclaimed {reclaimed:.1f}MB."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Read-only: report version counts and exit 1 if any store is uncompacted.",
    )
    parser.add_argument(
        "--max-versions",
        type=int,
        default=1,
        help="In --check mode, the max versions per table considered compacted (default 1).",
    )
    parser.add_argument(
        "--keep-days",
        type=int,
        default=0,
        help="Keep versions newer than this many days (default 0 = collapse to latest).",
    )
    args = parser.parse_args()

    store_paths = find_stores()
    if not store_paths:
        safe_print(f"No *_lancedb stores found under {DATA_DIR}.")
        return 0

    if args.check:
        ok = run_check(store_paths, args.max_versions)
        if not ok:
            safe_print(
                "Uncompacted stores found. Run: python maintenance/compact_lancedb.py"
            )
        return 0 if ok else 1

    run_compact(store_paths, timedelta(days=args.keep_days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
