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
    # backslash must be escaped FIRST, else the escapes themselves get mangled
    assert common.like_escape("a\\b") == "a\\\\b"


def test_like_filter_matches_literal_underscore(tmp_path):
    """Integration gate: the server's actual filter must treat `_` as a literal,
    so plugin='player_auth' selects player_auth and NOT player1auth. Also proves
    LanceDB/DataFusion honors the ESCAPE clause at runtime (a string test can't)."""
    import pyarrow as pa

    db = lancedb.connect(str(tmp_path / "t"))
    schema = pa.schema([
        pa.field("vector", pa.list_(pa.float32(), 4)),
        pa.field("file_path", pa.string()),
    ])
    t = db.create_table("x", schema=schema, data=[
        {"vector": [0.1, 0.2, 0.3, 0.4], "file_path": "plugins/player_auth/a.ts"},
        {"vector": [0.1, 0.2, 0.3, 0.4], "file_path": "plugins/player1auth/a.ts"},
    ])
    where = common.like_filter("file_path", "player_auth")
    rows = t.search([0.1, 0.2, 0.3, 0.4]).where(where).limit(10).to_list()
    assert {r["file_path"] for r in rows} == {"plugins/player_auth/a.ts"}


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


# --- Coverage recalibration (pin before/after the threshold change) ----------


def test_coverage_keeps_ast_for_import_heavy():
    """An import-heavy module must keep its per-declaration AST chunks, not collapse
    to text just because imports aren't captured (the miscalibrated <50% byte rule)."""
    src = ("\n".join(f"import {{ x{i} }} from './mod{i}';" for i in range(15))
           + "\nclass Foo {\n  bar() { return 1; }\n}\n")
    chunks = common.chunk_code(src.encode("utf-8"), "typescript")
    assert any(c["node_type"] == "class" for c in chunks), [c["node_type"] for c in chunks]


def test_coverage_falls_back_when_uncapturable():
    """A file the grammar can't capture (only top-level statements) must still be
    chunked via the text fallback — nothing silently dropped."""
    src = "const a = 1;\nconst b = 2;\nconsole.log(a + b);\n"
    chunks = common.chunk_code(src.encode("utf-8"), "typescript")
    assert chunks and all(c["node_type"] == "text_chunk" for c in chunks)


# --- Prefix-aware embedded cap -----------------------------------------------


def test_embedded_text_within_cap_incl_prefix():
    """The embedded string is prefix+content; a long path must not push it past the
    embedder's hard cap (silent truncation). build_embed_entries re-splits to fit."""
    from factorio_ai_tools.ingest.ingest_github_repo import build_embed_entries

    long_path = "/".join(f"dir{i}" for i in range(60)) + "/file.lua"
    chunk = {"node_name": "n", "node_type": "function", "content": "x = 1\n" * 400}
    entries = build_embed_entries(long_path, chunk, "h", "owner/repo")
    assert entries
    for e in entries:
        assert common.count_tokens(e["text_to_embed"]) <= common.EMBED_MAX_TOKENS


# --- Vocab, idempotency, bootstrap -------------------------------------------


def test_node_type_vocab(mini_repo, tmp_data_dir, fake_embedder, monkeypatch):
    from factorio_ai_tools.ingest import ingest_github_repo

    monkeypatch.setattr(sys, "argv", ["x", "--local-path", str(mini_repo)])
    ingest_github_repo.main()
    t = lancedb.connect(str(tmp_data_dir / "repo_lancedb")).open_table("codebase")
    types = {r["node_type"] for r in t.search().limit(10000).to_list()}
    assert types <= common.NODE_TYPES, f"unexpected node_types: {types - common.NODE_TYPES}"


def test_noop_reingest_writes_zero(mini_repo, tmp_data_dir, fake_embedder, monkeypatch):
    from factorio_ai_tools.ingest import ingest_github_repo

    monkeypatch.setattr(sys, "argv", ["x", "--local-path", str(mini_repo)])
    ingest_github_repo.main()
    t = lancedb.connect(str(tmp_data_dir / "repo_lancedb")).open_table("codebase")
    n1 = t.count_rows()
    assert n1 > 0
    ingest_github_repo.main()
    t2 = lancedb.connect(str(tmp_data_dir / "repo_lancedb")).open_table("codebase")
    assert t2.count_rows() == n1, "a no-op re-ingest changed the row count"


def test_ensure_stores_does_not_overwrite_existing(tmp_path):
    """Bootstrap must extract ONLY missing stores, never clobber a hand-built data/."""
    import shutil

    data_dir = tmp_path / "data"
    stores = ["a_lancedb", "b_lancedb", "c_lancedb"]
    for s in ("a_lancedb", "b_lancedb"):
        (data_dir / s).mkdir(parents=True)
        (data_dir / s / "keep.txt").write_text("ORIGINAL", encoding="utf-8")

    src = tmp_path / "src"
    for s in stores:
        (src / s).mkdir(parents=True)
        (src / s / "keep.txt").write_text("FROMZIP", encoding="utf-8")
    zip_file = shutil.make_archive(str(tmp_path / "bundle"), "zip", str(src))

    def fake_download(url, dest):
        shutil.copy(zip_file, dest)

    added = common.ensure_stores(str(data_dir), stores, url="x", download=fake_download)
    assert added == ["c_lancedb"]
    assert (data_dir / "a_lancedb" / "keep.txt").read_text(encoding="utf-8") == "ORIGINAL"
    assert (data_dir / "b_lancedb" / "keep.txt").read_text(encoding="utf-8") == "ORIGINAL"
    assert (data_dir / "c_lancedb" / "keep.txt").read_text(encoding="utf-8") == "FROMZIP"
