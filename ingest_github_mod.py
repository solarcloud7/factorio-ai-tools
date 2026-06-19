import os
import glob
import hashlib
import argparse
import subprocess
import shutil
import lancedb
import torch
from lancedb.pydantic import LanceModel, Vector
from sentence_transformers import SentenceTransformer
from tree_sitter import Language, Parser
import tree_sitter_lua as tslua

# Initialize Tree-sitter for Lua
TS_LUA = Language(tslua.language())
parser = Parser(TS_LUA)

from tree_sitter import Query, QueryCursor

# For Lua, we want to capture functions, local functions, and maybe tables.
query_str = """
(function_declaration) @function
(table_constructor) @table
"""
ts_query = Query(TS_LUA, query_str)

class ModChunk(LanceModel):
    repo_url: str
    file_path: str
    node_type: str
    node_name: str
    content: str
    content_hash: str
    vector: Vector(768)

def get_node_name(node, source_code):
    # In Lua, function names can be complex.
    # Try to grab the name if it's a direct child
    name_node = node.child_by_field_name('name')
    if name_node:
        return name_node.text.decode('utf8')
        
    # For local functions or variable assignments
    if node.type in ['function_declaration', 'local_function_declaration']:
        for child in node.children:
            if child.type == 'identifier':
                return child.text.decode('utf8')
    return "anonymous"

def extract_lua_chunks(file_path, src, content_hash, relative_path):
    tree = parser.parse(src)
    cursor = QueryCursor(ts_query)
    captures = cursor.captures(tree.root_node)
    
    chunks = []
    # If no captures, fallback to text chunking
    if not captures:
        return extract_text_chunks(file_path, src.decode('utf8', errors='ignore'), content_hash, relative_path)
        
    for capture_name, nodes in captures.items():
        for node in nodes:
            name = get_node_name(node, src)
            raw_code = node.text.decode('utf8', errors='ignore')
            
            chunks.append({
                "file_path": relative_path,
                "node_type": capture_name,
                "node_name": name,
                "content": raw_code,
                "content_hash": content_hash
            })
            
    return chunks

def extract_text_chunks(file_path, content, content_hash, relative_path):
    chunk_size = 1500
    overlap = 200
    chunks = []
    
    if len(content) == 0:
        return []
        
    file_name = os.path.basename(file_path)
    
    i = 0
    while i < len(content):
        chunk_content = content[i:i+chunk_size]
        chunks.append({
            "file_path": relative_path,
            "node_type": "text_file",
            "node_name": file_name,
            "content": chunk_content,
            "content_hash": content_hash
        })
        i += (chunk_size - overlap)
        
    return chunks

def clone_repo(repo_url, target_dir):
    if os.path.exists(target_dir):
        print(f"Cleaning up existing temp directory {target_dir}...")
        try:
            shutil.rmtree(target_dir)
        except Exception as e:
            print(f"Failed to remove {target_dir}: {e}")
            
    print(f"Cloning {repo_url} into {target_dir}...")
    try:
        subprocess.run(["git", "clone", "--depth", "1", repo_url, target_dir], check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"Failed to clone repository: {e.stderr.decode('utf8', errors='ignore')}")
        return False

def main():
    parser_arg = argparse.ArgumentParser(description="Ingest a Factorio mod from GitHub into LanceDB.")
    parser_arg.add_argument("--repo-url", required=True, help="GitHub repository URL (e.g. https://github.com/notnotmelon/maraxsis/)")
    args = parser_arg.parse_args()
    
    repo_url = args.repo_url.rstrip('/')
    mod_name = repo_url.split('/')[-1]
    temp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".mod_temp", mod_name)
    
    if not clone_repo(repo_url, temp_dir):
        return
        
    print(f"Connecting to LanceDB...")
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mod_lancedb")
    os.makedirs(db_path, exist_ok=True)
    db = lancedb.connect(db_path)
    
    if "codebase" in db.table_names():
        table = db.open_table("codebase")
        if "content_hash" not in table.schema.names or "repo_url" not in table.schema.names:
            print("Dropping existing codebase table to migrate to new schema...")
            del table
            db.drop_table("codebase")
            table = db.create_table("codebase", schema=ModChunk)
    else:
        table = db.create_table("codebase", schema=ModChunk)
        
    table = db.open_table("codebase")
    
    print("Finding files to ingest...")
    extensions = ['*.lua', '*.json', '*.cfg', '*.md', '*.txt']
    all_files = []
    
    for ext in extensions:
        for f in glob.glob(f"{temp_dir}/**/{ext}", recursive=True):
            if ".git" not in f and "locale" not in f and "graphics" not in f and "sound" not in f:
                all_files.append(f)
                
    print(f"Found {len(all_files)} total files.")
    
    all_chunks = []
    skipped_count = 0
    
    for f in all_files:
        try:
            with open(f, 'rb') as file:
                content_bytes = file.read()
            f_hash = hashlib.sha256(content_bytes).hexdigest()
        except:
            continue
            
        relative_path = os.path.relpath(f, temp_dir).replace('\\', '/')
        
        if len(table) > 0:
            existing = table.search().where(f"file_path = '{relative_path}' AND repo_url = '{repo_url}'").limit(1).to_list()
            if existing and existing[0].get('content_hash') == f_hash:
                skipped_count += 1
                continue
            table.delete(f"file_path = '{relative_path}' AND repo_url = '{repo_url}'")
            
        if f.endswith('.lua'):
            all_chunks.extend(extract_lua_chunks(f, content_bytes, f_hash, relative_path))
        else:
            try:
                text_content = content_bytes.decode('utf8')
                all_chunks.extend(extract_text_chunks(f, text_content, f_hash, relative_path))
            except:
                pass
                
    print(f"Skipped {skipped_count} unchanged files.")
    print(f"Extracted {len(all_chunks)} new/modified chunks.")
    
    if len(all_chunks) == 0:
        print("Database is perfectly up to date!")
        return
        
    print("Loading SentenceTransformer model...")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = SentenceTransformer("BAAI/bge-base-en-v1.5", device=device)
    
    print("Generating embeddings synchronously on the main thread (Deadlock safe!)...")
    batch_size = 100
    
    for i in range(0, len(all_chunks), batch_size):
        print(f"Ingesting batch {i} to {i+batch_size}...")
        batch = all_chunks[i:i+batch_size]
        texts = [c["content"] for c in batch]
        
        # Ensure we add the repo_url
        for c in batch:
            c["repo_url"] = repo_url
            
        embeddings = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)
        
        for j, item in enumerate(batch):
            item["vector"] = embeddings[j].tolist()
            
        table.add(batch)
        
    print("Ingestion complete!")

if __name__ == "__main__":
    main()
