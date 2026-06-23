# RAG Ingestion Pipeline — Validation Playbook & Lessons

A durable capture of the hard-won lessons, validation gates, and design rationale
for this project's ingestion → embedding → vector-store → search pipeline. Written
after a cycle where a narrow validation gate let a cluster of real bugs through;
the goal is that the *next* change is measured against "correct," not just
"well-sized."

> **Guiding principle:** it is cheaper to **measure too much** (broad validation
> gates) than to **cut too much** (rebuild/redeploy repeatedly). A measurement
> gate only protects the dimensions it measures — so its coverage must equal
> "correct."

For *what the pipeline is* (modules, stores, tools), see the reference docs in
[docs/README.md](README.md). This doc is the *why* and the *how-to-validate*.

## Contents

- [1. The core lesson](#1-the-core-lesson)
- [2. The right things to measure](#2-the-right-things-to-measure)
- [3. Failure modes a size-only gate missed](#3-failure-modes-a-size-only-gate-missed)
- [4. Design decisions and rationale](#4-design-decisions-and-rationale)
- [5. The measure-once dry-run protocol](#5-the-measure-once-dry-run-protocol)
- [6. Caveats and known limitations](#6-caveats-and-known-limitations)
- [7. Sources](#7-sources)

---

## 1. The core lesson

A chunk-health dry-run gate (token size, explosion, empty/zero-chunk, dup counts)
was built, the corpus validated against it across several passes, declared green,
and rebuilt. A subsequent multi-agent code review still found ~20 real bugs.
The post-mortem is the lesson worth keeping:

- **A gate only guards the dimensions it measures.** The original gate measured
  chunk *size/shape* and nothing else — not stored-metadata correctness
  (paths/keys/types), not the server query layer, not incremental/edge states.
- **The happy path is not the whole path.** One fresh full ingest was exercised;
  re-ingest of a changed file, rename/delete orphans, and partial-store overwrite
  were not.
- **"Green" is not "correct."** A passing gate proved the chunks were well-sized,
  not that the system was right.
- **Don't dismiss a reading the instrument gave you.** The auditor printed
  `WARN: N pure-duplicate(s) dropped`; the dups were repeatedly called "benign."
  That warning *was* the cross-file dedup data-loss bug. The light was yellow and
  we drove through it.

**Takeaway:** broaden the gate so it asserts the dimensions in
[the right things to measure](#2-the-right-things-to-measure). A code review is
itself a second, broader measurement — treat it as part of "measure twice,"
*before* the irreversible cut (merge/deploy).

---

## 2. The right things to measure

Researched and adversarially verified against primary sources (cAST/EMNLP 2025,
sbert + HuggingFace tokenizer docs, BAAI model card, BEIR/NeurIPS 2021,
ARES/NAACL 2024, RAGAS docs, LangChain Indexing, Auepora survey). For each: the
assertion, the failure it prevents, and how this pipeline stands today. Test
names refer to files under [tests/](../tests).

### 2.1 Chunking
| Gate | Assertion | Prevents | In this pipeline |
|---|---|---|---|
| **Coverage** | A file's chunks cover its meaningful source. | Silently dropped source regions. | `chunk_code` falls back to text chunks only when the AST misses a substantial fraction of the file's *non-import, non-comment* code (`_non_import_body_chars`, multi-line-import aware). Full verbatim round-trip is not asserted. |
| **AST boundary integrity** | Chunks fall on structural (function/class/table) boundaries, not mid-unit. | Split declarations → worse retrieval (+4.3 Recall@5) & generation (+2.67 Pass@1) per cAST. | tree-sitter top-level capture + recursive split; a non-empty source that yields zero chunks is flagged by `ChunkAuditor`. Mid-unit split is not separately asserted. |
| **Dedup keeps distinct docs** | Dedup must not collapse genuinely-distinct documents. The risky path is normalize-then-hash (whitespace/Unicode normalization can merge distinct docs). | Distinct files sharing boilerplate collapse → their `file_path`s become unsearchable. | `normalize_chunks` dedups **per file**, not store-wide. `test_dedup_keeps_distinct_files`. |
| **Metadata correctness** | `file_path` OS-stable, `repo_url` unique & well-formed, `node_type` ∈ documented vocab. | Backslash paths, key collisions, undocumented `node_type` → silent retrieval misses. | POSIX `file_path` (`to_posix`), `owner/repo` `repo_url` key (`repo_slug_from_url`), `NODE_TYPES` vocab. `test_repo_file_paths_are_posix`, `test_node_type_vocab`. |

### 2.2 Embedding integrity
| Gate | Assertion | Prevents | In this pipeline |
|---|---|---|---|
| **Token cap vs embedder** | Every chunk's post-tokenization length (real tokenizer) ≤ the embedder's hard cap (bge-base = **512**), **including any context prefix**. | Silent truncation — sentence-transformers truncate with no error; the raw HF tokenizer defaults to *no* truncation. `model.max_seq_length` is not universally reliable; measure with the tokenizer. | Measured on prefix+content with an effective cap of **510** (`EMBED_MAX_TOKENS`; 512 minus CLS+SEP). `build_embed_entries` re-splits so prefix+content fits. `test_embedded_text_within_cap_incl_prefix`. |
| **Vector contract** | 768-dim, L2-normalized. | Dim/normalization drift breaks search silently. | `embed()` asserts the dimension; all stores share it. |
| **Overflow detection** | Optionally `return_overflowing_tokens=True` to recover dropped content (`num_truncated_tokens` is unreliable). | Invisible content loss at the cap boundary. | Not implemented (optional). |

### 2.3 Retrieval evaluation
- **Golden / regression set + ranking metrics.** Implemented: `make eval`
  ([maintenance/eval_retrieval.py](../maintenance/eval_retrieval.py)) reports
  recall@1/3/5/10 for vector vs FTS(BM25) vs hybrid on
  [tests/golden/queries.yaml](../tests/golden/queries.yaml), with a ship-gate
  verdict. It needs the real model + live stores, so it is run manually, not in CI.
- **Always benchmark vector vs a BM25 baseline** (BEIR): dense retrieval often
  loses out-of-distribution, especially with a generic embedder like bge-base.
  This is the empirical test for whether hybrid actually helps.
- **Not yet implemented (research guidance for going deeper):** RAGAS Context
  Precision@K / Faithfulness / Answer Relevancy; evaluating retrieval and
  generation as separate targets (Auepora); BEIR for OOD generalization; ARES for
  a cheap per-change gate (~150 labels + synthetic data).

### 2.4 Data-store integrity
- **Idempotent / incremental**: content+metadata hash keyed on a stable
  per-document source id; a no-op re-ingest produces zero spurious writes.
  `test_noop_reingest_writes_zero`.
- **Orphan / stale prevention**: incremental cleanup deletes rows for sources no
  longer present (orphan reconcile — repo scoped by `repo_url`, clusterio
  store-wide). `test_orphan_rows_removed_on_delete`.
- **No destructive overwrite**: `ensure_stores` extracts only missing stores.
  `test_ensure_stores_does_not_overwrite_existing`.
- *(These patterns are sourced to LangChain's Indexing API; LanceDB-native
  equivalents remain an open question — see [Caveats](#6-caveats-and-known-limitations).)*

### 2.5 Gate design
- **Cheap, deterministic, fail-closed pre-commit dry-run**: structural / coverage
  / token / metadata / idempotency checks on the full corpus, with no embed/write
  (`--dry-run`).
- **Thresholded eval-set scoring** as the pre-ship quality gate (recall@k vs BM25).
- If an LLM-judge eval is added later, pin judge versions and use tolerance bands
  (judges are non-deterministic).

---

## 3. Failure modes a size-only gate missed

The bug classes a chunk-*size* gate let through, kept as cautionary knowledge —
each names the gate (above) that now catches it. These are fixed in the current
code; they are listed so the next change isn't measured by size alone.

**Wrong results / corrupt stored data:**
- **Store-wide dedup collapsing byte-identical files across plugins** — content-only
  dedup over the whole corpus merged distinct `file_path`s (e.g. identical
  `tsconfig.json`, bare re-export `index.ts`) into one, making them unsearchable.
  *Caught by: dedup-keeps-distinct-docs.*
- **Non-portable `file_path`** — backslash paths on Windows produce different
  vectors per OS. *Caught by: metadata correctness.*
- **`repo_url` key collisions** — a bare basename with every `.git` stripped
  (`octocat.github.io` → `octocathub.io`) collides for repos sharing a trailing
  name. *Caught by: metadata correctness / stable key.*
- **Inconsistent language routing** — `.lua` forced through the TypeScript grammar
  and `.js` text-chunked in one ingester but AST'd in another, losing declaration
  metadata. *Caught by: AST boundary integrity.* (Shared routing now lives in
  `kind_for_ext`.)
- **Miscalibrated coverage fallback** — counting import/blank lines in the coverage
  denominator made import-heavy files fall back to text even when the AST captured
  every declaration. *Caught by: coverage.*

**Wrong results (query layer):**
- **Unescaped `LIKE` metacharacters** — `_` is a single-char wildcard, so a
  `plugin`/`repo_url` filter matched far more than intended. Fixed with
  `like_escape`/`like_filter` (`% _ \` escaped + `ESCAPE`).
- **Bootstrap overwrite** — extracting the release zip over an existing populated
  `data/` clobbered local ingest work. Fixed: `ensure_stores` extracts only missing
  stores.

**Robustness:**
- **Orphan rows on rename/delete** and a delete-before-bulk-skip loss — incremental
  only deleted files it re-walked. *Caught by: orphan/stale prevention.*
- **Prefix not budgeted against the cap** — the `File:…Code:…` context prefix wasn't
  counted toward the 512 limit. *Caught by: token cap incl. prefix.*
- **Non-code decode without `errors="replace"`** — a non-UTF-8 byte dropped the file
  (and aborted the run under `--strict`/`--dry-run`).

---

## 4. Design decisions and rationale

- **5 distinct-schema stores** (`factorio` docs, `wiki`, `clusterio` TS, `forum`,
  generic `repo`), each its own ingest script sharing `ingest/common.py`.
  `repo_lancedb` is the generalization of the old per-mod ingester; `mod_lancedb`
  was retired (its data is a search target served via `search_github_code`).
- **Keep Lua `table_constructor` chunks** — the server serves their `content`, so
  vanilla/mod prototype data (recipes/entities) is a primary retrieval target. The
  explosion that motivated chunking work was *duplication*, not tables; the fix was
  top-level capture + recursive split, not dropping tables.
- **Token-correct sizing, not a character proxy** — code tokenizes ~2–4× denser
  than prose; a char cap lets 600–1500-token chunks pass and silently truncate at
  512. Sizing is measured with the real tokenizer (`count_tokens`).
- **Recursive AST split** (cAST): capture top-level declarations; split an oversized
  node by its children; line/char-window only at a leaf. Fall back to text chunks
  when the AST misses a substantial fraction of the file's non-import, non-comment
  code (`_non_import_body_chars`).
- **GPU torch via `make sync`** — `uv` resolves statically (it can't probe hardware)
  and a uv `--extra` pollutes the default lock; for a published package torch must
  stay a CPU base dep. So `make sync` detects `nvidia-smi` and installs the CUDA
  wheel locally only (PyPI/Docker/CI stay CPU-lean). A runtime GPU-mismatch alert
  (`gpu_torch_warning`) warns when a GPU is present but torch can't use it.
- **Hybrid retrieval over FTS + vector** — `common.hybrid_search` runs LanceDB
  hybrid (RRF over the ingest-built FTS index + vector), falling back to pure vector
  where no FTS index exists or on a transient error (cached per table). It is shipped
  on a non-regression basis, not a proven win — see
  [Caveats](#6-caveats-and-known-limitations).
- **Manual build + deploy** — `data/` is gitignored; build locally
  (`make ingest-all`), finalize (`make compact`), ship the 5-store
  `factorio_lancedb.zip` via `make deploy-dbs`. The asset name is load-bearing:
  `server.ensure_databases()` downloads exactly that asset from the latest release.

---

## 5. The measure-once dry-run protocol

`--dry-run` (or `FACTORIO_MCP_DRY_RUN=1`) runs the real fetch/clone/chunk + token
auditor with **no embed/write**, over the **full corpus**, fail-closed (it implies
strict). Iterate the spec until clean, then do exactly **one** real rebuild →
`make compact` → `make deploy-dbs`.

What the dry-run gate asserts today (a FAIL exits non-zero under strict):
- no chunk over the embedder's token cap (measured on the embedded prefix+content);
- no per-source explosion (a single source producing an absurd chunk count);
- no non-empty source that produced zero chunks;
- dedup/decode/skip counts surfaced (per-file dedup, `errors="replace"` decode,
  bulk-file skips), tiny-dropped counts surfaced.

Each ingester also builds an FTS index on its text column (`text` for
factorio/wiki, `content` for clusterio/forum/repo), so a fresh rebuild ships
hybrid-ready stores.

Beyond the dry-run, the offline test suite (`make test`) encodes the same
invariants (see [tests/test_pipeline_invariants.py](../tests/test_pipeline_invariants.py)),
`make eval` scores retrieval against the golden set before shipping, and after a
release `make smoke` installs the published wheel into an isolated venv, forces a
fresh DB download, and asserts every tool end-to-end.

---

## 6. Caveats and known limitations

- **Hybrid is shipped, not proven.** On the current golden set hybrid equals
  vector (vector is already near-saturated; FTS-alone is worse on prose). Hybrid
  never regresses there but does not measurably beat vector — it was shipped on a
  non-regression + best-practice basis (free FTS, expected help on lexical/
  identifier queries the small set can't express). Revisit with real query logs or
  a harder golden set.
- **The forum store ships as a stale stub.** `forum_links.txt` (the curated source
  list `ingest_forum.py` reads) was removed from the repo, so the store cannot be
  rebuilt and currently holds a single stale row with no FTS index;
  `search_factorio_forums` therefore falls back to vector over near-empty data.
  Restore the link list and re-ingest, or retire the tool.
- **Other deferred items.** The wiki ingester's `/`-in-title filter drops some
  legitimate English subpages; the factorio ingester can orphan rows if
  `VERSIONS_TO_SCRAPE` is edited; the forum delete is non-atomic. None are
  exercised by the shipped path today.
- **Existing installs do not auto-refresh data.** `ensure_databases` downloads only
  *missing* stores, so an install that already has all five keeps its local `data/`
  until a store directory is deleted.
- Numeric eval thresholds (e.g. faithfulness 0.90/0.95) are practitioner guidance —
  calibrate on your own golden set; code-retrieval may need different cutoffs than QA.
- "BM25 beats dense" is 2021-era (BEIR); modern instruction-tuned embedders often
  beat it. The durable takeaway is **validate, don't assume**.
- Data-store integrity patterns are sourced to LangChain (one vendor); the
  LanceDB-native equivalents for idempotency/orphan/no-overwrite are an open
  question to nail down.

---

## 7. Sources

- cAST — structural code chunking, EMNLP 2025: https://arxiv.org/abs/2506.15655
- BEIR — zero-shot IR benchmark, NeurIPS 2021: https://arxiv.org/abs/2104.08663
- ARES — automated RAG eval, NAACL 2024: https://arxiv.org/abs/2311.09476
- Auepora — RAG evaluation survey: https://arxiv.org/abs/2405.07437
- RAGAS metrics (Faithfulness, Context Precision@K, Answer Relevancy): https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/
- sentence-transformers truncation behavior: https://sbert.net/examples/sentence_transformer/applications/computing-embeddings/README.html
- HF tokenizer (truncation default / overflowing tokens): https://huggingface.co/docs/transformers/main_classes/tokenizer
- BAAI/bge-base-en-v1.5 (512 cap): https://huggingface.co/BAAI/bge-base-en-v1.5
- LangChain Indexing (idempotent ingestion, source id): https://www.langchain.com/blog/syncing-data-sources-to-vector-stores
