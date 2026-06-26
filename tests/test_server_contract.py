"""Drift guard: every ingest schema must cover the columns server.py reads, and
the server must expose a search tool per store. This would have caught the
gutted ingest scripts / schema drift that triggered this whole effort."""

import os

from factorio_ai_tools.ingest import (
    common,
    ingest_clusterio,
    ingest_factorio,
    ingest_forum,
    ingest_github_repo,
    ingest_prototypes,
    ingest_wiki,
)

ROOT = os.path.join(os.path.dirname(__file__), "..")
SERVER = os.path.join(ROOT, "src", "factorio_ai_tools", "server.py")

# Doc-drift guard inputs: a store/tool shipped without doc updates (as the prototypes
# store originally was, in PR #9) must fail CI. Extend BOTH when adding a store — see
# the "Adding a store" checklist in CLAUDE.md.
STORE_DIRS = ["factorio_lancedb", "wiki_lancedb", "clusterio_lancedb",
              "forum_lancedb", "repo_lancedb", "prototypes_lancedb"]
SEARCH_TOOLS = ["search_factorio_docs", "search_factorio_wiki", "search_factorio_forums",
                "search_clusterio_code", "search_github_code", "search_factorio_prototypes"]

# Columns server.py reads or filters on, per store -> the ingest schema MUST cover them.
CONTRACT = {
    "factorio": (ingest_factorio.FactorioDoc, {"text", "url", "class_name", "version"}),
    "wiki": (ingest_wiki.WikiDoc, {"text", "title", "url"}),
    "clusterio": (ingest_clusterio.CodeChunk, {"content", "file_path", "node_type", "node_name"}),
    "forum": (ingest_forum.SCHEMA, {"content", "class_name", "file_path"}),
    "repo": (ingest_github_repo.SCHEMA, {"content", "repo_url", "file_path", "node_type", "node_name"}),
    "prototypes": (ingest_prototypes.PrototypeRecord,
                   {"prototype_type", "prototype_name", "category", "content", "version", "content_hash"}),
}


def test_schemas_cover_server_reads():
    for store, (schema, required) in CONTRACT.items():
        cols = common._schema_columns(schema)
        assert required <= cols, f"{store}: schema missing columns server.py reads: {required - cols}"


def test_server_exposes_a_search_tool_per_store():
    src = open(SERVER, encoding="utf-8").read()
    for fn in SEARCH_TOOLS:
        assert f"def {fn}(" in src, f"server.py is missing search tool {fn}"


def test_every_store_documented_in_claude_md():
    """Doc-drift guard: every store the server opens must appear in CLAUDE.md's
    per-store list. PR #9 shipped prototypes_lancedb with no doc update — a review
    caught it after the fact; this fails CI on that gap instead."""
    claude = open(os.path.join(ROOT, "CLAUDE.md"), encoding="utf-8").read()
    missing = [s for s in STORE_DIRS if s not in claude]
    assert not missing, f"CLAUDE.md is missing per-store docs for: {missing}"


def test_every_search_tool_documented_in_tools_md():
    """Doc-drift guard: every search tool must be listed in docs/tools.md."""
    tools = open(os.path.join(ROOT, "docs", "tools.md"), encoding="utf-8").read()
    missing = [t for t in SEARCH_TOOLS if t not in tools]
    assert not missing, f"docs/tools.md is missing tool docs for: {missing}"


def test_all_schemas_carry_content_hash_and_vector():
    for store, (schema, _required) in CONTRACT.items():
        cols = common._schema_columns(schema)
        assert "vector" in cols, f"{store}: schema missing vector column"
        assert "content_hash" in cols, f"{store}: schema missing content_hash (incremental dedup)"


def test_prototype_umbrella_filter_groups_cover_subtypes():
    """search_factorio_prototypes advertises "item"/"entity" as filter values, but
    the store keeps each raw subtype — the umbrella expansion must cover them, and
    the server must actually use the shared groups (not an exact-match WHERE)."""
    groups = common.PROTOTYPE_TYPE_GROUPS
    assert {"module", "ammo", "gun"} <= groups["item"], "item umbrella misses item subtypes"
    assert {"furnace", "inserter", "assembling-machine"} <= groups["entity"], "entity umbrella misses entity subtypes"
    src = open(SERVER, encoding="utf-8").read()
    assert "PROTOTYPE_TYPE_GROUPS" in src, "server.py no longer expands umbrella prototype_type filters (#4 regression)"


def test_search_factorio_docs_requires_concrete_version():
    """The docs tool must REQUIRE a concrete version (no implicit 'latest' default)
    and validate against the pinned set — the moving 'latest' label was removed."""
    src = open(SERVER, encoding="utf-8").read()
    assert "factorio_version: str = None" in src, "factorio_version must have no default (be required)"
    assert "SUPPORTED_FACTORIO_VERSIONS" in src, "docs tool must validate the version against the pinned set"
    assert 'factorio_version="latest"' not in src, "the 'latest' default must be gone"
