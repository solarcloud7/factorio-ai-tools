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


def chunk_text(text, max_words=300, overlap=30):
    words = text.split()
    if not words:
        return []
    step = max(1, max_words - overlap)
    return [" ".join(words[i:i + max_words]) for i in range(0, len(words), step)]


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
    dry = common.dry_run_requested()
    if dry:
        common.safe_print("DRY RUN: chunk + audit only, no embed/write.")
        table = model = None
    else:
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
            if table is not None and len(table) > 0:
                existing = table.search().where(f"file_path = '{safe_url}'").limit(1).to_list()
                if existing and existing[0].get('content_hash') == current_hash:
                    common.safe_print(f"Skipping {url}, content unchanged.")
                    continue
                table.delete(f"file_path = '{safe_url}'")

            chunk_dicts = [{"content": f"Topic: {title}\n\n{ch}"} for ch in chunk_text(content)]
            chunk_dicts, nstats = common.normalize_chunks(
                chunk_dicts, content_key="content", max_tokens=common.EMBED_MAX_TOKENS
            )
            auditor.note_dups(nstats["dropped_dup"])
            auditor.note_source(url, len(content), len(chunk_dicts))
            for i, cd in enumerate(chunk_dicts):
                chunk_content = cd["content"]
                auditor.add(chunk_content, source=url)
                if not dry:
                    embedding = common.embed([chunk_content], model)[0].tolist()
                    records.append({
                        "id": f"forum_{topic_count}_{i}",
                        "file_path": url,
                        "class_name": title,
                        "content": chunk_content,
                        "version": "",  # forum posts aren't Factorio-versioned (no "latest" label)
                        "content_hash": current_hash,
                        "vector": embedding,
                    })

            topic_count += 1
            if topic_count % 10 == 0:
                common.safe_print(f"Processed {topic_count} topics...")

            if not dry and len(records) >= BATCH_SIZE:
                table.add(records)
                records = []

            time.sleep(0.5)  # Rate limit
        except Exception as e:
            common.safe_print(f"Error parsing {url}: {e}")

    if not dry and records:
        table.add(records)

    if not dry and table is not None and len(table) > 0:
        try:
            table.create_fts_index("content", replace=True)
        except Exception as e:
            common.safe_print(f"FTS index skipped: {e}")

    auditor.summary()
    common.safe_print(f"{'Audited' if dry else 'Embedded'} {topic_count} topics.")


if __name__ == '__main__':
    main()
