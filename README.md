# Factorio AI Tools (MCP Server)

A lightning-fast, hybrid-search Vector Database and Model Context Protocol (MCP) server designed to give LLMs absolute expertise over Factorio modding and Clusterio plugin development.

## Architecture

This project consists of 4 main components:
1. **Factorio Docs Ingestion (`ingest_factorio.py`)**: Scrapes the official Lua API documentation and Data Phase Prototypes across multiple versions (e.g. `1.1.110` and `latest`).
2. **Clusterio Codebase Ingestion (`ingest_clusterio.py`)**: Uses AST (Abstract Syntax Tree) parsing to semantically chunk the massive Node.js/TypeScript Clusterio plugin architecture.
3. **Factorio Wiki Ingestion (`ingest_wiki.py`)**: Scrapes the official Factorio Wiki via the MediaWiki API, exclusively extracting English wikitext for gameplay mechanics, ratios, and formulas.
4. **FastMCP Server (`server.py`)**: The bridge that connects the underlying LanceDB vector databases to an LLM via the standard Model Context Protocol.

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
- `decode_factorio_blueprint`: Convert Factorio blueprint strings (e.g. `0eNq...`) into easily readable/editable JSON.
- `encode_factorio_blueprint`: Compress generated JSON back into an importable Factorio blueprint string.
- `get_mcp_version_info`: Self-diagnostics tool to verify the currently loaded database versions.
