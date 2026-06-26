"""Store status report — read-only inventory of the LanceDB stores (`make status`).

Prints per-store row count, version.txt, FTS-index presence, and a health flag, so
you can see at a glance which stores are built/current and which need a re-ingest
(e.g. a thin/stub store with no FTS index). Touches no data — safe to run anytime.
"""

import os

import lancedb

from factorio_ai_tools.ingest import common

# Canonical store -> (dir, table, fts text column). Mirrors eval_retrieval.STORE_MAP;
# imported so a new store only has to be added in one place.
try:
    import eval_retrieval  # sibling module when run as `python maintenance/store_status.py`
    STORE_MAP = eval_retrieval.STORE_MAP
except Exception:
    from maintenance.eval_retrieval import STORE_MAP


def main():
    data_dir = common.get_data_dir()
    common.safe_print(f"Store status @ {data_dir}\n")
    common.safe_print(f"{'store':22} {'rows':>8}  {'version':<18} {'fts':<4} health")
    common.safe_print("-" * 66)
    ok = thin = missing = 0
    for store_dir, table_name, _fts_col in STORE_MAP.values():
        path = os.path.join(data_dir, store_dir)
        rows, fts, health = "-", "-", "absent"
        if os.path.isdir(path):
            try:
                t = lancedb.connect(path).open_table(table_name)
                rows = t.count_rows()
                fts = "yes" if list(t.list_indices()) else "no"
                if rows >= 5:
                    health, _ = "ok", (ok := ok + 1)
                else:
                    health, _ = "THIN/stub", (thin := thin + 1)
            except Exception as e:
                rows, health = "ERR", f"open failed: {str(e)[:30]}"
        else:
            missing += 1
        vpath = os.path.join(path, "version.txt")
        ver = open(vpath, encoding="utf-8").read().strip() if os.path.exists(vpath) else "-"
        common.safe_print(f"{store_dir:22} {str(rows):>8}  {ver:<18} {fts:<4} {health}")
    common.safe_print("-" * 66)
    common.safe_print(f"{ok} ok | {thin} thin/stub | {missing} missing  (of {len(STORE_MAP)} stores)")


if __name__ == "__main__":
    main()
