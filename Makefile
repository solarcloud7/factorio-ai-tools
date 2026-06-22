.PHONY: help compact ingest-all package-dbs deploy-dbs mcp

# Latest GitHub release tag; override with `make deploy-dbs TAG=vX.Y.Z`.
TAG ?= $(shell gh release view --json tagName -q .tagName)

help:
	@echo "Available commands:"
	@echo "  make ingest-all    - Additively (re)build data/repo_lancedb from all configured repos"
	@echo "  make compact       - Compact/finalize the LanceDB store (collapse version history)"
	@echo "  make package-dbs   - Zip data/*_lancedb into factorio_lancedb.zip"
	@echo "  make deploy-dbs    - Compact, package, and upload the final build to the latest release"
	@echo "  make mcp           - Start the MCP server"

compact:
	uv run --no-sync python src/factorio_ai_tools/ingest/compact_db.py

ingest-all:
	uv run --no-sync python src/factorio_ai_tools/ingest/ingest_github_repo.py --repo-url https://github.com/clusterio/clusterio-docker.git
	uv run --no-sync python src/factorio_ai_tools/ingest/ingest_github_repo.py --repo-url https://github.com/wube/factorio-data.git
	uv run --no-sync python src/factorio_ai_tools/ingest/ingest_github_repo.py --repo-url https://github.com/redruin1/factorio-draftsman.git
	uv run --no-sync python src/factorio_ai_tools/ingest/ingest_github_repo.py --repo-url https://github.com/Teoxoy/factorio-blueprint-editor.git

# Bundle the canonical repo_lancedb store into factorio_lancedb.zip. The asset
# name is load-bearing: server.py downloads exactly this from the latest release,
# and extracts it so data/repo_lancedb appears. (Legacy data/*_lancedb stores from
# the old architecture are deliberately excluded.)
package-dbs:
	uv run --no-sync python -c "import shutil; shutil.make_archive('factorio_lancedb', 'zip', root_dir='data', base_dir='repo_lancedb')"

# Manual release of the final full build: finalize (compact), package, and
# upload to the latest GitHub release (data/ is gitignored, so this is local).
deploy-dbs: compact package-dbs
	gh release upload $(TAG) factorio_lancedb.zip --clobber

mcp:
	.\start_mcp_server.bat
