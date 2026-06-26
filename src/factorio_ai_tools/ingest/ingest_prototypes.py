"""Ingest Factorio prototype definitions into ``data/prototypes_lancedb``.

Parses ``data:extend(...)`` calls from Lua files in the cloned ``factorio-data``
repo (FACTORIO_DATA_REPO env, default ``<repo_root>/factorio-data``) and stores
one structured text record per prototype. Covers base + Space Age prototypes.
Incremental by (prototype_type, prototype_name) SHA-256. Writes version.txt.
"""

import json
import os
from pathlib import Path

from lancedb.pydantic import LanceModel, Vector
from luaparser import ast as lua_ast
from luaparser import astnodes

from factorio_ai_tools.ingest import common

WANTED_ENTITY_TYPES = frozenset({
    "assembling-machine", "furnace", "rocket-silo", "mining-drill", "lab",
    "boiler", "generator", "reactor", "beacon", "inserter", "transport-belt",
    "splitter", "underground-belt", "storage-tank", "pipe", "pipe-to-ground",
    "train-stop", "locomotive", "cargo-wagon", "fluid-wagon", "electric-pole",
    "offshore-pump", "accumulator", "solar-panel", "electric-turret",
    "fluid-turret", "ammo-turret", "pump", "cargo-landing-pad",
    "space-platform-hub", "asteroid-collector", "agricultural-tower",
    "thruster", "lightning-attractor", "fusion-reactor", "fusion-generator",
    "cargo-bay",
})

SPACE_AGE_TYPES = frozenset({
    "quality", "planet", "space-location", "asteroid-chunk",
    "surface-property", "plant",
})

ITEM_TYPES = frozenset({
    "item", "ammo", "capsule", "gun", "rail-planner", "repair-tool",
    "selection-tool", "item-with-entity-data", "module", "tool", "armor",
    "mining-tool", "spidertron-remote", "space-platform-starter-pack",
})


class PrototypeRecord(LanceModel):
    prototype_type: str
    prototype_name: str
    category: str
    content: str
    version: str
    content_hash: str
    vector: Vector(common.EMBEDDING_DIM)


# --- Lua parsing helpers -------------------------------------------------------

def _get_key(node):
    """String key from a Name or String field-key node; None otherwise."""
    if isinstance(node, astnodes.Name):
        v = node.id
        return v.decode() if isinstance(v, bytes) else str(v)
    if isinstance(node, astnodes.String):
        v = node.s
        return v.decode() if isinstance(v, bytes) else str(v)
    return None


def _lua_table_to_python(node):
    """Recursively convert a luaparser AST node to a Python primitive/dict/list.

    Returns None for un-evaluable expressions (Concat, FunctionCall, etc.) so
    callers can filter parametric entries (name = 'base-' .. n) safely.
    """
    if isinstance(node, astnodes.String):
        v = node.s
        return v.decode() if isinstance(v, bytes) else str(v)
    if isinstance(node, astnodes.Number):
        return node.n
    if isinstance(node, astnodes.UMinusOp):
        # Negative literals parse as unary-minus over a Number (e.g. -0.3, -273),
        # NOT as a negative Number. Without this, every negative value (module
        # consumption bonuses, sub-zero temperatures, planet distances) drops to None.
        operand = _lua_table_to_python(node.operand)
        return -operand if isinstance(operand, (int, float)) else None
    if isinstance(node, astnodes.TrueExpr):
        return True
    if isinstance(node, astnodes.FalseExpr):
        return False
    if isinstance(node, astnodes.Nil):
        return None
    if isinstance(node, astnodes.Name):
        return None  # bare variable ref — not evaluable
    if isinstance(node, astnodes.Table):
        positional = [f.value for f in node.fields if f.key is None]
        named = [(f.key, f.value) for f in node.fields if f.key is not None]
        if positional and not named:
            return [_lua_table_to_python(v) for v in positional]
        result = {}
        for k_node, v_node in named:
            key = _get_key(k_node)
            if key is not None:
                result[key] = _lua_table_to_python(v_node)
        # Mixed positional+named table: preserve positional entries under Lua's
        # 1-based integer keys so they aren't silently dropped (string-keyed
        # accessors downstream are unaffected by the extra integer keys).
        for i, v_node in enumerate(positional, start=1):
            result.setdefault(i, _lua_table_to_python(v_node))
        return result
    return None  # Concat, Call, Index, BinOp, etc.


def extract_data_extend_calls(src):
    """Walk the Lua AST for ``data:extend({...})`` calls; return entry dicts."""
    try:
        tree = lua_ast.parse(src)
    except Exception as e:
        common.safe_print(f"luaparser error: {e}")
        return _regex_fallback_extract(src)

    results = []
    for node in lua_ast.walk(tree):
        if not isinstance(node, astnodes.Invoke):
            continue
        if _get_key(node.source) != "data" or _get_key(node.func) != "extend":
            continue
        if not node.args:
            continue
        arg = node.args[0]
        if not isinstance(arg, astnodes.Table):
            continue
        for field in arg.fields:
            if not isinstance(field.value, astnodes.Table):
                continue
            entry = _lua_table_to_python(field.value)
            if isinstance(entry, dict):
                results.append(entry)
    return results


def _regex_fallback_extract(src):
    """Fallback when luaparser fails: returns nothing and warns.

    A regex-only pass would produce entries with type+name but no ingredients,
    effects, or other data — stubs that would embed as correctly-named but
    numerically empty records, silently poisoning prototype search results.
    Returning [] is safer: miss the file entirely rather than insert bad data.
    """
    common.safe_print("  WARNING: luaparser failed on this file; skipping it entirely (no stubs inserted).")
    return []


# --- Content formatters -------------------------------------------------------

def _ing_list(ings):
    """Format ingredients/results list into 'name ×amount' strings.

    Handles flat amounts, ranged amounts (amount_min/amount_max), and the
    old positional shorthand {"iron-plate", 1}.
    """
    if not isinstance(ings, list):
        return []
    out = []
    for ing in ings:
        if isinstance(ing, dict):
            name = ing.get("name", "?")
            amount = ing.get("amount")
            if amount is not None:
                amt_str = str(amount)
            else:
                lo = ing.get("amount_min", 1)
                hi = ing.get("amount_max")
                amt_str = f"{lo}-{hi}" if hi is not None else str(lo)
            out.append(f"{name} ×{amt_str}")
        elif isinstance(ing, list) and len(ing) >= 2:
            out.append(f"{ing[0]} ×{ing[1]}")
    return out


def _format_recipe(d):
    parts = [f"Recipe: {d['name']}"]
    cat = d.get("category") or "crafting"
    parts.append(f"Category: {cat}")
    # Factorio's engine default is 0.5s when energy_required is absent from the Lua
    e = d.get("energy_required", 0.5)
    parts.append(f"Crafting time: {e}s")
    if d.get("enabled") is False:
        parts.append("Enabled: false (unlocked by technology)")
    if d.get("allow_productivity"):
        parts.append("Allow productivity modules: yes")
    ings = d.get("ingredients") or (d.get("normal") or {}).get("ingredients")
    if ings:
        parts_ing = _ing_list(ings)
        if parts_ing:
            parts.append(f"Ingredients: {', '.join(parts_ing)}")
    results = d.get("results") or (d.get("normal") or {}).get("results")
    if results and isinstance(results, list):
        res_parts = _ing_list(results)
        if res_parts:
            parts.append(f"Results: {', '.join(res_parts)}")
    elif d.get("result"):
        amt = d.get("result_count", 1)
        parts.append(f"Results: {d['result']} ×{amt}")
    return "\n".join(parts)


def _format_item(d):
    parts = [f"Item: {d['name']} (type: {d['type']})"]
    sg = d.get("subgroup")
    if sg:
        parts.append(f"Subgroup: {sg}")
    ss = d.get("stack_size")
    if ss is not None:
        parts.append(f"Stack size: {ss}")
    fv = d.get("fuel_value")
    if fv:
        parts.append(f"Fuel value: {fv}")
        fc = d.get("fuel_category")
        if fc:
            parts.append(f"Fuel category: {fc}")
    spoil = d.get("spoil_ticks")
    if spoil is not None:
        parts.append(f"Spoil ticks: {spoil}")
        sr = d.get("spoil_result")
        if sr:
            parts.append(f"Spoil result: {sr}")
    if d.get("type") == "module":
        eff = d.get("effect")
        if isinstance(eff, dict):
            bonuses = []
            for k, v in eff.items():
                bonus = v.get("bonus") if isinstance(v, dict) else v if isinstance(v, (int, float)) else None
                if bonus is not None:
                    # Module effects are fractional multipliers (0.5 -> +50%); a
                    # whole-number bonus like 1 is still +100%, so format ints as
                    # percentages too (the <100 guard avoids mangling raw counts).
                    bonuses.append(f"{k}: {bonus:+.0%}" if isinstance(bonus, (int, float)) and abs(bonus) < 100 else f"{k}: {bonus}")
            if bonuses:
                parts.append(f"Module effects: {', '.join(bonuses)}")
        cat = d.get("category")
        if cat:
            parts.append(f"Module category: {cat}")
        tier = d.get("tier")
        if tier is not None:
            parts.append(f"Module tier: {tier}")
    return "\n".join(parts)


def _format_fluid(d):
    parts = [f"Fluid: {d['name']}"]
    dt = d.get("default_temperature")
    if dt is not None:
        parts.append(f"Default temperature: {dt}C")
    mt = d.get("max_temperature")
    if mt is not None:
        parts.append(f"Max temperature: {mt}C")
    hc = d.get("heat_capacity")
    if hc:
        parts.append(f"Heat capacity: {hc}")
    fv = d.get("fuel_value")
    if fv:
        parts.append(f"Fuel value: {fv}")
    return "\n".join(parts)


def _format_technology(d):
    parts = [f"Technology: {d['name']}"]
    prereqs = d.get("prerequisites")
    if isinstance(prereqs, list):
        plain = [str(p) for p in prereqs if isinstance(p, str)]
        if plain:
            parts.append(f"Prerequisites: {', '.join(plain)}")
    unit = d.get("unit")
    if isinstance(unit, dict):
        # Factorio 2.0 techs use either a flat `count` or a `count_formula`
        # (e.g. "2^(L-6)*1000") that scales with level — render whichever exists.
        count = unit.get("count")
        amount = str(count) if count is not None else unit.get("count_formula", "?")
        time = unit.get("time")
        ings = _ing_list(unit.get("ingredients") or [])
        cost = f"{amount}x [{', '.join(ings)}] {time}s each" if ings else str(amount)
        parts.append(f"Research cost: {cost}")
    elif d.get("research_trigger"):
        parts.append(f"Research trigger: {d['research_trigger']}")
    effects = d.get("effects")
    if isinstance(effects, list):
        unlocked = [e.get("recipe") for e in effects
                    if isinstance(e, dict) and e.get("type") == "unlock-recipe" and e.get("recipe")]
        if unlocked:
            # Limit to first 20 to stay under token budget
            suffix = f" (+{len(unlocked)-20} more)" if len(unlocked) > 20 else ""
            parts.append(f"Unlocked recipes: {', '.join(unlocked[:20])}{suffix}")
    return "\n".join(parts)


def _format_entity(d):
    t = d.get("type", "entity")
    n = d.get("name", "?")
    parts = [f"{t.replace('-', ' ').title()}: {n}"]
    cs = d.get("crafting_speed")
    if cs is not None:
        parts.append(f"Crafting speed: {cs}")
    cats = d.get("crafting_categories")
    if isinstance(cats, list):
        parts.append(f"Crafting categories: {', '.join(str(c) for c in cats)}")
    eu = d.get("energy_usage")
    if eu:
        parts.append(f"Energy usage: {eu}")
    es = d.get("energy_source")
    if isinstance(es, dict):
        parts.append(f"Energy source type: {es.get('type', '?')}")
    mh = d.get("max_health")
    if mh is not None:
        parts.append(f"Max health: {mh}")
    ms = d.get("module_specification")
    if isinstance(ms, dict):
        slots = ms.get("module_slots", 0)
        parts.append(f"Module slots: {slots}")
    mining_speed = d.get("mining_speed")
    if mining_speed is not None:
        parts.append(f"Mining speed: {mining_speed}")
    return "\n".join(parts)


def _format_quality(d):
    parts = [f"Quality tier: {d['name']}"]
    lvl = d.get("level")
    if lvl is not None:
        parts.append(f"Level: {lvl}")
    bpu = d.get("beacon_power_usage_multiplier")
    if bpu is not None:
        parts.append(f"Beacon power usage multiplier: {bpu}")
    spd = d.get("science_pack_drain_multiplier")
    if spd is not None:
        parts.append(f"Science pack drain multiplier: {spd}")
    return "\n".join(parts)


def _format_planet(d):
    parts = [f"Planet/space location: {d['name']}"]
    dist = d.get("distance")
    if dist is not None:
        parts.append(f"Distance: {dist}")
    sp = d.get("surface_properties")
    if isinstance(sp, dict):
        props = [f"{k}={v}" for k, v in sp.items() if isinstance(v, (int, float, str))]
        if props:
            parts.append(f"Surface properties: {', '.join(props)}")
    asteroids = d.get("asteroid_spawn_definitions")
    if isinstance(asteroids, list) and asteroids:
        types_seen = set()
        for a in asteroids[:20]:
            if isinstance(a, dict):
                t = a.get("type")
                if isinstance(t, str):
                    types_seen.add(t)
        if types_seen:
            parts.append(f"Asteroid types: {', '.join(sorted(types_seen))}")
    return "\n".join(parts)


def _format_asteroid_chunk(d):
    parts = [f"Asteroid chunk: {d['name']}"]
    results = d.get("mining_results")
    if isinstance(results, list):
        items = [r.get("item") or r.get("name", "?") for r in results if isinstance(r, dict)]
        items = [i for i in items if i]
        if items:
            parts.append(f"Mining results: {', '.join(items)}")
    return "\n".join(parts)


def _format_surface_property(d):
    parts = [f"Surface property: {d['name']}"]
    dv = d.get("default_value")
    if dv is not None:
        parts.append(f"Default value: {dv}")
    lo = d.get("min_value")
    hi = d.get("max_value")
    if lo is not None or hi is not None:
        parts.append(f"Range: {lo} to {hi}")
    return "\n".join(parts)


def _format_plant(d):
    parts = [f"Plant: {d['name']}"]
    gt = d.get("growth_ticks")
    if gt is not None:
        parts.append(f"Growth ticks: {gt}")
    st = d.get("spoil_ticks")
    if st is not None:
        parts.append(f"Spoil ticks: {st}")
    seed = d.get("seed_item")
    if seed:
        parts.append(f"Seed item: {seed}")
    return "\n".join(parts)


def format_prototype(d):
    """Format a prototype dict into human-readable content for embedding.

    Returns None for unsupported types (caller silently skips). Only includes
    entries where both type and name are plain str.
    """
    pt = d.get("type")
    pn = d.get("name")
    if not isinstance(pt, str) or not isinstance(pn, str):
        return None
    if pt == "recipe":
        return _format_recipe(d)
    if pt in ITEM_TYPES:
        return _format_item(d)
    if pt == "fluid":
        return _format_fluid(d)
    if pt == "technology":
        return _format_technology(d)
    if pt in WANTED_ENTITY_TYPES:
        return _format_entity(d)
    if pt == "quality":
        return _format_quality(d)
    if pt in {"planet", "space-location"}:
        return _format_planet(d)
    if pt == "asteroid-chunk":
        return _format_asteroid_chunk(d)
    if pt == "surface-property":
        return _format_surface_property(d)
    if pt == "plant":
        return _format_plant(d)
    return None


# --- Main ingestion loop -------------------------------------------------------

def main():
    repo_path = os.environ.get("FACTORIO_DATA_REPO",
                               os.path.join(common.REPO_ROOT, "factorio-data"))
    if not os.path.exists(repo_path):
        common.safe_print(f"ERROR: factorio-data repo not found at {repo_path}.")
        common.safe_print("Run: git clone --depth 1 https://github.com/wube/factorio-data.git factorio-data")
        raise SystemExit(1)

    info_path = os.path.join(repo_path, "base", "info.json")
    version = "unknown"
    if os.path.exists(info_path):
        try:
            with open(info_path, "r", encoding="utf-8") as f:
                version = json.load(f).get("version", "unknown")
        except (OSError, json.JSONDecodeError):
            pass
    common.safe_print(f"Factorio data version: {version}")

    dry = common.dry_run_requested()
    if dry:
        common.safe_print("DRY RUN: audit only, no embed/write.")
        db = db_path = table = None
    else:
        common.safe_print("Connecting to LanceDB...")
        db, db_path = common.connect_store("prototypes_lancedb")
        table = common.ensure_table(db, "prototypes", PrototypeRecord)

    # Scan all <dlc>/prototypes/ dirs at the repo root so quality/, elevated-rails/,
    # and any future DLCs are included automatically without code changes.
    search_roots = sorted(
        child / "prototypes"
        for child in Path(repo_path).iterdir()
        if child.is_dir() and (child / "prototypes").is_dir()
    )
    lua_files = []
    for root in search_roots:
        lua_files.extend(root.rglob("*.lua"))
    common.safe_print(f"Found {len(lua_files)} Lua files.")

    existing_hashes = {}
    if table is not None and len(table) > 0:
        rows = table.search().select(["prototype_type", "prototype_name", "content_hash"]).limit(200_000).to_list()
        for row in rows:
            key = (row["prototype_type"], row["prototype_name"])
            existing_hashes[key] = row["content_hash"]
    common.safe_print(f"Existing records: {len(existing_hashes)}")

    # ---- Pass 1: parse every file, keeping the LAST definition per key.
    # search_roots is sorted (base/ before space-age/, quality/, ...), so a DLC
    # override of a base prototype wins. Dedup MUST complete before any table
    # mutation: the old per-occurrence approach could delete the row for the base
    # definition and then skip the (unchanged-vs-stored) DLC definition, dropping
    # the prototype from the store entirely on every incremental re-run.
    final = {}  # (pt, pn) -> {"content", "category", "content_hash"}
    skipped_para = 0
    for lua_file in lua_files:
        try:
            src = lua_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for entry in extract_data_extend_calls(src):
            pt = entry.get("type")
            pn = entry.get("name")
            if not isinstance(pt, str) or not isinstance(pn, str):
                skipped_para += 1
                continue
            content = format_prototype(entry)
            if content is None:
                continue
            category = ""
            if pt == "recipe":
                category = entry.get("category") or "crafting"
            elif pt in ITEM_TYPES:
                category = entry.get("subgroup") or ""
            final[(pt, pn)] = {
                "content": content,
                "category": category,
                "content_hash": common.get_hash(content),
            }

    # ---- Pass 2: diff the final, deduplicated set against what is already stored.
    # explosion_per_source=None: each prototype is exactly one record, no explosion risk
    auditor = common.ChunkAuditor("prototypes_lancedb", explosion_per_source=None)
    all_records = []
    skipped_count = changed_count = added_count = 0
    for (pt, pn), rec in final.items():
        content_hash = rec["content_hash"]
        auditor.add(rec["content"], source=pn, node_type=pt)
        if existing_hashes.get((pt, pn)) == content_hash:
            skipped_count += 1
            continue
        if table is not None and (pt, pn) in existing_hashes:
            safe_pt = pt.replace("'", "''")
            safe_pn = pn.replace("'", "''")
            table.delete(f"prototype_type = '{safe_pt}' AND prototype_name = '{safe_pn}'")
            changed_count += 1
        else:
            added_count += 1
        all_records.append({
            "prototype_type": pt,
            "prototype_name": pn,
            "category": rec["category"],
            "content": rec["content"],
            "version": version,
            "content_hash": content_hash,
        })

    current_keys = set(final)
    orphans_removed = False
    if table is not None and len(table) > 0 and current_keys:
        orphan_keys = set(existing_hashes) - current_keys
        for ek in orphan_keys:
            safe_pt = ek[0].replace("'", "''")
            safe_pn = ek[1].replace("'", "''")
            table.delete(f"prototype_type = '{safe_pt}' AND prototype_name = '{safe_pn}'")
        if orphan_keys:
            common.safe_print(f"Removed {len(orphan_keys)} orphaned records.")
            orphans_removed = True

    common.safe_print(
        f"Skipped {skipped_count} unchanged | {changed_count} changed | "
        f"{added_count} new | {skipped_para} parametric/unsupported skipped"
    )
    auditor.summary()

    if dry:
        return
    if not all_records:
        if orphans_removed:
            common.safe_print("Removed orphaned rows; rebuilding FTS index.")
            try:
                table.create_fts_index("content", replace=True)
            except Exception as e:
                common.safe_print(f"FTS index skipped: {e}")
        else:
            common.safe_print("Database is perfectly up to date!")
        _write_version(db_path, version)
        return

    model_emb = common.load_embedder()
    batch_size = 100
    for i in range(0, len(all_records), batch_size):
        common.safe_print(f"Ingesting batch {i} to {i + batch_size}...")
        batch = all_records[i:i + batch_size]
        embeddings = common.embed([r["content"] for r in batch], model_emb)
        for j, rec in enumerate(batch):
            rec["vector"] = embeddings[j].tolist()
        table.add(batch)

    try:
        table.create_fts_index("content", replace=True)
    except Exception as e:
        common.safe_print(f"FTS index skipped: {e}")

    _write_version(db_path, version)
    common.safe_print(f"Ingestion complete! {len(all_records)} records written.")


def _write_version(db_path, version):
    with open(os.path.join(db_path, "version.txt"), "w", encoding="utf-8") as f:
        f.write(version)


if __name__ == "__main__":
    main()
