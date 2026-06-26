# MCP tools

The server ([server.py](../src/factorio_ai_tools/server.py)) exposes these to MCP
clients. Every tool is wrapped in `@optional_tool()`, so any can be turned off with
`--disable-tools` or restricted with `--enable-tools` (comma-separated names). A
tool whose store failed to open returns a "run ingest_X first" error instead of
crashing the server.

## Search tools

All six accept a **list** of query strings (encoded in one batch) and clamp
`limit` to **1–20**. Each returns a formatted markdown string. Retrieval goes
through `common.hybrid_search`: LanceDB hybrid (RRF reranking over the store's FTS
index + the dense vector), falling back to pure vector where the store has no FTS
index or on a transient error.

| Tool | Signature | Store | Filters |
|---|---|---|---|
| `search_factorio_docs` | `(queries, class_filter=None, limit=5, factorio_version)` | factorio | `class_name`, `version` (**required**: `1.1.110` or `2.0.76`; no `latest`) |
| `search_clusterio_code` | `(queries, node_type=None, plugin=None, limit=5)` | clusterio | `node_type`, `plugin` (matched against `file_path`) |
| `search_factorio_wiki` | `(queries, limit=5)` | wiki | — |
| `search_factorio_forums` | `(queries, limit=5)` | forum | — |
| `search_github_code` | `(queries, repo_name=None, limit=5)` | repo | `repo_name` (matched against `repo_url`) |
| `search_factorio_prototypes` | `(queries, prototype_type=None, limit=5)` | prototypes | `prototype_type` (umbrella `item`/`entity` expand to subtypes) |

Filter values are escaped before use: single quotes are doubled for the SQL
literal, and `LIKE` filters (`plugin`, `repo_name`) escape `% _ \` via
`common.like_filter` with an `ESCAPE` clause, so `player_auth` cannot match
`player1auth`. See [stores.md](stores.md) for each store's columns and the
`node_type` vocabulary.

## Blueprint tools (no store)

- `decode_factorio_blueprint(blueprint_string)` — decodes a Factorio blueprint
  string (version byte `0` + base64 + zlib) to formatted JSON. Guards decompression
  at 10 MB.
- `encode_factorio_blueprint(json_string)` — the reverse: JSON → blueprint string.

## Utility tools (no store)

- `factorio_mod_portal_analyzer(mod_name)` — queries the mods.factorio.com API for
  a mod's metadata, dependencies, and latest release.
- `get_mcp_version_info()` — returns the tool version plus the factorio and
  clusterio dataset versions read from each store's `version.txt`.

## Prompt

- `factorio_clusterio_expert` (`@mcp.prompt()`) — supplies the Factorio modding +
  Clusterio plugin mental model (the three modding phases, the Clusterio package
  layers, and when to reach for each search tool).
