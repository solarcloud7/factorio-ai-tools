import requests
import json
import os
import time
import hashlib
import lancedb
import torch
from sentence_transformers import SentenceTransformer
from lancedb.pydantic import LanceModel, Vector

device = "cuda" if torch.cuda.is_available() else "cpu"
model_name = os.getenv("EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5")
print(f"Initializing SentenceTransformer {model_name} on {device}...")
model = SentenceTransformer(model_name, device=device)

class WikiDoc(LanceModel):
    text: str
    vector: Vector(768)
    title: str
    url: str
    content_hash: str

def get_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def chunk_text(text, title, content_hash, chunk_size=1500, overlap=200):
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        chunks.append({
            "text": f"# {title}\n\n{chunk}",
            "title": title,
            "url": f"https://wiki.factorio.com/{title.replace(' ', '_')}",
            "content_hash": content_hash
        })
        start += (chunk_size - overlap)
    return chunks

def main():
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wiki_lancedb")
    os.makedirs(db_path, exist_ok=True)
    db = lancedb.connect(db_path)
    
    if "docs" in db.table_names():
        table = db.open_table("docs")
        if "content_hash" not in table.schema.names:
            print("Dropping existing docs table to migrate to new schema...")
            del table
            db.drop_table("docs")
            table = db.create_table("docs", schema=WikiDoc)
    else:
        table = db.create_table("docs", schema=WikiDoc)
        
    table = db.open_table("docs")

    session = requests.Session()
    session.headers.update({"User-Agent": "FactorioAIToolsBot/1.0 (https://github.com/factorio-ai-tools)"})
    
    api_url = "https://wiki.factorio.com/api.php"
    
    print("Fetching list of all pages...")
    all_pages = []
    apcontinue = None
    
    while True:
        params = {
            "action": "query",
            "list": "allpages",
            "aplimit": 500,
            "format": "json"
        }
        if apcontinue:
            params["apcontinue"] = apcontinue
            
        resp = session.get(api_url, params=params)
        data = resp.json()
        
        pages = data.get("query", {}).get("allpages", [])
        for p in pages:
            title = p["title"]
            # Filter out non-English translations (they typically have /de, /ru, /fr etc)
            if "/" not in title:
                all_pages.append(title)
                
        if "continue" in data and "apcontinue" in data["continue"]:
            apcontinue = data["continue"]["apcontinue"]
        else:
            break
            
    print(f"Found {len(all_pages)} English pages to scrape.")
    
    all_chunks = []
    
    for i, title in enumerate(all_pages):
        safe_title = title.encode('ascii', 'replace').decode('ascii')
        print(f"[{i+1}/{len(all_pages)}] Fetching {safe_title}...")
        try:
            params = {
                "action": "parse",
                "page": title,
                "prop": "wikitext",
                "format": "json"
            }
            resp = session.get(api_url, params=params)
            data = resp.json()
            
            if "parse" in data and "wikitext" in data["parse"]:
                wikitext = data["parse"]["wikitext"]["*"]
                chash = get_hash(wikitext)
                
                if len(table) > 0:
                    existing = table.search().where(f"title = '{title}'").limit(1).to_list()
                    if existing and existing[0].get('content_hash') == chash:
                        print(f"Skipping {safe_title}, unchanged.")
                        continue
                    table.delete(f"title = '{title}'")
                    
                chunks = chunk_text(wikitext, title, chash)
                all_chunks.extend(chunks)
        except Exception as e:
            print(f"Failed to fetch {title}: {e}")
            
        time.sleep(0.1) # Be nice to the wiki server
        
    print(f"Extracted {len(all_chunks)} new chunks total.")
    
    if len(all_chunks) == 0:
        print("Nothing new to ingest.")
        return
    
    batch_size = 100
    for i in range(0, len(all_chunks), batch_size):
        print(f"Ingesting batch {i} to {i+batch_size}...")
        batch = all_chunks[i:i+batch_size]
        texts = [c["text"] for c in batch]
        
        embeddings = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)
        
        for j, item in enumerate(batch):
            item["vector"] = embeddings[j].tolist()
            
        table.add(batch)
        
    print("Creating FTS index for hybrid search...")
    table.create_fts_index("text")
    
    print("Ingestion complete!")

if __name__ == '__main__':
    main()
