"""Ingest a Clusterio TypeScript checkout into ``data/clusterio_lancedb``.

Code-aware: ``.ts``/``.js`` files are chunked per top-level declaration via
tree-sitter (with preceding comments); other files fall back to 1500-char text
chunks. Incremental by per-file SHA-256. Repo path from ``CLUSTERIO_REPO`` (env;
default ``./clusterio``). Writes ``version.txt`` from the repo's ``package.json``
(read by server.get_mcp_version_info).
"""

import glob
import json
import os

from lancedb.pydantic import LanceModel, Vector

from factorio_ai_tools.ingest import common

EXTENSIONS = ['*.ts', '*.js', '*.json', '*.md', '*.yml', '*.yaml', '*.lua',
              '*.sh', '*.bat', '*.ps1', '*.toml', '*.ini', 'Dockerfile']


class CodeChunk(LanceModel):
    file_path: str
    node_type: str
    node_name: str
    content: str
    content_hash: str
    vector: Vector(common.EMBEDDING_DIM)


def extract_chunks(file_path, src_bytes, content_hash):
    """AST chunks (with comments) for a .ts/.js file; coverage-fallback to text."""
    return [{
        "file_path": file_path,
        "node_type": c["node_type"],
        "node_name": c["node_name"],
        "content": c["content"],
        "content_hash": content_hash,
    } for c in common.chunk_code(src_bytes, "typescript", include_comments=True)]


def extract_text_chunks(file_path, content, content_hash):
    """1500-char/200-overlap fallback chunks for non-code files."""
    file_name = os.path.basename(file_path)
    return [{
        "file_path": file_path,
        "node_type": "text_file",
        "node_name": file_name,
        "content": chunk,
        "content_hash": content_hash,
    } for chunk in common.text_chunks_by_char(content, 1500, 200)]


def main():
    common.safe_print("Finding files to ingest...")
    repo_path = os.environ.get("CLUSTERIO_REPO", "./clusterio")

    all_files = []
    for ext in EXTENSIONS:
        for f in glob.glob(f"{repo_path}/**/{ext}", recursive=True):
            if not common.is_ignored_path(f):
                all_files.append(f)
    common.safe_print(f"Found {len(all_files)} total files.")

    dry = common.dry_run_requested()
    if dry:
        common.safe_print("DRY RUN: chunk + audit only, no embed/write.")
        db = db_path = table = None
    else:
        common.safe_print("Connecting to LanceDB...")
        db, db_path = common.connect_store("clusterio_lancedb")
        table = common.ensure_table(db, "codebase", CodeChunk)

    common.safe_print("Extracting chunks...")
    auditor = common.ChunkAuditor("clusterio_lancedb")
    all_chunks = []
    skipped_count = 0
    for f in all_files:
        try:
            with open(f, 'rb') as file:
                content_bytes = file.read()
            f_hash = common.get_hash(content_bytes)
        except OSError:
            continue

        # Store a clean repo-relative path (e.g. plugins/player_auth/..., not
        # ./clusterio\plugins\...) so results read well and per-plugin filtering works.
        rel_path = os.path.relpath(f, repo_path).replace(os.sep, "/")
        safe_f = rel_path.replace("'", "''")
        if table is not None and len(table) > 0:
            existing = table.search().where(f"file_path = '{safe_f}'").limit(1).to_list()
            if existing and existing[0].get('content_hash') == f_hash:
                skipped_count += 1
                continue
            table.delete(f"file_path = '{safe_f}'")

        if f.endswith('.ts') or f.endswith('.js'):
            file_chunks = extract_chunks(rel_path, content_bytes, f_hash)
        else:
            try:
                file_chunks = extract_text_chunks(rel_path, content_bytes.decode('utf8'), f_hash)
            except UnicodeDecodeError:
                file_chunks = []
        auditor.note_source(rel_path, len(content_bytes), len(file_chunks))
        all_chunks.extend(file_chunks)

    common.safe_print(f"Skipped {skipped_count} unchanged files.")
    common.safe_print(f"Extracted {len(all_chunks)} new/modified chunks.")

    all_chunks, nstats = common.normalize_chunks(all_chunks, content_key="content")
    auditor.note_dups(nstats["dropped_dup"])
    common.safe_print(
        f"Normalized to {len(all_chunks)} chunks "
        f"(dropped {nstats['dropped_tiny']} tiny, {nstats['dropped_dup']} dup)."
    )
    auditor.add_batch(all_chunks, text_key="content", source_key="file_path")
    auditor.summary()

    if dry:
        return
    if len(all_chunks) == 0:
        common.safe_print("Database is perfectly up to date!")
        _write_version(repo_path, db_path)
        return

    model = common.load_embedder()
    batch_size = 100
    for i in range(0, len(all_chunks), batch_size):
        common.safe_print(f"Ingesting batch {i} to {i + batch_size}...")
        batch = all_chunks[i:i + batch_size]
        embeddings = common.embed([c["content"] for c in batch], model)
        for j, item in enumerate(batch):
            item["vector"] = embeddings[j].tolist()
        table.add(batch)

    try:
        table.create_fts_index("content", replace=True)
    except Exception as e:
        common.safe_print(f"FTS index skipped: {e}")
    _write_version(repo_path, db_path)
    common.safe_print("Ingestion complete!")


def _write_version(repo_path, db_path):
    package_json_path = os.path.join(repo_path, "package.json")
    version = "unknown"
    if os.path.exists(package_json_path):
        try:
            with open(package_json_path, "r", encoding="utf-8") as f:
                version = json.load(f).get("version", "unknown")
        except (OSError, json.JSONDecodeError):
            pass
    with open(os.path.join(db_path, "version.txt"), "w", encoding="utf-8") as f:
        f.write(version)


if __name__ == "__main__":
    main()
