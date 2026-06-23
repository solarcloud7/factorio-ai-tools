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


def extract_chunks(file_path, src_bytes, content_hash, kind):
    """AST chunks (with comments) for a code file; coverage-fallback to text.
    ``kind`` comes from ``common.kind_for_ext`` so .lua is AST'd as Lua, not
    forced through the TypeScript grammar (which dropped every Lua file)."""
    return [{
        "file_path": file_path,
        "node_type": c["node_type"],
        "node_name": c["node_name"],
        "content": c["content"],
        "content_hash": content_hash,
    } for c in common.chunk_code(src_bytes, kind, include_comments=True)]


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
    seen_paths = set()  # every current file -> orphan reconcile after the loop
    for f in all_files:
        try:
            with open(f, 'rb') as file:
                content_bytes = file.read()
            f_hash = common.get_hash(content_bytes)
        except OSError:
            continue

        # Store a clean repo-relative path (e.g. plugins/player_auth/..., not
        # ./clusterio\plugins\...) so results read well and per-plugin filtering works.
        rel_path = common.to_posix(os.path.relpath(f, repo_path))
        seen_paths.add(rel_path)
        safe_f = rel_path.replace("'", "''")
        if table is not None and len(table) > 0:
            existing = table.search().where(f"file_path = '{safe_f}'").limit(1).to_list()
            if existing and existing[0].get('content_hash') == f_hash:
                skipped_count += 1
                continue
            table.delete(f"file_path = '{safe_f}'")

        kind = common.kind_for_ext(os.path.splitext(f)[1])
        if kind:
            file_chunks = extract_chunks(rel_path, content_bytes, f_hash, kind)
        else:
            text = content_bytes.decode('utf8', 'replace')
            if "�" in text:
                auditor.note_decode_replacements(1)
            file_chunks = extract_text_chunks(rel_path, text, f_hash)

        # Normalize PER FILE (not store-wide): a store-wide dedup collapses two
        # distinct files that happen to share content (e.g. re-export index.ts),
        # losing one file's rows entirely.
        file_chunks, nstats = common.normalize_chunks(file_chunks, content_key="content")
        auditor.note_dups(nstats["dropped_dup"])
        if len(file_chunks) > common.MAX_CHUNKS_PER_FILE:
            common.safe_print(f"Skipping bulk file {rel_path} ({len(file_chunks)} chunks).")
            auditor.note_skipped_file(rel_path, len(file_chunks))
            continue
        auditor.note_source(rel_path, len(content_bytes), len(file_chunks))
        all_chunks.extend(file_chunks)

    # Orphan reconcile: drop rows for files removed from the checkout since the
    # last run. Guarded on a non-empty set so a misconfigured CLUSTERIO_REPO
    # (zero files) can't wipe the whole store.
    if table is not None and len(table) > 0 and seen_paths:
        quoted = ", ".join("'" + p.replace("'", "''") + "'" for p in sorted(seen_paths))
        table.delete(f"file_path NOT IN ({quoted})")

    common.safe_print(f"Skipped {skipped_count} unchanged files.")
    common.safe_print(f"Extracted {len(all_chunks)} new/modified chunks.")

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
