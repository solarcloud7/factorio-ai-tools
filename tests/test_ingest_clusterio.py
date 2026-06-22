"""End-to-end test for the Clusterio ingester (offline via a local CLUSTERIO_REPO)."""

import lancedb

from factorio_ai_tools.ingest import ingest_clusterio


def test_clusterio_clean_paths_plugin_filter_version_exclusions(
    mini_clusterio, tmp_data_dir, fake_embedder, monkeypatch
):
    monkeypatch.setenv("CLUSTERIO_REPO", str(mini_clusterio))
    ingest_clusterio.main()

    t = lancedb.connect(str(tmp_data_dir / "clusterio_lancedb")).open_table("codebase")
    paths = {r["file_path"] for r in t.search().limit(10000).to_list()}

    # clean, repo-relative, forward-slash paths (no ./ prefix, no backslashes, no abs path)
    assert all("\\" not in p and not p.startswith("./") and not p.startswith(str(mini_clusterio)) for p in paths)
    assert any(p.startswith("plugins/player_auth/") for p in paths)
    assert any(p.startswith("packages/controller/") for p in paths)

    # lockfile excluded
    assert not any("package-lock.json" in p for p in paths)

    # per-plugin filtering works
    hits = t.search().where("file_path LIKE '%player_auth%'").limit(100).to_list()
    assert hits and all("player_auth" in h["file_path"] for h in hits)

    # version.txt written from package.json (feeds get_mcp_version_info)
    vp = tmp_data_dir / "clusterio_lancedb" / "version.txt"
    assert vp.exists() and vp.read_text(encoding="utf-8") == "9.9.9-test"
