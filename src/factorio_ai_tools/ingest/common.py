"""Shared helpers for the ingest scripts.

Each LanceDB store is built by its own script in this package, but they all share
one contract that MUST stay consistent with ``server.py`` or hybrid search breaks
silently:

* embedding model ``BAAI/bge-base-en-v1.5`` (env ``EMBEDDING_MODEL``), **768-dim,
  L2-normalized**;
* SHA-256 content hashing for incremental (skip-unchanged / delete-then-re-add)
  ingestion;
* local-``data/`` vs per-user data-dir resolution (identical to ``server.py``);
* Windows-safe console printing (PowerShell's default encoding raises on
  en-dashes/emoji);
* a schema-migration guard that drops+recreates a table whose columns are stale;
* tree-sitter parsers/queries for code-aware chunking (TypeScript/JS and Lua),
  using the modern ``Parser(lang)`` / ``Query`` / ``QueryCursor`` API.
"""

import hashlib
import os
import shutil
import stat

import torch
from sentence_transformers import SentenceTransformer

# common.py lives at src/factorio_ai_tools/ingest/common.py, so the repo root is
# four parents up. This MUST resolve to the same place as server.py's REPO_ROOT
# (three up from src/factorio_ai_tools/server.py) so ingest and serve agree on
# where data/ lives.
REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

EMBEDDING_DIM = 768


def safe_print(message):
    """Print without tripping PowerShell's default-encoding UnicodeEncodeError."""
    print(str(message).encode("ascii", "replace").decode("ascii"))


def get_data_dir():
    """Local ``data/`` when running from the repo/Docker, else the per-user dir."""
    local_data_dir = os.path.join(REPO_ROOT, "data")
    if os.path.exists(local_data_dir) or os.getenv("FACTORIO_MCP_LOCAL_MODE"):
        return local_data_dir
    return os.path.expanduser("~/.factorio-ai-tools/data")


def connect_store(store_name):
    """Connect to ``data/<store_name>`` (creating dirs); return (db, store_path)."""
    import lancedb

    data_dir = get_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    store_path = os.path.join(data_dir, store_name)
    return lancedb.connect(store_path), store_path


def get_hash(data):
    """SHA-256 hex digest of a ``str`` or ``bytes``."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def rmtree_force(path):
    """``shutil.rmtree`` that survives read-only files (e.g. Windows git pack
    files under ``.git/objects/pack``, which otherwise raise PermissionError)."""
    if not os.path.exists(path):
        return

    def _retry(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    try:
        shutil.rmtree(path, onexc=_retry)        # Python 3.12+
    except TypeError:
        shutil.rmtree(path, onerror=_retry)      # Python 3.11


# Directories never worth ingesting: dependencies, build output, VCS, caches.
IGNORED_DIRS = {
    ".git", "node_modules", "dist", "build", "out", "target",
    "__pycache__", "venv", ".venv", ".yarn", ".next", ".cache", "coverage",
}

# Generated dependency lockfiles: large, churny, no semantic value.
IGNORED_FILENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
    "Cargo.lock", "poetry.lock", "uv.lock", "Gemfile.lock", "composer.lock",
}


def is_ignored_path(path):
    """True if any path segment is an ignored dir or the basename is a lockfile."""
    parts = path.replace("\\", "/").split("/")
    return bool(parts) and (
        any(seg in IGNORED_DIRS for seg in parts) or parts[-1] in IGNORED_FILENAMES
    )


_MODEL = None


def load_embedder():
    """Load the shared SentenceTransformer once (CUDA->CPU auto, env override)."""
    global _MODEL
    if _MODEL is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model_name = os.getenv("EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5")
        safe_print(f"Loading embedding model {model_name} on {device}...")
        _MODEL = SentenceTransformer(model_name, device=device)
    return _MODEL


def embed(texts, model=None):
    """Encode a list of texts into 768-dim L2-normalized vectors."""
    model = model or load_embedder()
    return model.encode(texts, show_progress_bar=False, normalize_embeddings=True)


def _schema_columns(schema):
    """Column names for a pyarrow ``Schema`` or a ``LanceModel`` subclass."""
    if hasattr(schema, "names"):  # pyarrow.Schema
        return set(schema.names)
    if hasattr(schema, "model_fields"):  # lancedb.pydantic.LanceModel (pydantic v2)
        return set(schema.model_fields.keys())
    raise TypeError(f"Unsupported schema type: {type(schema)!r}")


def ensure_table(db, name, schema):
    """Open table ``name``, dropping+recreating it if its columns are stale.

    Generalizes the per-script ``if "content_hash" not in table.schema.names:
    drop`` guard: any time ``schema`` adds/renames a column, the existing table is
    rebuilt from scratch (a full re-ingest of that store). Returns an open handle.
    """
    if name in db.table_names():
        table = db.open_table(name)
        if _schema_columns(schema).issubset(set(table.schema.names)):
            return table
        safe_print(f"Schema for table '{name}' is stale; dropping to migrate...")
        db.drop_table(name)
    return db.create_table(name, schema=schema)


# --- Code-aware chunking (tree-sitter) ---------------------------------------

try:
    from tree_sitter import Language, Parser, Query, QueryCursor
    import tree_sitter_typescript as tsts
    import tree_sitter_lua as tslua

    HAS_TREE_SITTER = True
except ImportError:
    HAS_TREE_SITTER = False
    safe_print("Warning: tree-sitter not available; code files will be text-chunked.")

# Queries capture top-level declarations; the capture name (without @) becomes the
# stored ``node_type``. QueryCursor.captures matches descendants too, so nested
# methods/functions are captured without a manual tree walk.
_TS_QUERY = """
(class_declaration) @class
(interface_declaration) @interface
(function_declaration) @function
(method_definition) @method
"""
_LUA_QUERY = """
(function_declaration) @function
(table_constructor) @table
"""

_LANG_CACHE = {}


def _lang_and_query(kind):
    """Return cached (Language, Query) for 'typescript' or 'lua'; None if N/A."""
    if not HAS_TREE_SITTER:
        return None
    if kind not in _LANG_CACHE:
        if kind == "typescript":
            lang = Language(tsts.language_typescript())
            _LANG_CACHE[kind] = (lang, Query(lang, _TS_QUERY))
        elif kind == "lua":
            lang = Language(tslua.language())
            _LANG_CACHE[kind] = (lang, Query(lang, _LUA_QUERY))
        else:
            return None
    return _LANG_CACHE[kind]


def _node_name(node):
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return name_node.text.decode("utf-8", "replace")
    return "anonymous"


def _preceding_comments(node):
    comments = []
    prev = node.prev_sibling
    while prev is not None and prev.type == "comment":
        comments.insert(0, prev.text.decode("utf-8", "replace"))
        prev = prev.prev_sibling
    return "\n".join(comments)


def extract_ast_chunks(src_bytes, kind, include_comments=False):
    """Parse ``src_bytes`` (``bytes``) with the grammar for ``kind`` and return a
    list of ``{'node_name','node_type','content'}`` per captured declaration.

    Returns ``None`` when tree-sitter is unavailable or ``kind`` is unsupported
    (so the caller falls back to text chunking); ``[]`` when the file parsed but
    matched no declarations.
    """
    lq = _lang_and_query(kind)
    if lq is None:
        return None
    lang, query = lq
    tree = Parser(lang).parse(src_bytes)
    captures = QueryCursor(query).captures(tree.root_node)

    chunks = []
    for capture_name, nodes in captures.items():
        for node in nodes:
            code = node.text.decode("utf-8", "replace")
            if include_comments:
                comments = _preceding_comments(node)
                if comments:
                    code = f"{comments}\n{code}"
            chunks.append(
                {
                    "node_name": _node_name(node),
                    "node_type": capture_name,
                    "content": code,
                }
            )
    return chunks


def text_chunks_by_char(content, chunk_size=1500, overlap=200):
    """Fixed-size overlapping character windows (for prose/config/wiki text)."""
    chunks = []
    if not content:
        return chunks
    start = 0
    while start < len(content):
        chunks.append(content[start : start + chunk_size])
        start += chunk_size - overlap
    return chunks


def text_chunks_by_line(code, chunk_size=50, overlap=10):
    """Line-window ``{'node_name','node_type','content'}`` chunks for code files
    with no AST support (Dockerfile, yaml, etc)."""
    chunks = []
    lines = code.split("\n")
    for i in range(0, len(lines), chunk_size - overlap):
        window = lines[i : i + chunk_size]
        if not window:
            break
        text = "\n".join(window).strip()
        if text:
            chunks.append(
                {
                    "node_name": f"lines_{i + 1}_to_{i + len(window)}",
                    "node_type": "text_chunk",
                    "content": text,
                }
            )
    return chunks
