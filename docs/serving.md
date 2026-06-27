# Serving the MCP

The server runs over **stdio** (default, one process per client) or **SSE**
(`--sse --port 8000`, one shared process for many clients). For anything beyond a
single client, run **one shared SSE instance** and point every client at it — the
embedding model + 6 LanceDB stores load once (~1 GB) instead of per client.

## The shared instance (canonical)

`compose.yml` runs that one instance as a container. It:

- serves SSE on container port 8000, **published to host `:8000`**;
- sets **`FASTMCP_HOST=0.0.0.0`** — the server otherwise binds `127.0.0.1` inside
  the container, which refuses the published port and cross-container access;
- bind-mounts **`./data`** (serves this checkout's local stores) and the host
  HuggingFace cache (the model);
- has `restart: unless-stopped` + a healthcheck;
- joins the **external** Docker network **`factorio-shared`**, so containers from
  other compose projects reach it by service name at
  `http://factorio-ai-tools:8000/sse`.

### Start / stop

```powershell
docker network create factorio-shared   # one-time (idempotent); the network is external
make mcp        # docker compose up -d   — start the shared container, detached
make mcp-logs   # docker compose logs -f factorio-ai-tools
make mcp-down   # docker compose down    — stop it
```

`make mcp` does **not** rebuild the image. After changing **server code**, run
`docker compose up -d --build`. After a **data-only** re-ingest, `make mcp-down &&
make mcp` re-opens the fresh stores.

### No-Docker alternative

`make mcp-host` (`start_mcp_server.bat` → `uv run factorio-ai-tools --sse --port
8000`) serves the same SSE from a bare host process. It binds the **same** host
`:8000` as the container — run one or the other, never both.

## Connecting clients (one entry each, so nothing spawns its own)

- **Claude Code** — a single **user/global** SSE entry (no per-project stdio
  launchers):
  ```
  claude mcp add -s user --transport sse factorio-ai-tools http://localhost:8000/sse
  ```
- **Claude Desktop** — in `claude_desktop_config.json`, via the `mcp-remote` shim
  (the `cmd /c` wrapper lets Windows find `npx`):
  ```json
  "factorio-ai-tools": { "command": "cmd", "args": ["/c", "npx", "-y", "mcp-remote", "http://localhost:8000/sse"] }
  ```
- **Another docker-compose project** — join the external `factorio-shared` network
  and reach `http://factorio-ai-tools:8000/sse` by service name; do **not** add a
  second MCP service.

A stdio launcher (`… python.exe server.py`, or `docker run … :latest` per client)
starts its **own** copy — use the SSE entries above instead. All of them resolve
only while the shared container is up; that is intentional (no fallback that
quietly starts a duplicate).

## Operational notes

- **Stop the container before `make compact` / `make deploy-dbs`** (`make
  mcp-down`). Those rewrite/prune the LanceDB files, and Windows cannot replace a
  memory-mapped file the running container holds open.
- The container serves whatever is in `./data`, including in-progress builds — re-
  ingest locally, then restart it to serve the new stores. Public consumers get
  the stores from the release zip (`server.ensure_databases()`), not from `./data`.
