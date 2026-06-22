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
EMBED_MAX_TOKENS = 512        # auditor cap on the full embedded text (incl. context prefix)
CONTENT_MAX_TOKENS = 400      # cap on a chunk's raw content; leaves headroom for the prefix
MIN_CHUNK_CHARS = 10          # drop a chunk whose stripped raw content is shorter than this


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
        self.dups = self.decode_replacements = 0
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
        """Flag a non-empty source file/page that yielded zero chunks (silent loss)."""
        if n_bytes > 0 and n_chunks == 0:
            self._empty_sources.append((source, n_bytes))

    def note_dups(self, n):
        """Record pure-duplicate chunks dropped during normalization."""
        self.dups += n

    def note_decode_replacements(self, n):
        """Record files that needed UTF-8 replacement (possible binary/encoding issue)."""
        self.decode_replacements += n

    def summary(self):
        """Print the health report; return a stats dict; raise in strict FAIL."""
        sizes = self._tok_sizes
        median = int(statistics.median(sizes)) if sizes else 0
        explosions = sorted(
            ((s, c) for s, c in self._per_source.items() if c > self.explosion_per_source),
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
        if self.decode_replacements:
            warnings.append(f"{self.decode_replacements} file(s) needed utf-8 replacement")

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
        safe_print(f"RESULT: {result}  (strict={'on' if self.strict else 'off'})")

        stats = {
            "store": self.store, "total": self.total, "result": result,
            "empty": self.empty, "tiny": self.tiny, "oversized": self.oversized,
            "dups": self.dups, "decode_replacements": self.decode_replacements,
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


def load_embedder():
    """Load the shared SentenceTransformer once (CUDA->CPU auto, env override)."""
    global _MODEL
    if _MODEL is None:
        # Imported lazily: defining/using the chunk helpers (and the whole test
        # suite, which uses a fake embedder) must not pull in torch (~200MB).
        import torch
        from sentence_transformers import SentenceTransformer

        device = "cuda" if torch.cuda.is_available() else "cpu"
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
