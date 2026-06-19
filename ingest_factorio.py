import requests
import json
import lancedb
import torch
from sentence_transformers import SentenceTransformer
from lancedb.pydantic import LanceModel, Vector

# Initialize embedding model manually to avoid Windows LanceDB CUDA deadlock
device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Initializing SentenceTransformer on {device}...")
model = SentenceTransformer("BAAI/bge-base-en-v1.5", device=device)

class FactorioDoc(LanceModel):
    text: str
    vector: Vector(768)
    node_type: str
    class_name: str
    returns: str

def format_type(t):
    if isinstance(t, str):
        return t
    elif isinstance(t, dict):
        if 'complex_type' in t:
            ct = t['complex_type']
            if ct == 'type':
                return format_type(t.get('value', ''))
            elif ct == 'array':
                return f"array[{format_type(t.get('value', ''))}]"
            elif ct == 'dictionary':
                return f"dict[{format_type(t.get('key', ''))} -> {format_type(t.get('value', ''))}]"
            elif ct == 'union':
                opts = [format_type(o) for o in t.get('options', [])]
                return " | ".join(opts)
            elif ct == 'LuaCustomTable':
                return f"LuaCustomTable[{format_type(t.get('key', ''))}, {format_type(t.get('value', ''))}]"
            elif ct == 'function':
                return "function"
            elif ct == 'literal':
                return str(t.get('value', ''))
        return json.dumps(t)
    return str(t)

def parse_api():
    url = "https://lua-api.factorio.com/latest/runtime-api.json"
    print(f"Downloading API doc from {url}...")
    resp = requests.get(url)
    resp.raise_for_status()
    data = resp.json()

    chunks = []
    
    # Process classes
    for cls in data.get('classes', []):
        class_name = cls['name']
        cls_desc = cls.get('description', '')
        
        chunks.append({
            "text": f"# Class: {class_name}\n\n{cls_desc}",
            "node_type": "class",
            "class_name": class_name,
            "returns": ""
        })
        
        for method in cls.get('methods', []):
            method_name = method['name']
            m_desc = method.get('description', '')
            
            params = []
            for p in method.get('parameters', []):
                p_type = format_type(p.get('type', 'unknown'))
                params.append(f"- `{p['name']}` ({p_type}): {p.get('description', '')}")
            param_str = "\n".join(params) if params else "None"
            
            ret_types = []
            for r in method.get('return_values', []):
                ret_types.append(format_type(r.get('type', 'unknown')))
            ret_str = ", ".join(ret_types) if ret_types else "None"
            
            text = f"## Method: {class_name}.{method_name}\n\n{m_desc}\n\n### Parameters\n{param_str}\n\n### Returns\n{ret_str}"
            chunks.append({
                "text": text,
                "node_type": "method",
                "class_name": class_name,
                "returns": ret_str
            })
            
        for attr in cls.get('attributes', []):
            attr_name = attr['name']
            a_desc = attr.get('description', '')
            a_type = format_type(attr.get('type', 'unknown'))
            
            text = f"## Attribute: {class_name}.{attr_name} ({a_type})\n\n{a_desc}"
            chunks.append({
                "text": text,
                "node_type": "attribute",
                "class_name": class_name,
                "returns": a_type
            })

    # Process events
    for event in data.get('events', []):
        event_name = event['name']
        e_desc = event.get('description', '')
        
        edata = []
        for d in event.get('data', []):
            d_type = format_type(d.get('type', 'unknown'))
            edata.append(f"- `{d['name']}` ({d_type}): {d.get('description', '')}")
        edata_str = "\n".join(edata) if edata else "None"
        
        text = f"# Event: {event_name}\n\n{e_desc}\n\n### Data\n{edata_str}"
        chunks.append({
            "text": text,
            "node_type": "event",
            "class_name": "",
            "returns": ""
        })
        
    # Process concepts
    for concept in data.get('concepts', []):
        concept_name = concept['name']
        c_desc = concept.get('description', '')
        c_type = format_type(concept.get('type', 'unknown'))
        
        text = f"# Concept: {concept_name}\n\n{c_desc}\n\n### Type\n{c_type}"
        chunks.append({
            "text": text,
            "node_type": "concept",
            "class_name": "",
            "returns": c_type
        })
        
    return chunks

def main():
    chunks = parse_api()
    print(f"Extracted {len(chunks)} chunks.")
    
    import os
    print("Connecting to LanceDB...")
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "factorio_lancedb")
    db = lancedb.connect(db_path)
    
    if "docs" in db.list_tables():
        db.drop_table("docs")
        
    print("Creating table and generating embeddings (this may take a while)...")
    table = db.create_table("docs", schema=FactorioDoc)
    
    batch_size = 500
    for i in range(0, len(chunks), batch_size):
        print(f"Ingesting batch {i} to {i+batch_size}...")
        batch = chunks[i:i+batch_size]
        texts = [c["text"] for c in batch]
        
        # Manually generate embeddings on the main thread
        embeddings = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)
        
        for j, item in enumerate(batch):
            item["vector"] = embeddings[j].tolist()
            
        table.add(batch)
        
    print("Creating FTS index for hybrid search...")
    table.create_fts_index("text")
    
    print("Ingestion complete!")

if __name__ == '__main__':
    main()
