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

## Quick Install (Smithery)

The fastest way to install this for Claude Desktop is via Smithery:

```bash
npx -y @smithery/cli install factorio-ai-tools --client claude
```

## Manual Setup

1. Clone this repository.
2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the ingest scripts to populate the local vector databases (Optional if you cloned the pre-built `factorio_lancedb` and `clusterio_lancedb` folders):
   ```bash
   python ingest_factorio.py
   python ingest_clusterio.py
   ```
4. Start the MCP server:
   ```bash
   python server.py
   ```
