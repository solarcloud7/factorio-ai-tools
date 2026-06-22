"""Tests for the ChunkAuditor — the always-on, token-based regression guard for
silent chunking failures (oversized/tiny/empty/explosion/zero-chunk/dups).

The autouse conftest stub makes count_tokens ~= chars/4, so token thresholds
below are expressed against that ratio."""

import pytest

from factorio_ai_tools.ingest import common


def test_clean_input_passes():
    a = common.ChunkAuditor("s", min_chars=5, max_tokens=1000, explosion_per_source=10, strict=False)
    for i in range(3):
        a.add("a perfectly reasonable chunk of text", source=f"f{i}", node_type="function")
    assert a.summary()["result"] == "PASS"


def test_oversized_measured_in_tokens():
    a = common.ChunkAuditor("s", max_tokens=10, strict=False)
    a.add("x" * 200, source="f")  # ~50 tokens > 10
    assert a.summary()["oversized"] == 1


def test_flags_oversized_tiny_explosion_and_zero_chunk():
    a = common.ChunkAuditor("s", min_chars=10, max_tokens=25, explosion_per_source=5, strict=False)
    a.add("{}", source="x.lua", node_type="table")          # tiny (2 chars)
    a.add("Z" * 500, source="x.lua", node_type="table")     # ~125 tokens -> oversized
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
    a = common.ChunkAuditor("s", max_tokens=100, strict=True)
    a.add("Z" * 500, source="y")  # ~125 tokens -> oversized -> FAIL
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


def test_auditor_flags_explosion_synthetically():
    # The auditor's explosion guard (decoupled from the now-fixed AST query).
    a = common.ChunkAuditor("repro", explosion_per_source=50, strict=False)
    for i in range(60):
        a.add(f"chunk {i} content body", source="data.lua", node_type="table")
    s = a.summary()
    assert s["result"] == "FAIL"
    assert any(src == "data.lua" for src, _ in s["explosions"])


def test_note_dups_surfaces_in_summary():
    a = common.ChunkAuditor("s", strict=False)
    a.add("real content here", source="f")
    a.note_dups(3)
    assert a.summary()["dups"] == 3
