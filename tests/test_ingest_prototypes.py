"""Unit tests for ingest_prototypes formatting logic.

The ingester reads Factorio's `--dump-data` JSON (resolved `data.raw`), so these
fixtures are plain dicts shaped like the dump — no Lua parsing, no DB, no embedder.
"""

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
