"""End-to-end tests for the generic repo ingester (offline via --local-path)."""

import sys

import lancedb

from factorio_ai_tools.ingest import ingest_github_repo


def _ingest(repo, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ingest_github_repo", "--local-path", str(repo)])
    ingest_github_repo.main()


def test_repo_ingest_columns_hash_and_exclusions(mini_repo, tmp_data_dir, fake_embedder, monkeypatch):
    _ingest(mini_repo, monkeypatch)
    t = lancedb.connect(str(tmp_data_dir / "repo_lancedb")).open_table("codebase")
    rows = t.search().limit(10000).to_list()
    assert {"node_name", "node_type", "repo_url", "file_path", "content", "content_hash"} <= set(t.schema.names)
    paths = [r["file_path"].replace("\\", "/") for r in rows]

    assert rows and all(r["content_hash"] for r in rows)
    assert all(r["repo_url"] == "mini_repo" for r in rows)        # basename, not full path
    # real source ingested
    assert any(p.endswith("src/app.ts") for p in paths)
    assert any(p.endswith("mod.lua") for p in paths)
    # dependency/build artifacts excluded
    assert not any("node_modules" in p for p in paths)
    assert not any("dist/" in p for p in paths)
    assert not any("package-lock.json" in p for p in paths)


def test_repo_incremental_rerun_no_growth(mini_repo, tmp_data_dir, fake_embedder, monkeypatch):
    _ingest(mini_repo, monkeypatch)
    t = lancedb.connect(str(tmp_data_dir / "repo_lancedb")).open_table("codebase")
    n1 = len(t)
    _ingest(mini_repo, monkeypatch)
    t2 = lancedb.connect(str(tmp_data_dir / "repo_lancedb")).open_table("codebase")
    assert len(t2) == n1
