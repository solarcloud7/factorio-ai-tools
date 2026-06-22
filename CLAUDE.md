# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A hybrid-search RAG system and FastMCP server that gives LLMs expert knowledge of Factorio modding and Clusterio plugin development. Five ingest scripts (under `src/factorio_ai_tools/ingest/`) scrape/parse external sources into five local LanceDB vector stores and share `common.py` for the embedding/hashing/tree-sitter contract; `server.py` exposes those stores to MCP clients (Claude Desktop, etc.) as search tools.

## Commands

```powershell
# One-time setup (uv-managed; the code is a src/ package). The legacy
# `python -m venv venv; pip install -r requirements.txt` still works —
# requirements.txt is a pinned mirror — but uv + pyproject.toml are canonical.
uv sync

# Build/refresh the vector stores (each is incremental/idempotent — safe to re-run).
# Every store is written under data/. Run one at a time, or all via the Makefile.
uv run python -m factorio_ai_tools.ingest.ingest_factorio      # -> data/factorio_lancedb  (Lua API + prototype docs, versions 1.1.110 + latest)
uv run python -m factorio_ai_tools.ingest.ingest_wiki          # -> data/wiki_lancedb      (full Factorio wiki via MediaWiki API)
uv run python -m factorio_ai_tools.ingest.ingest_forum         # -> data/forum_lancedb     (curated topics from forum_links.txt)
uv run python -m factorio_ai_tools.ingest.ingest_clusterio     # -> data/clusterio_lancedb (set CLUSTERIO_REPO; defaults to ./clusterio)
uv run python -m factorio_ai_tools.ingest.ingest_github_repo --repo-url https://github.com/owner/repo   # -> data/repo_lancedb (any GitHub repo)

# Or build all five at once (clones Clusterio + the configured generic repos as needed):
make ingest-all

# Compact/prune all data/*_lancedb stores (collapses LanceDB version history).
# Canonical compactor; --check is a read-only guard. Runs as part of `make deploy-dbs`.
uv run python maintenance/compact_lancedb.py
uv run python maintenance/compact_lancedb.py --check

# Run the MCP server (stdio transport)
uv run factorio-ai-tools          # or: uv run python -m factorio_ai_tools.server
```

There is no test suite, linter, or build step beyond the above. `smithery.yaml` defines the Smithery deployment (build = install deps + run the ingest scripts; run = the server).

To test a tool manually, import `factorio_ai_tools.server` in a REPL — the `@mcp.tool()` functions are plain callables. Note that importing it eagerly loads the SentenceTransformer model and opens all five LanceDB connections (factorio / clusterio / wiki / forum / repo).

## Architecture

**Ingestion → LanceDB → MCP server.** Every store is built offline by an ingest script and queried at runtime by the server. The two halves only share `common.py`, the embedding model, and the on-disk store; they never call each other.

**Shared ingest module (`src/factorio_ai_tools/ingest/common.py`).** All five scripts import `common` for: `get_data_dir()` (local `data/` vs per-user dir — **must resolve to the same place as `server.py`'s `REPO_ROOT`**), `load_embedder()`/`embed()`, `get_hash()`, `safe_print()`, `ensure_table()` (the schema-migration guard), and code-aware chunking helpers. Keep new scripts on `common` so the contract stays consistent.

**Shared embedding contract (must stay consistent across all scripts and the server):**
- Model: `BAAI/bge-base-en-v1.5`, overridable via `EMBEDDING_MODEL` env var. Device auto-selects CUDA→CPU.
- Vectors are **768-dim** (`common.EMBEDDING_DIM`) and **L2-normalized** (`normalize_embeddings=True`). Any new ingest script or schema must match this dimension and normalization, or search breaks silently.

**Incremental / idempotent ingestion.** Every script (now including `ingest_github_repo.py`) hashes source content with SHA-256 (`common.get_hash`) and stores it as `content_hash`. On re-run it compares hashes per source unit (per URL, per file, per wiki page, per forum topic, per repo file) and **skips unchanged content; deletes-then-re-adds changed content.** This is why re-running an ingest script is cheap and safe — and why re-running no longer duplicates rows.

**Schema migration guard.** `common.ensure_table(db, name, schema)` opens a table and **drops + recreates it** if its columns are a stale subset of the target schema (works for both `LanceModel` and pyarrow schemas). Changing a schema therefore forces a full re-ingest of that store — account for that when editing schemas.

**Code-aware chunking via Tree-sitter.** `common.extract_ast_chunks(src_bytes, kind, include_comments=)` parses files into AST nodes (classes, functions, methods, interfaces / tables) using the modern `Parser(lang)` / `Query` / `QueryCursor` API (tree-sitter ≥ 0.22; **never** `parser.set_language`). `ingest_clusterio.py` uses it for TypeScript/JS (with preceding comments); `ingest_github_repo.py` uses it for TypeScript/JS and Lua. Non-code files fall back to sliding-window text chunking (`common.text_chunks_by_char`, 1500/200, for prose; `common.text_chunks_by_line`, 50/10, for the generic repo ingester). The doc/wiki/forum scripts use text chunking only.

**Per-store table names and key columns** (the server opens these by exact name; all stores live under `data/`):
- `data/factorio_lancedb` → table `docs`: `text`, `class_name`, `version`, `url`, `node_type`, `returns`, `source_url`, `content_hash`. Holds **both versions** `["1.1.110", "latest"]`; `search_factorio_docs` filters by `version` (default `latest`, so `latest` rows must exist) and optional `class_name`. FTS index on `text`. Writes `version.txt`.
- `data/clusterio_lancedb` → table `codebase`: `content`, `file_path`, `node_type`, `node_name`, `content_hash`. Writes `version.txt` from the repo's `package.json`.
- `data/wiki_lancedb` → table `docs`: `text`, `title`, `url`, `content_hash`. FTS index on `text`.
- `data/forum_lancedb` → table `forum`: `content`, `class_name` (= topic title), `file_path` (= URL), `version`, `id`, `content_hash`.
- `data/repo_lancedb` → table `codebase`: `content`, `repo_url` (= repo basename), `file_path`, `node_type`, `node_name`, `content_hash`. One store holds **multiple repos**; `search_github_code` filters by `repo_name` via `repo_url LIKE`. Built by the generic `ingest_github_repo.py`, which **supersedes the old per-mod `mod_lancedb`/`ingest_github_mod.py`** (retired — ingest any mod with `--repo-url`).

**Server resilience.** `server.py` opens each table in its own try/except and sets the handle to `None` on failure, so a missing store degrades only the affected tool (which returns a "run ingest_X first" error) rather than crashing the server. Search tools accept a **list of queries** (batched encode) and clamp `limit` to 1–20.

**Non-search tools** in `server.py` are self-contained (no DB): `decode_factorio_blueprint`/`encode_factorio_blueprint` (base64+zlib, version byte `0`, 10 MB decompress guard), `factorio_mod_portal_analyzer` (mods.factorio.com API), `get_mcp_version_info` (reads the `factorio_lancedb` + `clusterio_lancedb` `version.txt` files). There is also an `@mcp.prompt()`, `factorio_clusterio_expert`, that supplies the modding/Clusterio mental model. (A `factorio_log_inspector` tool existed historically but was deliberately removed in `82a5409`.)

## Conventions (from `.agents/AGENTS.md`)

- **Windows console printing:** never `print()` raw dynamic/scraped strings — PowerShell's default encoding throws `UnicodeEncodeError` on en-dashes/emojis. Use `common.safe_print(text)` (ascii-replace). The ingest scripts already do this; preserve it.
- **Committing:** always run `git status` to verify nothing modified/untracked is left behind before telling the user changes are pushed. Stage deliberately.
- **SQL-string safety:** LanceDB `.where()` clauses are built with f-strings, so user/dynamic values are escaped by doubling single quotes (`value.replace("'", "''")`). Keep this when adding filters.

## Data files & git

- The `data/` directory is **gitignored** (not committed). The LanceDB stores are large, regenerable build artifacts, so they are built locally and shipped as a GitHub Release asset instead of living in git history. `.mod_temp/` (clone scratch), `/clusterio/` (the `make ingest-clusterio` checkout), `venv/`/`.venv/`, `__pycache__/`, `.claude/`, and `.env` are also gitignored.
- `.gitattributes` enforces LF line endings for all text/source files regardless of `core.autocrlf`.
- **Manual build & deploy of the databases.** Ingestion is additive/idempotent, so iterate locally with `make ingest-all` (or individual `ingest_*` runs), then ship the final full build with `make deploy-dbs`: it runs `make compact`, zips **all five `data/*_lancedb` stores** into `factorio_lancedb.zip`, and `gh release upload`s it to the latest release. The artifact name `factorio_lancedb.zip` is load-bearing — `server.py`'s `ensure_databases()` downloads exactly that asset from `releases/latest/download/` and only short-circuits when **all five** store dirs are already present.
- **LanceDB hygiene.** Stores are append-only and never self-prune, so each ingest run accumulates versions/fragments. `make compact` runs `maintenance/compact_lancedb.py` (the canonical compactor: `Table.optimize()` across every `data/*_lancedb`, prune to latest) and is part of `make deploy-dbs`. `maintenance/hooks/pre-push` is an opt-in guard (`git config core.hooksPath maintenance/hooks`) that blocks pushes to `main` while any store is uncompacted, via `compact_lancedb.py --check`.
