# Architecture

The system has two halves that meet only at the on-disk LanceDB stores and the
shared embedding contract: **ingestion** builds the stores offline, and the
**MCP server** queries them at runtime. They never call each other.

## Contents

- [Data flow](#data-flow)
- [Modules](#modules)
- [Shared contracts](#shared-contracts)
- [Build and deploy](#build-and-deploy)

## Data flow

```
external sources                ingest scripts                data/<store>_lancedb        server.py
(lua-api docs, wiki API,   ──►  scrape/parse ──► chunk    ──►  embed (bge-base, 768-dim)  ──►  hybrid_search
 forum topics, Clusterio,       (common.py: tree-sitter        L2-normalized, written to       (RRF over FTS +
 GitHub repos)                   AST + text fallback)          a per-store LanceDB table)      vector) → MCP tool
```

Each ingest script reads one kind of source, chunks it, embeds the chunks, and
writes them to its own store under `data/`. The server opens those stores and
serves them through MCP tools. The only things both halves share are
[ingest/common.py](../src/factorio_ai_tools/ingest/common.py), the embedding
model, and the on-disk store.

## Modules

### Ingestion — [src/factorio_ai_tools/ingest/](../src/factorio_ai_tools/ingest)
| Module | Role |
|---|---|
| [common.py](../src/factorio_ai_tools/ingest/common.py) | Shared contract for every ingester: data-dir resolution, embedding model load + `embed()`, SHA-256 `get_hash`, `ensure_table` schema guard, tree-sitter chunking (`chunk_code`, `extract_ast_chunks`, `normalize_chunks`), the `ChunkAuditor` health gate, routing/key/SQL helpers (`kind_for_ext`, `repo_slug_from_url`, `to_posix`, `like_filter`, `NODE_TYPES`), `ensure_stores` bootstrap, and `hybrid_search`. |
| [ingest_factorio.py](../src/factorio_ai_tools/ingest/ingest_factorio.py) | Fetches `runtime-api.json` + `prototype-api.json` for the tracked versions and parses them into doc chunks. |
| [ingest_wiki.py](../src/factorio_ai_tools/ingest/ingest_wiki.py) | Pulls the Factorio wiki via the MediaWiki API. |
| [ingest_forum.py](../src/factorio_ai_tools/ingest/ingest_forum.py) | Ingests curated forum topics listed in `forum_links.txt`. |
| [ingest_clusterio.py](../src/factorio_ai_tools/ingest/ingest_clusterio.py) | AST-chunks a Clusterio TypeScript/Lua checkout. |
| [ingest_github_repo.py](../src/factorio_ai_tools/ingest/ingest_github_repo.py) | Generic GitHub-repo ingester (TypeScript/JS + Lua) into the shared `repo` store; one store holds many repos. |

### Server — [src/factorio_ai_tools/server.py](../src/factorio_ai_tools/server.py)
A FastMCP server. It opens each store in its own try/except (a missing store
degrades only the affected tool), loads the embedding model once, and exposes the
search/utility tools and the expert prompt documented in [tools.md](tools.md).
Search routes through `common.hybrid_search`. Runs over stdio by default, or SSE
with `--sse`.

### Maintenance — [maintenance/](../maintenance)
| Script | Role |
|---|---|
| [compact_lancedb.py](../maintenance/compact_lancedb.py) | Compacts every `data/*_lancedb` store (collapses LanceDB version history); `--check` is a read-only guard. |
| [eval_retrieval.py](../maintenance/eval_retrieval.py) | Scores recall@k for vector vs FTS vs hybrid on the golden set ([tests/golden/queries.yaml](../tests/golden/queries.yaml)). |

### Tests — [tests/](../tests)
Offline suite (fake embedder + tokenizer stub) covering the chunking helpers, the
`ChunkAuditor`, each ingester, the server contract, and the pipeline invariants in
[test_pipeline_invariants.py](../tests/test_pipeline_invariants.py). Run with
`make test`.

## Shared contracts

Every ingester and the server must agree on these or search breaks silently:

- **Embedding model** `BAAI/bge-base-en-v1.5` (override with `EMBEDDING_MODEL`),
  device CUDA→CPU auto. Vectors are **768-dim** (`common.EMBEDDING_DIM`) and
  **L2-normalized**; `embed()` asserts the dimension.
- **Token-correct chunk sizing** measured with the real tokenizer
  (`count_tokens`); the embedded text (context prefix + content) stays within the
  embedder's cap (`EMBED_MAX_TOKENS = 510`).
- **Incremental ingestion** keyed on a SHA-256 `content_hash` per source unit:
  unchanged content is skipped, changed content is deleted-then-re-added, and a
  source removed from disk is reconciled away (no orphan rows).
- **Schema guard** `ensure_table` drops + recreates a table whose columns are a
  stale subset of the target schema, so a schema change forces a full re-ingest of
  that store.
- **Data-dir resolution** `common.get_data_dir()` must resolve to the same place
  as the server's `DATA_DIR` (local `data/` when present, else the per-user dir).

## Build and deploy

`data/` is gitignored — the stores are large, regenerable build artifacts shipped
as a GitHub Release asset, not committed. Build locally with the `make` targets,
then ship:

| Target | Action |
|---|---|
| `make sync` | Install deps; swap in the CUDA torch wheel if an NVIDIA GPU is present. |
| `make ingest-all` | Build/refresh all five stores (each is incremental/idempotent). |
| `make compact` | Compact every store (prune version history). |
| `make package-dbs` | Zip the five stores into `factorio_lancedb.zip`. |
| `make deploy-dbs` | `compact` + `package-dbs` + upload the zip to the latest release. |
| `make test` | Run the offline test suite. |
| `make eval` | Retrieval recall@k (vector vs FTS vs hybrid) on the golden set. |
| `make smoke` | Release smoke test: install the published wheel into an isolated venv, force a fresh DB download, and assert every tool ([maintenance/smoke_release.py](../maintenance/smoke_release.py)). |
| `make mcp` | Start the MCP server. |

The asset name `factorio_lancedb.zip` is load-bearing: `server.ensure_databases()`
downloads exactly that from `releases/latest/download/` when a store is missing,
and `ensure_stores` extracts only the missing stores (it never clobbers an existing
`data/`).
