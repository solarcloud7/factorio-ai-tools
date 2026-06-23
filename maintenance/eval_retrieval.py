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

KS = (5, 10)
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
    # results[store][method][k] = list of per-query hits (0/1)
    results = {}
    method_available = {}

    for store, queries in by_store.items():
        store_dir, table_name, _text_col = STORE_MAP[store]
        path = os.path.join(data_dir, store_dir)
        if not os.path.exists(path):
            common.safe_print(f"SKIP {store}: {path} not found (run the ingester first).")
            continue
        table = lancedb.connect(path).open_table(table_name)

        results[store] = {m: {k: [] for k in KS} for m in ("vector", "fts", "hybrid")}
        method_available[store] = {"vector": True, "fts": True, "hybrid": True}

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
                    method_available[store][method] = False
                    if q is queries[0]:
                        common.safe_print(f"  ({store}/{method} unavailable: {str(e)[:80]})")
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

    regressions = []
    for store in results:
        n = len(by_store[store])
        per_method_at = {}
        for method in ("vector", "fts", "hybrid"):
            if not method_available[store][method]:
                continue
            cells = []
            for k in KS:
                hits = results[store][method][k]
                rec = sum(hits) / len(hits) if hits else 0.0
                per_method_at[(method, k)] = rec
                cells.append(f"{rec:<7.2f}")
            safe(f"{store:<10} {n:>3} {method:<7} " + " ".join(cells))
        # ship-gate check: where hybrid is available it must not lose to vector
        if method_available[store]["hybrid"]:
            for k in KS:
                h = per_method_at.get(("hybrid", k))
                v = per_method_at.get(("vector", k))
                if h is not None and v is not None and h + 1e-9 < v:
                    regressions.append(f"{store} r@{k}: hybrid {h:.2f} < vector {v:.2f}")
        safe("")

    if regressions:
        safe("VERDICT: hybrid REGRESSED vs vector — do NOT ship hybrid as-is:")
        for r in regressions:
            safe(f"  - {r}")
        return 1
    safe("VERDICT: hybrid >= vector wherever FTS is available (safe to ship hybrid).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
