"""Ingest Factorio prototype definitions into ``data/prototypes_lancedb``.

Reads Factorio's own ``--dump-data`` export (``data-raw-dump.json``) — the fully
resolved ``data.raw`` as JSON, keyed ``type -> name -> prototype`` — and stores one
structured text record per prototype holding exact numerical values. This is the
**vanilla baseline** (base + Space Age DLC); a modded game's dump differs.

Input path: ``FACTORIO_DATA_DUMP`` env, default
``<repo_root>/factorio-export/vanilla_2.1.8/data-raw-dump.json``. Produce the dump
with ``factorio --dump-data`` (see ``factorio-export/README.md``). Incremental by
(prototype_type, prototype_name) SHA-256 via ``merge_insert`` upsert; writes
``version.txt``.
"""

import json
import os
import re

from lancedb.pydantic import LanceModel, Vector

from factorio_ai_tools.ingest import common

# Routing vocab lives in common.py (shared with server.py's filter). Alias the
# names the formatters/dispatch use. Space-age types (quality/planet/…) dispatch
# via literals in format_prototype, not a set, so there's no SPACE_AGE_TYPES.
WANTED_ENTITY_TYPES = common.PROTOTYPE_ENTITY_TYPES
ITEM_TYPES = common.PROTOTYPE_ITEM_TYPES


class PrototypeRecord(LanceModel):
    prototype_type: str
    prototype_name: str
    category: str
    content: str
    version: str
    content_hash: str
    vector: Vector(common.EMBEDDING_DIM)


# --- Content formatters -------------------------------------------------------

def _ing_list(ings):
    """Format ingredients/results into 'name ×amount' strings.

    Handles the resolved dump shapes: dicts ({name, amount} or
    {name, amount_min/amount_max}) and the positional ['name', amount] form used
    by technology unit ingredients.
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


def _recipe_categories(d):
    """Recipe crafting categories. Factorio 2.1's resolved dump normalizes these
    to a `categories` LIST (a recipe can belong to several, e.g. electronic-circuit
    is ['crafting', 'electromagnetics']); older/source Lua used a singular
    `category` string. Return a list of category names (may be empty)."""
    cats = d.get("categories")
    if isinstance(cats, list):
        return [c for c in cats if isinstance(c, str)]
    c = d.get("category")
    return [c] if isinstance(c, str) else []


def _format_recipe(d):
    parts = [f"Recipe: {d['name']}"]
    cats = _recipe_categories(d) or ["crafting"]
    parts.append(f"Category: {', '.join(cats)}")
    # The dump omits energy_required when it's the engine default (0.5s).
    e = d.get("energy_required", 0.5)
    parts.append(f"Crafting time: {e}s")
    if d.get("enabled") is False:
        parts.append("Enabled: false (unlocked by technology)")
    if d.get("allow_productivity"):
        parts.append("Allow productivity modules: yes")
    # Resolved 2.1 dumps are flat; a non-resolved/older/modded dump may nest under a
    # `normal` difficulty block — recover from it, but only if it's a dict (a list
    # `normal` must not crash, the bug the old `(... or {}).get(...)` form had).
    normal = d.get("normal") if isinstance(d.get("normal"), dict) else {}
    ings = d.get("ingredients") or normal.get("ingredients")
    if ings:
        parts_ing = _ing_list(ings)
        if parts_ing:
            parts.append(f"Ingredients: {', '.join(parts_ing)}")
    results = d.get("results")
    if not isinstance(results, list):
        results = normal.get("results")
    if isinstance(results, list):
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
                # Module effects in the dump are scalars ({consumption: -0.3}); older
                # shapes nested {bonus: ...}. Exclude bool (subclass of int) so a
                # flag-style effect isn't rendered as a fake '+100%'.
                if isinstance(v, dict):
                    bonus = v.get("bonus")
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    bonus = v
                else:
                    bonus = None
                if bonus is not None and not isinstance(bonus, bool):
                    # Fractional multipliers (0.5 -> +50%); the <100 guard avoids
                    # mangling a raw count into a percentage.
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


def _category_for(d):
    """The `category` column value: a recipe's primary crafting category, or an
    item's subgroup. Empty for everything else."""
    pt = d.get("type")
    if pt == "recipe":
        cats = _recipe_categories(d)
        return cats[0] if cats else "crafting"
    if pt in ITEM_TYPES:
        return d.get("subgroup") or ""
    return ""


# --- Main ingestion loop -------------------------------------------------------

def _read_dump_version(dump_path):
    """Version string for the dump. The data.raw JSON carries no game version, so:
    a sibling version.txt (written at dump time) -> FACTORIO_VERSION env ->
    a 'vanilla_X.Y.Z' parent-dir name -> 'unknown'."""
    sibling = os.path.join(os.path.dirname(dump_path), "version.txt")
    if os.path.exists(sibling):
        try:
            with open(sibling, "r", encoding="utf-8") as f:
                v = f.read().strip()
            if v:
                return v
        except OSError:
            pass
    env = os.environ.get("FACTORIO_VERSION")
    if env:
        return env
    m = re.search(r"(\d+\.\d+\.\d+)", os.path.basename(os.path.dirname(dump_path)))
    return m.group(1) if m else "unknown"


def _delete_keys(table, keys):
    """Delete rows for a list of (prototype_type, prototype_name) keys, chunked into
    a few OR-predicates so a big version bump is a handful of commits, not N."""
    for i in range(0, len(keys), 200):
        clauses = []
        for pt, pn in keys[i:i + 200]:
            spt = pt.replace("'", "''")
            spn = pn.replace("'", "''")
            clauses.append(f"(prototype_type = '{spt}' AND prototype_name = '{spn}')")
        table.delete(" OR ".join(clauses))


def _write_version(db_path, version):
    with open(os.path.join(db_path, "version.txt"), "w", encoding="utf-8") as f:
        f.write(version)


def main():
    dump_path = os.environ.get(
        "FACTORIO_DATA_DUMP",
        os.path.join(common.REPO_ROOT, "factorio-export", "vanilla_2.1.8", "data-raw-dump.json"),
    )
    if not os.path.exists(dump_path):
        common.safe_print(f"ERROR: data-raw-dump.json not found at {dump_path}.")
        common.safe_print("Produce it with `factorio --dump-data` (see factorio-export/README.md),")
        common.safe_print("then set FACTORIO_DATA_DUMP or place it at the default path above.")
        raise SystemExit(1)

    version = _read_dump_version(dump_path)
    common.safe_print(f"Reading dump: {dump_path} (version {version})")
    try:
        with open(dump_path, "r", encoding="utf-8") as f:
            dump = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        common.safe_print(f"ERROR: could not read/parse the dump at {dump_path}: {e}")
        common.safe_print("A truncated/corrupt export won't parse — re-produce it with `factorio --dump-data`.")
        raise SystemExit(1)
    if not isinstance(dump, dict):
        common.safe_print(f"ERROR: dump root is a {type(dump).__name__}, expected a JSON object (type -> name -> prototype).")
        raise SystemExit(1)

    dry = common.dry_run_requested()
    if dry:
        common.safe_print("DRY RUN: audit only, no embed/write.")
        db = db_path = table = None
    else:
        common.safe_print("Connecting to LanceDB...")
        db, db_path = common.connect_store("prototypes_lancedb")
        table = common.ensure_table(db, "prototypes", PrototypeRecord)

    existing_hashes = {}
    if table is not None and len(table) > 0:
        rows = table.search().select(["prototype_type", "prototype_name", "content_hash"]).limit(500_000).to_list()
        for row in rows:
            existing_hashes[(row["prototype_type"], row["prototype_name"])] = row["content_hash"]
    common.safe_print(f"Existing records: {len(existing_hashes)}")

    # ---- Walk the resolved data.raw (type -> name -> prototype). Only wanted types
    # format to content; everything else (graphics/sound/particles/...) is skipped.
    # explosion_per_source=None: one record per prototype, no explosion risk.
    auditor = common.ChunkAuditor("prototypes_lancedb", explosion_per_source=None)
    final = {}  # (pt, pn) -> {"content", "category", "content_hash"}
    skipped_unsupported = 0
    for ptype, protos in dump.items():
        if not isinstance(protos, dict):
            continue
        for pname, raw in protos.items():
            if not isinstance(raw, dict):
                continue
            d = dict(raw)
            d.setdefault("type", ptype)
            d.setdefault("name", pname)
            content = format_prototype(d)
            if content is None:
                skipped_unsupported += 1
                continue
            final[(ptype, pname)] = {
                "content": content,
                "category": _category_for(d),
                "content_hash": common.get_hash(content),
            }
            auditor.add(content, source=pname, node_type=ptype)

    auditor.note_source("data-raw-dump.json", os.path.getsize(dump_path), len(final))

    # Refuse to touch the store if the dump parsed but produced ZERO records (an
    # empty/wrong dump, or a future format change that breaks every formatter).
    # Without this the orphan pass below would delete every existing row, silently
    # wiping the whole store. note_source/summary only WARN; this hard-stops.
    if not final:
        common.safe_print("ERROR: the dump parsed but produced 0 prototype records — refusing to touch the store.")
        common.safe_print("Check FACTORIO_DATA_DUMP points at a real Factorio data-raw-dump.json.")
        raise SystemExit(1)

    # ---- Diff against what's stored: collect changed+new to (re)embed, orphans to drop.
    all_records = []
    skipped_count = changed_count = added_count = 0
    for (pt, pn), rec in final.items():
        if existing_hashes.get((pt, pn)) == rec["content_hash"]:
            skipped_count += 1
            continue
        if (pt, pn) in existing_hashes:
            changed_count += 1
        else:
            added_count += 1
        all_records.append({
            "prototype_type": pt,
            "prototype_name": pn,
            "category": rec["category"],
            "content": rec["content"],
            "version": version,
            "content_hash": rec["content_hash"],
        })
    # `final` is non-empty here (guarded above), so this can never delete the store.
    orphan_keys = sorted(set(existing_hashes) - set(final)) if (table is not None and existing_hashes) else []

    common.safe_print(
        f"Skipped {skipped_count} unchanged | {changed_count} changed | "
        f"{added_count} new | {len(orphan_keys)} orphaned | {skipped_unsupported} unsupported types"
    )
    auditor.summary()
    if dry:
        return

    # ---- Embed FIRST (before any table mutation), then upsert atomically with
    # merge_insert keyed on (type, name) — updates changed rows, inserts new ones,
    # leaves unchanged rows untouched. No delete-before-add window (#3); no per-row
    # delete loop (#545). Orphans (gone from the dump) are removed separately.
    if all_records:
        model_emb = common.load_embedder()
        for i in range(0, len(all_records), 100):
            common.safe_print(f"Embedding batch {i} to {i + 100}...")
            batch = all_records[i:i + 100]
            embeddings = common.embed([r["content"] for r in batch], model_emb)
            for j, rec in enumerate(batch):
                rec["vector"] = embeddings[j].tolist()
        (table.merge_insert(["prototype_type", "prototype_name"])
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute(all_records))

    if orphan_keys:
        _delete_keys(table, orphan_keys)
        common.safe_print(f"Removed {len(orphan_keys)} orphaned records.")

    if all_records or orphan_keys:
        try:
            table.create_fts_index("content", replace=True)
        except Exception as e:
            common.safe_print(f"FTS index skipped: {e}")
    else:
        common.safe_print("Database is perfectly up to date!")

    _write_version(db_path, version)
    common.safe_print(
        f"Ingestion complete! {len(all_records)} records embedded, "
        f"{len(orphan_keys)} removed. Total in store: {len(final)}."
    )


if __name__ == "__main__":
    main()
