"""Gate-first invariants for the playbook (docs/rag-pipeline-playbook.md §2).

These encode the dimensions the chunk-health gate previously did NOT measure and
that the code review found bugs in. They are written to FAIL on the pre-fix code
(some reference Phase-2 helpers that don't exist yet) and PASS once the §3
regressions are fixed — so the gate's coverage proves it catches the bugs.
"""

import sys

import lancedb

from factorio_ai_tools.ingest import common

# --- Helper-driven invariants (drive Phase-2 helpers; AttributeError until added) ---


def test_kind_for_ext_routing():
    assert common.kind_for_ext(".lua") == "lua"
    assert common.kind_for_ext(".ts") == "typescript"
    assert common.kind_for_ext(".tsx") == "typescript"
    assert common.kind_for_ext(".js") == "typescript"
    assert common.kind_for_ext(".md") is None


def test_repo_slug_from_url_keeps_owner_and_strips_only_suffix():
    assert common.repo_slug_from_url("https://github.com/octocat/octocat.github.io") == "octocat/octocat.github.io"
    assert common.repo_slug_from_url("https://github.com/clusterio/clusterio.git") == "clusterio/clusterio"


def test_like_escape_escapes_metachars():
    assert common.like_escape("a_b%c") == r"a\_b\%c"
    assert common.like_escape("player_auth") == r"player\_auth"
    assert common.like_escape("plain") == "plain"


# --- Behavior invariants (fail on the current ingesters) ---


def test_dedup_keeps_distinct_files(tmp_path, tmp_data_dir, fake_embedder, monkeypatch):
    """Two distinct files with byte-identical content must both survive (their
    file_paths must remain searchable) — store-wide dedup wrongly collapses them."""
    from factorio_ai_tools.ingest import ingest_clusterio

    repo = tmp_path / "clus"
    for name in ("aaa", "bbb"):
        d = repo / "plugins" / name
        d.mkdir(parents=True)
        (d / "index.ts").write_text("export { default } from './controller';\n", encoding="utf-8")
    (repo / "package.json").write_text('{"version": "1.0.0"}', encoding="utf-8")
    monkeypatch.setenv("CLUSTERIO_REPO", str(repo))
    ingest_clusterio.main()

    t = lancedb.connect(str(tmp_data_dir / "clusterio_lancedb")).open_table("codebase")
    paths = {r["file_path"].replace("\\", "/") for r in t.search().limit(1000).to_list()}
    assert any(p.endswith("plugins/aaa/index.ts") for p in paths)
    assert any(p.endswith("plugins/bbb/index.ts") for p in paths)


def test_repo_file_paths_are_posix(mini_repo, tmp_data_dir, fake_embedder, monkeypatch):
    """Stored file_path must be POSIX (no backslash, no absolute prefix) for
    cross-OS portability and stable keys."""
    from factorio_ai_tools.ingest import ingest_github_repo

    monkeypatch.setattr(sys, "argv", ["x", "--local-path", str(mini_repo)])
    ingest_github_repo.main()
    t = lancedb.connect(str(tmp_data_dir / "repo_lancedb")).open_table("codebase")
    for r in t.search().limit(10000).to_list():
        assert "\\" not in r["file_path"], f"backslash in {r['file_path']!r}"


def test_orphan_rows_removed_on_delete(tmp_path, tmp_data_dir, fake_embedder, monkeypatch):
    """A source file deleted between runs must not leave orphan rows behind."""
    from factorio_ai_tools.ingest import ingest_github_repo

    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "a.lua").write_text("function a() return 1 end\n", encoding="utf-8")
    (repo / "b.lua").write_text("function b() return 2 end\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["x", "--local-path", str(repo)])
    ingest_github_repo.main()

    (repo / "b.lua").unlink()
    ingest_github_repo.main()

    t = lancedb.connect(str(tmp_data_dir / "repo_lancedb")).open_table("codebase")
    paths = {r["file_path"].replace("\\", "/") for r in t.search().limit(1000).to_list()}
    assert not any(p.endswith("b.lua") for p in paths), "orphan rows for deleted b.lua remain"
