.PHONY: help sync compact status dump-data clean ingest-all ingest-factorio ingest-wiki ingest-forum ingest-clusterio ingest-repos ingest-prototypes package-dbs deploy-dbs test eval smoke mcp mcp-logs mcp-down mcp-host inspect

# Latest GitHub release tag; override with `make deploy-dbs TAG=vX.Y.Z`.
TAG ?= $(shell gh release view --json tagName -q .tagName)

PY = uv run --no-sync python

# The release/package set: the 6 stores bundled into factorio_lancedb.zip. This
# MUST mirror server.py's RELEASE_STORES so package-dbs/deploy-dbs ship exactly what
# ensure_databases() extracts. prototypes_lancedb is built locally from a
# `factorio --dump-data` export (make ingest-prototypes) before packaging.
STORES = factorio_lancedb clusterio_lancedb wiki_lancedb forum_lancedb repo_lancedb prototypes_lancedb

help:
	@echo "Available commands:"
	@echo "  make sync          - Install deps; auto-select CUDA torch if an NVIDIA GPU is present"
	@echo "  make ingest-all    - (Re)build all 6 LanceDB stores (incremental/idempotent)"
	@echo "  make compact       - Compact/finalize every data/*_lancedb store"
	@echo "  make status        - Per-store inventory: row count, version, FTS index, health (read-only)"
	@echo "  make dump-data     - Vanilla factorio --dump-data export (no mods) for prototypes; needs FACTORIO_BIN + VER"
	@echo "  make package-dbs   - Zip the 6 stores into factorio_lancedb.zip"
	@echo "  make deploy-dbs    - Compact, package, upload to the latest release, then delete the local zip"
	@echo "  make clean         - Remove throwaway build artifacts (zip, scratch, caches); keeps data/ + dumps"
	@echo "  make test          - Run the offline test suite (chunk-health strict)"
	@echo "  make eval          - Retrieval recall@k: vector vs FTS vs hybrid (after re-ingest)"
	@echo "  make smoke         - Release smoke test: install published wheel, fresh download, assert tools"
	@echo "  make mcp           - Start the shared MCP container (SSE :8000, the canonical single instance)"
	@echo "  make mcp-logs      - Follow the shared MCP container's logs"
	@echo "  make mcp-down      - Stop the shared MCP container"
	@echo "  make mcp-host      - Serve MCP as a bare host process instead (no Docker; conflicts on :8000)"
	@echo "  make inspect       - Launch MCP Inspector (stdio; self-contained)"

ingest-factorio:
	$(PY) -m factorio_ai_tools.ingest.ingest_factorio

ingest-wiki:
	$(PY) -m factorio_ai_tools.ingest.ingest_wiki

ingest-forum:
	$(PY) -m factorio_ai_tools.ingest.ingest_forum

# Clones the Clusterio monorepo to ./clusterio (the script's default
# CLUSTERIO_REPO) on first run, then ingests it.
ingest-clusterio:
	$(PY) -c "import os,subprocess as s; s.run(['git','clone','--depth','1','https://github.com/clusterio/clusterio.git','clusterio'],check=True) if not os.path.exists('clusterio') else print('clusterio checkout present')"
	$(PY) -m factorio_ai_tools.ingest.ingest_clusterio

# Generic GitHub repos -> the shared repo_lancedb store (incremental).
# factorio-data: vanilla prototype definitions; draftsman/blueprint-editor:
# blueprint tooling; maraxsis: a worked-example Lua mod.
ingest-repos:
	$(PY) -m factorio_ai_tools.ingest.ingest_github_repo --repo-url https://github.com/wube/factorio-data.git
	$(PY) -m factorio_ai_tools.ingest.ingest_github_repo --repo-url https://github.com/redruin1/factorio-draftsman.git
	$(PY) -m factorio_ai_tools.ingest.ingest_github_repo --repo-url https://github.com/Teoxoy/factorio-blueprint-editor.git
	$(PY) -m factorio_ai_tools.ingest.ingest_github_repo --repo-url https://github.com/notnotmelon/maraxsis.git

# Reads a Factorio `--dump-data` export (no clone needed). Produce the dump from a
# vanilla install (see factorio-export/README.md) and point FACTORIO_DATA_DUMP at
# it, or place it at the default factorio-export/vanilla_<ver>/ path. Errors loudly
# if the dump is missing — this store can't be built without it.
ingest-prototypes:
	$(PY) -m factorio_ai_tools.ingest.ingest_prototypes

# prototypes needs a manually-produced dump, so it runs LAST and non-fatally (the
# leading '-'): a missing dump must never break refreshing the other five stores.
ingest-all: ingest-factorio ingest-wiki ingest-forum ingest-clusterio ingest-repos
	-$(PY) -m factorio_ai_tools.ingest.ingest_prototypes

compact:
	$(PY) maintenance/compact_lancedb.py

# Read-only inventory of data/*_lancedb: row count, version, FTS index, health.
status:
	$(PY) maintenance/store_status.py

# Produce a vanilla `factorio --dump-data` export (base + Space Age DLC, no community
# mods) for the prototypes store. Needs a Factorio install + the version label:
#   make dump-data FACTORIO_BIN="D:\factorio\bin\x64\factorio.exe" VER=2.0.76
dump-data:
	$(PY) maintenance/dump_data.py --factorio "$(FACTORIO_BIN)" --version "$(VER)"

# Bundle exactly the 6 stores into factorio_lancedb.zip (arcnames relative to
# data/, so each <store>/ sits at the zip root). The asset name is load-bearing:
# server.ensure_databases downloads exactly this from the latest release. Listing
# the stores explicitly excludes any stray dirs (e.g. a leftover mod_lancedb).
package-dbs:
	$(PY) -c "import os,zipfile; stores='$(STORES)'.split(); z=zipfile.ZipFile('factorio_lancedb.zip','w',zipfile.ZIP_DEFLATED); [z.write(os.path.join(r,f), os.path.relpath(os.path.join(r,f),'data')) for s in stores for r,_,fs in os.walk(os.path.join('data',s)) for f in fs]; z.close(); print('Packaged', len(stores), 'stores into factorio_lancedb.zip')"

# Manual release of the final full build: finalize (compact), package, upload to the
# latest GitHub release, then delete the local zip — once it's on the release it's a
# throwaway artifact (the source of truth is data/ + the release asset), so we don't
# leave a stale ~100 MB file behind.
deploy-dbs: compact package-dbs
	gh release upload $(TAG) factorio_lancedb.zip --clobber
	$(PY) -c "import os; os.path.exists('factorio_lancedb.zip') and os.remove('factorio_lancedb.zip'); print('Removed local factorio_lancedb.zip')"

# Remove throwaway build artifacts (the release zip, dump-mod scratch, py caches).
# KEEPS data/ stores and factorio-export/vanilla_* dumps (the ingest inputs).
clean:
	$(PY) -c "import shutil,glob,os; [os.remove(p) for p in glob.glob('factorio_lancedb.zip')]; [shutil.rmtree(p,ignore_errors=True) for p in ['factorio-export/.vanilla-mods','.pytest_cache','build','dist']+glob.glob('**/__pycache__',recursive=True)+glob.glob('*.egg-info')]; print('Cleaned build artifacts (kept data/ + factorio-export/vanilla_* dumps).')"

# Sync deps, then auto-select torch by hardware: pyproject keeps the CPU wheel as
# default (clean for PyPI/Docker/CI); on a box with an NVIDIA GPU this swaps in the
# CUDA wheel so ingestion embeds on the GPU. Reproducible across venv recreation.
# The leading '-' makes uv sync non-fatal: on Windows it can't refresh the
# console-script .exe while an MCP server is running from this venv, but deps are
# synced before that step, so we continue to the torch selection regardless.
sync:
	-uv sync --group dev
	$(PY) -c "import shutil, subprocess; subprocess.run(['uv','pip','install','--reinstall','torch','--index-url','https://download.pytorch.org/whl/cu124'], check=True) if shutil.which('nvidia-smi') else print('No NVIDIA GPU detected -> keeping CPU torch.')"

test:
	$(PY) -m pytest -q

# Retrieval recall@k on the golden set: vector vs FTS vs hybrid (real model + live
# stores). The ship-gate for hybrid search; run after a re-ingest. Not in CI.
eval:
	$(PY) maintenance/eval_retrieval.py

# Release smoke test: install the PUBLISHED wheel into an isolated venv, force a
# fresh DB download from the latest release, and assert every tool. Run AFTER a
# release+deploy. `make smoke VERSION=1.2.0` pins a version; default = PyPI latest.
# `make smoke LOCAL=1` runs the same checks against local code+data (fast).
smoke:
	$(PY) maintenance/smoke_release.py $(if $(VERSION),--version $(VERSION),) $(if $(LOCAL),--local,)

# Serving the MCP. The CANONICAL instance is the shared Docker container (compose.yml):
# one process on the factorio-shared network, reached by name by every consumer (the
# Discord bot, Claude Desktop), with the model + 6 stores resident once. `make mcp` starts
# THAT, detached (it has restart: unless-stopped + a healthcheck). The bare host-process
# path (mcp-host) is kept for quick no-Docker server dev, but it binds the SAME host :8000
# as the container — run one or the other, never both.
mcp:
	docker compose up -d

mcp-logs:
	docker compose logs -f factorio-ai-tools

mcp-down:
	docker compose down

mcp-host:
	.\start_mcp_server.bat

inspect:
	npx --yes @modelcontextprotocol/inspector uv run factorio-ai-tools
