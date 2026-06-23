"""Factorio AI Tools — a hybrid-search RAG MCP server for Factorio modding and
Clusterio plugin development.

Two halves that meet only at the on-disk LanceDB stores: the ``ingest`` package
builds the stores offline, and ``server`` queries them at runtime. See
``docs/architecture.md``.
"""
