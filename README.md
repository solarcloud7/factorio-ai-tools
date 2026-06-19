# Factorio AI Tools

A unified Model Context Protocol (MCP) server that empowers Claude (and other LLMs) with comprehensive context for Factorio modding and server management.

This single server exposes two powerful semantic search tools:
1. **`search_factorio_docs`**: Semantic RAG search over the entire Factorio Lua API documentation.
2. **`search_clusterio_code`**: AST-aware semantic RAG search over the [Clusterio](https://github.com/clusterio/clusterio) TypeScript codebase.

## Key Technical Features
- **Tree-sitter AST Chunking**: Code boundaries are extracted directly from the syntax tree (classes, methods, functions) rather than arbitrary text splits, guaranteeing that source code context is never broken.
- **JSDoc Preservation**: Extracted code blocks automatically include their preceding JSDoc comments to feed maximum semantic context to the AI.
- **Deadlock-Safe Embeddings**: Replaces LanceDB's native PyTorch embedding registry with an explicit, synchronous Main Thread embedding pipeline. This protects Windows users from the notorious CUDA multiprocessor deadlock bug (`#3559`).
- **MD5 Hash Sync**: Updates to the local databases are near-instant because nodes are hashed prior to embedding; only new or changed nodes hit the GPU.

## Installation

There are two ways to install this MCP server into Claude Desktop: using the automated Smithery CLI, or manually installing the Python environment.

### Option 1: Quick Install via Smithery (Recommended)

You can automatically install this server and its dependencies directly into Claude Desktop using the Smithery CLI:

```bash
npx -y @smithery/cli install solarcloud7/factorio-ai-tools --client claude
```
*Note: This will automatically clone the repository, install Python requirements, and update your `claude_desktop_config.json` file.*

### Option 2: Manual Setup

If you prefer to set it up manually without `npx`, follow these steps:

1. Clone this repository:
   ```bash
   git clone https://github.com/solarcloud7/factorio-ai-tools.git
   cd factorio-ai-tools
   ```
2. Create a virtual environment and install the Python dependencies:
   ```bash
   python -m venv venv
   # On Windows:
   .\venv\Scripts\activate
   # On Mac/Linux:
   source venv/bin/activate
   
   pip install -r requirements.txt
   ```
3. *(Optional)* Run the ingest scripts to populate the local vector databases if you didn't download the pre-built `factorio_lancedb` and `clusterio_lancedb` folders:
   ```bash
   python ingest_factorio.py
   python ingest_clusterio.py
   ```
4. Add the server to your Claude Desktop config (usually at `%APPDATA%\Claude\claude_desktop_config.json` on Windows or `~/Library/Application Support/Claude/claude_desktop_config.json` on Mac):
   ```json
   "mcpServers": {
     "factorio-ai-tools": {
       "command": "C:\\path\\to\\factorio-ai-tools\\venv\\Scripts\\python.exe",
       "args": [
         "C:\\path\\to\\factorio-ai-tools\\server.py"
       ]
     }
   }
   ```
5. Restart Claude Desktop.
