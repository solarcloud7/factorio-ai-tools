# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A hybrid-search RAG system and FastMCP server that gives LLMs expert knowledge of Factorio modding and Clusterio plugin development. Five ingestion scripts scrape/parse external sources into local LanceDB vector stores; `server.py` exposes those stores to MCP clients (Claude Desktop, etc.) as search tools.

## Commands

```powershell
# One-time setup
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Build/refresh the vector databases (each is idempotent — safe to re-run).
# Scripts live in ingest/; every store is written under data/.
python ingest/ingest_factorio.py      # -> data/factorio_lancedb  (Lua API + prototype docs, multiple versions)
python ingest/ingest_clusterio.py     # -> data/clusterio_lancedb (set CLUSTERIO_REPO; defaults to ./clusterio)
python ingest/ingest_wiki.py          # -> data/wiki_lancedb      (full Factorio wiki via MediaWiki API)
python ingest/ingest_forum.py         # -> data/forum_lancedb     (curated topics from forum_links.txt)
python ingest/ingest_github_mod.py --repo-url https://github.com/notnotmelon/maraxsis   # -> data/mod_lancedb

# Compact/prune all data/*_lancedb stores (collapses LanceDB version history).
# Run before merging data changes to main; --check is a read-only guard.
python maintenance/compact_lancedb.py
python maintenance/compact_lancedb.py --check

# Run the MCP server (stdio transport)
python server.py
```

There is no test suite, linter, or build step beyond the above. `smithery.yaml` defines the Smithery deployment (build = pip install + the three core ingest scripts; run = `python server.py`).

To test a tool manually, import `server.py` in a REPL — the `@mcp.tool()` functions are plain callables. Note that importing `server.py` eagerly loads the SentenceTransformer model and opens all five LanceDB connections.

## Architecture

**Ingestion → LanceDB → MCP server.** Every database is built offline by an ingest script and queried at runtime by the server. The two halves only share the embedding model and the on-disk store; they never call each other.

**Shared embedding contract (must stay consistent across all scripts and the server):**
- Model: `BAAI/bge-base-en-v1.5`, overridable via `EMBEDDING_MODEL` env var. Device auto-selects CUDA→CPU.
- Vectors are **768-dim** and **L2-normalized** (`normalize_embeddings=True`). Any new ingest script or schema must match this dimension and normalization, or search breaks silently.

**Incremental / idempotent ingestion.** Every script hashes source content with SHA-256 (`get_hash`/`hashlib.sha256`) and stores it as `content_hash`. On re-run it compares hashes per source unit (per URL, per file, per wiki page, per forum topic) and **skips unchanged content; deletes-then-re-adds changed content.** This is why re-running an ingest script is cheap and safe.

**Schema migration guard.** Each ingest script checks whether the existing table has the current columns (e.g. `if "content_hash" not in table.schema.names`) and **drops + recreates the whole table** if the schema is stale. Changing a `LanceModel`/pyarrow schema therefore forces a full re-ingest of that store — account for that when editing schemas.

**Code-aware chunking via Tree-sitter.** `ingest_clusterio.py` (TypeScript) and `ingest_github_mod.py` (Lua) parse files into AST nodes (classes, functions, methods, interfaces/tables) using a Tree-sitter `Query`, and store each node as a chunk with `node_type`/`node_name`. Non-code files fall back to fixed-size sliding-window text chunking (`extract_text_chunks`, 1500 chars / 200 overlap). The doc/wiki/forum scripts use plain text chunking only.

**Per-store table names and key columns** (the server opens these by exact name; all stores live under `data/`):
- `data/factorio_lancedb` → table `docs`: `text`, `class_name`, `version`, `url`, `node_type`. Holds **multiple Factorio versions** (`["1.1.110", "latest"]`); search filters by `version`. Writes `version.txt`.
- `data/clusterio_lancedb` → table `codebase`: `content`, `file_path`, `node_type`, `node_name`. Writes `version.txt` from the repo's `package.json`.
- `data/wiki_lancedb` → table `docs`: `text`, `title`, `url`.
- `data/forum_lancedb` → table `forum`: `content`, `class_name` (= topic title), `file_path` (= URL), `version`.
- `data/mod_lancedb` → table `codebase`: `content`, `repo_url`, `file_path`, `node_type`, `node_name`. One store holds **multiple mods**; search filters by `mod_name` via `repo_url LIKE`.

**Server resilience.** `server.py` opens each table in its own try/except and sets the handle to `None` on failure, so a missing database degrades only the affected tool (which returns a "run ingest_X.py first" error) rather than crashing the server. Search tools accept a **list of queries** (batched encode) and clamp `limit` to 1–20.

**Non-search tools** in `server.py` are self-contained (no DB): `decode_factorio_blueprint`/`encode_factorio_blueprint` (base64+zlib, version byte `0`, 10 MB decompress guard), `factorio_mod_portal_analyzer` (mods.factorio.com API), `factorio_log_inspector` (OS-aware path to `factorio-current.log`), `get_mcp_version_info` (reads the `version.txt` files).

## Conventions (from `.agents/AGENTS.md`)

- **Windows console printing:** never `print()` raw dynamic/scraped strings — PowerShell's default encoding throws `UnicodeEncodeError` on en-dashes/emojis. Wrap dynamic output: `print(text.encode('ascii', 'replace').decode('ascii'))`. The ingest scripts already do this; preserve it.
- **Committing:** always run `git status` to verify nothing modified/untracked is left behind before telling the user changes are pushed. Stage deliberately.
- **SQL-string safety:** LanceDB `.where()` clauses are built with f-strings, so user/dynamic values are escaped by doubling single quotes (`value.replace("'", "''")`). Keep this when adding filters.

## Data files & git

- The `data/*_lancedb/` stores are committed to the repo and marked `binary` in `.gitattributes` (no line-ending conversion). `.mod_temp/` (clone scratch for `ingest_github_mod.py`, at repo root), `venv/`, `__pycache__/`, `.claude/`, and `.env` are gitignored.
- `.gitattributes` enforces LF line endings for all text/source files regardless of `core.autocrlf`.
- `forum_links.txt` is the curated input list for `ingest_forum.py` (one URL per line, `#` comments allowed).
- **LanceDB hygiene.** Stores are append-only and never self-prune, so each ingest run accumulates versions/fragments. Let that history grow on feature branches (a PR diff then shows what data changed), but run `python maintenance/compact_lancedb.py` before merging to `main` to collapse it (`Table.optimize()` with `cleanup_older_than=0`). `maintenance/hooks/pre-push` is an opt-in guard (`git config core.hooksPath maintenance/hooks`) that blocks pushes to `main` while any store is uncompacted, via `compact_lancedb.py --check`.
