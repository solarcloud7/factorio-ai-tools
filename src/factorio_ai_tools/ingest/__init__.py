"""Ingestion scripts that build the LanceDB stores under ``data/``.

Each module ingests one source (factorio docs, wiki, forum, Clusterio, generic
GitHub repos); they all share ``common`` for the embedding / chunking / hashing
contract. See ``docs/architecture.md`` and ``docs/stores.md``.
"""
