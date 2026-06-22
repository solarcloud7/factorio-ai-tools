"""Parse/chunk units for the forum and wiki ingesters (offline)."""

import os

from factorio_ai_tools.ingest import ingest_forum, ingest_wiki

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


class _Resp:
    status_code = 200

    def __init__(self, text):
        self.text = text


def test_forum_scrape_parsing(monkeypatch):
    html = open(os.path.join(FIXTURES, "forum_topic.html"), encoding="utf-8").read()
    monkeypatch.setattr(ingest_forum.requests, "get", lambda *a, **k: _Resp(html))
    title, content = ingest_forum.scrape_topic_content("http://x")
    assert title == "How to use circuit networks"
    assert "circuit networks" in content
    assert "Second reply" in content


def test_forum_chunk_text_word_based():
    chunks = ingest_forum.chunk_text(" ".join(["word"] * 700), max_words=300)
    assert len(chunks) == 3


def test_wiki_chunk_text_prepends_title_and_builds_url():
    chunks = ingest_wiki.chunk_text("body " * 500, "Iron plate", "hash")
    assert chunks
    assert all(c["title"] == "Iron plate" for c in chunks)
    assert all(c["url"] == "https://wiki.factorio.com/Iron_plate" for c in chunks)
    assert all(c["text"].startswith("# Iron plate") for c in chunks)
    assert all({"text", "title", "url", "content_hash"} <= set(c) for c in chunks)
