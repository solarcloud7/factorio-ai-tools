import os
import sys
import argparse
import subprocess
import shutil
import glob
from pathlib import Path

# Adjust path to find sibling modules if needed, or rely on venv
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.append(REPO_ROOT)

import lancedb
import pyarrow as pa
import torch
from sentence_transformers import SentenceTransformer

def get_data_dir():
    local_data_dir = os.path.join(REPO_ROOT, "data")
    if os.path.exists(local_data_dir) or os.getenv("FACTORIO_MCP_LOCAL_MODE"):
        return local_data_dir
    return os.path.expanduser("~/.factorio-ai-tools/data")

def init_lancedb():
    data_dir = get_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "repo_lancedb")
    return lancedb.connect(db_path)

# Safe imports for tree-sitter
try:
    from tree_sitter import Language, Parser
    import tree_sitter_typescript as tst
    import tree_sitter_lua as tslua
    HAS_TREE_SITTER = True
except ImportError:
    HAS_TREE_SITTER = False
    print("Warning: tree-sitter packages not fully available. Falling back to text chunking for all files.")

def get_parser(ext: str):
    if not HAS_TREE_SITTER:
        return None, None
    try:
        if ext in ('.ts', '.tsx'):
            return Language(tst.language_typescript()), Parser()
        elif ext in ('.js', '.jsx'):
            # Typescript parser usually works for JS too, or we can use generic chunking
            return None, None
        elif ext == '.lua':
            return Language(tslua.language()), Parser()
    except Exception as e:
        print(f"Error initializing tree-sitter for {ext}: {e}")
    return None, None

def chunk_file_ast(file_path: str, ext: str) -> list[dict]:
    """Returns chunks dicts: {'node_name': str, 'node_type': str, 'content': str}"""
    chunks = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            code = f.read()
    except Exception:
        return chunks

    if not code.strip():
        return chunks

    lang, parser = get_parser(ext)
    if lang is None or parser is None:
        return chunk_file_text(code)

    parser.set_language(lang)
    tree = parser.parse(bytes(code, "utf8"))

    # We want top level classes, functions, and interfaces.
    # A simple tree walk:
    def traverse(node):
        if node.type in ("function_declaration", "class_declaration", "method_definition", "interface_declaration", "function"):
            name = "unknown"
            for child in node.children:
                if child.type == "identifier":
                    name = code[child.start_byte:child.end_byte]
                    break
            
            chunk_text = code[node.start_byte:node.end_byte]
            chunks.append({
                "node_name": name,
                "node_type": node.type,
                "content": chunk_text
            })
        elif node.type == "program" or node.type == "chunk" or node.type == "export_statement" or node.type == "declaration":
            for child in node.children:
                traverse(child)
                
    traverse(tree.root_node)
    
    # If AST didn't find anything substantial, fallback to full text
    if not chunks:
        chunks = chunk_file_text(code)
        
    return chunks

def chunk_file_text(code: str) -> list[dict]:
    """Fallback text chunker for unknown languages (dockerfile, yaml, bash, etc)"""
    chunks = []
    # Simple line-based chunker: 50 lines per chunk with 10 line overlap
    lines = code.split("\n")
    chunk_size = 50
    overlap = 10
    
    for i in range(0, len(lines), chunk_size - overlap):
        chunk_lines = lines[i:i + chunk_size]
        if not chunk_lines:
            break
        chunk_text = "\n".join(chunk_lines).strip()
        if chunk_text:
            chunks.append({
                "node_name": f"lines_{i+1}_to_{i+len(chunk_lines)}",
                "node_type": "text_chunk",
                "content": chunk_text
            })
            
    return chunks

def get_ignored_dirs():
    return {".git", "node_modules", "dist", "build", "__pycache__", "venv", ".venv", "out", "target"}

def get_supported_extensions():
    # Only ingest text-based code/config files
    return {
        ".ts", ".js", ".tsx", ".jsx", ".json", ".yaml", ".yml",
        ".py", ".sh", ".bash", ".lua", ".md", ".txt", ".toml",
        ".rs", ".cpp", ".c", ".h", ".go", ".dockerfile"
    }

def main():
    parser = argparse.ArgumentParser(description="Ingest a generic GitHub repository into the LanceDB pipeline.")
    parser.add_argument("--repo-url", type=str, help="GitHub URL to clone and ingest")
    parser.add_argument("--local-path", type=str, help="Local directory path to ingest")
    args = parser.parse_args()

    if not args.repo_url and not args.local_path:
        print("Error: Must provide either --repo-url or --local-path")
        sys.exit(1)

    repo_url = args.repo_url or args.local_path
    
    if args.repo_url:
        repo_name = args.repo_url.split("/")[-1].replace(".git", "")
        target_dir = os.path.join(REPO_ROOT, ".mod_temp", repo_name)
        if os.path.exists(target_dir):
            print(f"Removing existing temp dir: {target_dir}")
            shutil.rmtree(target_dir)
            
        print(f"Cloning {args.repo_url} into {target_dir}...")
        os.makedirs(os.path.join(REPO_ROOT, ".mod_temp"), exist_ok=True)
        subprocess.run(["git", "clone", "--depth", "1", args.repo_url, target_dir], check=True)
    else:
        target_dir = args.local_path
        repo_name = os.path.basename(os.path.abspath(target_dir))
        
    print(f"Ingesting repository: {repo_name} from {target_dir}")
    
    db = init_lancedb()
    
    # Initialize sentence transformer
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Loading embedding model on {device}...")
    model_name = os.getenv("EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5")
    model = SentenceTransformer(model_name, device=device)
    
    # LanceDB Schema
    schema = pa.schema([
        pa.field("vector", pa.list_(pa.float32(), 768)),
        pa.field("repo_url", pa.string()),
        pa.field("file_path", pa.string()),
        pa.field("node_name", pa.string()),
        pa.field("node_type", pa.string()),
        pa.field("content", pa.string())
    ])
    
    try:
        table = db.open_table("codebase")
        print("Opened existing 'codebase' table.")
    except Exception:
        table = db.create_table("codebase", schema=schema)
        print("Created new 'codebase' table.")
        
    ignored_dirs = get_ignored_dirs()
    supported_exts = get_supported_extensions()
    
    batch = []
    BATCH_SIZE = 50
    total_chunks = 0
    
    for root, dirs, files in os.walk(target_dir):
        # Remove ignored dirs
        dirs[:] = [d for d in dirs if d not in ignored_dirs and not d.startswith(".")]
        
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext not in supported_exts and "dockerfile" not in file.lower():
                continue
                
            file_path = os.path.join(root, file)
            rel_path = os.path.relpath(file_path, target_dir)
            
            # Use appropriate chunker
            chunks = chunk_file_ast(file_path, ext)
            
            for chunk in chunks:
                # Contextualize the chunk for the embedder
                context_text = f"File: {rel_path}\nComponent: {chunk['node_name']}\nType: {chunk['node_type']}\nCode:\n{chunk['content']}"
                
                batch.append({
                    "text_to_embed": context_text,
                    "repo_url": repo_name,
                    "file_path": rel_path,
                    "node_name": chunk['node_name'],
                    "node_type": chunk['node_type'],
                    "content": chunk['content']
                })
                
                if len(batch) >= BATCH_SIZE:
                    texts = [b["text_to_embed"] for b in batch]
                    vectors = model.encode(texts, normalize_embeddings=True)
                    
                    rows = []
                    for i, b in enumerate(batch):
                        rows.append({
                            "vector": vectors[i].tolist(),
                            "repo_url": b["repo_url"],
                            "file_path": b["file_path"],
                            "node_name": b["node_name"],
                            "node_type": b["node_type"],
                            "content": b["content"]
                        })
                    
                    table.add(rows)
                    total_chunks += len(rows)
                    print(f"Ingested {total_chunks} chunks...")
                    batch = []

    # Final batch
    if batch:
        texts = [b["text_to_embed"] for b in batch]
        vectors = model.encode(texts, normalize_embeddings=True)
        rows = []
        for i, b in enumerate(batch):
            rows.append({
                "vector": vectors[i].tolist(),
                "repo_url": b["repo_url"],
                "file_path": b["file_path"],
                "node_name": b["node_name"],
                "node_type": b["node_type"],
                "content": b["content"]
            })
        table.add(rows)
        total_chunks += len(rows)

    print(f"\nDone! Ingested {total_chunks} total chunks for repository {repo_name}.")
    
if __name__ == "__main__":
    main()
