"""Retrieval eval: recall@k for vector vs FTS (BM25) vs hybrid on a golden set.

This is the Phase-5 ship-gate for hybrid search (docs/rag-pipeline-playbook.md §5):
pytest-green is NOT evidence that hybrid retrieves better than plain vector —
only a recall comparison on real, built stores with the real embedding model is.
Run it locally after a re-ingest (`make eval`); it is not part of the offline
pytest suite (needs the 200MB model and the live LanceDB stores).

For each golden query it issues the same query three ways and checks whether a
RELEVANT result (one whose fields contain the query's distinctive `expect`
substring) appears in the top k. Stores without an FTS index report vector-only.
"""

import os
import sys

import yaml

from factorio_ai_tools.ingest import common

# store key -> (store dir, table name, text column used for FTS/keyword search)
STORE_MAP = {
    "factorio": ("factorio_lancedb", "docs", "text"),
    "clusterio": ("clusterio_lancedb", "codebase", "content"),
    "wiki": ("wiki_lancedb", "docs", "text"),
    "forum": ("forum_lancedb", "forum", "content"),
    "repo": ("repo_lancedb", "codebase", "content"),
}

# Report fine-grained ks too: on a small corpus recall@10 saturates at 1.00 for
# every method (no headroom), so @1/@3 are where vector vs hybrid actually differ.
KS = (1, 3, 5, 10)
GOLDEN = os.path.join(os.path.dirname(__file__), "..", "tests", "golden", "queries.yaml")


def _hit(rows, expect, k):
    """1 if any of the top-k rows contains ``expect`` in a string field, else 0."""
    e = expect.lower()
    for r in rows[:k]:
        blob = " ".join(str(v) for v in r.values() if isinstance(v, str)).lower()
        if e in blob:
            return 1
    return 0


def _vector(table, vec, k):
    return table.search(vec).limit(k).to_list()


def _fts(table, query, k):
    return table.search(query, query_type="fts").limit(k).to_list()


def _hybrid(table, query, vec, k):
    from lancedb.rerankers import RRFReranker

    return (table.search(query_type="hybrid")
            .vector(vec).text(query).rerank(RRFReranker()).limit(k).to_list())


def main():
    import lancedb

    with open(GOLDEN, "r", encoding="utf-8") as fh:
        golden = yaml.safe_load(fh)["queries"]

    data_dir = common.get_data_dir()
    model = common.load_embedder()

    by_store = {}
    for q in golden:
        by_store.setdefault(q["store"], []).append(q)

    kmax = max(KS)
    methods = ("vector", "fts", "hybrid")
    # results[store][method][k] = list of per-query hits (0/1) for queries that ran;
    # errors[store][method] = count of queries where the method raised. Tracking a
    # COUNT (not a single bool) means one late failure can't discard the earlier
    # hits or silently flip a whole method to "unavailable".
    results, errors, first_err = {}, {}, {}

    for store, queries in by_store.items():
        store_dir, table_name, _text_col = STORE_MAP[store]
        path = os.path.join(data_dir, store_dir)
        if not os.path.exists(path):
            common.safe_print(f"SKIP {store}: {path} not found (run the ingester first).")
            continue
        table = lancedb.connect(path).open_table(table_name)

        results[store] = {m: {k: [] for k in KS} for m in methods}
        errors[store] = {m: 0 for m in methods}
        first_err[store] = {m: "" for m in methods}

        for q in queries:
            vec = common.embed([q["query"]], model)[0].tolist()
            expect = q["expect"]
            runners = {
                "vector": lambda: _vector(table, vec, kmax),
                "fts": lambda: _fts(table, q["query"], kmax),
                "hybrid": lambda: _hybrid(table, q["query"], vec, kmax),
            }
            for method, run in runners.items():
                try:
                    rows = run()
                except Exception as e:
                    errors[store][method] += 1
                    if not first_err[store][method]:
                        first_err[store][method] = str(e)[:90].encode("ascii", "replace").decode("ascii")
                    continue
                for k in KS:
                    results[store][method][k].append(_hit(rows, expect, k))

    # --- report ------------------------------------------------------------
    safe = common.safe_print
    safe("")
    safe("=== Retrieval eval: recall@k (vector vs fts vs hybrid) ===")
    header = f"{'store':<10} {'n':>3} {'method':<7} " + " ".join(f"r@{k:<6}" for k in KS)
    safe(header)
    safe("-" * len(header))

    regressions, partial = [], []
    for store in results:
        n = len(by_store[store])
        rec_at = {}
        for method in methods:
            ran = len(results[store][method][KS[0]])
            errcnt = errors[store][method]
            if ran == 0:
                # never produced a result -> structurally unavailable (e.g. no FTS index)
                if errcnt:
                    safe(f"{store:<10} {n:>3} {method:<7} (unavailable: {first_err[store][method]})")
                continue
            cells = []
            for k in KS:
                rec = sum(results[store][method][k]) / ran
                rec_at[(method, k)] = rec
                cells.append(f"{rec:<7.2f}")
            note = ""
            if errcnt:
                # ran on some, errored on others: a real problem, not "unavailable".
                note = f"  [!] errored on {errcnt}/{n} (recall over {ran} ok)"
                if method == "hybrid":
                    partial.append(f"{store}: hybrid errored on {errcnt}/{n} queries ({first_err[store][method]})")
            safe(f"{store:<10} {n:>3} {method:<7} " + " ".join(cells) + note)
        # ship-gate: where both ran, hybrid must not lose to vector
        for k in KS:
            h, v = rec_at.get(("hybrid", k)), rec_at.get(("vector", k))
            if h is not None and v is not None and h + 1e-9 < v:
                regressions.append(f"{store} r@{k}: hybrid {h:.2f} < vector {v:.2f}")
        safe("")

    if regressions or partial:
        safe("VERDICT: NOT safe to ship hybrid as-is:")
        for r in regressions:
            safe(f"  - REGRESSION: {r}")
        for p in partial:
            safe(f"  - PARTIAL FAILURE: {p}")
        return 1
    safe("VERDICT: hybrid >= vector wherever FTS is available, with no per-query errors (safe to ship hybrid).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
