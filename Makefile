.PHONY: help compact ingest-all ingest-factorio ingest-wiki ingest-forum ingest-clusterio ingest-repos package-dbs deploy-dbs test mcp

# Latest GitHub release tag; override with `make deploy-dbs TAG=vX.Y.Z`.
TAG ?= $(shell gh release view --json tagName -q .tagName)

PY = uv run --no-sync python

# The 5 distinct-schema stores, in the order ingest-all builds them.
STORES = factorio_lancedb clusterio_lancedb wiki_lancedb forum_lancedb repo_lancedb

help:
	@echo "Available commands:"
	@echo "  make ingest-all    - (Re)build all 5 LanceDB stores (incremental/idempotent)"
	@echo "  make compact       - Compact/finalize every data/*_lancedb store"
	@echo "  make package-dbs   - Zip the 5 stores into factorio_lancedb.zip"
	@echo "  make deploy-dbs    - Compact, package, and upload the final build to the latest release"
	@echo "  make test          - Run the offline test suite (chunk-health strict)"
	@echo "  make mcp           - Start the MCP server"

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

test:
	$(PY) -m pytest -q

mcp:
	.\start_mcp_server.bat
