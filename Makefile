.PHONY: help compact ingest-all mcp

help:
	@echo "Available commands:"
	@echo "  make compact       - Compact and condense the LanceDB database"
	@echo "  make ingest-all    - Run the ingestion script for all configured repositories"
	@echo "  make mcp           - Start the MCP server"

compact:
	uv run --no-sync python src/factorio_ai_tools/ingest/compact_db.py

ingest-all:
	uv run --no-sync python src/factorio_ai_tools/ingest/ingest_github_repo.py --repo-url https://github.com/clusterio/clusterio-docker.git
	uv run --no-sync python src/factorio_ai_tools/ingest/ingest_github_repo.py --repo-url https://github.com/wube/factorio-data.git
	uv run --no-sync python src/factorio_ai_tools/ingest/ingest_github_repo.py --repo-url https://github.com/redruin1/factorio-draftsman.git
	uv run --no-sync python src/factorio_ai_tools/ingest/ingest_github_repo.py --repo-url https://github.com/Teoxoy/factorio-blueprint-editor.git

mcp:
	.\start_mcp_server.bat
