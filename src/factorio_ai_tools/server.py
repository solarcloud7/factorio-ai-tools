"""FastMCP server exposing the Factorio AI Tools search tools over MCP.

Opens the six LanceDB stores under ``data/`` (each in its own try/except, so a
missing store degrades only its tool rather than crashing the server), loads the
shared embedding model once, and serves hybrid search (``common.hybrid_search``)
plus the blueprint / mod-portal / version utilities and the
``factorio_clusterio_expert`` prompt. Runs over stdio by default, or SSE with
``--sse``. See ``docs/tools.md``.
"""

import os
import sys
import lancedb
import base64
import zlib
import json
import torch
from sentence_transformers import SentenceTransformer
from mcp.server.fastmcp import FastMCP

from factorio_ai_tools.ingest import common

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

# Initialize FastMCP server.
# host: mcp>=1.x no longer reads the FASTMCP_HOST env var on its own — FastMCP.__init__ passes an
# explicit host default ("127.0.0.1") straight into its pydantic Settings, which overrides the env.
# So read FASTMCP_HOST here and pass it through. Defaults to localhost (safe for stdio/local dev);
# set FASTMCP_HOST=0.0.0.0 to bind all interfaces (required when running in a container so other
# containers/hosts can reach the SSE port).
mcp = FastMCP(
    "Factorio AI Tools",
    host=os.environ.get("FASTMCP_HOST", "127.0.0.1"),
    port=args.port,
)

def optional_tool():
    def decorator(func):
        if tool_enabled(func.__name__):
            return mcp.tool()(func)
        return func
    return decorator

import urllib.request

# Determine if we are running locally (git/docker) or via PyPI/uvx
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_DATA_DIR = os.path.join(REPO_ROOT, "data")
USER_DATA_DIR = os.path.expanduser("~/.factorio-ai-tools/data")

if os.path.exists(LOCAL_DATA_DIR) or os.getenv("FACTORIO_MCP_LOCAL_MODE"):
    DATA_DIR = LOCAL_DATA_DIR
else:
    DATA_DIR = USER_DATA_DIR

# Stores bundled in the release zip — used to trigger the bootstrap download.
# All six ship in factorio_lancedb.zip; ensure_databases() extracts any that are
# missing. MUST stay in sync with the Makefile STORES var that packages the zip:
# a store listed here but absent from the zip would re-download it on every start.
RELEASE_STORES = ["factorio_lancedb", "clusterio_lancedb", "wiki_lancedb", "forum_lancedb", "repo_lancedb", "prototypes_lancedb"]

def ensure_databases():
    missing = [s for s in RELEASE_STORES if not os.path.exists(os.path.join(DATA_DIR, s))]
    if not missing:
        return
    print(f"Databases missing {missing}; downloading to {DATA_DIR}...", file=sys.stderr)
    url = "https://github.com/solarcloud7/factorio-ai-tools/releases/latest/download/factorio_lancedb.zip"
    try:
        # Extracts ONLY the missing stores so a hand-built data/ is never clobbered.
        added = common.ensure_stores(DATA_DIR, RELEASE_STORES, url=url)
        print(f"Databases installed: {added}", file=sys.stderr)
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

# Hybrid retrieval (RRF over the ingest-built FTS index + vector) lives in
# common.hybrid_search so the server and the offline tests share one
# implementation. Validated by maintenance/eval_retrieval.py: hybrid >= vector on
# the golden set everywhere FTS exists. Stores with no FTS index (forum) fall back
# to pure vector; a transient/query-specific error falls back for that query only.
hybrid_search = common.hybrid_search

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
            
    proto_v = "unknown"
    proto_path = os.path.join(db_path_prototypes, "version.txt")
    if os.path.exists(proto_path):
        with open(proto_path, "r", encoding="utf-8") as f:
            proto_v = f.read().strip()

    return json.dumps({
        "tool_version": TOOL_VERSION,
        "factorio_docs_version": fac_v,
        "clusterio_code_version": clus_v,
        "factorio_prototypes_version": proto_v,
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

When writing Lua code, always use the `search_factorio_docs` tool to verify the syntax of the Control API or the properties of the Data Phase Prototypes. It requires an explicit `factorio_version` — pass `2.0.76` for stable Factorio 2.x, `2.1.8` for the experimental release (what mod creators target to prepare), or `1.1.110` for legacy 1.1 (there is no "latest").
When dealing with Node.js IPC or Plugin architecture, always use `search_clusterio_code`.
For gameplay mechanics, formulas, ratios, fluid mechanics, or general game knowledge, use the `search_factorio_wiki` tool.
For exact numerical values — recipe ingredients/amounts, crafting times, assembler speeds, technology research costs, quality tier bonuses, planet surface conditions — use `search_factorio_prototypes`. It too requires an explicit `factorio_version` (`2.0.76` or `2.1.8`), since values change between releases. Combine it with `search_factorio_wiki` when a complete answer needs both precise values AND gameplay context.
Never assume a method or concept exists without verifying it in the docs.
You also have the ability to decode and encode Factorio Blueprint strings using `decode_factorio_blueprint` and `encode_factorio_blueprint`. You can use these tools to dynamically inspect, generate, or optimize factory layouts directly for the user!"""

@optional_tool()
def search_factorio_docs(queries: list[str], class_filter: str = None, limit: int = 5, factorio_version: str = None) -> str:
    """
    Search Factorio API documentation.

    Args:
        queries: A list of search query strings to batch process.
        class_filter: Optional class name to filter by (e.g., 'LuaEntity').
        limit: Maximum number of chunks to return per query (default 5, max 20).
        factorio_version: REQUIRED — the exact Factorio version to search. Must be
            one of the pinned versions (1.1.110 = legacy 1.1, 2.0.76 = the 2.x
            baseline). There is no "latest" — pass a concrete version.
    """
    if table_factorio is None:
        return "Error: Factorio database table not found. Please run ingest/ingest_factorio.py first."

    if not queries:
        return "No queries provided."

    if factorio_version not in common.SUPPORTED_FACTORIO_VERSIONS:
        valid = ", ".join(common.SUPPORTED_FACTORIO_VERSIONS)
        return (f"Error: factorio_version is required and must be one of: {valid}. "
                f"(The moving 'latest' label was removed — pass a concrete version.)")

    try:
        limit = min(max(1, limit), 20)
        
        # Generate vectors for all queries in a single batch
        query_vecs = model.encode(queries, normalize_embeddings=True)
        
        all_formatted_chunks = []
        
        for idx, query_vec in enumerate(query_vecs):
            conditions = []
            if class_filter:
                safe_class = class_filter.replace("'", "''")
                conditions.append(f"class_name = '{safe_class}'")

            # factorio_version is validated above against the pinned set, so it's a
            # known-clean literal (no escaping needed).
            conditions.append(f"version = '{factorio_version}'")

            results = hybrid_search(table_factorio, queries[idx], query_vec, limit,
                                    where=" AND ".join(conditions))
            
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
def search_clusterio_code(queries: list[str], node_type: str = None, plugin: str = None, limit: int = 5) -> str:
    """
    Search the Clusterio TypeScript codebase using semantic AST-chunked RAG.

    Args:
        queries: A list of semantic search queries to batch process.
        node_type: Optional AST node-type filter. Code nodes: 'class', 'interface', 'function', 'method' (TypeScript), 'table' (Lua); fallbacks: 'text_chunk' (uncapturable code lines), 'text_file' (non-code files).
        plugin: Optional plugin/package name to scope the search to one component, matched against the file path (e.g. 'subspace_storage', 'player_auth', 'inventory_sync', 'controller').
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
            conditions = []
            if node_type:
                safe_node = node_type.replace("'", "''")
                conditions.append(f"node_type = '{safe_node}'")
            if plugin:
                # Escaped LIKE so 'player_auth' can't match 'player1auth' (_ wildcard).
                conditions.append(common.like_filter("file_path", plugin))

            results = hybrid_search(table_clusterio, queries[idx], query_vec, limit,
                                    where=" AND ".join(conditions) if conditions else None)
            
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
            results = hybrid_search(table_wiki, queries[idx], query_vec, limit)
            
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
            results = hybrid_search(table_forum, queries[idx], query_vec, limit)
            
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

db_path_prototypes = os.path.join(DATA_DIR, "prototypes_lancedb")
try:
    db_prototypes = lancedb.connect(db_path_prototypes)
    table_prototypes = db_prototypes.open_table("prototypes")
except Exception as e:
    print(f"Warning: Could not open prototypes table. Run ingest_prototypes.py first. Error: {e}", file=sys.stderr)
    table_prototypes = None

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
            where = common.like_filter("repo_url", repo_name) if repo_name else None
            results = hybrid_search(table_repo, queries[idx], query_vec, limit, where=where)
            
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
def search_factorio_prototypes(queries: list[str], prototype_type: str = None, limit: int = 5, factorio_version: str = None) -> str:
    """
    Search Factorio prototype definitions for exact numerical data: recipe ingredients
    and crafting times, assembler speeds and energy usage, technology research costs,
    item stack sizes, quality tier bonuses, and Space Age planet/surface conditions.

    Use this tool when you need PRECISE VALUES from the game data files — not
    explanations of how things work (use search_factorio_wiki for that) and not
    modding API syntax (use search_factorio_docs for that). Example use cases:
    - "What are the ingredients and crafting time for electronic-circuit?"
    - "Which assembling machines can craft recipes in category 'crafting'?"
    - "What does the legendary quality tier do to crafting speed?"
    - "What surface conditions does Vulcanus have?"

    Args:
        queries: Semantic search queries.
        factorio_version: REQUIRED — the exact Factorio version whose prototype values
                          to search. Must be "2.0.76" (stable) or "2.1.8" (experimental).
                          Values are version-specific (recipe categories, energy use, etc.
                          change between releases), so there is no "latest" and no default.
        prototype_type: Optional filter. Umbrella values "item" (covers ammo,
                        module, gun, armor, capsule, …) and "entity" (covers
                        furnace, inserter, assembling-machine, …) expand to all
                        their subtypes. Or pass an exact type: "recipe",
                        "technology", "module", "furnace", "quality", "planet", …
        limit: Max results per query (1–20, default 5).
    """
    if table_prototypes is None:
        return "Error: Prototypes table not found. Run ingest_prototypes.py first."

    if not queries:
        return "No queries provided."

    if factorio_version not in common.SUPPORTED_PROTOTYPE_VERSIONS:
        valid = ", ".join(common.SUPPORTED_PROTOTYPE_VERSIONS)
        return (f"Error: factorio_version is required and must be one of: {valid}. "
                f"Prototype values are version-specific (e.g. recipe categories changed "
                f"between 2.0.76 and 2.1.x), so pass a concrete version (there is no 'latest').")

    try:
        limit = min(max(1, limit), 20)
        query_vecs = model.encode(queries, normalize_embeddings=True)
        all_formatted_chunks = []

        # Always scope to the requested version (validated above). An umbrella value
        # ("item"/"entity") then expands to every raw subtype actually stored
        # (ammo/module/gun…, furnace/inserter…); a specific subtype or
        # recipe/fluid/technology/quality/… matches itself.
        conditions = [f"version = '{factorio_version}'"]
        if prototype_type:
            group = common.PROTOTYPE_TYPE_GROUPS.get(prototype_type)
            if group:
                vals = ", ".join("'" + t.replace("'", "''") + "'" for t in sorted(group))
                conditions.append(f"prototype_type IN ({vals})")
            else:
                safe_pt = prototype_type.replace("'", "''")
                conditions.append(f"prototype_type = '{safe_pt}'")
        where = " AND ".join(conditions)

        for idx, query_vec in enumerate(query_vecs):
            results = hybrid_search(table_prototypes, queries[idx], query_vec, limit, where=where)

            all_formatted_chunks.append(f"### Prototype results for query: '{queries[idx]}'")
            if not results:
                all_formatted_chunks.append("No results found.")
            else:
                for row in results:
                    header = f"**{row['prototype_type']}: {row['prototype_name']}**"
                    if row.get("category"):
                        header += f" (category: {row['category']})"
                    all_formatted_chunks.append(f"{header}\n{row['content']}")
            all_formatted_chunks.append("---")

        return "\n\n".join(all_formatted_chunks)

    except Exception as e:
        return f"Error executing prototype search: {str(e)}"


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
