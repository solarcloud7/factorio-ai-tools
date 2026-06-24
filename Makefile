.PHONY: help sync compact ingest-all ingest-factorio ingest-wiki ingest-forum ingest-clusterio ingest-repos package-dbs deploy-dbs test eval smoke mcp inspect

# Latest GitHub release tag; override with `make deploy-dbs TAG=vX.Y.Z`.
TAG ?= $(shell gh release view --json tagName -q .tagName)

PY = uv run --no-sync python

# The 5 distinct-schema stores, in the order ingest-all builds them.
STORES = factorio_lancedb clusterio_lancedb wiki_lancedb forum_lancedb repo_lancedb

help:
	@echo "Available commands:"
	@echo "  make sync          - Install deps; auto-select CUDA torch if an NVIDIA GPU is present"
	@echo "  make ingest-all    - (Re)build all 5 LanceDB stores (incremental/idempotent)"
	@echo "  make compact       - Compact/finalize every data/*_lancedb store"
	@echo "  make package-dbs   - Zip the 5 stores into factorio_lancedb.zip"
	@echo "  make deploy-dbs    - Compact, package, and upload the final build to the latest release"
	@echo "  make test          - Run the offline test suite (chunk-health strict)"
	@echo "  make eval          - Retrieval recall@k: vector vs FTS vs hybrid (after re-ingest)"
	@echo "  make smoke         - Release smoke test: install published wheel, fresh download, assert tools"
	@echo "  make mcp           - Start the MCP server over SSE (port 8000)"
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

ingest-all: ingest-factorio ingest-wiki ingest-forum ingest-clusterio ingest-repos

compact:
	$(PY) maintenance/compact_lancedb.py

# Bundle exactly the 5 stores into factorio_lancedb.zip (arcnames relative to
# data/, so each <store>/ sits at the zip root). The asset name is load-bearing:
# server.ensure_databases downloads exactly this from the latest release. Listing
# the stores explicitly excludes any stray dirs (e.g. a leftover mod_lancedb).
package-dbs:
	$(PY) -c "import os,zipfile; stores='$(STORES)'.split(); z=zipfile.ZipFile('factorio_lancedb.zip','w',zipfile.ZIP_DEFLATED); [z.write(os.path.join(r,f), os.path.relpath(os.path.join(r,f),'data')) for s in stores for r,_,fs in os.walk(os.path.join('data',s)) for f in fs]; z.close(); print('Packaged', len(stores), 'stores into factorio_lancedb.zip')"

# Manual release of the final full build: finalize (compact), package, and
# upload to the latest GitHub release (data/ is gitignored, so this is local).
deploy-dbs: compact package-dbs
	gh release upload $(TAG) factorio_lancedb.zip --clobber

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

mcp:
	.\start_mcp_server.bat

inspect:
	npx --yes @modelcontextprotocol/inspector uv run factorio-ai-tools
