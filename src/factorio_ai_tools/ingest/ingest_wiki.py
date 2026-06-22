"""Ingest the full Factorio wiki into ``data/wiki_lancedb`` via the MediaWiki API.

Enumerates all English pages (``action=query&list=allpages``), fetches each
page's wikitext, and stores 1500-char/200-overlap text chunks. Incremental:
skips a page whose wikitext SHA-256 is unchanged, else deletes-then-re-adds it.
"""

import time

import requests
from lancedb.pydantic import LanceModel, Vector

from factorio_ai_tools.ingest import common


class WikiDoc(LanceModel):
    text: str
    vector: Vector(common.EMBEDDING_DIM)
    title: str
    url: str
    content_hash: str


def chunk_text(text, title, content_hash, chunk_size=1500, overlap=200):
    chunks = []
    for chunk in common.text_chunks_by_char(text, chunk_size, overlap):
        chunks.append({
            "text": f"# {title}\n\n{chunk}",
            "title": title,
            "url": f"https://wiki.factorio.com/{title.replace(' ', '_')}",
            "content_hash": content_hash
        })
    return chunks


def main():
    db, _db_path = common.connect_store("wiki_lancedb")
    table = common.ensure_table(db, "docs", WikiDoc)

    session = requests.Session()
    session.headers.update({"User-Agent": "FactorioAIToolsBot/1.0 (https://github.com/factorio-ai-tools)"})
    api_url = "https://wiki.factorio.com/api.php"

    common.safe_print("Fetching list of all pages...")
    all_pages = []
    apcontinue = None
    while True:
        params = {"action": "query", "list": "allpages", "aplimit": 500, "format": "json"}
        if apcontinue:
            params["apcontinue"] = apcontinue

        resp = session.get(api_url, params=params)
        data = resp.json()

        for p in data.get("query", {}).get("allpages", []):
            title = p["title"]
            # Filter out non-English translations (they carry a /de, /ru, ... suffix)
            if "/" not in title:
                all_pages.append(title)

        if "continue" in data and "apcontinue" in data["continue"]:
            apcontinue = data["continue"]["apcontinue"]
        else:
            break

    common.safe_print(f"Found {len(all_pages)} English pages to scrape.")

    all_chunks = []
    for i, title in enumerate(all_pages):
        common.safe_print(f"[{i + 1}/{len(all_pages)}] Fetching {title}...")
        try:
            params = {"action": "parse", "page": title, "prop": "wikitext", "format": "json"}
            resp = session.get(api_url, params=params)
            data = resp.json()

            if "parse" in data and "wikitext" in data["parse"]:
                wikitext = data["parse"]["wikitext"]["*"]
                chash = common.get_hash(wikitext)

                safe_db_title = title.replace("'", "''")
                if len(table) > 0:
                    existing = table.search().where(f"title = '{safe_db_title}'").limit(1).to_list()
                    if existing and existing[0].get('content_hash') == chash:
                        common.safe_print(f"Skipping {title}, unchanged.")
                        continue
                    table.delete(f"title = '{safe_db_title}'")

                all_chunks.extend(chunk_text(wikitext, title, chash))
        except Exception as e:
            common.safe_print(f"Failed to fetch {title}: {e}")

        time.sleep(0.1)  # Be nice to the wiki server

    common.safe_print(f"Extracted {len(all_chunks)} new chunks total.")

    if len(all_chunks) == 0:
        common.safe_print("Nothing new to ingest.")
        return

    model = common.load_embedder()
    batch_size = 100
    for i in range(0, len(all_chunks), batch_size):
        common.safe_print(f"Ingesting batch {i} to {i + batch_size}...")
        batch = all_chunks[i:i + batch_size]
        embeddings = common.embed([c["text"] for c in batch], model)
        for j, item in enumerate(batch):
            item["vector"] = embeddings[j].tolist()
        table.add(batch)

    common.safe_print("Creating FTS index for hybrid search...")
    table.create_fts_index("text", replace=True)
    common.safe_print("Ingestion complete!")


if __name__ == '__main__':
    main()
