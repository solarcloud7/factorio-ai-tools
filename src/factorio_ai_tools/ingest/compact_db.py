import lancedb
from datetime import timedelta
from factorio_ai_tools.ingest.ingest_github_repo import init_lancedb

def compact_database():
    print("Connecting to LanceDB...")
    db = init_lancedb()
    
    table_names = db.table_names() if hasattr(db, 'table_names') else db.list_tables()
    if not table_names:
        print("No tables found to compact.")
        return

    for table_name in table_names:
        print(f"Compacting table: {table_name}")
        table = db.open_table(table_name)
        
        print("  - Optimizing files...")
        table.optimize()
        
        print("  - Cleaning up old versions instantly...")
        table.cleanup_old_versions(older_than=timedelta(0))
        
    print("Database compaction complete!")

if __name__ == "__main__":
    compact_database()
