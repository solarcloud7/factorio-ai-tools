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
    hash: str
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

def extract_chunks(file_path):
    with open(file_path, 'rb') as f:
        src = f.read()
    
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
                
            content_hash = hashlib.md5(full_content.encode('utf8')).hexdigest()
            
            chunks.append({
                "file_path": file_path,
                "node_type": capture_name,
                "node_name": name,
                "content": full_content,
                "hash": content_hash
            })
        
    return chunks

def main():
    print("Finding TypeScript files...")
    repo_path = "./clusterio"
    ts_files = glob.glob(f"{repo_path}/**/*.ts", recursive=True)
    print(f"Found {len(ts_files)} TypeScript files.")
    
    print("Parsing AST and extracting chunks...")
    all_chunks = []
    for f in ts_files:
        all_chunks.extend(extract_chunks(f))
        
    print(f"Extracted {len(all_chunks)} total AST chunks.")
    
    print("Connecting to LanceDB...")
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clusterio_lancedb")
    db = lancedb.connect(db_path)
    
    if "codebase" not in db.list_tables():
        table = db.create_table("codebase", schema=CodeChunk)
        existing_hashes = set()
    else:
        table = db.open_table("codebase")
        # Load existing hashes to skip re-embedding
        # In a real sync, we'd also delete rows for files/hashes that no longer exist
        existing_hashes = set([r['hash'] for r in table.search().limit(100000).to_list()])
        
    new_chunks = [c for c in all_chunks if c['hash'] not in existing_hashes]
    print(f"Found {len(new_chunks)} new or modified chunks that need embedding.")
    
    if len(new_chunks) == 0:
        print("Database is perfectly up to date!")
        return

    print("Loading SentenceTransformer model...")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = SentenceTransformer("BAAI/bge-base-en-v1.5", device=device)
    
    print("Generating embeddings synchronously on the main thread (Deadlock safe!)...")
    batch_size = 200
    
    for i in range(0, len(new_chunks), batch_size):
        print(f"Ingesting batch {i} to {i+batch_size}...")
        batch = new_chunks[i:i+batch_size]
        texts = [c["content"] for c in batch]
        
        embeddings = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)
        
        for j, item in enumerate(batch):
            item["vector"] = embeddings[j].tolist()
            
        table.add(batch)
        
    print("Ingestion complete!")

if __name__ == "__main__":
    main()
