# Factorio AI Tools (MCP Server)

A lightning-fast, hybrid-search Vector Database and Model Context Protocol (MCP) server designed to give LLMs absolute expertise over Factorio modding and Clusterio plugin development.

## Architecture

This project consists of 4 main components:
1. **Factorio Docs Ingestion (`ingest_factorio.py`)**: Scrapes the official Lua API documentation and Data Phase Prototypes across multiple versions (e.g. `1.1.110` and `latest`).
2. **Clusterio Codebase Ingestion (`ingest_clusterio.py`)**: Uses AST (Abstract Syntax Tree) parsing to semantically chunk the massive Node.js/TypeScript Clusterio plugin architecture.
3. **Factorio Wiki Ingestion (`ingest_wiki.py`)**: Scrapes the official Factorio Wiki via the MediaWiki API, exclusively extracting English wikitext for gameplay mechanics, ratios, and formulas.
4. **GitHub Mod Ingestion (`ingest_github_mod.py`)**: A generalized pipeline that clones, AST-parses (via `tree-sitter-lua`), and incrementally hashes any GitHub Mod codebase (e.g., Maraxsis) into a semantic `mod_lancedb` index.
5. **FastMCP Server (`server.py`)**: The bridge that connects the underlying LanceDB vector databases to an LLM via the standard Model Context Protocol.

## Setup

1. Create a python virtual environment:
   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   ```

2. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```

3. Build the databases. Run each script at least once to populate the LanceDB indexes:
   ```powershell
   python ingest_factorio.py
   python ingest_clusterio.py
   python ingest_wiki.py
   ```

4. **(Optional) Ingest a specific GitHub Mod:** If you want your AI to natively understand a complex mod (like Krastorio 2 or Maraxsis), you can point the GitHub ingestor at it.
   ```powershell
   python ingest_github_mod.py --repo-url https://github.com/notnotmelon/maraxsis
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
