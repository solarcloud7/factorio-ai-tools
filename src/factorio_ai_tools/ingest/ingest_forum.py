"""Ingest curated Factorio forum topics into ``data/forum_lancedb``.

Reads topic URLs from ``forum_links.txt`` (repo root; ``#`` comments allowed),
scrapes each with BeautifulSoup, and stores 300-word chunks. Incremental by
per-topic SHA-256. NOTE: the ``forum`` table repurposes columns to match the
server's read contract: ``class_name`` holds the topic *title* and ``file_path``
holds the topic *URL*.

Unlike the pre-restore original, all work is wrapped in ``main()`` behind an
``if __name__ == '__main__'`` guard so importing this module does not trigger a
live network scrape.
"""

import os
import time

import pyarrow as pa
import requests
from bs4 import BeautifulSoup

from factorio_ai_tools.ingest import common

FORUM_LINKS_FILE = os.path.join(common.REPO_ROOT, "forum_links.txt")
BATCH_SIZE = 50

SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("file_path", pa.string()),    # topic URL
    pa.field("class_name", pa.string()),   # topic title
    pa.field("content", pa.string()),
    pa.field("version", pa.string()),
    pa.field("content_hash", pa.string()),
    pa.field("vector", pa.list_(pa.float32(), common.EMBEDDING_DIM)),
])


def chunk_text(text, max_words=300):
    words = text.split()
    return [" ".join(words[i:i + max_words]) for i in range(0, len(words), max_words)]


def load_topic_urls():
    if not os.path.exists(FORUM_LINKS_FILE):
        common.safe_print(f"Error: {FORUM_LINKS_FILE} not found!")
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
    topic_text = [post.get_text(separator=' ', strip=True) for post in posts]
    return title, "\n\n---\n\n".join(topic_text)


def main():
    db, _db_path = common.connect_store("forum_lancedb")
    table = common.ensure_table(db, "forum", SCHEMA)
    model = common.load_embedder()
    auditor = common.ChunkAuditor("forum_lancedb")

    common.safe_print(f"Reading curated links from {FORUM_LINKS_FILE}...")
    topic_urls = load_topic_urls()
    common.safe_print(f"Found {len(topic_urls)} curated topics to ingest.")

    records = []
    topic_count = 0
    for url in topic_urls:
        try:
            title, content = scrape_topic_content(url)
            if not content:
                continue

            current_hash = common.get_hash(content)

            safe_url = url.replace("'", "''")
            if len(table) > 0:
                existing = table.search().where(f"file_path = '{safe_url}'").limit(1).to_list()
                if existing and existing[0].get('content_hash') == current_hash:
                    common.safe_print(f"Skipping {url}, content unchanged.")
                    continue
                table.delete(f"file_path = '{safe_url}'")

            topic_chunks = chunk_text(content)
            auditor.note_source(url, len(content), len(topic_chunks))
            for i, chunk in enumerate(topic_chunks):
                chunk_content = f"Topic: {title}\n\n{chunk}"
                auditor.add(chunk_content, source=url)
                embedding = common.embed([chunk_content], model)[0].tolist()
                records.append({
                    "id": f"forum_{topic_count}_{i}",
                    "file_path": url,
                    "class_name": title,
                    "content": chunk_content,
                    "version": "latest",
                    "content_hash": current_hash,
                    "vector": embedding,
                })

            topic_count += 1
            if topic_count % 10 == 0:
                common.safe_print(f"Processed {topic_count} topics...")

            if len(records) >= BATCH_SIZE:
                table.add(records)
                records = []

            time.sleep(0.5)  # Rate limit
        except Exception as e:
            common.safe_print(f"Error parsing {url}: {e}")

    if records:
        table.add(records)

    auditor.summary()
    common.safe_print(f"Ingestion complete! Embedded {topic_count} topics into LanceDB.")


if __name__ == '__main__':
    main()
