"""Unit tests for ingest_prototypes parsing and formatting logic.

No DB, no embedder — pure parsing, no network access.
"""

import pytest
from factorio_ai_tools.ingest.ingest_prototypes import (
    _lua_table_to_python,
    extract_data_extend_calls,
    format_prototype,
)
from luaparser import ast as lua_ast
from luaparser import astnodes


def _first_table_value(src):
    """Parse `x = {...}` and return the Table node's Python conversion."""
    tree = lua_ast.parse(src)
    for node in lua_ast.walk(tree):
        if isinstance(node, astnodes.Table):
            return _lua_table_to_python(node)
    raise AssertionError("no Table node found")

RECIPE_LUA = """
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
"""

TECH_LUA = """
data:extend({
  {
    type = "technology",
    name = "automation",
    prerequisites = {"electronics"},
    unit = {count=100, ingredients={{"automation-science-pack",1}}, time=10},
    effects = {{type="unlock-recipe", recipe="assembling-machine-1"}},
  }
})
"""

PARAMETRIC_LUA = """
data:extend({
  { type = "item", name = "base-item-" .. n, stack_size = 1 }
})
"""

QUALITY_LUA = """
data:extend({
  {
    type = "quality",
    name = "legendary",
    level = 4,
    beacon_power_usage_multiplier = 0.5,
    science_pack_drain_multiplier = 1,
  }
})
"""

PLANET_LUA = """
data:extend({
  {
    type = "planet",
    name = "vulcanus",
    distance = 2.0,
    surface_properties = {
      pressure = 1000,
      temperature = 300,
    },
  }
})
"""

PAREN_LESS_LUA = """
data:extend{
  {
    type = "fluid",
    name = "water",
    default_temperature = 15,
    max_temperature = 100,
  }
}
"""

ASSEMBLER_LUA = """
data:extend({
  {
    type = "assembling-machine",
    name = "assembling-machine-1",
    crafting_speed = 0.5,
    crafting_categories = {"crafting", "basic-crafting"},
    energy_usage = "75kW",
    energy_source = {type = "electric"},
    max_health = 300,
    module_specification = {module_slots = 0},
  }
})
"""


def test_recipe_parsed_correctly():
    entries = extract_data_extend_calls(RECIPE_LUA)
    assert len(entries) == 1
    e = entries[0]
    assert e["type"] == "recipe"
    assert e["name"] == "electronic-circuit"
    assert isinstance(e["ingredients"], list)


def test_recipe_content_has_ingredients_and_time():
    content = format_prototype({
        "type": "recipe",
        "name": "electronic-circuit",
        "category": "crafting",
        "energy_required": 0.5,
        "enabled": False,
        "ingredients": [
            {"name": "iron-plate", "amount": 1},
            {"name": "copper-cable", "amount": 3},
        ],
        "results": [{"name": "electronic-circuit", "amount": 1}],
    })
    assert content is not None
    assert "iron-plate" in content
    assert "copper-cable" in content
    assert "0.5" in content


def test_parametric_prototype_filtered():
    entries = extract_data_extend_calls(PARAMETRIC_LUA)
    # name is a Concat node → _lua_table_to_python returns None → entry skipped
    # or entry has name=None; format_prototype returns None either way
    usable = [e for e in entries if isinstance(e.get("name"), str)]
    assert len(usable) == 0


def test_technology_parsed_and_formatted():
    entries = extract_data_extend_calls(TECH_LUA)
    assert entries
    assert entries[0]["type"] == "technology"
    content = format_prototype(entries[0])
    assert content is not None
    assert "automation-science-pack" in content or "assembling-machine-1" in content


def test_only_top_level_entries_extracted():
    # The ingredient dicts inside a recipe must NOT appear as top-level prototypes
    entries = extract_data_extend_calls(RECIPE_LUA)
    assert len(entries) == 1, f"Expected 1 entry, got {len(entries)}"


def test_quality_prototype_parsed_and_formatted():
    entries = extract_data_extend_calls(QUALITY_LUA)
    assert entries
    assert entries[0]["type"] == "quality"
    content = format_prototype(entries[0])
    assert content is not None
    assert "legendary" in content
    assert "level" in content.lower() or "Level" in content


def test_planet_prototype_parsed_and_formatted():
    entries = extract_data_extend_calls(PLANET_LUA)
    assert entries
    assert entries[0]["type"] == "planet"
    content = format_prototype(entries[0])
    assert content is not None
    assert "vulcanus" in content


def test_paren_less_extend_parsed():
    entries = extract_data_extend_calls(PAREN_LESS_LUA)
    assert entries
    assert entries[0]["type"] == "fluid"
    assert entries[0]["name"] == "water"


def test_assembler_entity_formatted():
    entries = extract_data_extend_calls(ASSEMBLER_LUA)
    assert entries
    content = format_prototype(entries[0])
    assert content is not None
    assert "assembling-machine-1" in content
    assert "0.5" in content  # crafting speed


def test_unsupported_type_returns_none():
    content = format_prototype({"type": "achievement", "name": "some-achievement"})
    assert content is None


def test_format_prototype_filters_non_string_name():
    content = format_prototype({"type": "recipe", "name": None})
    assert content is None


def test_fluid_formatted():
    content = format_prototype({
        "type": "fluid",
        "name": "petroleum-gas",
        "default_temperature": 25,
        "max_temperature": 100,
        "fuel_value": "3MJ",
    })
    assert content is not None
    assert "petroleum-gas" in content
    assert "25" in content


def test_negative_numbers_parsed_not_dropped():
    # Negative literals parse as UMinusOp(Number), not a negative Number. Before
    # the fix these dropped to None (efficiency-module bonuses, sub-zero temps).
    d = _first_table_value("x = {bonus = -0.3, temperature = -273, distance = -1.5}")
    assert d["bonus"] == -0.3
    assert d["temperature"] == -273
    assert d["distance"] == -1.5


def test_efficiency_module_negative_bonus_in_content():
    content = format_prototype({
        "type": "module",
        "name": "efficiency-module",
        "effect": {"consumption": {"bonus": -0.3}},
    })
    assert content is not None
    assert "Module effects" in content
    assert "-30%" in content  # -0.3 rendered as a percentage, not omitted


def test_mixed_positional_and_named_table_keeps_both():
    # A Lua table mixing positional and named entries must not drop the positional
    # values; they're preserved under Lua's 1-based integer keys.
    d = _first_table_value("x = {'foo', 'bar', type = 'item'}")
    assert d["type"] == "item"
    assert d[1] == "foo"
    assert d[2] == "bar"


def test_technology_count_formula_rendered():
    # Factorio 2.0 techs use count_formula instead of a flat count; before the fix
    # the cost rendered as the garbled "Nonex [...]".
    content = format_prototype({
        "type": "technology",
        "name": "mining-productivity-3",
        "unit": {"count_formula": "2^(L-6)*1000",
                 "ingredients": [{"name": "automation-science-pack", "amount": 1}],
                 "time": 60},
    })
    assert content is not None
    assert "None" not in content
    assert "2^(L-6)*1000" in content


def test_integer_module_bonus_rendered_as_percent():
    # A whole-number bonus (1) is still +100%, not a raw "1".
    content = format_prototype({
        "type": "module",
        "name": "speed-module",
        "effect": {"speed": {"bonus": 1}},
    })
    assert content is not None
    assert "+100%" in content
