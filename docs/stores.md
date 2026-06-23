# LanceDB stores

Five distinct-schema stores live under `data/`, each built by one ingest script
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

## factorio_lancedb → `docs`

Factorio Lua API + prototype documentation. Columns: `text`, `vector`,
`node_type`, `class_name`, `returns`, `version`, `url`, `source_url`,
`content_hash`. Holds **two versions** (`VERSIONS_TO_SCRAPE = ["1.1.110",
"latest"]`); `search_factorio_docs` filters by `version` (default `latest`) and
optional `class_name`. Writes `version.txt`.

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

## node_type vocabulary

The code chunkers (`clusterio`, `repo`) emit `node_type` from
`common.NODE_TYPES`: `class`, `interface`, `function`, `method`, `table`
(tree-sitter captures), plus `node`, `text_chunk` (line-window fallback for
uncapturable code), and `text_file` (non-code files). The factorio doc ingester
parses an API schema rather than source, so it uses its own vocabulary
(`prototype_property`, `attribute`, `event`, …).
