"""Phase 2 gate: prompt + tool definitions are generated from the catalog."""
from pathlib import Path

from voiceorder.agent import prompt
from voiceorder.core import tools
from voiceorder.core.catalog import Catalog
from voiceorder.core.ports import RestaurantContext

FIXTURE = Path(__file__).resolve().parents[2] / "voiceorder" / "fixtures" / "menu_taqueria.json"


def _ctx() -> RestaurantContext:
    return RestaurantContext(
        restaurant_id="taqueria-demo",
        restaurant_name="Taqueria Demo",
        pos_profile="square_like",
        voice_platform="text",
        transfer_number="+15550134200",
    )


def test_catalog_reference_covers_every_item():
    catalog = Catalog.from_json(FIXTURE)
    ref_text = prompt.build_catalog_reference(catalog)
    for item in catalog.all_items():
        assert item.ref in ref_text
        assert item.name in ref_text
        assert f"${item.base_price:.2f}" in ref_text


def test_required_modifier_groups_are_marked():
    catalog = Catalog.from_json(FIXTURE)
    ref_text = prompt.build_catalog_reference(catalog)
    assert "*Rice & Beans" in ref_text  # required group on burritos
    assert "Salsa" in ref_text  # optional group on tacos


def test_tool_schemas_match_tool_names():
    schemas = prompt.build_tool_schemas()
    assert [s["name"] for s in schemas] == tools.TOOL_NAMES
    for schema in schemas:
        assert schema["description"]
        assert schema["input_schema"]["type"] == "object"
        for required in schema["input_schema"].get("required", []):
            assert required in schema["input_schema"]["properties"]


def test_system_prompt_mentions_key_rules():
    catalog = Catalog.from_json(FIXTURE)
    text = prompt.build_system_prompt(catalog, _ctx())
    assert "Taqueria Demo" in text
    assert "B2" in text  # refs are visible to the agent
    assert "transfer_call" in text
    assert "submit_order" in text
    assert "card number" in text
