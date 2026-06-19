import os
import lancedb
import torch
from sentence_transformers import SentenceTransformer
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP server
mcp = FastMCP("Factorio AI Tools")

# Connect to LanceDB instances
db_path_factorio = os.path.join(os.path.dirname(os.path.abspath(__file__)), "factorio_lancedb")
db_path_clusterio = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clusterio_lancedb")

db_factorio = lancedb.connect(db_path_factorio)
db_clusterio = lancedb.connect(db_path_clusterio)

try:
    table_factorio = db_factorio.open_table("docs")
except Exception as e:
    print(f"Warning: Could not open Factorio docs table. Did you run ingest_factorio.py? Error: {e}")
    table_factorio = None

try:
    table_clusterio = db_clusterio.open_table("codebase")
except Exception as e:
    print(f"Warning: Could not open Clusterio codebase table. Did you run ingest_clusterio.py? Error: {e}")
    table_clusterio = None

# Initialize embedding model globally
device = "cuda" if torch.cuda.is_available() else "cpu"
model = SentenceTransformer("BAAI/bge-base-en-v1.5", device=device)

@mcp.tool()
def search_factorio_docs(query: str, class_filter: str = None) -> str:
    """
    Search Factorio API documentation.
    
    Args:
        query: The search query string.
        class_filter: Optional class name to filter by (e.g., 'LuaEntity').
    """
    if table_factorio is None:
        return "Error: Factorio database table not found. Please run ingest_factorio.py first."
        
    try:
        # Generate vector
        query_vec = model.encode(query, normalize_embeddings=True).tolist()
        
        # Build the base query
        q = table_factorio.search(query_vec)
        
        # Apply class filter if provided
        if class_filter:
            q = q.where(f"class_name = '{class_filter}'")
            
        # Limit to top 5 results
        results = q.limit(5).to_list()
        
        if len(results) == 0:
            return "No results found for your query."
            
        # Format the results
        formatted_chunks = []
        for row in results:
            formatted_chunks.append(row['text'])
            
        return "\n\n---\n\n".join(formatted_chunks)
        
    except Exception as e:
        return f"Error executing search: {str(e)}"

@mcp.tool()
def search_clusterio_code(query: str, node_type: str = None) -> str:
    """
    Search the Clusterio TypeScript codebase using semantic AST-chunked RAG.
    
    Args:
        query: The semantic search query (e.g., "how does teleporter sending work?").
        node_type: Optional AST node type filter ('class_declaration', 'function_declaration', 'method_definition', 'interface_declaration').
    """
    if table_clusterio is None:
        return "Error: Clusterio database table not found. Please run ingest_clusterio.py first."
        
    try:
        # Generate vector
        query_vec = model.encode(query, normalize_embeddings=True).tolist()
        
        # Build the base query
        q = table_clusterio.search(query_vec)
        
        # Apply filter if provided
        if node_type:
            q = q.where(f"node_type = '{node_type}'")
            
        # Limit to top 5 most relevant AST nodes
        results = q.limit(5).to_list()
        
        if len(results) == 0:
            return "No results found for your query."
            
        # Format the results
        formatted_chunks = []
        for i, row in enumerate(results):
            chunk = (
                f"### Result {i+1} - {row['node_name']} ({row['node_type']})\n"
                f"**File:** `{row['file_path']}`\n\n"
                f"```typescript\n"
                f"{row['content']}\n"
                f"```"
            )
            formatted_chunks.append(chunk)
            
        return "\n\n---\n\n".join(formatted_chunks)
        
    except Exception as e:
        return f"Error executing search: {str(e)}"

if __name__ == '__main__':
    # Run the server using stdio
    mcp.run()
