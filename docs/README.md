# Documentation

Reference docs for the Factorio AI Tools MCP server — a hybrid-search RAG system
that exposes expert Factorio modding and Clusterio plugin knowledge to LLM
clients. Each doc describes the current state of the code and is checkable against
it.

| Doc | What it covers |
|---|---|
| [architecture.md](architecture.md) | The modules, the ingest → LanceDB → server data flow, and the shared contracts every part must honor. |
| [stores.md](stores.md) | The six LanceDB stores: table names, schemas, key columns, and FTS indexes. |
| [tools.md](tools.md) | The MCP tools and prompt the server exposes — signatures, behavior, and which store each queries. |
| [rag-pipeline-playbook.md](rag-pipeline-playbook.md) | Validation gates, the dry-run protocol, and known limitations for the ingest pipeline. |

Project-level guidance lives at the repo root:

- [../README.md](../README.md) — install and quick start.
- [../CLAUDE.md](../CLAUDE.md) — instructions for Claude Code working in this repo.
- [../.agents/AGENTS.md](../.agents/AGENTS.md) — conventions (Windows printing, SQL escaping, committing).
