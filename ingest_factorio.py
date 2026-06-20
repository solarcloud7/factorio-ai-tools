import requests
import json
import os
import hashlib
import lancedb
import torch
from sentence_transformers import SentenceTransformer
from lancedb.pydantic import LanceModel, Vector

device = "cuda" if torch.cuda.is_available() else "cpu"
model_name = os.getenv("EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5")
print(f"Initializing SentenceTransformer {model_name} on {device}...")
model = SentenceTransformer(model_name, device=device)

class FactorioDoc(LanceModel):
    text: str
    vector: Vector(768)
    node_type: str
    class_name: str
    returns: str
    version: str
    url: str
    source_url: str
    content_hash: str

def get_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

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
            elif ct == 'tuple':
                opts = [format_type(o) for o in t.get('values', [])]
                return f"tuple[{', '.join(opts)}]"
            elif ct == 'struct':
                return "struct"
        return json.dumps(t)
    return str(t)

def parse_runtime_api(data, version_name, source_url, content_hash):
    chunks = []
    
    for cls in data.get('classes', []):
        class_name = cls['name']
        cls_desc = cls.get('description', '')
        chunks.append({
            "text": f"# Class: {class_name}\n\n{cls_desc}",
            "node_type": "class",
            "class_name": class_name,
            "returns": "",
            "version": version_name,
            "url": f"https://lua-api.factorio.com/{version_name}/classes/{class_name}.html",
            "source_url": source_url,
            "content_hash": content_hash
        })
        
        for method in cls.get('methods', []):
            method_name = method['name']
            m_desc = method.get('description', '')
            params = []
            for p in method.get('parameters', []):
                p_type = format_type(p.get('type', 'unknown'))
                params.append(f"- `{p['name']}` ({p_type}): {p.get('description', '')}")
            param_str = "\n".join(params) if params else "None"
            ret_types = [format_type(r.get('type', 'unknown')) for r in method.get('return_values', [])]
            ret_str = ", ".join(ret_types) if ret_types else "None"
            
            text = f"## Method: {class_name}.{method_name}\n\n{m_desc}\n\n### Parameters\n{param_str}\n\n### Returns\n{ret_str}"
            chunks.append({
                "text": text,
                "node_type": "method",
                "class_name": class_name,
                "returns": ret_str,
                "version": version_name,
                "url": f"https://lua-api.factorio.com/{version_name}/classes/{class_name}.html#method_{method_name}",
                "source_url": source_url,
                "content_hash": content_hash
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
                "returns": a_type,
                "version": version_name,
                "url": f"https://lua-api.factorio.com/{version_name}/classes/{class_name}.html#{attr_name}",
                "source_url": source_url,
                "content_hash": content_hash
            })

    for event in data.get('events', []):
        event_name = event['name']
        e_desc = event.get('description', '')
        edata = [f"- `{d['name']}` ({format_type(d.get('type', 'unknown'))}): {d.get('description', '')}" for d in event.get('data', [])]
        text = f"# Event: {event_name}\n\n{e_desc}\n\n### Data\n" + ("\n".join(edata) if edata else "None")
        chunks.append({
            "text": text,
            "node_type": "event",
            "class_name": "",
            "returns": "",
            "version": version_name,
            "url": f"https://lua-api.factorio.com/{version_name}/events.html#{event_name}",
            "source_url": source_url,
            "content_hash": content_hash
        })
        
    for concept in data.get('concepts', []):
        concept_name = concept['name']
        c_desc = concept.get('description', '')
        c_type = format_type(concept.get('type', 'unknown'))
        text = f"# Concept: {concept_name}\n\n{c_desc}\n\n### Type\n{c_type}"
        chunks.append({
            "text": text,
            "node_type": "concept",
            "class_name": "",
            "returns": c_type,
            "version": version_name,
            "url": f"https://lua-api.factorio.com/{version_name}/concepts/{concept_name}.html",
            "source_url": source_url,
            "content_hash": content_hash
        })
        
    return chunks

def parse_prototype_api(data, version_name, source_url, content_hash):
    chunks = []
    
    for proto in data.get('prototypes', []):
        proto_name = proto['name']
        p_desc = proto.get('description', '')
        chunks.append({
            "text": f"# Prototype: {proto_name}\n\n{p_desc}",
            "node_type": "prototype",
            "class_name": proto_name,
            "returns": "",
            "version": version_name,
            "url": f"https://lua-api.factorio.com/{version_name}/prototypes/{proto_name}.html",
            "source_url": source_url,
            "content_hash": content_hash
        })
        
        for prop in proto.get('properties', []):
            prop_name = prop['name']
            p_desc = prop.get('description', '')
            p_type = format_type(prop.get('type', 'unknown'))
            text = f"## Prototype Property: {proto_name}.{prop_name} ({p_type})\n\n{p_desc}"
            chunks.append({
                "text": text,
                "node_type": "prototype_property",
                "class_name": proto_name,
                "returns": p_type,
                "version": version_name,
                "url": f"https://lua-api.factorio.com/{version_name}/prototypes/{proto_name}.html#{prop_name}",
                "source_url": source_url,
                "content_hash": content_hash
            })
            
    for t in data.get('types', []):
        t_name = t['name']
        t_desc = t.get('description', '')
        t_type = format_type(t.get('type', 'unknown'))
        text = f"# Prototype Type: {t_name}\n\n{t_desc}\n\n### Type\n{t_type}"
        chunks.append({
            "text": text,
            "node_type": "prototype_type",
            "class_name": "",
            "returns": t_type,
            "version": version_name,
            "url": f"https://lua-api.factorio.com/{version_name}/types/{t_name}.html",
            "source_url": source_url,
            "content_hash": content_hash
        })
        
    return chunks

def main():
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "factorio_lancedb")
    os.makedirs(db_path, exist_ok=True)
    db = lancedb.connect(db_path)
    
    if "docs" in db.table_names():
        table = db.open_table("docs")
        if "content_hash" not in table.schema.names:
            print("Dropping existing docs table to migrate to new schema...")
            del table
            db.drop_table("docs")
            table = db.create_table("docs", schema=FactorioDoc)
    else:
        table = db.create_table("docs", schema=FactorioDoc)
        
    table = db.open_table("docs")

    versions_to_scrape = ["1.1.110", "latest"]
    all_chunks = []
    
    for ver in versions_to_scrape:
        print(f"\n--- Scraping version: {ver} ---")
        rt_url = f"https://lua-api.factorio.com/{ver}/runtime-api.json"
        pt_url = f"https://lua-api.factorio.com/{ver}/prototype-api.json"
        
        for url, parse_func in [(rt_url, parse_runtime_api), (pt_url, parse_prototype_api)]:
            resp = requests.get(url)
            if resp.status_code != 200:
                print(f"Failed to fetch {url}")
                continue
                
            chash = get_hash(resp.text)
            
            if len(table) > 0:
                existing = table.search().where(f"source_url = '{url}'").limit(1).to_list()
                if existing and existing[0].get('content_hash') == chash:
                    print(f"Skipping {url}, content unchanged.")
                    continue
                table.delete(f"source_url = '{url}'")
                
            all_chunks.extend(parse_func(resp.json(), ver, url, chash))
        
    print(f"\nExtracted {len(all_chunks)} new chunks total.")
    
    if len(all_chunks) == 0:
        print("Nothing new to ingest.")
        return
    
    batch_size = 100
    for i in range(0, len(all_chunks), batch_size):
        print(f"Ingesting batch {i} to {i+batch_size}...")
        batch = all_chunks[i:i+batch_size]
        texts = [c["text"] for c in batch]
        
        embeddings = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)
        
        for j, item in enumerate(batch):
            item["vector"] = embeddings[j].tolist()
            
        table.add(batch)
        
    print("Creating FTS index for hybrid search...")
    table.create_fts_index("text")
    
    # Save version info
    with open(os.path.join(db_path, "version.txt"), "w") as f:
        f.write(",".join(versions_to_scrape))
        
    print("Ingestion complete!")

if __name__ == '__main__':
    main()
