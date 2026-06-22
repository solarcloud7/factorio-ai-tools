"""Tests for the ChunkAuditor — the always-on, regression guard for silent
chunking failures (oversized/tiny/empty/explosion/zero-chunk)."""

import pytest

from factorio_ai_tools.ingest import common


def test_clean_input_passes():
    a = common.ChunkAuditor("s", min_chars=5, max_chars=1000, explosion_per_source=10, strict=False)
    for i in range(3):
        a.add("a perfectly reasonable chunk of text", source=f"f{i}", node_type="function")
    assert a.summary()["result"] == "PASS"


def test_flags_oversized_tiny_explosion_and_zero_chunk():
    a = common.ChunkAuditor("s", min_chars=10, max_chars=100, explosion_per_source=5, strict=False)
    a.add("{}", source="x.lua", node_type="table")          # tiny + (empty after strip? no)
    a.add("Z" * 500, source="x.lua", node_type="table")     # oversized
    for _ in range(8):
        a.add("a table literal chunk", source="x.lua", node_type="table")  # explosion on x.lua
    a.note_source("empty.lua", 1234, 0)                     # non-empty file, zero chunks
    s = a.summary()
    assert s["result"] == "FAIL"
    assert s["oversized"] == 1
    assert s["tiny"] == 1
    assert s["explosions"] and s["explosions"][0][0] == "x.lua"
    assert s["empty_sources"] == [("empty.lua", 1234)]


def test_empty_chunk_counted():
    a = common.ChunkAuditor("s", strict=False)
    a.add("   ", source="f")
    assert a.summary()["empty"] == 1


def test_strict_raises_on_fail():
    a = common.ChunkAuditor("s", max_chars=100, strict=True)
    a.add("Z" * 500, source="y")
    with pytest.raises(common.ChunkHealthError):
        a.summary()


def test_strict_defaults_from_env(monkeypatch):
    monkeypatch.setenv("FACTORIO_MCP_STRICT_CHUNKS", "1")
    assert common.ChunkAuditor("s").strict is True


def test_add_batch_measures_the_embedded_field():
    a = common.ChunkAuditor("s", explosion_per_source=2, strict=False)
    recs = [{"content": "hello world chunk", "file_path": "a", "node_type": "function"}
            for _ in range(5)]
    a.add_batch(recs, text_key="content", source_key="file_path")
    s = a.summary()
    assert s["total"] == 5
    assert s["explosions"]  # 5 chunks from source "a" exceeds threshold 2


def test_reproduces_lua_table_blowup_and_flags_it():
    """The exact class of bug we hit with factorio-data: a data-heavy Lua file
    where every table_constructor becomes a chunk -> explosion + tiny `{}`."""
    src = "data = {" + ",".join("{a=%d, b={}}" % i for i in range(60)) + "}\n"
    chunks = common.extract_ast_chunks(src.encode(), "lua")
    a = common.ChunkAuditor("repro", min_chars=10, max_chars=2000, explosion_per_source=50, strict=False)
    for c in chunks:
        a.add(c["content"], source="data.lua", node_type=c["node_type"])
    s = a.summary()
    assert s["by_type"].get("table", 0) > 50          # the blowup
    assert s["result"] == "FAIL"                       # and it is caught, not silent
    assert any(src == "data.lua" for src, _ in s["explosions"])
