"""Unit tests for ingest_prototypes formatting logic.

The ingester reads Factorio's `--dump-data` JSON (resolved `data.raw`), so these
fixtures are plain dicts shaped like the dump — no Lua parsing, no DB, no embedder.
"""

import json

import pytest

from factorio_ai_tools.ingest import ingest_prototypes
from factorio_ai_tools.ingest.ingest_prototypes import (
    _category_for,
    _recipe_categories,
    format_prototype,
)


# --- recipe: 2.1 `categories` array + back-compat singular `category` ----------

def test_recipe_renders_multiple_categories():
    # 2.1's resolved dump normalizes to a `categories` list (electronic-circuit is
    # both 'crafting' and 'electromagnetics'); all of them should render.
    d = {"type": "recipe", "name": "electronic-circuit",
         "categories": ["crafting", "electromagnetics"], "energy_required": 0.5,
         "ingredients": [{"type": "item", "name": "iron-plate", "amount": 1},
                         {"type": "item", "name": "copper-cable", "amount": 3}],
         "results": [{"type": "item", "name": "electronic-circuit", "amount": 1}]}
    content = format_prototype(d)
    assert "Category: crafting, electromagnetics" in content
    assert "iron-plate ×1" in content and "copper-cable ×3" in content
    assert "Crafting time: 0.5s" in content
    # category column takes the primary (first) category
    assert _category_for(d) == "crafting"


def test_recycling_recipe_category_not_defaulted():
    # Regression: a recycling recipe carries categories=['recycling']; it must NOT
    # fall back to the "crafting" default (the bug the dump exposed).
    d = {"type": "recipe", "name": "gun-turret-recycling", "categories": ["recycling"],
         "ingredients": [{"name": "gun-turret", "amount": 1}],
         "results": [{"name": "iron-gear-wheel", "amount": 2}]}
    assert "Category: recycling" in format_prototype(d)
    assert _category_for(d) == "recycling"


def test_recipe_singular_category_back_compat():
    d = {"type": "recipe", "name": "r", "category": "smelting",
         "ingredients": [{"name": "iron-ore", "amount": 1}]}
    assert "Category: smelting" in format_prototype(d)
    assert _recipe_categories(d) == ["smelting"]


def test_recipe_default_category_when_absent():
    d = {"type": "recipe", "name": "r", "ingredients": [{"name": "wood", "amount": 1}]}
    assert "Category: crafting" in format_prototype(d)
    assert _category_for(d) == "crafting"


def test_recipe_ranged_and_positional_amounts():
    d = {"type": "recipe", "name": "r",
         "ingredients": [{"name": "a", "amount_min": 2, "amount_max": 5}, ["b", 4]]}
    out = format_prototype(d)
    assert "a ×2-5" in out and "b ×4" in out


# --- module effects: dump scalar shape, percent, int, bool guard ---------------

def test_efficiency_module_negative_bonus():
    d = {"type": "module", "name": "efficiency-module", "effect": {"consumption": -0.3}}
    assert "consumption: -30%" in format_prototype(d)


def test_speed_module_multiple_effects():
    d = {"type": "module", "name": "speed-module",
         "effect": {"speed": 0.2, "consumption": 0.5, "quality": -0.01}}
    out = format_prototype(d)
    assert "speed: +20%" in out and "consumption: +50%" in out


def test_integer_module_bonus_is_percent():
    d = {"type": "module", "name": "m", "effect": {"speed": 1}}
    assert "speed: +100%" in format_prototype(d)


def test_bool_module_effect_not_rendered_as_percent():
    # bool is a subclass of int; a flag-style effect must not become "+100%".
    d = {"type": "module", "name": "m", "effect": {"some_flag": True}}
    out = format_prototype(d)
    assert "+100%" not in out and "Module effects" not in out


# --- other types ---------------------------------------------------------------

def test_item_subgroup_is_category_column():
    d = {"type": "item", "name": "iron-plate", "subgroup": "raw-material", "stack_size": 100}
    assert "Subgroup: raw-material" in format_prototype(d)
    assert _category_for(d) == "raw-material"


def test_technology_count_formula_rendered():
    d = {"type": "technology", "name": "t",
         "unit": {"count_formula": "2^(L-7)*1000", "time": 60,
                  "ingredients": [["automation-science-pack", 1]]}}
    content = format_prototype(d)
    assert "2^(L-7)*1000" in content and "None" not in content
    assert "automation-science-pack" in content


def test_technology_flat_count_rendered():
    d = {"type": "technology", "name": "automation",
         "unit": {"count": 10, "time": 10, "ingredients": [["automation-science-pack", 1]]}}
    assert "10x [automation-science-pack ×1] 10s each" in format_prototype(d)


def test_assembler_entity_formatted():
    d = {"type": "assembling-machine", "name": "assembling-machine-2",
         "crafting_speed": 0.75, "crafting_categories": ["crafting", "advanced-crafting"],
         "energy_usage": "150kW", "energy_source": {"type": "electric"}, "max_health": 350}
    out = format_prototype(d)
    assert "Assembling Machine: assembling-machine-2" in out
    assert "Crafting speed: 0.75" in out and "150kW" in out


def test_fluid_formatted():
    d = {"type": "fluid", "name": "water", "default_temperature": 15, "max_temperature": 100}
    out = format_prototype(d)
    assert "water" in out and "15C" in out


def test_quality_formatted():
    d = {"type": "quality", "name": "legendary", "level": 5,
         "science_pack_drain_multiplier": 0.95}
    assert "legendary" in format_prototype(d)


def test_planet_surface_properties():
    d = {"type": "planet", "name": "vulcanus", "distance": 10,
         "surface_properties": {"pressure": 4000, "gravity": 40}}
    out = format_prototype(d)
    assert "vulcanus" in out and "pressure=4000" in out


def test_unsupported_type_returns_none():
    assert format_prototype({"type": "sound", "name": "x"}) is None
    assert format_prototype({"type": "recipe", "name": None}) is None


def test_category_for_non_recipe_non_item_is_empty():
    assert _category_for({"type": "technology", "name": "t"}) == ""


# --- `normal` difficulty-block fallback (non-resolved / modded / older dumps) ---

def test_recipe_normal_dict_fallback_recovers_ingredients():
    # A non-flat dump nests ingredients/results under a `normal` dict; recover them.
    d = {"type": "recipe", "name": "r", "categories": ["crafting"],
         "normal": {"ingredients": [{"name": "iron-plate", "amount": 2}],
                    "results": [{"name": "r", "amount": 1}]}}
    out = format_prototype(d)
    assert "Ingredients: iron-plate ×2" in out
    assert "Results: r ×1" in out


def test_recipe_normal_as_list_does_not_crash():
    # A LIST-valued `normal` is the shape that used to crash _format_recipe; it must
    # be ignored, not raise.
    d = {"type": "recipe", "name": "r", "categories": ["crafting"], "normal": ["a", "b"]}
    out = format_prototype(d)  # must not raise
    assert "Recipe: r" in out and "Ingredients:" not in out


# --- main() guards against bad dumps (dry-run: no DB connect, no embed/write) ----

def _expect_main_aborts(tmp_path, monkeypatch, dump_text):
    p = tmp_path / "data-raw-dump.json"
    p.write_text(dump_text, encoding="utf-8")
    monkeypatch.setenv("FACTORIO_DATA_DUMP", str(p))
    monkeypatch.setenv("FACTORIO_MCP_DRY_RUN", "1")  # no LanceDB connect, no embed
    with pytest.raises(SystemExit):
        ingest_prototypes.main()


def test_corrupt_dump_aborts(tmp_path, monkeypatch):
    # A truncated/corrupt export must error cleanly, not raise a raw JSONDecodeError.
    _expect_main_aborts(tmp_path, monkeypatch, "{ not valid json ")


def test_non_dict_root_aborts(tmp_path, monkeypatch):
    # A JSON array/scalar root must be caught, not crash on dump.items().
    _expect_main_aborts(tmp_path, monkeypatch, "[1, 2, 3]")


def test_zero_record_dump_refuses_to_touch_store(tmp_path, monkeypatch):
    # A dump that parses but yields 0 recognized prototypes must REFUSE (the orphan
    # pass would otherwise delete the whole store). Only unsupported types here.
    _expect_main_aborts(tmp_path, monkeypatch,
                        json.dumps({"sound": {"s": {"type": "sound", "name": "s"}}}))


# --- multi-version isolation (REAL ingest path, mocked embedder) ----------------
# These exercise connect/ensure/diff/merge_insert/delete against a real tmp LanceDB
# store, only stubbing the embedder, because the version-scoping bug class (a re-ingest
# of one version deleting another's rows) can only surface against actual store state.

def _write_dump(parent, version, protos):
    d = parent / f"vanilla_{version}"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "data-raw-dump.json"
    p.write_text(json.dumps(protos), encoding="utf-8")
    return str(p)


def _rows(table):
    return table.search().select(
        ["prototype_type", "prototype_name", "version", "content"]).limit(10_000).to_list()


def _recipe(name, amount, categories=None, singular=None):
    d = {"type": "recipe", "name": name, "ingredients": [{"name": "a", "amount": amount}]}
    if categories is not None:
        d["categories"] = categories
    if singular is not None:
        d["category"] = singular
    return d


@pytest.fixture
def mocked_store(tmp_path, monkeypatch):
    """A real LanceDB store in tmp + a stub embedder, so the actual connect / ensure_table
    / merge_insert / delete path runs without loading the model."""
    import lancedb
    import numpy as np

    from factorio_ai_tools.ingest import common
    monkeypatch.setattr(common, "load_embedder", lambda: object())
    monkeypatch.setattr(common, "embed",
                        lambda texts, model: np.zeros((len(texts), common.EMBEDDING_DIM), dtype="float32"))
    store_dir = str(tmp_path / "prototypes_lancedb")
    db = lancedb.connect(store_dir)
    monkeypatch.setattr(common, "connect_store", lambda name: (db, store_dir))
    return tmp_path, db, store_dir


def test_two_versions_coexist(mocked_store, monkeypatch):
    # Same (recipe, r) in BOTH versions with different content. The merge key includes
    # version, so they must NOT clobber each other — both rows survive.
    tmp_path, db, store_dir = mocked_store
    v0 = _write_dump(tmp_path / "p0", "2.0.76",
                     {"recipe": {"r": _recipe("r", 1, singular="crafting"),
                                 "only076": _recipe("only076", 1, singular="crafting")}})
    v1 = _write_dump(tmp_path / "p1", "2.1.8",
                     {"recipe": {"r": _recipe("r", 7, categories=["crafting"])}})
    monkeypatch.setenv("FACTORIO_DATA_DUMP", v0)
    ingest_prototypes.main()
    monkeypatch.setenv("FACTORIO_DATA_DUMP", v1)
    ingest_prototypes.main()

    rows = _rows(db.open_table("prototypes"))
    r076 = [x for x in rows if x["prototype_name"] == "r" and x["version"] == "2.0.76"]
    r218 = [x for x in rows if x["prototype_name"] == "r" and x["version"] == "2.1.8"]
    assert len(r076) == 1 and len(r218) == 1, "both versions' (recipe, r) rows must coexist"
    assert "a x1" in r076[0]["content"].replace("×", "x")
    assert "a x7" in r218[0]["content"].replace("×", "x")
    with open(store_dir + "/version.txt", encoding="utf-8") as f:
        assert f.read().strip() == "2.0.76,2.1.8"


def test_reingest_one_version_does_not_delete_another(mocked_store, monkeypatch):
    # THE store-wipe guard: re-ingesting 2.1.8 (with an orphaned proto) must prune only
    # 2.1.8's orphan and leave every 2.0.76 row untouched.
    tmp_path, db, store_dir = mocked_store
    v0 = _write_dump(tmp_path / "p0", "2.0.76",
                     {"recipe": {"keep": _recipe("keep", 1, singular="crafting")}})
    monkeypatch.setenv("FACTORIO_DATA_DUMP", v0)
    ingest_prototypes.main()
    # Seed 2.1.8 with x and y...
    v1a = _write_dump(tmp_path / "p1a", "2.1.8",
                      {"recipe": {"x": _recipe("x", 1, categories=["crafting"]),
                                  "y": _recipe("y", 1, categories=["crafting"])}})
    monkeypatch.setenv("FACTORIO_DATA_DUMP", v1a)
    ingest_prototypes.main()
    # ...then re-ingest 2.1.8 with only x (y becomes a 2.1.8 orphan).
    v1b = _write_dump(tmp_path / "p1b", "2.1.8",
                      {"recipe": {"x": _recipe("x", 1, categories=["crafting"])}})
    monkeypatch.setenv("FACTORIO_DATA_DUMP", v1b)
    ingest_prototypes.main()

    names = {(x["version"], x["prototype_name"]) for x in _rows(db.open_table("prototypes"))}
    assert ("2.1.8", "y") not in names, "2.1.8's orphan should be pruned"
    assert ("2.1.8", "x") in names, "the surviving 2.1.8 proto must stay"
    assert ("2.0.76", "keep") in names, "the OTHER version must be untouched (store-wipe guard)"
