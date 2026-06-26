"""Factorio docs ingester: parse-function units + an offline end-to-end run."""

import json
import os
import sys

import lancedb

from factorio_ai_tools.ingest import ingest_factorio

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


def test_parse_runtime_api_shapes():
    chunks = ingest_factorio.parse_runtime_api(_load("runtime_api.json"), "2.0.76", "src", "h")
    types = {c["node_type"] for c in chunks}
    assert {"class", "method", "attribute", "event", "concept"} <= types
    required = {"text", "node_type", "class_name", "version", "url", "source_url", "content_hash"}
    assert all(required <= set(c) for c in chunks)


def test_parse_prototype_api_shapes():
    chunks = ingest_factorio.parse_prototype_api(_load("prototype_api.json"), "2.0.76", "src", "h")
    types = {c["node_type"] for c in chunks}
    assert {"prototype", "prototype_property", "prototype_type"} <= types


class _Resp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200
        self.text = json.dumps(payload)

    def json(self):
        return self._p


def test_factorio_e2e_populates_both_versions(tmp_data_dir, fake_embedder, monkeypatch):
    runtime, proto = _load("runtime_api.json"), _load("prototype_api.json")
    monkeypatch.setattr(
        ingest_factorio.requests, "get",
        lambda url, *a, **k: _Resp(runtime if "runtime-api" in url else proto),
    )
    monkeypatch.setattr(sys, "argv", ["ingest_factorio"])
    ingest_factorio.main()

    t = lancedb.connect(str(tmp_data_dir / "factorio_lancedb")).open_table("docs")
    versions = {r["version"] for r in t.search().limit(10000).to_list()}
    # Both pinned concrete versions must exist; the moving "latest" label is gone
    # (search_factorio_docs requires a concrete version).
    assert {"1.1.110", "2.0.76"} <= versions and "latest" not in versions
    assert (tmp_data_dir / "factorio_lancedb" / "version.txt").exists()
