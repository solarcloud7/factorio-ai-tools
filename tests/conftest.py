"""Shared pytest fixtures: an offline fake embedder, a temp data dir, and small
fixture repos. These keep the suite fast and network/model-free."""

import os

import numpy as np
import pytest

from factorio_ai_tools.ingest import common

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


class FakeEmbedder:
    """Deterministic, L2-normalized, 768-d stand-in for SentenceTransformer.

    Avoids the ~400MB model download and any network in tests. Returns a unit
    vector per text (already normalized), with the active dimension chosen by a
    stable SHA-256 digest so results are reproducible across runs.
    """

    max_seq_length = 512

    def encode(self, texts, show_progress_bar=False, normalize_embeddings=True):
        vecs = np.zeros((len(texts), common.EMBEDDING_DIM), dtype="float32")
        for i, text in enumerate(texts):
            idx = int(common.get_hash(text)[:8], 16) % common.EMBEDDING_DIM
            vecs[i, idx] = 1.0
        return vecs

    def get_sentence_embedding_dimension(self):
        return common.EMBEDDING_DIM


@pytest.fixture
def fake_embedder(monkeypatch):
    """Patch the shared singleton so common.embed()/load_embedder() use the fake."""
    model = FakeEmbedder()
    monkeypatch.setattr(common, "_MODEL", model)
    return model


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    """Redirect common.get_data_dir() to a temp dir (no real data/ is touched)."""
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(common, "get_data_dir", lambda: str(d))
    return d


@pytest.fixture
def mini_repo(tmp_path):
    """A small generic repo: TS + Lua source, a doc, plus artifacts that MUST be excluded."""
    repo = tmp_path / "mini_repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.ts").write_text(
        "// a widget\nexport class Widget { render() { return 1; } }\n"
        "function helper() { return 2; }\n",
        encoding="utf-8",
    )
    (repo / "mod.lua").write_text(
        "function on_init()\n  return 1\nend\nlocal data = { a = 1, b = 2 }\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text(
        "# Title\n" + "\n".join(f"line {i}" for i in range(80)), encoding="utf-8"
    )
    # Artifacts that must NOT be ingested:
    (repo / "package-lock.json").write_text('{"lockfileVersion": 3}', encoding="utf-8")
    (repo / "node_modules" / "dep").mkdir(parents=True)
    (repo / "node_modules" / "dep" / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
    (repo / "dist").mkdir()
    (repo / "dist" / "bundle.js").write_text("var compiled = 1\n", encoding="utf-8")
    return repo


@pytest.fixture
def mini_clusterio(tmp_path):
    """A small Clusterio-shaped checkout: a core package + one plugin + package.json."""
    repo = tmp_path / "mini_clusterio"
    (repo / "packages" / "controller").mkdir(parents=True)
    (repo / "packages" / "controller" / "controller.ts").write_text(
        "export function startController() { return 1; }\n", encoding="utf-8"
    )
    (repo / "plugins" / "player_auth").mkdir(parents=True)
    (repo / "plugins" / "player_auth" / "index.ts").write_text(
        "export class PlayerAuth { login() { return 1; } }\n", encoding="utf-8"
    )
    (repo / "package.json").write_text('{"version": "9.9.9-test"}', encoding="utf-8")
    (repo / "package-lock.json").write_text('{"lockfileVersion": 3}', encoding="utf-8")
    return repo
