<div align="center">
  <img src="docs/assets/factorio-ai-tools.png" alt="Factorio AI Tools Icon" width="200"/>
  <br/>
  <a href="https://pypi.org/project/factorio-ai-tools/"><img src="https://img.shields.io/pypi/v/factorio-ai-tools" alt="PyPI - Version"/></a>
  <a href="https://github.com/solarcloud7/factorio-ai-tools/releases"><img src="https://img.shields.io/github/v/release/solarcloud7/factorio-ai-tools" alt="GitHub Release"/></a>
</div>

# Factorio AI Tools (MCP Server)

A lightning-fast, hybrid-search Vector Database and Model Context Protocol (MCP) server designed to give LLMs absolute expertise over Factorio modding and Clusterio plugin development.

## Architecture

This project consists of five ingestion pipelines (sharing `ingest/common.py` for the embedding, hashing, and tree-sitter contract) feeding five LanceDB vector stores, plus the MCP server:
1. **Factorio Docs (`ingest_factorio.py` → `factorio_lancedb`)**: Scrapes the official Lua API documentation and Data Phase Prototypes across multiple versions (`1.1.110` and `latest`).
2. **Factorio Wiki (`ingest_wiki.py` → `wiki_lancedb`)**: Scrapes the Factorio Wiki via the MediaWiki API (English wikitext) for gameplay mechanics, ratios, and formulas.
3. **Clusterio Codebase (`ingest_clusterio.py` → `clusterio_lancedb`)**: AST-parses the Node.js/TypeScript Clusterio plugin architecture (tree-sitter).
4. **Factorio Forums (`ingest_forum.py` → `forum_lancedb`)**: Scrapes a curated list of forum topics (`forum_links.txt`) for community solutions and discussions.
5. **Generic GitHub Repos (`ingest_github_repo.py` → `repo_lancedb`)**: Clones and AST-parses (tree-sitter TypeScript/JS + Lua) any GitHub repository — base game data, libraries, or any mod — into one shared, multi-repo index.
6. **FastMCP Server (`server.py`)**: The bridge that connects the underlying LanceDB vector stores to an LLM via the standard Model Context Protocol.

For developer reference — module layout, the store schemas, the MCP tool list, and the validation playbook — see **[docs/](docs/README.md)**.

## Setup & Usage

There are two primary ways to install and use this MCP server locally with Claude Desktop (or any other MCP client):

### Method 1: Using `uvx` (Recommended)
If you have `uv` installed, this is the cleanest way to run the server. It will automatically download the package from PyPI and fetch the necessary vector databases on the first run.
Add the following to your Claude Desktop config (`%APPDATA%\Claude\claude_desktop_config.json` or `~/Library/Application Support/Claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "factorio-ai-tools": {
      "command": "uvx",
      "args": ["factorio-ai-tools"]
    }
  }
}
```

### Method 2: Docker (Pre-packaged Datasets)
If you have Docker Desktop installed, you can simply pull the pre-packaged container natively. The Docker container includes the databases inside the image, so no additional downloads are required at runtime.
```json
{
  "mcpServers": {
    "factorio-ai-tools": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "ghcr.io/solarcloud7/factorio-ai-tools:latest"]
    }
    }
  }
}
```

### Method 3: Global SSE Server (Save RAM/VRAM)
By default, standard `stdio` MCP execution spawns a completely separate Python process for *every single client connection*. Because this server uses PyTorch and `sentence-transformers`, every connection will load the embedding model again, consuming roughly ~500MB of RAM/VRAM per instance. 

If you want to use the MCP server across multiple IDEs or workspaces simultaneously without duplicating memory, you can run a single global HTTP SSE server in the background:

```powershell
uv run factorio-ai-tools --sse --port 8000
```

Then, configure your IDE or Claude client to connect to the SSE endpoint (e.g., `http://localhost:8000/sse`) instead of executing the CLI via `stdio`.

### Selective Tool Loading (Optional)
By default, the server loads all available tools. If you only want to expose specific tools to your LLM, you can use the `--enable-tools` or `--disable-tools` arguments.
For example, to *only* load the doc search and the blueprint decoder using `uvx`:
```json
      "command": "uvx",
      "args": [
        "factorio-ai-tools",
        "--enable-tools", "search_factorio_docs,decode_factorio_blueprint"
      ]
```
---

### Manual Developer Setup
If you wish to run the python scripts manually or ingest custom codebases:
1. **`make sync`** (uv-based, recommended) — installs all dependencies and **auto-selects the CUDA torch wheel when an NVIDIA GPU is present** (otherwise the CPU wheel), so ingestion embeds on the GPU. `pyproject.toml` keeps the CPU wheel as the default, so PyPI/Docker/CI stay lean; the GPU swap is local only and survives venv recreation (just re-run `make sync`). Or, without make: `uv sync` then, on a GPU box, `uv pip install --reinstall torch --index-url https://download.pytorch.org/whl/cu124`.
   - *Legacy:* `python -m venv venv && pip install -r requirements.txt` (CPU only).
3. *(Optional)* Run the ingestion scripts (`python -m factorio_ai_tools.ingest.ingest_factorio`, etc.) to rebuild the LanceDB tables.
4. *(Optional)* Ingest a specific GitHub repo or mod into the shared `repo_lancedb` index:
   ```powershell
   python -m factorio_ai_tools.ingest.ingest_github_repo --repo-url https://github.com/notnotmelon/maraxsis
   ```

## Maintenance (Database Hygiene)

LanceDB is append-only: every ingest run adds new immutable versions and small data fragments, and **nothing is garbage-collected automatically**. Re-running an ingest script grows the on-disk history (e.g. `factorio_lancedb` had 155 versions / 469 files before its first compaction). To keep the committed stores lean:

```powershell
python maintenance/compact_lancedb.py          # compact + prune every data/*_lancedb store
python maintenance/compact_lancedb.py --check   # read-only; exits non-zero if a store is uncompacted
```

This runs LanceDB's `Table.optimize()` on each store — compacting fragments, pruning old versions, and folding new rows into existing indices. Do **not** run it while the server or an ingest script is writing.

**Recommended workflow:** let the version history accumulate on feature branches so a PR diff shows exactly what data changed, then run the compaction script before merging to `main` so the committed history stays collapsed.

To enforce that automatically, opt into the bundled pre-push guard (it blocks pushes to `main` while any store is uncompacted):

```powershell
git config core.hooksPath maintenance/hooks
# or copy maintenance/hooks/pre-push into .git/hooks/
```



## Tools Included

Every tool can be turned on/off individually via `--enable-tools` / `--disable-tools` (see [Selective Tool Loading](#selective-tool-loading-optional)). All search tools accept a **list of queries** in one call and clamp `limit` to 1–20.

**Knowledge search** (one per vector store):
- `search_factorio_docs`: Look up Lua Runtime API methods, concepts, and events plus Data Phase prototypes. Filter by `class_name` and `version` (`1.1.110` vs `latest`).
- `search_factorio_wiki`: Game mechanics, ratios, fluid mechanics, and formulas straight from the Factorio Wiki.
- `search_factorio_forums`: Curated Factorio forum topics — community solutions, edge cases, and discussions.
- `search_clusterio_code`: Semantically search the Clusterio Node.js/TypeScript architecture. Filter by `node_type`.
- `search_github_code`: Search any ingested GitHub repository (base game `factorio-data`, `factorio-draftsman`, the blueprint editor, Clusterio Docker, and **any mod you ingest** via `ingest_github_repo`). Filter by `repo_name`.

**Blueprints:**
- `decode_factorio_blueprint`: Convert a Factorio blueprint string (e.g. `0eNq...`) into readable/editable JSON.
- `encode_factorio_blueprint`: Compress generated JSON back into an importable Factorio blueprint string.

**Utilities:**
- `factorio_mod_portal_analyzer`: Scrape and summarize a mod on the Factorio Mod Portal for its dependencies and release versions.
- `get_mcp_version_info`: Self-diagnostics — report the currently loaded database versions.

**Prompt:**
- `factorio_clusterio_expert`: An MCP prompt (not a tool) that primes the model with the Factorio modding phases (settings → data → control) and the Clusterio plugin architecture, and tells it which search tool to reach for.
