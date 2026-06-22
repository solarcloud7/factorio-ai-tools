"""Unit tests for the shared ingest helpers."""

import os
import stat

import lancedb
import pyarrow as pa
import pytest
from lancedb.pydantic import LanceModel, Vector

from factorio_ai_tools.ingest import common


def test_repo_root_resolves_to_repo():
    # common.py is at src/factorio_ai_tools/ingest/common.py -> 4 parents up = repo root.
    assert os.path.basename(common.REPO_ROOT) == "factorio-ai-tools"


def test_get_hash_str_and_bytes_match():
    assert common.get_hash("x") == common.get_hash(b"x")
    assert common.get_hash("a") != common.get_hash("b")
    assert len(common.get_hash("a")) == 64


def test_extract_ast_chunks_typescript():
    chunks = common.extract_ast_chunks(
        b"class Foo { bar(){ return 1; } }\nfunction baz(){}", "typescript"
    )
    types = {c["node_type"] for c in chunks}
    names = {c["node_name"] for c in chunks}
    assert {"class", "function", "method"} <= types
    assert {"Foo", "baz", "bar"} <= names


def test_extract_ast_chunks_lua():
    chunks = common.extract_ast_chunks(
        b"function greet(n)\n return n\nend\nlocal t = {a=1}", "lua"
    )
    types = {c["node_type"] for c in chunks}
    assert "function" in types and "table" in types


def test_extract_ast_chunks_unsupported_returns_none():
    assert common.extract_ast_chunks(b"x = 1", "python") is None


def test_text_chunks_by_char_overlap():
    chunks = common.text_chunks_by_char("a" * 3500, chunk_size=1500, overlap=200)
    assert len(chunks) == 3
    assert all(len(c) <= 1500 for c in chunks)


def test_text_chunks_by_line():
    chunks = common.text_chunks_by_line("\n".join(str(i) for i in range(120)))
    assert len(chunks) >= 2
    assert all(c["node_type"] == "text_chunk" for c in chunks)


def test_is_ignored_path():
    assert common.is_ignored_path("a/node_modules/x.js")
    assert common.is_ignored_path("pkg/dist/b.js")
    assert common.is_ignored_path("x/package-lock.json")
    assert common.is_ignored_path("a\\build\\c.js")
    assert not common.is_ignored_path("plugins/player_auth/index.ts")


def test_rmtree_force_removes_readonly(tmp_path):
    d = tmp_path / "ro"
    d.mkdir()
    f = d / "locked.txt"
    f.write_text("x")
    os.chmod(f, stat.S_IREAD)
    common.rmtree_force(str(d))
    assert not d.exists()


class _Model(LanceModel):
    name: str
    vector: Vector(common.EMBEDDING_DIM)
    content_hash: str


def test_ensure_table_migrates_then_noop(tmp_path):
    db = lancedb.connect(str(tmp_path / "store"))
    stale = pa.schema([
        pa.field("name", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), common.EMBEDDING_DIM)),
    ])
    db.create_table("t", schema=stale)
    t = common.ensure_table(db, "t", _Model)           # missing content_hash -> drop+recreate
    assert "content_hash" in t.schema.names
    versions = len(t.list_versions())
    t2 = common.ensure_table(db, "t", _Model)           # already current -> no-op
    assert len(t2.list_versions()) == versions


def test_embed_rejects_wrong_dimension(monkeypatch):
    import numpy as np

    class Bad:
        def encode(self, texts, **k):
            return np.zeros((len(texts), 99), dtype="float32")

    monkeypatch.setattr(common, "_MODEL", Bad())
    with pytest.raises(common.ChunkHealthError):
        common.embed(["x"])
