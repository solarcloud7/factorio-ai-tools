# RAG Pipeline — Validation Reference

Validation gates, the dry-run protocol, and known limitations for the ingest → embedding → vector-store → search pipeline.

## Contents

- [1. Validation principles](#1-validation-principles)
- [2. What to measure](#2-what-to-measure)
- [3. The dry-run protocol](#3-the-dry-run-protocol)
- [4. Caveats and known limitations](#4-caveats-and-known-limitations)
- [5. Sources](#5-sources)

---

## 1. Validation principles

- A gate only guards the dimensions it measures — coverage must equal "correct."
- Validate the whole path: re-ingest of a changed file, rename/delete orphans, and partial-store overwrite, not just one fresh full ingest.
- A code review is a second, broader measurement — treat it as part of "measure twice," before the irreversible cut (merge/deploy).

---

## 2. What to measure

Test names refer to files under [tests/](../tests).

### 2.1 Chunking

| Gate | Assertion | Prevents | In this pipeline |
|---|---|---|---|
| **Coverage** | A file's chunks cover its meaningful source. | Silently dropped source regions. | `chunk_code` falls back to text chunks only when the AST misses a substantial fraction of the file's *non-import, non-comment* code (`_non_import_body_chars`, multi-line-import aware). Full verbatim round-trip is not asserted. |
| **AST boundary integrity** | Chunks fall on structural (function/class/table) boundaries, not mid-unit. | Split declarations → worse retrieval (+4.3 Recall@5) & generation (+2.67 Pass@1) per cAST. | tree-sitter top-level capture + recursive split; a non-empty source that yields zero chunks is flagged by `ChunkAuditor`. Mid-unit split is not separately asserted. |
| **Dedup keeps distinct docs** | Dedup must not collapse genuinely-distinct documents. | Distinct files sharing boilerplate collapse → their `file_path`s become unsearchable. | `normalize_chunks` dedups **per file**, not store-wide. `test_dedup_keeps_distinct_files`. |
| **Metadata correctness** | `file_path` OS-stable, `repo_url` unique & well-formed, `node_type` ∈ documented vocab. | Backslash paths, key collisions, undocumented `node_type` → silent retrieval misses. | POSIX `file_path` (`to_posix`), `owner/repo` `repo_url` key (`repo_slug_from_url`), `NODE_TYPES` vocab. `test_repo_file_paths_are_posix`, `test_node_type_vocab`. |

### 2.2 Embedding integrity

| Gate | Assertion | Prevents | In this pipeline |
|---|---|---|---|
| **Token cap vs embedder** | Every chunk's post-tokenization length (real tokenizer) ≤ the embedder's hard cap (bge-base = **512**), **including any context prefix**. | Silent truncation — sentence-transformers truncate with no error; the raw HF tokenizer defaults to *no* truncation. | Measured on prefix+content with an effective cap of **510** (`EMBED_MAX_TOKENS`; 512 minus CLS+SEP). `build_embed_entries` re-splits so prefix+content fits. `test_embedded_text_within_cap_incl_prefix`. |
| **Vector contract** | 768-dim, L2-normalized. | Dim/normalization drift breaks search silently. | `embed()` asserts the dimension; all stores share it. |
| **Overflow detection** | Optionally `return_overflowing_tokens=True` to recover dropped content. | Invisible content loss at the cap boundary. | Not implemented. |

### 2.3 Retrieval evaluation

- **Golden set + ranking metrics.** `make eval` ([maintenance/eval_retrieval.py](../maintenance/eval_retrieval.py)) reports recall@1/3/5/10 for vector vs FTS(BM25) vs hybrid on [tests/golden/queries.yaml](../tests/golden/queries.yaml), with a ship-gate verdict. Requires the real model + live stores; run manually, not in CI.

### 2.4 Data-store integrity

- **Idempotent / incremental**: content+metadata hash keyed on a stable per-document source id; a no-op re-ingest produces zero spurious writes. `test_noop_reingest_writes_zero`.
- **Orphan / stale prevention**: incremental cleanup deletes rows for sources no longer present (orphan reconcile — repo scoped by `repo_url`, clusterio store-wide). `test_orphan_rows_removed_on_delete`.
- **No destructive overwrite**: `ensure_stores` extracts only missing stores. `test_ensure_stores_does_not_overwrite_existing`.

### 2.5 Gate design

- **Cheap, deterministic, fail-closed pre-commit dry-run**: structural / coverage / token / metadata / idempotency checks on the full corpus, with no embed/write (`--dry-run`).
- **Thresholded eval-set scoring** as the pre-ship quality gate (recall@k vs BM25).

---

## 3. The dry-run protocol

`--dry-run` (or `FACTORIO_MCP_DRY_RUN=1`) runs the real fetch/clone/chunk + token auditor with **no embed/write**, over the **full corpus**, fail-closed (it implies strict). Iterate the spec until clean, then do exactly **one** real rebuild → `make compact` → `make deploy-dbs`.

What the dry-run gate asserts today (a FAIL exits non-zero under strict):
- no chunk over the embedder's token cap (measured on the embedded prefix+content);
- no per-source explosion (a single source producing an absurd chunk count);
- no non-empty source that produced zero chunks;
- dedup/decode/skip counts surfaced (per-file dedup, `errors="replace"` decode, bulk-file skips), tiny-dropped counts surfaced.

Each ingester also builds an FTS index on its text column (`text` for factorio/wiki, `content` for clusterio/forum/repo), so a fresh rebuild ships hybrid-ready stores.

Beyond the dry-run, the offline test suite (`make test`) encodes the same invariants (see [tests/test_pipeline_invariants.py](../tests/test_pipeline_invariants.py)), `make eval` scores retrieval against the golden set before shipping, and after a release `make smoke` installs the published wheel into an isolated venv, forces a fresh DB download, and asserts every tool end-to-end.

---

## 4. Caveats and known limitations

- **Hybrid is shipped, not proven.** On the current golden set hybrid equals vector (vector is already near-saturated; FTS-alone is worse on prose). Hybrid never regresses there but does not measurably beat vector — it was shipped on a non-regression + best-practice basis. Revisit with real query logs or a harder golden set.
- **The forum store ships as a stale stub.** `forum_links.txt` (the curated source list `ingest_forum.py` reads) was removed from the repo, so the store cannot be rebuilt and currently holds a single stale row with no FTS index; `search_factorio_forums` therefore falls back to vector over near-empty data. Restore the link list and re-ingest, or retire the tool.
- **Other deferred items.** The wiki ingester's `/`-in-title filter drops some legitimate English subpages; the factorio ingester can orphan rows if `VERSIONS_TO_SCRAPE` is edited; the forum delete is non-atomic. None are exercised by the shipped path today.
- **Existing installs do not auto-refresh data.** `ensure_databases` downloads only *missing* release stores, so an install that already has all six release stores keeps its local `data/` until a store directory is deleted.
- Numeric eval thresholds (e.g. faithfulness 0.90/0.95) are practitioner guidance — calibrate on your own golden set; code-retrieval may need different cutoffs than QA.
- Data-store integrity patterns are sourced to LangChain (one vendor); the LanceDB-native equivalents for idempotency/orphan/no-overwrite are an open question to nail down.

---

## 5. Sources

- cAST — structural code chunking, EMNLP 2025: https://arxiv.org/abs/2506.15655
- BEIR — zero-shot IR benchmark, NeurIPS 2021: https://arxiv.org/abs/2104.08663
- ARES — automated RAG eval, NAACL 2024: https://arxiv.org/abs/2311.09476
- Auepora — RAG evaluation survey: https://arxiv.org/abs/2405.07437
- RAGAS metrics (Faithfulness, Context Precision@K, Answer Relevancy): https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/
- sentence-transformers truncation behavior: https://sbert.net/examples/sentence_transformer/applications/computing-embeddings/README.html
- HF tokenizer (truncation default / overflowing tokens): https://huggingface.co/docs/transformers/main_classes/tokenizer
- BAAI/bge-base-en-v1.5 (512 cap): https://huggingface.co/BAAI/bge-base-en-v1.5
- LangChain Indexing (idempotent ingestion, source id): https://www.langchain.com/blog/syncing-data-sources-to-vector-stores
