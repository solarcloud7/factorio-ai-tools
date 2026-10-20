# Plan: Structured Factorio Prototype Data Store (`prototypes_lancedb`)

## Context

The existing RAG stores answer "how do I use this API?" but not "what does this prototype
look like?" The Factorio wiki buries recipe ingredients and machine stats inside unstructured
prose chunks. `wube/factorio-data` is already in `repo_lancedb` as generic AST/text chunks,
useful for code navigation but not for structured queries like "all assemblers and their
crafting speeds."

A sixth store (`prototypes_lancedb`) adds one record per prototype — structured text enabling
both semantic search ("iron production recipes") and filtered search by `prototype_type`.

---

## What already exists (no changes needed)

- `luaparser>=3.1.0` is **already in `pyproject.toml`** (resolved to `4.0.1` in `uv.lock`).
  No dep change required.
- `wube/factorio-data` can be cloned fresh by the new `ingest-prototypes` Makefile target
  (separate from the `ingest-repos` clone into `.mod_temp/`), or reused if already present.

---

## Data flow

```
factorio-data/base/prototypes/**/*.lua
        │
        ▼
  luaparser.ast.parse()          ← walk Invoke nodes for data:extend({...})
        │
        ▼
  _lua_table_to_python()         ← Table → dict (String/Number/Bool/Table)
        │
        ▼
  format_*() per type            ← dict → human-readable structured text
        │
        ▼
  PrototypeRecord (LanceDB)      ← prototype_type, prototype_name, category, content, version
        │
        ▼
  common.embed() → vector        ← BAAI/bge-base-en-v1.5, 768-dim, L2-normalized
        │
        ▼
  data/prototypes_lancedb        ← table "prototypes", FTS index on "content"
        │
        ▼
  server.search_factorio_prototypes()
```

---

## Files to create / modify

### 1. NEW: `src/factorio_ai_tools/ingest/ingest_prototypes.py`

**Schema** (LanceModel, consistent with `ingest_clusterio.py` pattern):
```python
class PrototypeRecord(LanceModel):
    prototype_type: str    # "recipe", "item", "assembling-machine", ...
    prototype_name: str    # "iron-plate", "electronic-circuit", ...
    category: str          # recipe category / entity subgroup; "" if N/A
    content: str           # embedded + FTS target (human-readable summary)
    version: str           # from base/info.json .version
    content_hash: str      # SHA-256(content) for incremental dedup
    vector: Vector(common.EMBEDDING_DIM)
```

**Repo path**: env `FACTORIO_DATA_REPO`, default `./factorio-data`. The Makefile clones it
with `git clone --depth 1` if not already present (same one-liner pattern as `ingest-clusterio`).

**Prototype discovery**: `Path(repo_path, "base", "prototypes").rglob("*.lua")`. Filter by
each entry's `.type` field — no hardcoded filenames, handles both flat and sub-dir layouts.

**Lua parser core**:

```python
def extract_data_extend_calls(src: str) -> list[dict]:
    """Walk AST for data:extend({...}) Invoke nodes; convert each entry to dict."""
    try:
        tree = luaparser.ast.parse(src)
    except Exception as e:
        safe_print(f"luaparser error: {e}")
        return _regex_fallback_extract(src)

    results = []
    for node in luaparser.ast.walk(tree):
        if not isinstance(node, luaparser.astnodes.Invoke):
            continue
        # Match `data:extend(...)` — source=Name('data'), func='extend'
        # Verify exact attribute names at impl time: print(dir(node)) in REPL
        if not (_name(node.source) == "data" and _method_name(node.func) == "extend"):
            continue
        if not node.args:
            continue
        arg = node.args[0]
        if not isinstance(arg, luaparser.astnodes.Table):
            continue
        for field in arg.fields:
            entry = _lua_table_to_python(field.value)
            if isinstance(entry, dict):
                results.append(entry)
    return results
```

> **Important**: `luaparser` 4.0.1 (June 2026) may have changed attribute names vs 3.x.
> At implementation time, verify with:
> ```python
> from luaparser import ast, astnodes
> t = ast.parse('data:extend({{type="recipe",name="x"}})')
> for n in ast.walk(t): print(type(n).__name__, dir(n))
> ```
> Attributes to confirm: `.source`/`.func` for `Invoke`; `.args` or `.arguments` for
> Call/Invoke; `.fields` for `Table`; `.key`/`.value` for `Field`; `.s`/`.id` for String/Name;
> `.n` for Number.

**`_lua_table_to_python(node)`**: recursively converts:
- `String` → `str` (via `.s`)
- `Number` → `float` (via `.n`)  
- `TrueExpr`/`FalseExpr` → `bool`
- `Table` → `dict` (named fields) or `list` (index fields)
- `Name` → `str` (via `.id`) for simple bare-name values like `true`/`false` symbols
- Anything else → `None` (concat exprs, function calls — filters parametric prototypes)

**Only include entries where both `type` and `name` are plain `str`** (filters out
`name = "mod-" .. setting_value` concat-expression entries).

**`_regex_fallback_extract(src)`**: simple re.findall for `type\s*=\s*"([^"]+)"` +
`name\s*=\s*"([^"]+)"` within each `{...}` block; emits a warning; returns minimal dicts.

**Content formatters** (return `str | None`; `None` means skip):

| Type family | Types matched | Key fields in content |
|-------------|---------------|-----------------------|
| `recipe` | `recipe` | category, ingredients+amounts, results+amounts, energy_required, enabled, allow_productivity |
| item subtypes | `item`, `ammo`, `capsule`, `gun`, `rail-planner`, `repair-tool`, `selection-tool`, `item-with-entity-data`, `module`, `tool`, `armor`, `mining-tool`, `spidertron-remote`, `space-platform-starter-pack` | subgroup, stack_size, fuel_value/fuel_category; for `module`: effect bonuses (speed/productivity/efficiency/quality), category, tier; for items with spoilage: spoil_ticks, spoil_result |
| `fluid` | `fluid` | default_temperature, max_temperature, heat_capacity, fuel_value (if any) |
| `technology` | `technology` | prerequisites, research cost (unit.count × ingredients OR research_trigger), unlocked recipes from effects |
| entity (allowlist) | see below | crafting_speed, crafting_categories, energy_usage, energy_source.type, max_health, module_specification |
| `quality` | `quality` | level, crafting speed bonus (multiplier field), science_pack_drain_multiplier, beacon_power_usage_multiplier |
| planet/space | `planet`, `space-location` | distance, surface_properties (temperature/pressure/gravity via surface-property keys), asteroid_spawn_definitions resource types |
| `asteroid-chunk` | `asteroid-chunk` | mining_results (item yields) |
| `surface-property` | `surface-property` | default_value, min/max range |
| `plant` | `plant` | growth_ticks, harvest_emissions, seed item, spoil_ticks |

**Entity allowlist** (`WANTED_ENTITY_TYPES`) — vanilla + Space Age:
`assembling-machine`, `furnace`, `rocket-silo`, `mining-drill`, `lab`, `boiler`,
`generator`, `reactor`, `beacon`, `inserter`, `transport-belt`, `splitter`,
`underground-belt`, `storage-tank`, `pipe`, `pipe-to-ground`, `train-stop`,
`locomotive`, `cargo-wagon`, `fluid-wagon`, `electric-pole`, `offshore-pump`,
`accumulator`, `solar-panel`, `electric-turret`, `fluid-turret`, `ammo-turret`, `pump`,
`cargo-landing-pad`, `space-platform-hub`, `asteroid-collector`,
`agricultural-tower`, `thruster`, `lightning-attractor`,
`fusion-reactor`, `fusion-generator`, `cargo-bay`.

**Space Age types handled directly** (no entity allowlist — all instances included):
`quality`, `planet`, `space-location`, `asteroid-chunk`, `surface-property`, `plant`.

**Prototype discovery scope**: scan `base/prototypes/` AND `space-age/prototypes/` (if
present in the checkout) to capture Space Age data. Pattern:
```python
search_roots = [Path(repo_path, "base", "prototypes")]
sa = Path(repo_path, "space-age", "prototypes")
if sa.exists():
    search_roots.append(sa)
for root in search_roots:
    for lua_file in root.rglob("*.lua"):
        ...
```

All other types: silently skip (no record, no warning).

**Incremental logic** (keyed on `(prototype_type, prototype_name)`):
```python
# Load existing hashes in one scan
existing = {
    (r["prototype_type"], r["prototype_name"]): r["content_hash"]
    for r in table.search().limit(100_000).to_list()
}
# Per entry: skip if hash matches, else delete-then-re-add
safe_type = pt.replace("'", "''")
safe_name = pn.replace("'", "''")
table.delete(f"prototype_type = '{safe_type}' AND prototype_name = '{safe_name}'")
# After all entries: delete orphans not in current_keys
```

**FTS index**: `table.create_fts_index("content", replace=True)` after batch insert,
wrapped in try/except (same as all other ingesters).

**Version file**: `data/prototypes_lancedb/version.txt` from `base/info.json["version"]`.

**Schema migration**: `common.ensure_table(db, "prototypes", PrototypeRecord)`.

**Chunk health**: use `common.ChunkAuditor("prototypes_lancedb")`. Call `auditor.add(content,
source=prototype_name, node_type=prototype_type)` per record. `explosion_per_source=None`
(each prototype is one record, not many chunks — the explosion check is irrelevant here).
Call `auditor.summary()` at the end.

---

### 2. `src/factorio_ai_tools/server.py`

**Line 70 — `ALL_STORES`**:
```python
ALL_STORES = ["factorio_lancedb", "clusterio_lancedb", "wiki_lancedb", "forum_lancedb",
              "repo_lancedb", "prototypes_lancedb"]
```

**After line 452 — open prototypes store** (follow the repo_lancedb one-try/except pattern):
```python
db_path_prototypes = os.path.join(DATA_DIR, "prototypes_lancedb")
try:
    db_prototypes = lancedb.connect(db_path_prototypes)
    table_prototypes = db_prototypes.open_table("prototypes")
except Exception as e:
    print(f"Warning: Could not open prototypes table. Run ingest_prototypes.py first. Error: {e}",
          file=sys.stderr)
    table_prototypes = None
```

**New tool** (use `@optional_tool()` like all other tools):
```python
@optional_tool()
def search_factorio_prototypes(queries: list[str], prototype_type: str = None, limit: int = 5) -> str:
    """
    Search Factorio prototype definitions for exact numerical data: recipe ingredients and
    crafting times, assembler speeds and energy usage, technology research costs, item stack
    sizes, quality tier bonuses, and Space Age planet/surface conditions.

    Use this tool when you need PRECISE VALUES from the game data files — not explanations
    of how things work (use search_factorio_wiki for that) and not modding API syntax
    (use search_factorio_docs for that). Example use cases:
    - "What are the ingredients and crafting time for electronic-circuit?"
    - "Which assembling machines can craft recipes in category 'crafting'?"
    - "What does the legendary quality tier do to crafting speed?"
    - "What surface conditions does Vulcanus have?"

    For complete answers, combine with search_factorio_wiki (for mechanics and ratios) or
    search_factorio_docs (for the modding API to read/write these values at runtime).

    Args:
        queries: Semantic search queries.
        prototype_type: Optional filter — "recipe", "item", "technology",
                        "assembling-machine", "furnace", "quality", "planet", etc.
        limit: Max results per query (1–20, default 5).
    """
    if table_prototypes is None:
        return "Error: Prototypes table not found. Run ingest_prototypes.py first."
    ...
    where = f"prototype_type = '{prototype_type.replace(\"'\", \"''\")}'" if prototype_type else None
    results = hybrid_search(table_prototypes, queries[idx], query_vec, limit, where=where)
    # Return prototype_type, prototype_name, category, content per result
```

**Also update `factorio_clusterio_expert` prompt** (line ~168) — add one line guiding tool selection:
```
For exact numerical values (recipe ingredients, crafting speeds, research costs, quality
bonuses, planet surface conditions), use `search_factorio_prototypes`. For gameplay
mechanics and explanations, use `search_factorio_wiki`. Combine both when a complete answer
needs both values AND context (e.g. "what assemblers can I use for circuits" = structured
lookup of recipe category + entities with that category, plus wiki context on ratios).
```

**`get_mcp_version_info`** — add `prototypes_version` to the returned JSON:
```python
proto_v = "unknown"
proto_path = os.path.join(db_path_prototypes, "version.txt")
if os.path.exists(proto_path):
    with open(proto_path, "r", encoding="utf-8") as f:
        proto_v = f.read().strip()
# add "factorio_prototypes_version": proto_v to the returned dict
```

---

### 3. `Makefile`

**Line 9 — `STORES`**:
```makefile
STORES = factorio_lancedb clusterio_lancedb wiki_lancedb forum_lancedb repo_lancedb prototypes_lancedb
```

**New target** (after `ingest-repos`):
```makefile
ingest-prototypes:
	$(PY) -c "import os,subprocess as s; s.run(['git','clone','--depth','1','https://github.com/wube/factorio-data.git','factorio-data'],check=True) if not os.path.exists('factorio-data') else print('factorio-data checkout present')"
	$(PY) -m factorio_ai_tools.ingest.ingest_prototypes
```

**`ingest-all`** — append `ingest-prototypes`:
```makefile
ingest-all: ingest-factorio ingest-wiki ingest-forum ingest-clusterio ingest-repos ingest-prototypes
```

**`help` text** — "all 5 LanceDB stores" → "all 6 LanceDB stores".

---

### 4. `tests/test_server_contract.py`

Add to imports: `from factorio_ai_tools.ingest import ingest_prototypes`

Add to `CONTRACT`:
```python
"prototypes": (ingest_prototypes.PrototypeRecord,
               {"prototype_type", "prototype_name", "category", "content", "content_hash"}),
```

Add to the tool-name assertion:
```python
"search_factorio_prototypes",
```

---

### 5. NEW: `tests/test_ingest_prototypes.py`

Unit tests (no DB, no embedder — pure parsing logic):

```python
RECIPE_LUA = '''
data:extend({
  {
    type = "recipe",
    name = "electronic-circuit",
    category = "crafting",
    energy_required = 0.5,
    enabled = false,
    ingredients = {
      {type="item", name="iron-plate", amount=1},
      {type="item", name="copper-cable", amount=3},
    },
    results = {{type="item", name="electronic-circuit", amount=1}},
  }
})
'''

TECH_LUA = '''
data:extend({
  {
    type = "technology",
    name = "automation",
    prerequisites = {"electronics"},
    unit = {count=100, ingredients={{"automation-science-pack",1}}, time=10},
    effects = {{type="unlock-recipe", recipe="assembling-machine-1"}},
  }
})
'''

PARAMETRIC_LUA = '''
data:extend({
  { type = "item", name = "base-item-" .. n, stack_size = 1 }
})
'''

def test_recipe_parsed_correctly():
    entries = extract_data_extend_calls(RECIPE_LUA)
    assert len(entries) == 1
    e = entries[0]
    assert e["type"] == "recipe" and e["name"] == "electronic-circuit"
    assert isinstance(e["ingredients"], list)

def test_recipe_content_has_ingredients():
    # format_prototype() produces content with ingredient names and time
    content = format_prototype({"type":"recipe","name":"electronic-circuit",
                                "category":"crafting","energy_required":0.5,
                                "ingredients":[{"name":"iron-plate","amount":1},
                                               {"name":"copper-cable","amount":3}],
                                "results":[{"name":"electronic-circuit","amount":1}]})
    assert "iron-plate" in content and "copper-cable" in content
    assert "0.5" in content

def test_parametric_prototype_skipped():
    entries = extract_data_extend_calls(PARAMETRIC_LUA)
    # concat-expression name → filtered out (name is not a plain str)
    assert len(entries) == 0

def test_technology_parsed_correctly():
    entries = extract_data_extend_calls(TECH_LUA)
    assert entries and entries[0]["type"] == "technology"
    content = format_prototype(entries[0])
    assert "automation-science-pack" in content or "assembling-machine-1" in content

def test_ingredient_sub_tables_not_extracted():
    # The ingredient dicts inside a recipe must NOT appear as top-level prototypes
    entries = extract_data_extend_calls(RECIPE_LUA)
    # Only 1 entry (the recipe), not 3 (recipe + 2 ingredient dicts)
    assert len(entries) == 1

QUALITY_LUA = '''
data:extend({
  {
    type = "quality",
    name = "legendary",
    level = 4,
    color = {r=1, g=0.7, b=0},
    beacon_power_usage_multiplier = 0.5,
    science_pack_drain_multiplier = 1,
    multiplier_tooltip_filter = {"mining-drill", "crafting-machine"},
  }
})
'''

def test_quality_prototype_parsed_and_formatted():
    entries = extract_data_extend_calls(QUALITY_LUA)
    assert entries and entries[0]["type"] == "quality"
    content = format_prototype(entries[0])
    assert "legendary" in content and "level" in content.lower()

PLANET_LUA = '''
data:extend({
  {
    type = "planet",
    name = "vulcanus",
    distance = 2.0,
    orientation = 0.1,
    asteroid_spawn_definitions = {},
    surface_properties = {
      ["day-night-cycle"] = 0,
      pressure = 1000,
      temperature = 300,
    },
  }
})
'''

def test_planet_prototype_parsed_and_formatted():
    entries = extract_data_extend_calls(PLANET_LUA)
    assert entries and entries[0]["type"] == "planet"
    content = format_prototype(entries[0])
    assert "vulcanus" in content
```

Import `extract_data_extend_calls` and `format_prototype` directly from
`factorio_ai_tools.ingest.ingest_prototypes` (these are module-level functions, not
behind `main()`).

---

## Phase 2: Mod prototype support (not in this plan)

The repos ingested via `ingest-repos` (`maraxsis`, `draftsman`, etc.) contain their own
`data:extend()` calls. Phase 2 would extend `ingest_prototypes.py` to accept a
`--repo-path <dir> --mod-name <slug>` flag (mirroring `ingest_github_repo`'s `--local-path`),
scan that repo's `.lua` files, and store records with an additional `mod_name` column so
they're attributable and filterable alongside vanilla prototypes. The `search_factorio_prototypes`
tool would gain a `mod_name` filter. This is deferred — Phase 1 focuses on getting the vanilla
+ Space Age structured baseline right.

---

## Reused utilities from `common.py`

| Utility | Used for |
|---------|----------|
| `common.get_data_dir()` | store path resolution |
| `common.connect_store("prototypes_lancedb")` | LanceDB connection |
| `common.ensure_table(db, "prototypes", PrototypeRecord)` | schema migration guard |
| `common.load_embedder()` / `common.embed()` | 768-dim L2-normalized vectors |
| `common.get_hash(content)` | SHA-256 for content_hash |
| `common.safe_print()` | Windows console safety |
| `common.EMBEDDING_DIM` (768) | Vector dimension |
| `common.hybrid_search()` | server-side retrieval |
| `common.ChunkAuditor` | chunk health reporting |

---

## Implementation order

1. Write `ingest_prototypes.py` (schema → Lua parser → formatters → incremental main loop)
2. Update `server.py` (store connection + tool + version info)
3. Update `Makefile` (`STORES`, `ingest-prototypes`, `ingest-all`, help text)
4. Update `tests/test_server_contract.py` (import + CONTRACT + tool name)
5. Write `tests/test_ingest_prototypes.py`
6. `make test` — all tests pass

---

## Verification

**Offline (`make test`):**
- All existing tests still pass (no regression to existing stores)
- `test_server_contract.py::test_schemas_cover_server_reads` covers the new schema
- `test_server_contract.py::test_server_exposes_a_search_tool_per_store` covers the new tool
- New parser unit tests pass without network/model

**Live (after `make ingest-prototypes`):**
```
# Row count sanity
python -c "import lancedb; t=lancedb.connect('data/prototypes_lancedb').open_table('prototypes'); print(len(t))"
# Expect: > 500 rows

# Spot-check electronic-circuit recipe
python -c "
import lancedb
t = lancedb.connect('data/prototypes_lancedb').open_table('prototypes')
rows = t.search().where(\"prototype_name = 'electronic-circuit'\").limit(1).to_list()
print(rows[0]['content'])
"
# Expect: content mentions copper-cable, iron-plate, 0.5s

# Spot-check assembling-machine-1 entity
python -c "
import lancedb
t = lancedb.connect('data/prototypes_lancedb').open_table('prototypes')
rows = t.search().where(\"prototype_name = 'assembling-machine-1'\").limit(1).to_list()
print(rows[0]['content'])
"
# Expect: crafting_speed, energy usage

# Spot-check automation technology
python -c "
import lancedb
t = lancedb.connect('data/prototypes_lancedb').open_table('prototypes')
rows = t.search().where(\"prototype_name = 'automation'\").limit(1).to_list()
print(rows[0]['content'])
"
# Expect: assembling-machine-1 in unlocked recipes

# Spot-check quality tier
python -c "
import lancedb
t = lancedb.connect('data/prototypes_lancedb').open_table('prototypes')
rows = t.search().where(\"prototype_type = 'quality'\").limit(10).to_list()
print([r['prototype_name'] for r in rows])
"
# Expect: ['normal', 'uncommon', 'rare', 'epic', 'legendary'] (Space Age tiers)

# Spot-check planet
python -c "
import lancedb
t = lancedb.connect('data/prototypes_lancedb').open_table('prototypes')
rows = t.search().where(\"prototype_type = 'planet'\").limit(10).to_list()
for r in rows: print(r['prototype_name'], '|', r['content'][:80])
"
# Expect: nauvis, vulcanus, gleba, fulgora, aquilo (if space-age dir present)

# Recipe count by type
python -c "
import lancedb, collections
t = lancedb.connect('data/prototypes_lancedb').open_table('prototypes')
types = collections.Counter(r['prototype_type'] for r in t.search().limit(100000).to_list())
print(types.most_common(15))
"
```