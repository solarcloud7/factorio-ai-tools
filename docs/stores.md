# LanceDB stores

Six stores live under `data/`, each built by one ingest script
and queried by one server tool. Every store shares the embedding contract: a
`vector` column of **768** float32s, L2-normalized (`common.EMBEDDING_DIM`). Each
ingester also builds a full-text-search (FTS) index on its text column, so a
freshly rebuilt store supports hybrid retrieval.

`ensure_table` rebuilds a table whose columns are a stale subset of the target
schema, so changing a schema below forces a full re-ingest of that store.

| Store (`data/…`) | Table | Built by | Queried by | FTS column |
|---|---|---|---|---|
| `factorio_lancedb` | `docs` | [ingest_factorio.py](../src/factorio_ai_tools/ingest/ingest_factorio.py) | `search_factorio_docs` | `text` |
| `wiki_lancedb` | `docs` | [ingest_wiki.py](../src/factorio_ai_tools/ingest/ingest_wiki.py) | `search_factorio_wiki` | `text` |
| `forum_lancedb` | `forum` | [ingest_forum.py](../src/factorio_ai_tools/ingest/ingest_forum.py) | `search_factorio_forums` | `content` |
| `clusterio_lancedb` | `codebase` | [ingest_clusterio.py](../src/factorio_ai_tools/ingest/ingest_clusterio.py) | `search_clusterio_code` | `content` |
| `repo_lancedb` | `codebase` | [ingest_github_repo.py](../src/factorio_ai_tools/ingest/ingest_github_repo.py) | `search_github_code` | `content` |
| `prototypes_lancedb` | `prototypes` | [ingest_prototypes.py](../src/factorio_ai_tools/ingest/ingest_prototypes.py) | `search_factorio_prototypes` | `content` |

## factorio_lancedb → `docs`

Factorio Lua API + prototype documentation. Columns: `text`, `vector`,
`node_type`, `class_name`, `returns`, `version`, `url`, `source_url`,
`content_hash`. Holds **three pinned versions** (`common.SUPPORTED_FACTORIO_VERSIONS =
("1.1.110", "2.0.76", "2.1.8")` — 1.1 legacy, 2.0.76 stable, 2.1.8 experimental; no
moving `latest`); `search_factorio_docs` **requires** a concrete `version` (one of
those) plus optional `class_name`. Writes `version.txt`.

## wiki_lancedb → `docs`

The Factorio wiki. Columns: `text`, `vector`, `title`, `url`, `content_hash`.

## forum_lancedb → `forum`

Curated forum topics. Columns: `id`, `file_path` (topic URL), `class_name` (topic
title), `content`, `version`, `content_hash`, `vector`. Source list:
`forum_links.txt` (see the forum caveat in
[rag-pipeline-playbook.md](rag-pipeline-playbook.md)).

## clusterio_lancedb → `codebase`

A Clusterio checkout, AST-chunked. Columns: `file_path` (POSIX, repo-relative),
`node_type`, `node_name`, `content`, `content_hash`, `vector`.
`search_clusterio_code` can filter by `node_type` and by `plugin` (matched against
`file_path`). Writes `version.txt` from the repo's `package.json`.

## repo_lancedb → `codebase`

One store holding **many** GitHub repos. Columns: `vector`, `repo_url`,
`file_path` (POSIX), `node_name`, `node_type`, `content`, `content_hash`.
`repo_url` is the `owner/repo` slug for `--repo-url` clones (a basename for
`--local-path`); `search_github_code` scopes to one repo by matching `repo_name`
against `repo_url`. Built by the generic ingester, which superseded the retired
per-mod `mod_lancedb`.

## prototypes_lancedb → `prototypes`

Exact numerical Factorio prototype values, one structured record per prototype
**per version**. Columns: `prototype_type`, `prototype_name`, `category`, `content`,
`version`, `content_hash`, `vector`. `search_factorio_prototypes` REQUIRES a
`factorio_version` (`2.0.76` or `2.1.8` — values change between releases) and
optionally filters by `prototype_type` (umbrella values `item`/`entity` expand to
their raw subtypes). **Multi-version:** the dedup key and orphan-pruning are
`(prototype_type, prototype_name, version)`-scoped, so each version is a separate
`factorio-export/vanilla_<ver>/` dump and re-ingesting one version never touches
another's rows. Built from Factorio's own `factorio --dump-data` JSON export (the
fully-resolved `data.raw`, env `FACTORIO_DATA_DUMP` for a single dump, else all
`vanilla_*/` dumps) — no Lua parsing. The **vanilla baseline** (base + official DLC);
modded games change `data.raw`, so a modded game differs. Built locally via
`make ingest-prototypes`, then shipped in the release zip like the other stores.
Writes a comma-joined `version.txt` of every version present.

## node_type vocabulary

The code chunkers (`clusterio`, `repo`) emit `node_type` from
`common.NODE_TYPES`: `class`, `interface`, `function`, `method`, `table`
(tree-sitter captures), plus `node`, `text_chunk` (line-window fallback for
uncapturable code), and `text_file` (non-code files). The factorio doc ingester
parses an API schema rather than source, so it uses its own vocabulary
(`prototype_property`, `attribute`, `event`, …).
