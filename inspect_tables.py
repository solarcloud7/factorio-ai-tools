"""Inspect what tables exist in each LanceDB store."""
from factorio_ai_tools.ingest import common

stores = [
    "factorio_lancedb",
    "wiki_lancedb",
    "clusterio_lancedb",
    "forum_lancedb",
    "repo_lancedb",
]

for store_name in stores:
    try:
        db, store_path = common.connect_store(store_name)
        tables_result = db.list_tables()
        print(f"{store_name}: {tables_result}")
        
        # Extract table names from the result
        if hasattr(tables_result, '__iter__'):
            # Handle different return types
            for item in tables_result:
                if isinstance(item, str):
                    table_name = item
                elif isinstance(item, dict):
                    table_name = item.get('name')
                elif hasattr(item, 'name'):
                    table_name = item.name
                else:
                    table_name = str(item)
                
                print(f"  Opening table: {table_name}")
                table = db.open_table(table_name)
                print(f"    {table_name}: {len(table)} rows")
                print(f"    Schema columns: {table.schema.names}")
    except Exception as e:
        print(f"{store_name}: ERROR - {e}")
        import traceback
        traceback.print_exc()
