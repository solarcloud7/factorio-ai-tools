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
import statistics
import sys

# common.py lives at src/factorio_ai_tools/ingest/common.py, so the repo root is
# four parents up. This MUST resolve to the same place as server.py's REPO_ROOT
# (three up from src/factorio_ai_tools/server.py) so ingest and serve agree on
# where data/ lives.
REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

EMBEDDING_DIM = 768

# Token budgets for chunking. The embedder (bge-base-en-v1.5) hard-caps at 512
# tokens and silently truncates beyond, so sizing is measured in REAL tokens, not
# chars (code tokenizes ~2-4x denser than prose, so a char cap under-counts).
EMBED_MAX_TOKENS = 510        # effective content budget: 512 hard cap minus the CLS+SEP
                              # special tokens the model adds (count_tokens uses
                              # add_special_tokens=False), measured on the full embedded text
CONTENT_MAX_TOKENS = 400      # cap on a chunk's raw content; leaves headroom for the prefix
MIN_CHUNK_CHARS = 10          # drop a chunk whose stripped raw content is shorter than this
MAX_CHUNKS_PER_FILE = 400     # a single file producing more is bulk data (e.g. a serialized
                              # blueprint or changelog) and is skipped with a visible warning


class ChunkHealthError(Exception):
    """Raised when chunk-health validation fails in strict mode, or when an
    invariant like the embedding dimension is violated."""


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

# Generated dependency lockfiles + bulky low-value files: large, churny, no
# semantic value for retrieval. (Factorio's changelog.txt alone text-chunks into
# ~1840 windows that drown the store.)
IGNORED_FILENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
    "Cargo.lock", "poetry.lock", "uv.lock", "Gemfile.lock", "composer.lock",
    "changelog.txt", "changelog-last.txt",
}


def is_ignored_path(path):
    """True if any path segment is an ignored dir or the basename is a lockfile."""
    parts = path.replace("\\", "/").split("/")
    return bool(parts) and (
        any(seg in IGNORED_DIRS for seg in parts) or parts[-1] in IGNORED_FILENAMES
    )


# --- Routing & keys (shared by every ingester so they can't drift) -----------

# One routing table for language detection. Both code ingesters MUST use this so
# they agree on what is AST-chunked: a hardcoded 'typescript' here once skipped
# every .lua file, and a separate ext check elsewhere text-chunked .js.
_EXT_KIND = {
    ".ts": "typescript", ".tsx": "typescript", ".js": "typescript", ".jsx": "typescript",
    ".lua": "lua",
}


def kind_for_ext(ext):
    """Tree-sitter language for a file extension, or ``None`` to text-chunk it.
    ``ext`` is matched case-insensitively and may be given with or without a dot."""
    e = ext.lower()
    if not e.startswith("."):
        e = "." + e
    return _EXT_KIND.get(e)


def repo_slug_from_url(url):
    """``owner/repo`` slug for a clone URL — a unique key (so two repos named
    ``index``/``main`` don't collide) that strips only the ``.git`` suffix, never
    the owner segment. Drops any ``?query``/``#fragment`` and trailing slash first.
    Falls back to the last segment for a bare name."""
    s = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    parts = [p for p in s.split("/") if p]
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return parts[-1] if parts else url


def to_posix(path):
    """Store every ``file_path`` POSIX-style so keys/filters are stable across OSes
    (Windows ``os.path.relpath`` yields backslashes that break ``LIKE`` and reads)."""
    return path.replace("\\", "/")


def like_escape(value):
    """Escape the SQL ``LIKE`` wildcards (``\\ % _``) in a literal so a filter like
    ``player_auth`` can't match ``player1auth`` (``_`` is a single-char wildcard).
    Backslash MUST be escaped first. Pair with ``ESCAPE '\\'`` in the clause."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def like_filter(column, value):
    """A ready ``<column> LIKE '%...%' ESCAPE '\\'`` fragment with both LIKE
    metachars and the single-quote SQL-literal delimiter escaped."""
    esc = like_escape(value).replace("'", "''")
    return f"{column} LIKE '%{esc}%' ESCAPE '\\'"


def ensure_stores(data_dir, stores, *, url, download=None):
    """Bootstrap missing LanceDB stores from the released zip WITHOUT clobbering
    stores already present (the old ``extractall`` overwrote a hand-built
    ``data/``). Extracts only members whose top-level dir is a missing store.
    Returns the list of stores added. ``download(url, dest)`` is injectable."""
    missing = [s for s in stores if not os.path.exists(os.path.join(data_dir, s))]
    if not missing:
        return []
    import zipfile

    os.makedirs(data_dir, exist_ok=True)
    if download is None:
        import urllib.request

        download = urllib.request.urlretrieve
    zip_path = os.path.join(data_dir, "databases.zip")
    download(url, zip_path)
    want = set(missing)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            top = member.replace("\\", "/").split("/")[0]
            if top in want:
                zf.extract(member, data_dir)
    os.remove(zip_path)
    return missing


# --- Chunk-health visibility -------------------------------------------------

def _strict_chunks_default():
    """Strict: --strict-chunks, FACTORIO_MCP_STRICT_CHUNKS env, or a dry-run (the
    measure-once gate must exit non-zero when the corpus isn't ready-to-cut)."""
    if "--strict-chunks" in sys.argv or dry_run_requested():
        return True
    return os.getenv("FACTORIO_MCP_STRICT_CHUNKS", "").lower() in ("1", "true", "yes", "on")


def dry_run_requested():
    """Dry-run: chunk + audit the FULL corpus with NO embed/write (the measure-once
    gate). Triggered by --dry-run on the command line or FACTORIO_MCP_DRY_RUN env.
    Dry-run implies strict so an unhealthy corpus exits non-zero."""
    return "--dry-run" in sys.argv or os.getenv("FACTORIO_MCP_DRY_RUN", "").lower() in (
        "1", "true", "yes", "on"
    )


class ChunkAuditor:
    """Accumulates chunk statistics during an ingest run and surfaces pathologies
    that are otherwise SILENT: empty/tiny chunks, chunks larger than the embedder
    can encode (truncated without error), per-source explosion (e.g. capturing
    every Lua table literal), and non-empty sources that produced zero chunks.

    Works for both buffered ingesters (``add_batch`` once) and streaming ones
    (``add`` per chunk). Call ``summary()`` at the end of every run: it always
    prints a report (the "forever visibility"), and in strict mode raises
    ``ChunkHealthError`` on a FAIL so CI / opt-in runs fail loudly.
    """

    def __init__(self, store, *, max_tokens=EMBED_MAX_TOKENS, min_chars=MIN_CHUNK_CHARS,
                 explosion_per_source=400, strict=None):
        self.store = store
        self.max_tokens = max_tokens
        self.min_chars = min_chars
        self.explosion_per_source = explosion_per_source
        self.strict = _strict_chunks_default() if strict is None else strict
        self.total = self.empty = self.tiny = self.oversized = 0
        self.dups = self.decode_replacements = self.skipped_large = 0
        self.dropped_tiny = 0  # tiny chunks dropped by normalize_chunks (pre-add)
        self._skipped_examples = []
        self._tok_sizes = []
        self._by_type = {}
        self._per_source = {}
        self._oversized_examples = []
        self._empty_sources = []

    def add(self, embedded_text, *, source=None, node_type=None):
        """Record one chunk, measured (in tokens) on the text that gets embedded."""
        text = embedded_text or ""
        tokens = count_tokens(text)
        self.total += 1
        self._tok_sizes.append(tokens)
        if node_type is not None:
            self._by_type[node_type] = self._by_type.get(node_type, 0) + 1
        if source is not None:
            self._per_source[source] = self._per_source.get(source, 0) + 1
        if not text.strip():
            self.empty += 1
        elif len(text) < self.min_chars:
            self.tiny += 1
        if tokens > self.max_tokens:
            self.oversized += 1
            if len(self._oversized_examples) < 5:
                self._oversized_examples.append((source, node_type, tokens))

    def add_batch(self, records, *, text_key, source_key=None, node_type_key="node_type"):
        for r in records:
            self.add(
                r.get(text_key, ""),
                source=r.get(source_key) if source_key else None,
                node_type=r.get(node_type_key),
            )

    def note_source(self, source, n_bytes, n_chunks):
        """Flag a source that yielded zero chunks despite having enough content to
        expect one (the silent-loss bug, e.g. a real Lua file whose AST captured
        only ``{}``). A file smaller than ``min_chars`` legitimately produces no
        chunk — flagging it is a false positive (4-byte JSON test fixtures), so the
        check is gated on ``n_bytes >= min_chars``."""
        if n_bytes >= self.min_chars and n_chunks == 0:
            self._empty_sources.append((source, n_bytes))

    def note_dups(self, n):
        """Record pure-duplicate chunks dropped during normalization."""
        self.dups += n

    def note_tiny(self, n):
        """Record sub-min-char chunks dropped during normalization. normalize_chunks
        drops these BEFORE add() ever sees them, so without this the auditor's tiny
        signal would silently read zero (a visibility regression of the per-file
        normalize move)."""
        self.dropped_tiny += n

    def note_decode_replacements(self, n):
        """Record files that needed UTF-8 replacement (possible binary/encoding issue)."""
        self.decode_replacements += n

    def note_skipped_file(self, source, n_chunks):
        """Record a bulk file skipped for exceeding the per-file chunk cap."""
        self.skipped_large += 1
        if len(self._skipped_examples) < 5:
            self._skipped_examples.append((source, n_chunks))

    def summary(self):
        """Print the health report; return a stats dict; raise in strict FAIL."""
        sizes = self._tok_sizes
        median = int(statistics.median(sizes)) if sizes else 0
        # explosion_per_source=None disables the check (for stores whose "source"
        # is a whole API dump, e.g. factorio's runtime-api.json -> thousands of
        # legitimate, distinct, token-capped doc chunks).
        explosions = sorted(
            ((s, c) for s, c in self._per_source.items()
             if self.explosion_per_source is not None and c > self.explosion_per_source),
            key=lambda x: -x[1],
        )

        problems, warnings = [], []
        if self.oversized:
            problems.append(f"{self.oversized} oversized (>{self.max_tokens} tokens -> silently truncated)")
        if explosions:
            problems.append(f"{len(explosions)} source(s) exploded (>{self.explosion_per_source} chunks each)")
        if self._empty_sources:
            problems.append(f"{len(self._empty_sources)} non-empty source(s) produced 0 chunks")
        if self.dups:
            warnings.append(f"{self.dups} pure-duplicate(s) dropped")
        if self.empty:
            warnings.append(f"{self.empty} empty")
        if self.tiny:
            warnings.append(f"{self.tiny} tiny (<{self.min_chars} chars)")
        if self.dropped_tiny:
            warnings.append(f"{self.dropped_tiny} tiny dropped by normalize (<{self.min_chars} chars)")
        if self.decode_replacements:
            warnings.append(f"{self.decode_replacements} file(s) needed utf-8 replacement")
        if self.skipped_large:
            warnings.append(f"{self.skipped_large} bulk file(s) skipped (> {MAX_CHUNKS_PER_FILE} chunks)")

        result = "FAIL" if problems else ("WARN" if warnings else "PASS")

        safe_print("")
        safe_print(f"=== Chunk health: {self.store} ===")
        safe_print(
            f"chunks: {self.total} | sources: {len(self._per_source)} | "
            f"tokens min/median/max: "
            f"{min(sizes) if sizes else 0}/{median}/{max(sizes) if sizes else 0}"
        )
        if self._by_type:
            types = " ".join(f"{k}={v}" for k, v in sorted(self._by_type.items(), key=lambda x: -x[1]))
            safe_print(f"node_types: {types}")
        for w in warnings:
            safe_print(f"  WARN: {w}")
        for p in problems:
            safe_print(f"  FAIL: {p}")
        for s, c in explosions[:5]:
            safe_print(f"    explosion: {s} = {c} chunks")
        for s, nt, tok in self._oversized_examples:
            safe_print(f"    oversized: {s} ({nt}) = {tok} tokens")
        for s, n in self._empty_sources[:5]:
            safe_print(f"    zero-chunk: {s} ({n} bytes)")
        for s, n in self._skipped_examples:
            safe_print(f"    skipped-bulk: {s} ({n} chunks)")
        safe_print(f"RESULT: {result}  (strict={'on' if self.strict else 'off'})")

        stats = {
            "store": self.store, "total": self.total, "result": result,
            "empty": self.empty, "tiny": self.tiny, "oversized": self.oversized,
            "dups": self.dups, "decode_replacements": self.decode_replacements,
            "skipped_large": self.skipped_large,
            "explosions": explosions, "by_type": dict(self._by_type),
            "empty_sources": list(self._empty_sources),
            "tokens": {
                "min": min(sizes) if sizes else 0, "median": median,
                "max": max(sizes) if sizes else 0,
            },
        }
        if self.strict and result == "FAIL":
            raise ChunkHealthError(f"{self.store}: chunk-health FAIL -> {'; '.join(problems)}")
        return stats


_MODEL = None


def gpu_torch_warning(cuda_available=None, has_nvidia_smi=None):
    """Return an alert string if an NVIDIA GPU is present but torch can't use it
    (CPU-only wheel, or a CUDA wheel with a broken/mismatched driver) — embedding
    would silently fall back to CPU and run ~10-20x slower. Returns None when the
    config is fine (GPU+CUDA, or genuinely no GPU). Args are injectable for tests."""
    import shutil

    if has_nvidia_smi is None:
        has_nvidia_smi = shutil.which("nvidia-smi") is not None
    if cuda_available is None:
        import torch

        cuda_available = torch.cuda.is_available()
    if has_nvidia_smi and not cuda_available:
        return ("NVIDIA GPU detected but torch cannot use CUDA (CPU-only wheel?) — "
                "embedding will run on CPU (much slower). Run `make sync` to install "
                "the CUDA wheel.")
    return None


def load_embedder():
    """Load the shared SentenceTransformer once (CUDA->CPU auto, env override)."""
    global _MODEL
    if _MODEL is None:
        # Imported lazily: defining/using the chunk helpers (and the whole test
        # suite, which uses a fake embedder) must not pull in torch (~200MB).
        import torch
        from sentence_transformers import SentenceTransformer

        device = "cuda" if torch.cuda.is_available() else "cpu"
        warning = gpu_torch_warning(cuda_available=(device == "cuda"))
        if warning:
            safe_print(f"WARNING: {warning}")
        model_name = os.getenv("EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5")
        safe_print(f"Loading embedding model {model_name} on {device}...")
        _MODEL = SentenceTransformer(model_name, device=device)
    return _MODEL


def embed(texts, model=None):
    """Encode a list of texts into 768-dim L2-normalized vectors."""
    model = model or load_embedder()
    vectors = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)
    if len(vectors) and vectors.shape[1] != EMBEDDING_DIM:
        raise ChunkHealthError(
            f"Embedding dimension {vectors.shape[1]} != expected {EMBEDDING_DIM}; "
            "the embedding model is misconfigured (every store must share 768-dim vectors)."
        )
    return vectors


# --- Hybrid retrieval (shared so server.py and tests use one implementation) ---
# RRF over the ingest-built FTS index + vector. A store with NO FTS index falls
# back to pure vector, cached per table (permanent — correct). A transient/
# query-specific hybrid error (a lock, an FTS-parser-hostile query) is NOT cached:
# it falls back for that one query only, so a single odd query can't permanently
# disable hybrid for the whole process.
_RRF = None
_NO_HYBRID = set()  # id(table) -> table genuinely has no FTS index (permanent)


def _rrf():
    global _RRF
    if _RRF is None:
        from lancedb.rerankers import RRFReranker

        _RRF = RRFReranker()
    return _RRF


def is_missing_fts_error(msg):
    """True if the error means the table has no FTS index (structural, permanent),
    vs a transient/query-specific failure. LanceDB raises e.g. 'Cannot perform full
    text search unless an INVERTED index has been created ...'."""
    m = str(msg).lower()
    return "inverted index" in m or ("full text search" in m and "index" in m)


def _vector_only(table, vec, limit, where):
    q = table.search(vec)
    if where:
        q = q.where(where)
    return q.limit(limit).to_list()


def hybrid_search(table, query_str, query_vec, limit, where=None):
    """Top-``limit`` rows via hybrid search (RRF over FTS + vector), else pure
    vector. ``query_vec`` is the already-encoded query embedding; ``query_str``
    drives the FTS half. ``where`` is an optional pre-built (and SQL-escaped) filter
    clause, threaded through both the hybrid and the vector-fallback paths."""
    vec = query_vec.tolist() if hasattr(query_vec, "tolist") else query_vec
    if id(table) not in _NO_HYBRID:
        try:
            q = table.search(query_type="hybrid").vector(vec).text(query_str).rerank(_rrf())
            if where:
                q = q.where(where)
            return q.limit(limit).to_list()
        except Exception as e:
            detail = str(e)[:160].encode("ascii", "replace").decode("ascii")
            if is_missing_fts_error(e):
                _NO_HYBRID.add(id(table))  # permanent: this store has no FTS index
                # stderr, not safe_print: the MCP server speaks JSON on stdout.
                print(f"No FTS index for a store ({detail}); using vector search.", file=sys.stderr)
            else:
                # transient/query-specific — do NOT poison the table; this query only.
                print(f"Hybrid failed for one query ({detail}); vector fallback for it.", file=sys.stderr)
    return _vector_only(table, vec, limit, where)


_TOKENIZER = None


def count_tokens(text):
    """Number of tokens for ``text`` via the embedder's real tokenizer (cached).

    Lazy-loads only the tokenizer (~MBs), never the 200MB model. This is the unit
    of truth for chunk sizing — a character proxy under-counts code (which
    tokenizes far denser than prose) and would let truncated chunks pass. Tests
    monkeypatch this to stay offline.
    """
    global _TOKENIZER
    if _TOKENIZER is None:
        from transformers import AutoTokenizer

        model_name = os.getenv("EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5")
        _TOKENIZER = AutoTokenizer.from_pretrained(model_name)
        # We measure (then split) texts longer than 512; raise the limit so the
        # tokenizer doesn't log a "longer than max" warning for every measurement.
        _TOKENIZER.model_max_length = 10 ** 9
    return len(_TOKENIZER.encode(text or "", add_special_tokens=False))


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
(function_definition) @function
(table_constructor) @table
"""

# node_types the CODE chunkers (clusterio + repo) emit: the tree-sitter capture
# names ('class', 'interface', 'function', 'method', 'table') plus the non-AST
# fallbacks. Single source of truth for the server filter docstring and the gate
# test. (The factorio doc ingester parses an API schema, not source, so it has its
# own vocab — 'prototype_property', 'attribute', 'event', ... — not listed here.)
NODE_TYPES = frozenset({
    "class", "interface", "function", "method", "table",  # tree-sitter captures
    "node",         # a captured node with no specific capture name
    "text_chunk",   # line-window fallback for code with no usable AST
    "text_file",    # non-code files (prose/config)
})

# --- Prototype type vocab (shared by ingest_prototypes.py and server.py) -------
# prototypes_lancedb stores each prototype's RAW Lua type (e.g. "ammo", "module",
# "furnace"). These sets keep the ingester's routing and the server's filter in
# sync. PROTOTYPE_TYPE_GROUPS lets search_factorio_prototypes accept the umbrella
# values "item"/"entity" and expand them to every raw subtype actually stored.
PROTOTYPE_ENTITY_TYPES = frozenset({
    "assembling-machine", "furnace", "rocket-silo", "mining-drill", "lab",
    "boiler", "generator", "reactor", "beacon", "inserter", "transport-belt",
    "splitter", "underground-belt", "storage-tank", "pipe", "pipe-to-ground",
    "train-stop", "locomotive", "cargo-wagon", "fluid-wagon", "electric-pole",
    "offshore-pump", "accumulator", "solar-panel", "electric-turret",
    "fluid-turret", "ammo-turret", "pump", "cargo-landing-pad",
    "space-platform-hub", "asteroid-collector", "agricultural-tower",
    "thruster", "lightning-attractor", "fusion-reactor", "fusion-generator",
    "cargo-bay",
})

PROTOTYPE_ITEM_TYPES = frozenset({
    "item", "ammo", "capsule", "gun", "rail-planner", "repair-tool",
    "selection-tool", "item-with-entity-data", "module", "tool", "armor",
    "mining-tool", "spidertron-remote", "space-platform-starter-pack",
})

# Umbrella filter values -> the raw subtypes they cover. An exact subtype (e.g.
# "module", "furnace") still matches itself; recipe/fluid/technology/quality/etc.
# are stored under their own type and match directly.
PROTOTYPE_TYPE_GROUPS = {
    "item": PROTOTYPE_ITEM_TYPES,
    "entity": PROTOTYPE_ENTITY_TYPES,
}

# --- Factorio version pinning (shared by ingest_factorio.py + server's docs filter) ---
# Concrete, hard-coded versions only. The moving "latest" label is deliberately NOT
# used — it drifted (latest silently became 2.1.x while callers expected an earlier
# release), so callers must query by a concrete version. 1.1.110 is legacy 1.1;
# 2.0.76 is the stable 2.x baseline; 2.1.8 is the experimental release mod creators
# are already targeting to prepare their mods. The docs site serves per-version URLs
# (lua-api.factorio.com/<ver>/), so each is scraped concretely.
SUPPORTED_FACTORIO_VERSIONS = ("1.1.110", "2.0.76", "2.1.8")

# Versions the prototypes store holds. This is a SUBSET of SUPPORTED_FACTORIO_VERSIONS:
# prototype values come from a `factorio --dump-data` export, and there's no practical
# vanilla dump for the 1.1 era (different game/schema), so 1.1.110 is docs-only. Each
# 2.x version is a separate dump (factorio-export/vanilla_<ver>/) ingested as its own
# version-scoped rows; search_factorio_prototypes REQUIRES one of these.
SUPPORTED_PROTOTYPE_VERSIONS = ("2.0.76", "2.1.8")

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


def _norm(text):
    """Whitespace-normalized form, for exact-duplicate detection."""
    return " ".join(text.split())


def _hard_split(text, max_tokens):
    """Last resort for an unbreakable run (e.g. a minified single line): split by
    characters into pieces each <= ``max_tokens``. Splitting mid-token is accepted
    here only because the alternative is silent truncation by the embedder."""
    pieces, i = [], 0
    approx = max(1, max_tokens * 3)  # conservative chars/token for dense code
    while i < len(text):
        piece = text[i:i + approx]
        while count_tokens(piece) > max_tokens and len(piece) > 1:
            piece = piece[: max(1, int(len(piece) * 0.8))]
        pieces.append(piece)
        i += len(piece)
    return pieces


def _line_windows(text, max_tokens, overlap_frac=0.1):
    """Split ``text`` into windows each <= ``max_tokens``, preferring line
    boundaries with ~``overlap_frac`` overlap; a single line over budget is
    hard-split by characters (last resort) so no window ever exceeds the cap."""
    lines = text.split("\n")
    counts = [count_tokens(ln) + 1 for ln in lines]  # +1 approximates the newline
    windows, i, n = [], 0, len(lines)
    while i < n:
        j, total = i, 0
        while j < n and (j == i or total + counts[j] <= max_tokens):
            total += counts[j]
            j += 1
        window = "\n".join(lines[i:j]).strip()
        if window:
            if count_tokens(window) > max_tokens:
                windows.extend(_hard_split(window, max_tokens))
            else:
                windows.append(window)
        if j >= n:
            break
        i = max(i + 1, j - int((j - i) * overlap_frac))
    return windows


def _emit(out, seen, name, ntype, text):
    """Append a chunk, skipping empties and exact (normalized) duplicates."""
    text = text.strip()
    if not text:
        return
    key = _norm(text)
    if key in seen:
        return
    seen.add(key)
    out.append({"node_name": name, "node_type": ntype, "content": text})


def _split_node(node, name, ntype, max_tokens, out, seen):
    """Emit ``node`` as one chunk if it fits, else split recursively by child
    nodes (cAST), falling to a line-window only at a leaf."""
    text = node.text.decode("utf-8", "replace")
    if count_tokens(text) <= max_tokens:
        _emit(out, seen, name, ntype, text)
        return
    named = [c for c in node.children if c.is_named]
    if len(named) <= 1:
        for w in _line_windows(text, max_tokens):
            _emit(out, seen, name, ntype, w)
        return
    group = []

    def flush():
        if not group:
            return
        gtext = "\n".join(c.text.decode("utf-8", "replace") for c in group)
        if count_tokens(gtext) <= max_tokens:
            _emit(out, seen, name, ntype, gtext)
        else:
            for w in _line_windows(gtext, max_tokens):
                _emit(out, seen, name, ntype, w)
        group.clear()

    for c in named:
        ctext = c.text.decode("utf-8", "replace")
        if count_tokens(ctext) > max_tokens:
            flush()
            _split_node(c, name, ntype, max_tokens, out, seen)
        else:
            trial = "\n".join(x.text.decode("utf-8", "replace") for x in group) + "\n" + ctext
            if group and count_tokens(trial) > max_tokens:
                flush()
            group.append(c)
    flush()


def extract_ast_chunks(src_bytes, kind, include_comments=False, max_tokens=CONTENT_MAX_TOKENS):
    """Parse ``src_bytes`` and return ``{'node_name','node_type','content'}`` chunks.

    Captures only TOP-LEVEL declarations (no captured ancestor), so a nested node
    is never stored both standalone and inside its parent (the duplication that
    exploded factorio-data). A top-level node over ``max_tokens`` is split
    recursively by its child nodes (a big class at its methods, a big
    ``data:extend{...}`` per entry), down to a line-window only at a leaf. Returns
    ``None`` when tree-sitter/kind is unavailable (caller falls back to text).
    """
    lq = _lang_and_query(kind)
    if lq is None:
        return None
    lang, query = lq
    tree = Parser(lang).parse(src_bytes)
    captures = QueryCursor(query).captures(tree.root_node)

    cap_name = {}
    for nm, nodes in captures.items():
        for nd in nodes:
            cap_name[nd.id] = nm
    captured_ids = set(cap_name)

    def has_captured_ancestor(node):
        p = node.parent
        while p is not None:
            if p.id in captured_ids:
                return True
            p = p.parent
        return False

    tops = sorted(
        (nd for nodes in captures.values() for nd in nodes if not has_captured_ancestor(nd)),
        key=lambda n: n.start_byte,
    )

    out, seen = [], set()
    for node in tops:
        name = _node_name(node)
        ntype = cap_name.get(node.id, "node")
        if include_comments:
            comments = _preceding_comments(node)
            if comments:
                combined = f"{comments}\n{node.text.decode('utf-8', 'replace')}"
                if count_tokens(combined) <= max_tokens:
                    _emit(out, seen, name, ntype, combined)
                    continue
        _split_node(node, name, ntype, max_tokens, out, seen)
    return out


def _non_import_body_chars(code):
    """Sum of chars on lines that are real code — the coverage *denominator*.

    Excludes blank lines, comments (``//`` ``/* */`` ``--`` ``#``), and
    import/require/re-export statements **including multi-line** ``import { ... }
    from '...'`` / ``export { ... } from '...'`` blocks. AST captures declarations,
    not imports/comments, so counting those would make an import-heavy (or
    comment-heavy, since repo ingest uses ``include_comments=False``) module look
    falsely uncovered and collapse to coarse text chunks. Stateful so a multi-line
    import's continuation lines are excluded too, not just the opener."""
    total = 0
    in_import = in_block_comment = False
    for ln in code.splitlines():
        s = ln.strip()
        if not s:
            continue
        if in_block_comment:
            if "*/" in s:
                in_block_comment = False
            continue
        if s.startswith("/*"):
            if "*/" not in s:
                in_block_comment = True
            continue
        if s.startswith(("//", "--", "#")):
            continue
        if in_import:
            # continuation of a multi-line import/export clause; it ends on the
            # line bearing `from` or a bare close of the brace/paren list.
            if " from " in s or s.endswith(";") or s.startswith(("}", ")")):
                in_import = False
            continue
        is_import = (
            s.startswith(("import ", "from ")) or "require(" in s or "require " in s
            or (s.startswith("export ") and (" from " in s or s.startswith(("export {", "export *"))))
        )
        if is_import:
            # a multi-line opener (`import {` / `export {` with no `from`/`;` yet)
            # starts an exclusion run until its clause closes.
            if s.startswith(("import", "export")) and " from " not in s and not s.endswith(";"):
                in_import = True
            continue
        total += len(ln)
    return total


def chunk_code(src_bytes, kind, include_comments=False, max_tokens=CONTENT_MAX_TOKENS):
    """AST chunks for a code file, with a coverage-based fallback to text-line
    chunks. Falls back only when the AST is unavailable/empty OR misses a
    substantial fraction of the file's *non-import* code — so content the grammar's
    captures legitimately miss (top-level statements, arrow functions assigned to
    locals, ``data:extend`` lists) is never silently dropped, while import-heavy
    modules keep their per-declaration AST chunks instead of collapsing to text.
    Returns ``[{'node_name','node_type','content'}]``."""
    ast = (
        extract_ast_chunks(src_bytes, kind, include_comments=include_comments, max_tokens=max_tokens)
        if kind else None
    )
    code = src_bytes.decode("utf-8", "replace")
    if ast:
        body = _non_import_body_chars(code)
        covered = sum(len(c["content"]) for c in ast)
        if body == 0 or covered >= 0.5 * body:
            return ast
    return text_chunks_by_line(code)


def normalize_chunks(chunks, *, content_key="content", max_tokens=CONTENT_MAX_TOKENS,
                     min_chars=MIN_CHUNK_CHARS, dedup=True):
    """Enforce chunk-health invariants on a list of chunk dicts and report what was
    dropped. Drops chunks whose stripped content < ``min_chars``, token-splits any
    chunk over ``max_tokens`` into line-windows, and (when ``dedup``) drops exact
    duplicate content within the batch. The text lives under ``content_key``.

    ``dedup=False`` is for stores where identical text is legitimately distinct
    (e.g. factorio docs whose text repeats across versions, distinguished only by
    metadata). Returns ``(normalized_chunks, {"dropped_tiny", "dropped_dup"})`` —
    the universal guarantee every ingester applies before embedding.
    """
    out, seen = [], set()
    dropped_tiny = dropped_dup = 0
    for ch in chunks:
        text = (ch.get(content_key) or "")
        if len(text.strip()) < min_chars:
            dropped_tiny += 1
            continue
        pieces = [text] if count_tokens(text) <= max_tokens else _line_windows(text, max_tokens)
        for piece in pieces:
            if len(piece.strip()) < min_chars:
                dropped_tiny += 1
                continue
            if dedup:
                key = _norm(piece)
                if key in seen:
                    dropped_dup += 1
                    continue
                seen.add(key)
            new_chunk = dict(ch)
            new_chunk[content_key] = piece
            out.append(new_chunk)
    return out, {"dropped_tiny": dropped_tiny, "dropped_dup": dropped_dup}


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
