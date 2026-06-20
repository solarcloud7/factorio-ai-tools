# Factorio AI Tools (MCP Server)

A lightning-fast, hybrid-search Vector Database and Model Context Protocol (MCP) server designed to give LLMs absolute expertise over Factorio modding and Clusterio plugin development.

## Architecture

This project consists of 4 main components:
1. **Factorio Docs Ingestion (`ingest_factorio.py`)**: Scrapes the official Lua API documentation and Data Phase Prototypes across multiple versions (e.g. `1.1.110` and `latest`).
2. **Clusterio Codebase Ingestion (`ingest_clusterio.py`)**: Uses AST (Abstract Syntax Tree) parsing to semantically chunk the massive Node.js/TypeScript Clusterio plugin architecture.
3. **Factorio Wiki Ingestion (`ingest_wiki.py`)**: Scrapes the official Factorio Wiki via the MediaWiki API, exclusively extracting English wikitext for gameplay mechanics, ratios, and formulas.
4. **GitHub Mod Ingestion (`ingest_github_mod.py`)**: A generalized pipeline that clones, AST-parses (via `tree-sitter-lua`), and incrementally hashes any GitHub Mod codebase (e.g., Maraxsis) into a semantic `mod_lancedb` index.
5. **FastMCP Server (`server.py`)**: The bridge that connects the underlying LanceDB vector databases to an LLM via the standard Model Context Protocol.

## Setup & Usage

There are two primary ways to install and use this MCP server locally with Claude Desktop:

### Method 1: Standalone Windows Executable (Easiest)
For standard Windows users, you don't need Python or any dependencies installed.
1. Download the latest `factorio-ai-tools-windows.zip` from the [GitHub Releases](https://github.com/solarcloud7/factorio-ai-tools/releases) page.
2. Extract the `.zip` file into a folder on your computer (e.g. `C:\FactorioMCP`).
3. Add the following to your Claude Desktop config (`%APPDATA%\Claude\claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "factorio-ai-tools": {
      "command": "C:\\FactorioMCP\\factorio-ai-tools.exe",
      "args": []
    }
  }
}
```

### Method 2: Docker (Best for Mac/Linux/Devs)
If you have Docker Desktop installed, you can simply pull the pre-packaged container natively.
1. Add the following to your Claude Desktop config (no download required!):
```json
{
  "mcpServers": {
    "factorio-ai-tools": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "ghcr.io/solarcloud7/factorio-ai-tools:latest"]
    }
  }
}
```

---

### Manual Developer Setup
If you wish to run the python scripts manually or ingest custom codebases:
1. Create a python virtual environment: `python -m venv venv` and activate it.
2. Run `pip install -r requirements.txt`.
3. *(Optional)* Run the ingestion scripts (`python ingest_factorio.py`, etc.) to rebuild the LanceDB tables.
4. *(Optional)* Ingest a specific GitHub Mod:
   ```powershell
   python ingest_github_mod.py --repo-url https://github.com/notnotmelon/maraxsis
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

## Usage

Hook this server into Claude Desktop (or any other MCP client) by adding the following to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "factorio-ai-tools": {
      "command": "C:\\path\\to\\factorio-ai-tools\\venv\\Scripts\\python.exe",
      "args": [
        "C:\\path\\to\\factorio-ai-tools\\server.py"
      ]
    }
  }
}
```

## Tools Included

- `search_factorio_docs`: Look up Lua Runtime API methods, concepts, events, and Data Phase prototypes. Supports version filtering (`1.1.110` vs `latest`).
- `search_clusterio_code`: Semantically search the Clusterio Node.js architecture.
- `search_factorio_wiki`: Access game mechanics, ratios, and fluid mechanics straight from the Wiki.
- `search_mod_code`: Semantically search through specific downloaded GitHub mods (e.g., `maraxsis`) to read their Lua codebase.
- `decode_factorio_blueprint`: Convert Factorio blueprint strings (e.g. `0eNq...`) into easily readable/editable JSON.
- `encode_factorio_blueprint`: Compress generated JSON back into an importable Factorio blueprint string.
- `factorio_mod_portal_analyzer`: Scrape and summarize the Factorio Mod Portal for any given mod to retrieve dependencies and release versions.
- `factorio_log_inspector`: Autonomously sweep your OS for `factorio-current.log` and extract crash stack traces.
- `get_mcp_version_info`: Self-diagnostics tool to verify the currently loaded database versions.
