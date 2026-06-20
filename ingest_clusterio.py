import os
import glob
import hashlib
import torch
import lancedb
from lancedb.pydantic import LanceModel, Vector
from sentence_transformers import SentenceTransformer
from tree_sitter import Language, Parser
import tree_sitter_typescript as tsts

# Initialize Tree-sitter
TS_LANGUAGE = Language(tsts.language_typescript())
parser = Parser(TS_LANGUAGE)

from tree_sitter import Query, QueryCursor

query_str = """
(class_declaration) @class
(interface_declaration) @interface
(function_declaration) @function
(method_definition) @method
"""
ts_query = Query(TS_LANGUAGE, query_str)

# Initialize LanceDB Model
class CodeChunk(LanceModel):
    file_path: str
    node_type: str
    node_name: str
    content: str
    content_hash: str
    vector: Vector(768)

def get_preceding_comments(node):
    comments = []
    prev = node.prev_sibling
    while prev and prev.type == 'comment':
        comments.insert(0, prev.text.decode('utf8'))
        prev = prev.prev_sibling
    return "\n".join(comments)

def get_node_name(node):
    name_node = node.child_by_field_name('name')
    if name_node:
        return name_node.text.decode('utf8')
    return "anonymous"

def extract_chunks(file_path, src, content_hash):
    tree = parser.parse(src)
    cursor = QueryCursor(ts_query)
    captures = cursor.captures(tree.root_node)
    
    chunks = []
    for capture_name, nodes in captures.items():
        for node in nodes:
            name = get_node_name(node)
            
            # Grab comments + raw code
            comments = get_preceding_comments(node)
            raw_code = node.text.decode('utf8')
            
            full_content = raw_code
            if comments:
                full_content = f"{comments}\n{raw_code}"
                
            
            chunks.append({
                "file_path": file_path,
                "node_type": capture_name,
                "node_name": name,
                "content": full_content,
                "content_hash": content_hash
            })
        
    return chunks

def extract_text_chunks(file_path, content, content_hash):
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
            "file_path": file_path,
            "node_type": "text_file",
            "node_name": file_name,
            "content": chunk_content,
            "content_hash": content_hash
        })
        i += (chunk_size - overlap)
        
    return chunks

def main():
    print("Finding files to ingest...")
    repo_path = os.environ.get("CLUSTERIO_REPO", "./clusterio")
    
    extensions = ['*.ts', '*.js', '*.json', '*.md', '*.yml', '*.yaml', '*.lua', '*.sh', '*.bat', '*.ps1', '*.toml', '*.ini', 'Dockerfile']
    all_files = []
    for ext in extensions:
        for f in glob.glob(f"{repo_path}/**/{ext}", recursive=True):
            if "node_modules" not in f and ".git" not in f:
                all_files.append(f)
        
    print(f"Found {len(all_files)} total files.")
    
    print("Connecting to LanceDB...")
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clusterio_lancedb")
    db = lancedb.connect(db_path)
    
    if "codebase" in db.table_names():
        table = db.open_table("codebase")
        if "content_hash" not in table.schema.names:
            print("Dropping existing codebase table to migrate to new schema...")
            del table
            db.drop_table("codebase")
            table = db.create_table("codebase", schema=CodeChunk)
    else:
        table = db.create_table("codebase", schema=CodeChunk)
        
    table = db.open_table("codebase")
    
    print("Extracting chunks...")
    all_chunks = []
    skipped_count = 0
    for f in all_files:
        try:
            with open(f, 'rb') as file:
                content_bytes = file.read()
            f_hash = hashlib.sha256(content_bytes).hexdigest()
        except:
            continue
            
        safe_f = f.replace("'", "''")
        if len(table) > 0:
            existing = table.search().where(f"file_path = '{safe_f}'").limit(1).to_list()
            if existing and existing[0].get('content_hash') == f_hash:
                skipped_count += 1
                continue
            table.delete(f"file_path = '{safe_f}'")
            
        if f.endswith('.ts') or f.endswith('.js'):
            all_chunks.extend(extract_chunks(f, content_bytes, f_hash))
        else:
            try:
                text_content = content_bytes.decode('utf8')
                all_chunks.extend(extract_text_chunks(f, text_content, f_hash))
            except:
                pass
                
    print(f"Skipped {skipped_count} unchanged files.")
    print(f"Extracted {len(all_chunks)} new/modified chunks.")
    
    if len(all_chunks) == 0:
        print("Database is perfectly up to date!")
        return
        
    new_chunks = all_chunks

    print("Loading SentenceTransformer model...")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model_name = os.getenv("EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5")
    model = SentenceTransformer(model_name, device=device)
    
    print("Generating embeddings synchronously on the main thread (Deadlock safe!)...")
    batch_size = 100
    
    for i in range(0, len(new_chunks), batch_size):
        print(f"Ingesting batch {i} to {i+batch_size}...")
        batch = new_chunks[i:i+batch_size]
        texts = [c["content"] for c in batch]
        
        embeddings = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)
        
        for j, item in enumerate(batch):
            item["vector"] = embeddings[j].tolist()
            
        table.add(batch)
        
    print("Ingestion complete!")
    
    # Save version info
    import json
    package_json_path = os.path.join(repo_path, "package.json")
    version = "unknown"
    if os.path.exists(package_json_path):
        try:
            with open(package_json_path, "r", encoding="utf-8") as f:
                version = json.load(f).get("version", "unknown")
        except:
            pass
            
    with open(os.path.join(db_path, "version.txt"), "w", encoding="utf-8") as f:
        f.write(version)

if __name__ == "__main__":
    main()
