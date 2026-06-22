import os
import sys
import lancedb
import base64
import zlib
import json
import torch
from sentence_transformers import SentenceTransformer
from mcp.server.fastmcp import FastMCP

# Define tool version
TOOL_VERSION = "1.0.0"

import argparse

parser = argparse.ArgumentParser(description="Factorio AI Tools MCP Server")
parser.add_argument("--enable-tools", type=str, help="Comma-separated list of tools to enable")
parser.add_argument("--disable-tools", type=str, help="Comma-separated list of tools to disable")
parser.add_argument("--sse", action="store_true", help="Run as an SSE server (global shared instance)")
parser.add_argument("--port", type=int, default=8000, help="Port to run the SSE server on (default: 8000)")
args, _ = parser.parse_known_args()

enabled_tools = [t.strip() for t in args.enable_tools.split(",")] if args.enable_tools else None
disabled_tools = [t.strip() for t in args.disable_tools.split(",")] if args.disable_tools else []

def tool_enabled(tool_name: str) -> bool:
    if disabled_tools and tool_name in disabled_tools:
        return False
    if enabled_tools and tool_name not in enabled_tools:
        return False
    return True

# Initialize FastMCP server
mcp = FastMCP("Factorio AI Tools", port=args.port)

def optional_tool():
    def decorator(func):
        if tool_enabled(func.__name__):
            return mcp.tool()(func)
        return func
    return decorator

import urllib.request
import zipfile
import shutil

# Determine if we are running locally (git/docker) or via PyPI/uvx
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_DATA_DIR = os.path.join(REPO_ROOT, "data")
USER_DATA_DIR = os.path.expanduser("~/.factorio-ai-tools/data")

if os.path.exists(LOCAL_DATA_DIR) or os.getenv("FACTORIO_MCP_LOCAL_MODE"):
    DATA_DIR = LOCAL_DATA_DIR
else:
    DATA_DIR = USER_DATA_DIR

# The release asset (factorio_lancedb.zip) bundles all of these; the bootstrap
# only short-circuits when every one is already present, so a partial extract
# (e.g. repo_lancedb alone) still triggers a re-download.
ALL_STORES = ["factorio_lancedb", "clusterio_lancedb", "wiki_lancedb", "forum_lancedb", "repo_lancedb"]

def ensure_databases():
    if all(os.path.exists(os.path.join(DATA_DIR, s)) for s in ALL_STORES):
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    print(f"Databases not found locally. Downloading to {DATA_DIR}...", file=sys.stderr)
    url = "https://github.com/solarcloud7/factorio-ai-tools/releases/latest/download/factorio_lancedb.zip"
    zip_path = os.path.join(DATA_DIR, "databases.zip")
    
    try:
        urllib.request.urlretrieve(url, zip_path)
        print("Extracting databases...", file=sys.stderr)
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(DATA_DIR)
        os.remove(zip_path)
        print("Databases successfully installed!", file=sys.stderr)
    except Exception as e:
        print(f"Failed to download databases: {e}", file=sys.stderr)

ensure_databases()

db_path_factorio = os.path.join(DATA_DIR, "factorio_lancedb")
db_path_clusterio = os.path.join(DATA_DIR, "clusterio_lancedb")
db_path_wiki = os.path.join(DATA_DIR, "wiki_lancedb")
db_path_forum = os.path.join(DATA_DIR, "forum_lancedb")

db_factorio = lancedb.connect(db_path_factorio)
db_clusterio = lancedb.connect(db_path_clusterio)
db_wiki = lancedb.connect(db_path_wiki)
db_forum = lancedb.connect(db_path_forum)

try:
    table_factorio = db_factorio.open_table("docs")
except Exception as e:
    print(f"Warning: Could not open Factorio docs table. Did you run ingest/ingest_factorio.py? Error: {e}", file=sys.stderr)
    table_factorio = None

try:
    table_clusterio = db_clusterio.open_table("codebase")
except Exception as e:
    print(f"Warning: Could not open Clusterio codebase table. Did you run ingest/ingest_clusterio.py? Error: {e}", file=sys.stderr)
    table_clusterio = None

try:
    table_wiki = db_wiki.open_table("docs")
except Exception as e:
    print(f"Warning: Could not open Factorio wiki table. Did you run ingest/ingest_wiki.py? Error: {e}", file=sys.stderr)
    table_wiki = None

try:
    table_forum = db_forum.open_table("forum")
except Exception as e:
    print(f"Warning: Could not open Factorio forum table. Did you run ingest/ingest_forum.py? Error: {e}", file=sys.stderr)
    table_forum = None

# Initialize embedding model globally
device = "cuda" if torch.cuda.is_available() else "cpu"
model_name = os.getenv("EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5")
model = SentenceTransformer(model_name, device=device)

@optional_tool()
def get_mcp_version_info() -> str:
    """
    Get the version metadata of the MCP server and the internal Factorio/Clusterio LanceDB datasets.
    Use this to self-diagnose if the search index is out of date for the user's modding scenario.
    """
    fac_v = "unknown"
    fac_path = os.path.join(db_path_factorio, "version.txt")
    if os.path.exists(fac_path):
        with open(fac_path, "r", encoding="utf-8") as f:
            fac_v = f.read().strip()
            
    clus_v = "unknown"
    clus_path = os.path.join(db_path_clusterio, "version.txt")
    if os.path.exists(clus_path):
        with open(clus_path, "r", encoding="utf-8") as f:
            clus_v = f.read().strip()
            
    return json.dumps({
        "tool_version": TOOL_VERSION,
        "factorio_docs_version": fac_v,
        "clusterio_code_version": clus_v
    }, indent=2)

@mcp.prompt()
def factorio_clusterio_expert() -> str:
    """
    A foundational system prompt describing the Factorio modding architecture and the Clusterio plugin ecosystem.
    Use this to give the AI the 'bird's eye view' mental map required to mod Clusterio effectively.
    """
    return """You are a master Factorio modder and Clusterio plugin developer.
Factorio modding is split into 3 distinct phases:
1. **Settings Phase**: Defines mod settings (startup, map, per-user).
2. **Data Phase**: Defines Prototypes (`data:extend(...)` for items, entities, recipes).
3. **Control Phase**: The runtime Lua API (events, surfaces, entities, game state).

Clusterio is a multi-server Factorio management ecosystem split into multiple packages:
- **Master**: The central Node.js server.
- **Controller**: The web UI and master controller plugin host.
- **Instance**: A bridge running alongside a single Factorio server.
- **Host**: Manages multiple instances on a single machine.

When building a Clusterio plugin, you must implement logic across these layers, using `messages` (IPC) to pass data between the Factorio Lua runtime, the Node.js Instance, and the Master server.

When writing Lua code, always use the `search_factorio_docs` tool to verify the syntax of the Control API or the properties of the Data Phase Prototypes.
When dealing with Node.js IPC or Plugin architecture, always use `search_clusterio_code`.
For gameplay mechanics, formulas, ratios, fluid mechanics, or general game knowledge, use the `search_factorio_wiki` tool.
Never assume a method or concept exists without verifying it in the docs.
You also have the ability to decode and encode Factorio Blueprint strings using `decode_factorio_blueprint` and `encode_factorio_blueprint`. You can use these tools to dynamically inspect, generate, or optimize factory layouts directly for the user!"""

@optional_tool()
def search_factorio_docs(queries: list[str], class_filter: str = None, limit: int = 5, factorio_version: str = "latest") -> str:
    """
    Search Factorio API documentation.
    
    Args:
        queries: A list of search query strings to batch process.
        class_filter: Optional class name to filter by (e.g., 'LuaEntity').
        limit: Maximum number of chunks to return per query (default 5, max 20).
        factorio_version: The Factorio version to search against. Defaults to 'latest' (which uses the newest version in the db). Pass '1.1.110' for legacy mods.
    """
    if table_factorio is None:
        return "Error: Factorio database table not found. Please run ingest/ingest_factorio.py first."
        
    if not queries:
        return "No queries provided."
        
    try:
        limit = min(max(1, limit), 20)
        
        # Generate vectors for all queries in a single batch
        query_vecs = model.encode(queries, normalize_embeddings=True)
        
        all_formatted_chunks = []
        
        for idx, query_vec in enumerate(query_vecs):
            q = table_factorio.search(query_vec.tolist())
            
            conditions = []
            if class_filter:
                safe_class = class_filter.replace("'", "''")
                conditions.append(f"class_name = '{safe_class}'")
            
            ver = factorio_version if factorio_version not in ("latest", "") else "latest"
            safe_ver = ver.replace("'", "''")
            conditions.append(f"version = '{safe_ver}'")
            
            q = q.where(" AND ".join(conditions))
                
            results = q.limit(limit).to_list()
            
            all_formatted_chunks.append(f"### Results for query: '{queries[idx]}'")
            
            if len(results) == 0:
                all_formatted_chunks.append("No results found.")
            else:
                for row in results:
                    all_formatted_chunks.append(f"**URL:** {row['url']}\n{row['text']}")
            
            all_formatted_chunks.append("---")
            
        return "\n\n".join(all_formatted_chunks)
        
    except Exception as e:
        return f"Error executing search: {str(e)}"

@optional_tool()
def search_clusterio_code(queries: list[str], node_type: str = None, limit: int = 5) -> str:
    """
    Search the Clusterio TypeScript codebase using semantic AST-chunked RAG.
    
    Args:
        queries: A list of semantic search queries to batch process.
        node_type: Optional AST node type filter ('class_declaration', 'function_declaration', 'method_definition', 'interface_declaration', 'text_file').
        limit: Maximum number of chunks to return per query (default 5, max 20).
    """
    if table_clusterio is None:
        return "Error: Clusterio database table not found. Please run ingest/ingest_clusterio.py first."
        
    if not queries:
        return "No queries provided."
        
    try:
        limit = min(max(1, limit), 20)
        
        # Generate vectors
        query_vecs = model.encode(queries, normalize_embeddings=True)
        
        all_formatted_chunks = []
        
        for idx, query_vec in enumerate(query_vecs):
            q = table_clusterio.search(query_vec.tolist())
            
            if node_type:
                safe_node = node_type.replace("'", "''")
                q = q.where(f"node_type = '{safe_node}'")
                
            results = q.limit(limit).to_list()
            
            all_formatted_chunks.append(f"### Results for query: '{queries[idx]}'")
            
            if len(results) == 0:
                all_formatted_chunks.append("No results found.")
            else:
                for i, row in enumerate(results):
                    chunk = (
                        f"**Result {i+1}** - {row['node_name']} ({row['node_type']})\n"
                        f"**File:** `{row['file_path']}`\n\n"
                        f"```typescript\n"
                        f"{row['content']}\n"
                        f"```"
                    )
                    all_formatted_chunks.append(chunk)
            
            all_formatted_chunks.append("---")
            
        return "\n\n".join(all_formatted_chunks)
        
    except Exception as e:
        return f"Error executing search: {str(e)}"

@optional_tool()
def search_factorio_wiki(queries: list[str], limit: int = 5) -> str:
    """
    Search the official Factorio Wiki for gameplay mechanics, recipes, tutorials, or formulas.
    
    Args:
        queries: A list of search query strings to batch process.
        limit: Maximum number of chunks to return per query (default 5, max 20).
    """
    if table_wiki is None:
        return "Error: Factorio Wiki database table not found. Please run ingest/ingest_wiki.py first."
        
    if not queries:
        return "No queries provided."
        
    try:
        limit = min(max(1, limit), 20)
        
        # Generate vectors
        query_vecs = model.encode(queries, normalize_embeddings=True)
        
        all_formatted_chunks = []
        
        for idx, query_vec in enumerate(query_vecs):
            q = table_wiki.search(query_vec.tolist())
            results = q.limit(limit).to_list()
            
            all_formatted_chunks.append(f"### Wiki Results for query: '{queries[idx]}'")
            
            if len(results) == 0:
                all_formatted_chunks.append("No results found.")
            else:
                for row in results:
                    chunk = f"**{row['title']}**\n{row['url']}\n{row['text']}"
                    all_formatted_chunks.append(chunk)
            
            all_formatted_chunks.append("---")
            
        return "\n\n".join(all_formatted_chunks)
        
    except Exception as e:
        return f"Error executing wiki search: {str(e)}"

@optional_tool()
def search_factorio_forums(queries: list[str], limit: int = 5) -> str:
    """
    Search the official Factorio Forums (specifically Modding Help) for obscure bugs, community fixes, and undocumented tricks.
    
    Args:
        queries: A list of search query strings to batch process.
        limit: Maximum number of chunks to return per query (default 5, max 20).
    """
    if table_forum is None:
        return "Error: Factorio Forum database table not found. Please run ingest/ingest_forum.py first."
        
    if not queries:
        return "No queries provided."
        
    try:
        limit = min(max(1, limit), 20)
        
        # Generate vectors
        query_vecs = model.encode(queries, normalize_embeddings=True)
        
        all_formatted_chunks = []
        
        for idx, query_vec in enumerate(query_vecs):
            q = table_forum.search(query_vec.tolist())
            results = q.limit(limit).to_list()
            
            all_formatted_chunks.append(f"### Forum Results for query: '{queries[idx]}'")
            
            if len(results) == 0:
                all_formatted_chunks.append("No results found.")
            else:
                for row in results:
                    chunk = f"**{row['class_name']}**\n{row['file_path']}\n{row['content']}"
                    all_formatted_chunks.append(chunk)
            
            all_formatted_chunks.append("---")
            
        return "\n\n".join(all_formatted_chunks)
        
    except Exception as e:
        return f"Error executing forum search: {str(e)}"

@optional_tool()
def decode_factorio_blueprint(blueprint_string: str) -> str:
    """
    Decodes a Factorio blueprint string (e.g. '0eNq...') into a formatted JSON string.
    Use this to inspect the entities, items, or configuration of a blueprint.
    
    Args:
        blueprint_string: The raw Factorio blueprint string starting with the version byte (usually '0').
    """
    try:
        blueprint_string = blueprint_string.strip()
        if not blueprint_string:
            return "Error: Empty blueprint string."
            
        # The first character is the version byte
        version_byte = blueprint_string[0]
        if version_byte != '0':
            return f"Error: Unsupported version byte '{version_byte}'. Only '0' is supported."
            
        b64_data = blueprint_string[1:]
        
        # Decode base64
        compressed_data = base64.b64decode(b64_data, validate=True)
        
        # Decompress zlib (with 10MB protection)
        d = zlib.decompressobj()
        raw = d.decompress(compressed_data, 10 * 1024 * 1024)
        if d.unconsumed_tail:
            return "Error: Blueprint too large (decompressed size exceeds the 10MB safety limit)."
        json_data = raw.decode('utf-8')
        
        # Parse and re-format JSON nicely
        parsed = json.loads(json_data)
        return json.dumps(parsed, indent=2)
        
    except Exception as e:
        return f"Error decoding blueprint: {str(e)}"

@optional_tool()
def encode_factorio_blueprint(json_string: str) -> str:
    """
    Encodes a raw Factorio blueprint JSON string back into a Factorio blueprint string (e.g. '0eNq...').
    Use this when you have generated or modified a blueprint JSON and need to provide the player with the importable string.
    
    Args:
        json_string: The JSON representation of the blueprint.
    """
    try:
        # Validate it's proper JSON
        parsed = json.loads(json_string)
        compact_json = json.dumps(parsed, separators=(',', ':')).encode('utf-8')
        
        # Compress zlib
        compressed_data = zlib.compress(compact_json)
        
        # Encode base64
        b64_data = base64.b64encode(compressed_data).decode('utf-8')
        
        # Factorio 1.1/2.0 standard blueprint version byte is '0'
        return '0' + b64_data
        
    except Exception as e:
        return f"Error encoding blueprint: {str(e)}"

import urllib.request
import platform

db_path_repo = os.path.join(DATA_DIR, "repo_lancedb")
try:
    db_repo = lancedb.connect(db_path_repo)
    table_repo = db_repo.open_table("codebase")
except Exception as e:
    print(f"Warning: Could not open Repo codebase table. Error: {e}", file=sys.stderr)
    table_repo = None

@optional_tool()
def search_github_code(queries: list[str], repo_name: str = None, limit: int = 5) -> str:
    """
    Search an ingested GitHub codebase (like clusterio-docker or mods) using semantic AST-chunked RAG.
    
    Args:
        queries: A list of semantic search queries to batch process.
        repo_name: Optional repository name filter (e.g. 'clusterio-docker') which corresponds to the GitHub repo URL.
        limit: Maximum number of chunks to return per query (default 5, max 20).
    """
    if table_repo is None:
        return "Error: Repo database table not found. Please run ingest/ingest_github_repo.py first."
        
    if not queries:
        return "No queries provided."
        
    try:
        limit = min(max(1, limit), 20)
        
        # Generate vectors
        query_vecs = model.encode(queries, normalize_embeddings=True)
        
        all_formatted_chunks = []
        
        for idx, query_vec in enumerate(query_vecs):
            q = table_repo.search(query_vec.tolist())
            
            if repo_name:
                safe_repo = repo_name.replace("'", "''")
                q = q.where(f"repo_url LIKE '%{safe_repo}%'")
                
            results = q.limit(limit).to_list()
            
            all_formatted_chunks.append(f"### Results for query: '{queries[idx]}'")
            
            if len(results) == 0:
                all_formatted_chunks.append("No results found.")
            else:
                for i, row in enumerate(results):
                    chunk = (
                        f"**Result {i+1}** - {row['node_name']} ({row['node_type']}) in Repo: {row['repo_url']}\n"
                        f"**File:** `{row['file_path']}`\n\n"
                        f"```\n"
                        f"{row['content']}\n"
                        f"```"
                    )
                    all_formatted_chunks.append(chunk)
            
            all_formatted_chunks.append("---")
            
        return "\n\n".join(all_formatted_chunks)
        
    except Exception as e:
        return f"Error executing mod code search: {str(e)}"

@optional_tool()
def factorio_mod_portal_analyzer(mod_name: str) -> str:
    """
    Query the Factorio Mod Portal API for a specific mod to fetch metadata, dependencies, releases, and description.
    
    Args:
        mod_name: The exact internal name of the mod (e.g. 'maraxsis', 'space-exploration').
    """
    url = f"https://mods.factorio.com/api/mods/{mod_name}/full"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'FactorioAITools/1.0'})
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode('utf-8'))
            
        # Format the output to be readable by the LLM without dumping massive amounts of JSON
        res = [f"# Mod: {data.get('title', mod_name)} ({data.get('name')})"]
        res.append(f"**Downloads**: {data.get('downloads_count')}")
        res.append(f"**Category**: {data.get('category')} | **Created At**: {data.get('created_at')}")
        res.append(f"**Summary**: {data.get('summary')}")
        
        releases = data.get('releases', [])
        if releases:
            latest = releases[-1]
            res.append(f"\n## Latest Release ({latest.get('version')}) for Factorio {latest.get('info_json', {}).get('factorio_version')}")
            deps = latest.get('info_json', {}).get('dependencies', [])
            res.append("**Dependencies**:")
            if deps:
                for d in deps:
                    res.append(f"- {d}")
            else:
                res.append("None")
                
        res.append(f"\n## Description\n{(data.get('description') or '')[:2000]}" + ("... (truncated)" if len(data.get('description') or '') > 2000 else ""))
        return "\n".join(res)
        
    except urllib.error.HTTPError as e:
        return f"HTTP Error fetching mod portal data: {e.code} {e.reason}"
    except Exception as e:
        return f"Error fetching mod portal data: {str(e)}"


def main():
    if args.sse:
        print(f"Starting Factorio AI Tools MCP Server on SSE (port {args.port})...")
        mcp.run(transport='sse')
    else:
        # Run the server using stdio
        mcp.run()

if __name__ == '__main__':
    main()
