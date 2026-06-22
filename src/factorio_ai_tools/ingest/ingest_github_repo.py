"""Ingest any GitHub repo (or local dir) into the shared ``data/repo_lancedb`` store.

This is the generalized successor of the old per-mod Lua ingester: one store,
table ``codebase``, holding many repos distinguished by the ``repo_url`` column
(the repo basename). Code-aware chunking via tree-sitter for TypeScript/JS and
Lua; everything else is line-window text-chunked. Incremental by per-file
SHA-256 keyed on (repo_url, file_path): unchanged files are skipped, changed
files are deleted-then-re-added (so re-running no longer duplicates rows).
"""

import argparse
import os
import shutil
import subprocess
import sys

import pyarrow as pa

from factorio_ai_tools.ingest import common

SCHEMA = pa.schema([
    pa.field("vector", pa.list_(pa.float32(), common.EMBEDDING_DIM)),
    pa.field("repo_url", pa.string()),
    pa.field("file_path", pa.string()),
    pa.field("node_name", pa.string()),
    pa.field("node_type", pa.string()),
    pa.field("content", pa.string()),
    pa.field("content_hash", pa.string()),
])

IGNORED_DIRS = {".git", "node_modules", "dist", "build", "__pycache__", "venv", ".venv", "out", "target"}
SUPPORTED_EXTS = {
    ".ts", ".js", ".tsx", ".jsx", ".json", ".yaml", ".yml",
    ".py", ".sh", ".bash", ".lua", ".md", ".txt", ".toml",
    ".rs", ".cpp", ".c", ".h", ".go", ".dockerfile",
}


def chunk_file(src_bytes, ext):
    """Return [{'node_name','node_type','content'}] for a file's bytes.

    .ts/.tsx -> TypeScript AST; .lua -> Lua AST; anything else (incl. .js/.jsx)
    -> line-window text chunks. Falls back to text chunks when the AST yields
    nothing (e.g. a grammar that can't parse the dialect).
    """
    kind = None
    if ext in (".ts", ".tsx"):
        kind = "typescript"
    elif ext == ".lua":
        kind = "lua"

    chunks = common.extract_ast_chunks(src_bytes, kind) if kind else None
    if not chunks:  # None (unsupported/unavailable) or [] (no declarations found)
        code = src_bytes.decode("utf-8", "replace")
        chunks = common.text_chunks_by_line(code)
    return chunks


def main():
    parser = argparse.ArgumentParser(description="Ingest a generic GitHub repository into the LanceDB pipeline.")
    parser.add_argument("--repo-url", type=str, help="GitHub URL to clone and ingest")
    parser.add_argument("--local-path", type=str, help="Local directory path to ingest")
    args = parser.parse_args()

    if not args.repo_url and not args.local_path:
        common.safe_print("Error: Must provide either --repo-url or --local-path")
        sys.exit(1)

    if args.repo_url:
        repo_name = args.repo_url.split("/")[-1].replace(".git", "")
        target_dir = os.path.join(common.REPO_ROOT, ".mod_temp", repo_name)
        if os.path.exists(target_dir):
            common.safe_print(f"Removing existing temp dir: {target_dir}")
            shutil.rmtree(target_dir)
        common.safe_print(f"Cloning {args.repo_url} into {target_dir}...")
        os.makedirs(os.path.join(common.REPO_ROOT, ".mod_temp"), exist_ok=True)
        subprocess.run(["git", "clone", "--depth", "1", args.repo_url, target_dir], check=True)
    else:
        target_dir = args.local_path
        repo_name = os.path.basename(os.path.abspath(target_dir))

    common.safe_print(f"Ingesting repository: {repo_name} from {target_dir}")

    db, _db_path = common.connect_store("repo_lancedb")
    table = common.ensure_table(db, "codebase", SCHEMA)
    model = common.load_embedder()

    safe_repo = repo_name.replace("'", "''")
    has_rows = len(table) > 0

    batch = []
    batch_size = 50
    total_chunks = 0
    skipped_files = 0

    def flush():
        nonlocal batch, total_chunks
        if not batch:
            return
        vectors = common.embed([b["text_to_embed"] for b in batch], model)
        rows = [{
            "vector": vectors[i].tolist(),
            "repo_url": b["repo_url"],
            "file_path": b["file_path"],
            "node_name": b["node_name"],
            "node_type": b["node_type"],
            "content": b["content"],
            "content_hash": b["content_hash"],
        } for i, b in enumerate(batch)]
        table.add(rows)
        total_chunks += len(rows)
        common.safe_print(f"Ingested {total_chunks} chunks...")
        batch = []

    for root, dirs, files in os.walk(target_dir):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]

        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext not in SUPPORTED_EXTS and "dockerfile" not in file.lower():
                continue

            file_path = os.path.join(root, file)
            rel_path = os.path.relpath(file_path, target_dir)

            try:
                with open(file_path, "rb") as fh:
                    src_bytes = fh.read()
            except OSError:
                continue
            f_hash = common.get_hash(src_bytes)

            # Incremental: skip unchanged, else delete this file's rows and re-add.
            if has_rows:
                safe_path = rel_path.replace("'", "''")
                where = f"repo_url = '{safe_repo}' AND file_path = '{safe_path}'"
                existing = table.search().where(where).limit(1).to_list()
                if existing and existing[0].get("content_hash") == f_hash:
                    skipped_files += 1
                    continue
                table.delete(where)

            for chunk in chunk_file(src_bytes, ext):
                context_text = (f"File: {rel_path}\nComponent: {chunk['node_name']}\n"
                                f"Type: {chunk['node_type']}\nCode:\n{chunk['content']}")
                batch.append({
                    "text_to_embed": context_text,
                    "repo_url": repo_name,
                    "file_path": rel_path,
                    "node_name": chunk["node_name"],
                    "node_type": chunk["node_type"],
                    "content": chunk["content"],
                    "content_hash": f_hash,
                })
                if len(batch) >= batch_size:
                    flush()

    flush()
    common.safe_print(f"Skipped {skipped_files} unchanged files.")
    common.safe_print(f"\nDone! Ingested {total_chunks} total chunks for repository {repo_name}.")


if __name__ == "__main__":
    main()
