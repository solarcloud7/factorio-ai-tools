# RAG Ingestion Pipeline — Validation Playbook & Lessons

A durable capture of the hard-won lessons, validation gates, open defects, and
design rationale for this project's ingestion → embedding → vector-store →
search pipeline. Written after a cycle where a narrow validation gate let a
cluster of real bugs through; the goal is that the *next* change is measured
against "correct," not just "well-sized."

> **Guiding principle:** it is cheaper to **measure too much** (broad validation
> gates) than to **cut too much** (rebuild/redeploy repeatedly). A measurement
> gate only protects the dimensions it measures — so its coverage must equal
> "correct."

---

## 0. Status — applied on branch `feat/pipeline-hardening` (2026-06-22)

This playbook has now been **applied**: the gate was broadened first (gate-first
invariants that fail on the buggy code), the §3 defects fixed until green, the
corpus re-validated by dry-run, rebuilt once on GPU, and hybrid shipped after the
eval. The ✅/⚠️/❌ marks in §2–§3 below reflect this branch.

**Done (in scope):** §3.1 (1–5), §3.2 (6–7), §3.3 (8 orphans, 9 prefix budget,
10 decode-replace) all fixed; FTS now queried (hybrid, §3.4); 510 cap; node_type
docstring. New: `tests/test_pipeline_invariants.py` (gate-first), per-file dedup,
`kind_for_ext`, POSIX paths, `owner/repo` keys, orphan reconcile, `ensure_stores`,
escaped `LIKE`, `tests/golden/queries.yaml` + `make eval`. **56 offline tests
pass**; dry-run all-green on the real corpus; one GPU re-ingest + compact done.

**Deferred (pre-existing, out of this branch's scope):** §3.3#11 wiki pagination
guard; §3.4 wiki `/` subpage filter, factorio version-orphan, forum non-atomic
delete. Not introduced by recent work; tracked here for the next cut.

**Measured, not assumed (hybrid):** the eval ship-gate showed hybrid **==** vector
on the golden set (vector already near-saturated; FTS-alone worse on prose). It
never *beats* vector here — shipping was a deliberate call (best-practice + free
FTS + helps lexical/identifier queries this small set can't express), not an
eval-proven win. Revisit with real query logs / a harder set (§2.3).

---

## 1. The core lesson — why "measure twice, cut once" still failed

We built a chunk-health dry-run gate (token size, explosion, empty/zero-chunk,
dup counts), validated the corpus against it across four passes, went green, and
rebuilt. A subsequent code review still found ~20 real bugs. Post-mortem:

- **A gate only guards the dimensions it measures.** Ours measured chunk
  *size/shape* (the bug that bit us *last* time) and nothing else — not stored-
  metadata correctness (paths/keys/types), not the server query layer, not
  incremental/edge states.
- **We ran the happy path only** — one fresh full ingest. Re-ingest of a changed
  file, rename/delete orphans, and partial-store overwrite were never exercised.
- **We over-generalized "green" into "correct."** A passing gate proved the
  chunks were well-sized, not that the system was right.
- **We dismissed a reading the instrument gave us.** The auditor printed
  `WARN: N pure-duplicate(s) dropped`; we repeatedly called dups "benign." That
  warning *was* the cross-file dedup data-loss bug. The light was yellow; we
  drove through it.

**Takeaway:** broaden the gate so it asserts the dimensions below. A code review
is itself a second, broader measurement — treat it as part of "measure twice,"
*before* the irreversible cut (merge/deploy).

---

## 2. The right things to measure (validation gates)

Researched and adversarially verified against primary sources (cAST/EMNLP 2025,
sbert + HuggingFace tokenizer docs, BAAI model card, BEIR/NeurIPS 2021,
ARES/NAACL 2024, RAGAS docs, LangChain Indexing, Auepora survey). For each:
the assertion, the pass criterion, the failure it prevents, and whether we have
it today.

### 2.1 Chunking
| Gate | Assertion / pass criterion | Prevents | Have? |
|---|---|---|---|
| **Coverage round-trip** | Concatenating a file's chunks reproduces the source **verbatim, modulo overlap**. Lossless per file. | Silently dropped/omitted source regions. | ⚠️ coverage recalibrated + both-way test (§3.1#5 fixed); full verbatim round-trip still not asserted |
| **AST boundary integrity** | Chunks fall on structural (function/class/table) boundaries, not mid-unit. | Split declarations → worse retrieval (+4.3 Recall@5) & generation (+2.67 Pass@1) per cAST. | ⚠️ tree-sitter boundaries + zero-chunk gate; mid-unit split not asserted |
| **Dedup keeps distinct docs** | Dedup must NOT collapse genuinely-distinct documents. Test the **normalize-then-hash** path explicitly (whitespace/lowercase/Unicode normalization can merge distinct docs); pure byte-exact is the safe form. | Distinct files sharing boilerplate collapse → their `file_path`s become unsearchable. | ✅ per-file dedup + `test_dedup_keeps_distinct_files` |
| **Metadata correctness** | `file_path` normalized & OS-stable, keys (`repo_url`) unique & well-formed, `node_type` ∈ documented vocab. | Backslash paths, key collisions, undocumented `node_type` → silent retrieval misses. | ✅ POSIX paths, `owner/repo` key, `NODE_TYPES` vocab + tests |

### 2.2 Embedding integrity
| Gate | Assertion | Prevents | Have? |
|---|---|---|---|
| **Token cap vs embedder** | Every chunk's **post-tokenization** length (real tokenizer) ≤ hard cap (bge-base = **512**), **including any context prefix**. | Silent truncation — sentence-transformers truncate with no error; raw HF tokenizer defaults to *no* truncation and can over-produce. `model.max_seq_length` is NOT universally reliable — measure with the tokenizer. | ✅ token count + prefix budgeted (`build_embed_entries`, eff. cap 510) + `test_embedded_text_within_cap_incl_prefix` |
| **Vector contract** | 768-dim, L2-normalized. | Dim/normalization drift breaks search silently. | ✅ |
| **Overflow detection** | Optionally `return_overflowing_tokens=True` to recover dropped content. (`num_truncated_tokens` is unreliable — refuted.) | Invisible content loss at the cap boundary. | ❌ optional |

### 2.3 Retrieval evaluation (our biggest blind spot — currently none)
- **Golden / regression set** + ranking metrics: **RAGAS Context Precision@K**, recall@k, MRR, nDCG.
- **Answer quality**: **RAGAS Faithfulness** = supported-claims/total-claims (gate ~0.90; ~0.95 high-stakes) + **Answer Relevancy**.
- **Evaluate retrieval AND generation as two separate targets** (Auepora survey) plus end-to-end.
- **Always benchmark vector vs a BM25 baseline** (BEIR): dense often loses out-of-distribution, *especially with a generic embedder like bge-base*. This is the empirical test for whether "hybrid" actually helps. ✅ now implemented: `make eval` (`maintenance/eval_retrieval.py`) reports recall@1/3/5/10 for vector vs FTS(BM25) vs hybrid on `tests/golden/queries.yaml`, with a ship-gate verdict.
- **BEIR** for OOD generalization; **ARES/RAGAS** to keep eval cheap (ARES: ~150 labels + synthetic data → affordable per-change gate).

### 2.4 Data-store integrity
- **Idempotent / incremental**: content+metadata hash + a **stable per-document source id**; a no-op re-ingest must produce **zero spurious writes**. ✅ `test_noop_reingest_writes_zero`.
- **Orphan/stale prevention**: incremental cleanup deletes rows sharing the new docs' source id → a missing/unstable id leaves orphans (rename/delete) or risks over-deletion. ✅ orphan reconcile (repo scoped by `repo_url`; clusterio store-wide) + `test_orphan_rows_removed_on_delete`.
- **No destructive overwrite** ✅ `ensure_stores` extracts only missing stores (`test_ensure_stores_does_not_overwrite_existing`); **cross-platform path consistency** (Windows `\` vs POSIX `/`, case-folding) ✅ `to_posix` + `test_repo_file_paths_are_posix`.
- *(Pattern sourced to LangChain's Indexing API; LanceDB-native equivalents are an open question — see §6.)*

### 2.5 Gate design
- **Cheap, deterministic, fail-closed pre-commit dry-run**: structural / coverage / token / metadata / idempotency checks on the full corpus, **no embed/write**. (We have `--dry-run`; broaden its assertions.)
- **Thresholded eval-set scoring** as the pre-ship quality gate (faithfulness, recall@k vs BM25).
- Pin LLM-judge versions; use tolerance bands (judges are non-deterministic).

---

## 3. Open defect register (from the xhigh multi-agent review)

Deduped across three review passes (133 agents). Severity, mechanism, the gate
from §2 that would catch it, and whether fixing it requires re-ingesting a store.

### 3.1 🔴 Wrong results / corrupt stored data (fix → re-ingest) — ✅ ALL FIXED + re-ingested
1. **clusterio store-wide dedup collapses byte-identical files across plugins** — `normalize_chunks` dedups by content only over the whole corpus; distinct `file_path`s (e.g. 8 identical `tsconfig.json`, bare re-export `index.ts`) collapse to one → unsearchable, non-deterministic. *Gate: 2.1 dedup-distinctness.* Fix: dedup **per-file** (as the repo ingester already does).
2. **repo `file_path` uses `\` on Windows** (clusterio normalizes to `/`) — non-portable, different vectors per OS; the deployed store has this. *Gate: 2.1 metadata.* Fix: normalize to `/`.
3. **`repo_url` = bare basename + `.replace(".git","")`** — strips every `.git` (`octocat.github.io`→`octocathub.io`); two repos with the same trailing name collide in the shared store. *Gate: 2.1/2.4 stable key.* Fix: `removesuffix(".git")` + key by owner/repo.
4. **`.lua` never AST-chunked in clusterio** (hardcoded `'typescript'`); **`.js/.jsx` text-chunked in repo** but AST'd in clusterio — inconsistent, lost declaration metadata. *Gate: 2.1 AST boundary.*
5. **chunk_code 50% coverage gate miscalibrated** — `body` counts imports/blanks, `covered` counts only declaration chars, so import-heavy files text-chunk even when the AST captured every function. *Gate: 2.1 coverage.*

### 3.2 🔴 Wrong results (code-only, no re-ingest) — ✅ ALL FIXED
6. **`LIKE` metacharacters unescaped** (`server.py` plugin & `repo_url` filters) — verified `LIKE '%%'` matched all 3,455 rows; `_` matched `/`. Fix: escape `%`/`_`/`\` + `ESCAPE`.
7. **`ensure_databases` extractall overwrites existing local stores** if any one store is missing — silent loss of local ingest work. Fix: extract-missing / skip-if-exists.

### 3.3 🟠 Robustness — ✅ 8–10 fixed; ⚠️ 11 deferred (pre-existing)
8. **delete-before-bulk-skip data loss** + **orphan rows on rename/delete** (repo & clusterio) — incremental only deletes files it re-walks; a file growing >400 chunks loses its old rows. *Gate: 2.4 idempotency/orphans.*
9. **strict-chunks fails open + prefix not budgeted vs 512** — repo writes during the walk then audits after; the `File:…Code:…` prefix isn't counted against the cap (held in practice at max 439, not guaranteed for long paths). *Gate: 2.2 token cap incl. prefix.*
10. **clusterio non-code `decode('utf8')` without `errors='replace'`** — a non-UTF-8 byte drops the file (and aborts the whole run under `--strict`/`--dry-run`).
11. **wiki pagination `resp.json()` unguarded** — a transient 429/503 (non-JSON body) aborts the entire ingest.

### 3.4 🟡 Minor / docs / pre-existing
- ✅ **FTS built every run but never queried** → FIXED: `server.hybrid_search` queries it (RRF over FTS+vector, vector fallback); benchmarked via `make eval` (§2.3).
- ✅ off-by-2 fixed (`EMBED_MAX_TOKENS = 510`: bge adds CLS+SEP) · ✅ `node_type` docstring now lists the real vocab (`NODE_TYPES`) · `make test` non-strict vs CI strict · **wiki `/` filter drops legit English subpages** (pre-existing, deferred) · factorio orphans rows if `VERSIONS_TO_SCRAPE` edited (latent, deferred) · forum non-atomic delete (store unused, deferred) · **release has no DB asset / no CI regenerates it** (fresh installs 404 until manual `make deploy-dbs`).

---

## 4. Design decisions & rationale (so they aren't re-litigated)

- **5 distinct-schema stores** (`factorio` docs, `wiki`, `clusterio` TS, `forum`, generic `repo`), each its own ingest script sharing `ingest/common.py`. `repo_lancedb` is the generalization of the old per-mod ingester; **`mod_lancedb` retired** (its data is a search target served via `search_github_code`).
- **Keep Lua `table_constructor` chunks** — verified `server.py` serves their `content`, so vanilla/mod prototype data (recipes/entities) is a primary retrieval target. The explosion was *duplication*, not tables; the fix was top-level capture + recursive split, **not** dropping tables (a tempting wrong turn the research refuted).
- **Token-correct sizing, not character proxy** — code tokenizes ~2–4× denser than prose; a char cap lets 600–1500-token chunks pass and silently truncate at 512. Measure with the real tokenizer.
- **Recursive AST split** (cAST): capture top-level declarations; split an oversized node by its children; line/char-window only at a leaf. Coverage-fallback to text when the AST covers too little (note: §3.1#5 — the 50% threshold is miscalibrated).
- **GPU torch via `make sync`** — `uv` resolves statically (can't probe hardware) and a uv `--extra` pollutes the default lock; for a published package torch must stay a CPU base dep. So `make sync` detects `nvidia-smi` and installs the CUDA wheel locally only (PyPI/Docker/CI stay CPU-lean). Plus a runtime **GPU-mismatch alert** (`gpu_torch_warning`): warns when a GPU is present but torch can't use it.
- **Manual build + deploy** — `data/` is gitignored; build locally (`make ingest-all`), finalize (`make compact`), ship the 5-store `factorio_lancedb.zip` via `make deploy-dbs`. The name is load-bearing: `server.ensure_databases()` downloads exactly that asset.

---

## 5. Measure-once dry-run protocol

`--dry-run` (or `FACTORIO_MCP_DRY_RUN=1`) runs the **real** fetch/clone/chunk +
token auditor with **no embed/write**, over the **full corpus**, fail-closed
(implies strict). Iterate the spec until clean, then do exactly **one** real
rebuild → `compact` → deploy.

**Current ready-to-cut thresholds:** 0 over-512-token · 0 explosion · 0
non-empty-zero-chunk · 0 pure-dup-FAIL · FTS on all 5 · schema final.

**Thresholds to ADD** (from §2, so the gate's coverage = "correct"):
- coverage round-trip lossless per file (§2.1)
- dedup never drops a distinct `file_path` (§2.1)
- all `file_path` POSIX-normalized; `repo_url` well-formed/unique; `node_type` ∈ documented set (§2.1)
- token cap measured on **prefix+content** (§2.2)
- a **no-op re-ingest** writes zero rows; rename/delete leaves no orphans (§2.4)
- (later) a small **golden retrieval set** scored vs BM25 (§2.3)

---

## 6. Caveats & open questions

- Numeric thresholds (faithfulness 0.90/0.95) are **practitioner guidance — calibrate on your own golden set**, and code-retrieval may need different cutoffs than QA.
- RAGAS/ARES use **LLM judges → non-deterministic**: pin judge versions, use tolerance bands.
- The dedup finding rests on a weaker single-author source (2-1 vote); the strong "all byte-exact impls are identical" claim was **refuted** — so test the normalize-then-hash path explicitly.
- "BM25 beats dense" is 2021-era (BEIR); modern instruction-tuned embedders often beat it — durable takeaway is **validate, don't assume** (reinforced here by generic bge-base).
- Data-store patterns are sourced to **LangChain** (one vendor); **LanceDB-native** idempotency/orphan/no-overwrite mechanisms are an open question to nail down.
- None of the research was validated against this repo's actual LanceDB code — it defines *what* to measure, not that we implement it.

## 7. Sources (primary)
- cAST — structural code chunking, EMNLP 2025: https://arxiv.org/abs/2506.15655
- BEIR — zero-shot IR benchmark, NeurIPS 2021: https://arxiv.org/abs/2104.08663
- ARES — automated RAG eval, NAACL 2024: https://arxiv.org/abs/2311.09476
- Auepora — RAG evaluation survey: https://arxiv.org/abs/2405.07437
- RAGAS metrics (Faithfulness, Context Precision@K, Answer Relevancy): https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/
- sentence-transformers truncation behavior: https://sbert.net/examples/sentence_transformer/applications/computing-embeddings/README.html
- HF tokenizer (truncation default / overflowing tokens): https://huggingface.co/docs/transformers/main_classes/tokenizer
- BAAI/bge-base-en-v1.5 (512 cap): https://huggingface.co/BAAI/bge-base-en-v1.5
- LangChain Indexing (idempotent ingestion, source id): https://www.langchain.com/blog/syncing-data-sources-to-vector-stores
