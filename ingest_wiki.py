import requests
import json
import os
import time
import lancedb
import torch
from sentence_transformers import SentenceTransformer
from lancedb.pydantic import LanceModel, Vector

device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Initializing SentenceTransformer on {device}...")
model = SentenceTransformer("BAAI/bge-base-en-v1.5", device=device)

class WikiDoc(LanceModel):
    text: str
    vector: Vector(768)
    title: str
    url: str

def chunk_text(text, title, chunk_size=1500, overlap=200):
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        chunks.append({
            "text": f"# {title}\n\n{chunk}",
            "title": title,
            "url": f"https://wiki.factorio.com/{title.replace(' ', '_')}"
        })
        start += (chunk_size - overlap)
    return chunks

def main():
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
                chunks = chunk_text(wikitext, title)
                all_chunks.extend(chunks)
        except Exception as e:
            print(f"Failed to fetch {title}: {e}")
            
        time.sleep(0.1) # Be nice to the wiki server
        
    print(f"Extracted {len(all_chunks)} chunks total.")
    
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wiki_lancedb")
    os.makedirs(db_path, exist_ok=True)
    db = lancedb.connect(db_path)
    
    if "docs" in db.list_tables():
        db.drop_table("docs")
        
    print("Creating table and generating embeddings (this may take a while)...")
    table = db.create_table("docs", schema=WikiDoc)
    
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
