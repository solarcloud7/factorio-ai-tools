"""Analyze chunk health across all five LanceDB stores."""
import statistics
from pathlib import Path
from factorio_ai_tools.ingest import common

def analyze_store(store_name, table_name, text_column, description=""):
    """Analyze a single store and return a stats dict."""
    print(f"\n{'='*70}")
    print(f"Analyzing: {store_name} (table: {table_name}, text col: {text_column})")
    if description:
        print(f"  {description}")
    print('='*70)
    
    try:
        db, store_path = common.connect_store(store_name)
        table = db.open_table(table_name)
        
        print(f"  Store path: {store_path}")
        
        # Get row count
        actual_count = len(table)
        print(f"  Row count: {actual_count}")
        print(f"  Fetching all rows...")
        
        # Fetch all rows using search().to_list()
        all_rows = table.search().to_list()
        
        if not all_rows:
            print(f"  No rows found!")
            return None
        
        print(f"  Rows fetched: {len(all_rows)}")
        
        # Analyze text sizes
        sizes = []
        oversized_rows = []
        tiny_rows = []
        
        for idx, row in enumerate(all_rows):
            text = row.get(text_column, "")
            if text is None:
                text = ""
            text_str = str(text)
            size = len(text_str)
            sizes.append(size)
            
            if size > 2048:
                oversized_rows.append((idx, size))
            
            if len(text_str.strip()) < 10:
                tiny_rows.append((idx, size))
        
        # Calculate percentiles
        min_size = min(sizes) if sizes else 0
        max_size = max(sizes) if sizes else 0
        median_size = int(statistics.median(sizes)) if sizes else 0
        
        # p95
        if sizes:
            sorted_sizes = sorted(sizes)
            p95_idx = int(len(sorted_sizes) * 0.95)
            p95_size = sorted_sizes[p95_idx]
        else:
            p95_size = 0
        
        oversized_count = len(oversized_rows)
        oversized_pct = (oversized_count / actual_count * 100) if actual_count > 0 else 0
        
        tiny_count = len(tiny_rows)
        tiny_pct = (tiny_count / actual_count * 100) if actual_count > 0 else 0
        
        # File path analysis (for stores with file_path column)
        file_explosion = None
        if "file_path" in table.schema.names:
            file_counts = {}
            for row in all_rows:
                fp = row.get("file_path")
                if fp:
                    file_counts[fp] = file_counts.get(fp, 0) + 1
            
            # Top 5 files by chunk count
            top_files = sorted(file_counts.items(), key=lambda x: -x[1])[:5]
            
            # Count files with > 400 chunks
            explosion_count = sum(1 for count in file_counts.values() if count > 400)
            
            file_explosion = {
                "top_5": top_files,
                "files_over_400": explosion_count,
                "total_unique_files": len(file_counts)
            }
        
        # Node type analysis (for stores with node_type column)
        node_type_counts = None
        if "node_type" in table.schema.names:
            node_type_counts = {}
            for row in all_rows:
                nt = row.get("node_type")
                if nt:
                    node_type_counts[nt] = node_type_counts.get(nt, 0) + 1
        
        # Print summary
        print(f"\n  SIZE ANALYSIS:")
        print(f"    Row count: {actual_count}")
        print(f"    Char sizes - min: {min_size}, median: {median_size}, p95: {p95_size}, max: {max_size}")
        print(f"    Oversized (>2048 chars): {oversized_count} ({oversized_pct:.1f}%)")
        print(f"    Tiny (<10 chars): {tiny_count} ({tiny_pct:.1f}%)")
        
        if file_explosion:
            print(f"\n  FILE ANALYSIS:")
            print(f"    Total unique files: {file_explosion['total_unique_files']}")
            print(f"    Top 5 files by chunk count:")
            for fp, cnt in file_explosion['top_5']:
                print(f"      {fp}: {cnt} chunks")
            print(f"    Files with >400 chunks: {file_explosion['files_over_400']}")
        
        if node_type_counts:
            print(f"\n  NODE TYPE DISTRIBUTION:")
            sorted_types = sorted(node_type_counts.items(), key=lambda x: -x[1])
            for nt, cnt in sorted_types:
                pct = cnt / actual_count * 100
                print(f"    {nt}: {cnt} ({pct:.1f}%)")
        
        result = {
            "store_name": store_name,
            "table_name": table_name,
            "text_column": text_column,
            "row_count": actual_count,
            "sizes": {
                "min": min_size,
                "median": median_size,
                "p95": p95_size,
                "max": max_size,
            },
            "oversized": {
                "count": oversized_count,
                "percentage": round(oversized_pct, 1)
            },
            "tiny": {
                "count": tiny_count,
                "percentage": round(tiny_pct, 1)
            },
            "file_analysis": file_explosion,
            "node_type_distribution": node_type_counts,
        }
        
        return result
    
    except Exception as e:
        print(f"  ERROR: {e}")
        import traceback
        traceback.print_exc()
        return None


def main():
    print("CHUNK HEALTH ANALYSIS - ALL FIVE STORES")
    print("="*70)
    print(f"EMBEDDING MODEL: BAAI/bge-base-en-v1.5 (512 tokens = ~2048 chars)")
    print()
    
    stores_config = [
        ("factorio_lancedb", "docs", "text", "Factorio Lua API docs - embeds 'text' column"),
        ("wiki_lancedb", "docs", "text", "Factorio wiki pages - embeds 'text' column"),
        ("clusterio_lancedb", "codebase", "content", "Clusterio TypeScript repo - embeds 'content' column"),
        ("forum_lancedb", "forum", "content", "Factorio forum topics - embeds 'content' column"),
        ("repo_lancedb", "codebase", "content", "Generic GitHub repos - embeds contextualized string (~50 char prefix)"),
    ]
    
    results = []
    for store_name, table_name, text_column, description in stores_config:
        result = analyze_store(store_name, table_name, text_column, description)
        if result:
            results.append(result)
    
    # Generate summary table
    print("\n" + "="*70)
    print("SUMMARY TABLE")
    print("="*70)
    
    print(f"\n{'Store':<25} {'Rows':<8} {'Min':<6} {'Med':<6} {'P95':<7} {'Max':<7} {'Oversized':<12} {'Tiny':<10}")
    print("-" * 95)
    
    for result in results:
        store = result['store_name'].replace('_lancedb', '')
        rows = result['row_count']
        sizes = result['sizes']
        oversized = result['oversized']
        tiny = result['tiny']
        
        oversized_str = f"{oversized['count']} ({oversized['percentage']}%)"
        tiny_str = f"{tiny['count']} ({tiny['percentage']}%)"
        
        print(f"{store:<25} {rows:<8} {sizes['min']:<6} {sizes['median']:<6} {sizes['p95']:<7} {sizes['max']:<7} {oversized_str:<12} {tiny_str:<10}")
    
    # Special analysis for repo_lancedb with contextualized string
    print("\n" + "="*70)
    print("REPO_LANCEDB SPECIAL ANALYSIS: CONTEXTUALIZED STRING SIZE")
    print("="*70)
    print("Note: repo embeds a contextualized string with ~50 char prefix (File: ..., Component: ..., Type: ...)")
    
    for result in results:
        if result['store_name'] == 'repo_lancedb':
            # Approximate contextualized size by adding ~50 chars to each content
            original_sizes = result['sizes']
            prefix_size = 50
            contextualized_sizes = {
                'min': original_sizes['min'] + prefix_size,
                'median': original_sizes['median'] + prefix_size,
                'p95': original_sizes['p95'] + prefix_size,
                'max': original_sizes['max'] + prefix_size,
            }
            
            # Recount oversized with contextualized threshold
            db, _ = common.connect_store('repo_lancedb')
            table = db.open_table('codebase')
            rows = table.search().to_list()
            
            contextualized_oversized = 0
            for row in rows:
                content = row.get('content', '')
                if content:
                    contextualized_size = len(str(content)) + prefix_size
                    if contextualized_size > 2048:
                        contextualized_oversized += 1
            
            pct = (contextualized_oversized / len(rows) * 100) if rows else 0
            
            print(f"\nOriginal (content only):")
            print(f"  Oversized: {result['oversized']['count']} ({result['oversized']['percentage']}%)")
            print(f"\nWith contextualized prefix (~50 chars):")
            print(f"  Char sizes - min: {contextualized_sizes['min']}, median: {contextualized_sizes['median']}, p95: {contextualized_sizes['p95']}, max: {contextualized_sizes['max']}")
            print(f"  Oversized: {contextualized_oversized} ({pct:.1f}%)")


if __name__ == "__main__":
    main()
