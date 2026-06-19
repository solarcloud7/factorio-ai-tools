import os
import time
import requests
import hashlib
from bs4 import BeautifulSoup
import lancedb
import pyarrow as pa
from sentence_transformers import SentenceTransformer
import torch

# Configuration
FORUM_LINKS_FILE = "forum_links.txt"
LANCEDB_PATH = "forum_lancedb"
BATCH_SIZE = 50

# Setup device and model
device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Loading embedding model on {device}...".encode('ascii', 'replace').decode('ascii'))
model = SentenceTransformer("BAAI/bge-base-en-v1.5", device=device)

# Setup LanceDB
print("Initializing LanceDB...".encode('ascii', 'replace').decode('ascii'))
db = lancedb.connect(LANCEDB_PATH)

schema = pa.schema([
    pa.field("id", pa.string()),
    pa.field("file_path", pa.string()),
    pa.field("class_name", pa.string()),
    pa.field("content", pa.string()),
    pa.field("version", pa.string()),
    pa.field("content_hash", pa.string()),
    pa.field("vector", pa.list_(pa.float32(), 768))
])

if "forum" in db.table_names():
    table = db.open_table("forum")
    if "content_hash" not in table.schema.names:
        print("Dropping existing forum table to migrate to new schema...")
        db.drop_table("forum")
        table = db.create_table("forum", schema=schema)
else:
    table = db.create_table("forum", schema=schema)

table = db.open_table("forum")

def get_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def chunk_text(text, max_words=300):
    words = text.split()
    chunks = []
    for i in range(0, len(words), max_words):
        chunks.append(" ".join(words[i:i + max_words]))
    return chunks

def load_topic_urls():
    if not os.path.exists(FORUM_LINKS_FILE):
        print(f"Error: {FORUM_LINKS_FILE} not found!".encode('ascii', 'replace').decode('ascii'))
        return []
    with open(FORUM_LINKS_FILE, 'r', encoding='utf-8') as f:
        return [line.strip() for line in f if line.strip() and not line.startswith('#')]

def scrape_topic_content(url):
    headers = {"User-Agent": "FactorioAITools/1.0"}
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        return None, ""
        
    soup = BeautifulSoup(response.text, 'html.parser')
    
    title_elem = soup.select_one('h2.topic-title a')
    title = title_elem.text.strip() if title_elem else "Unknown Topic"
    
    posts = soup.select('div.content')
    topic_text = []
    for post in posts:
        topic_text.append(post.get_text(separator=' ', strip=True))
        
    return title, "\n\n---\n\n".join(topic_text)

print(f"Reading curated links from {FORUM_LINKS_FILE}...".encode('ascii', 'replace').decode('ascii'))
topic_urls = load_topic_urls()
print(f"Found {len(topic_urls)} curated topics to ingest.".encode('ascii', 'replace').decode('ascii'))

records = []
topic_count = 0

for url in topic_urls:
    try:
        title, content = scrape_topic_content(url)
        if not content:
            continue
            
        current_hash = get_hash(content)
        
        existing = table.search().where(f"file_path = '{url}'").limit(1).to_list()
        if existing and existing[0].get('content_hash') == current_hash:
            print(f"Skipping {url}, content unchanged.".encode('ascii', 'replace').decode('ascii'))
            continue
            
        table.delete(f"file_path = '{url}'")
            
        chunks = chunk_text(content)
        
        for i, chunk in enumerate(chunks):
            # Prepend title to context
            chunk_content = f"Topic: {title}\n\n{chunk}"
            embedding = model.encode(chunk_content).tolist()
            
            records.append({
                "id": f"forum_{topic_count}_{i}",
                "file_path": url,
                "class_name": title,
                "content": chunk_content,
                "version": "latest",
                "content_hash": current_hash,
                "vector": embedding
            })
            
        topic_count += 1
        if topic_count % 10 == 0:
            print(f"Processed {topic_count} topics...".encode('ascii', 'replace').decode('ascii'))
            
        if len(records) >= BATCH_SIZE:
            table.add(records)
            records = []
            
        time.sleep(0.5) # Rate limit
    except Exception as e:
        print(f"Error parsing {url}: {e}".encode('ascii', 'replace').decode('ascii'))

if records:
    table.add(records)

print(f"Ingestion complete! Embedded {topic_count} topics into LanceDB.".encode('ascii', 'replace').decode('ascii'))
