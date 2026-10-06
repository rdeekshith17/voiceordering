"""Every golden script parses, has a unique id, and references real menu data."""
from pathlib import Path

from voiceorder.core.catalog import Catalog
from voiceorder.eval.harness import load_scripts
from voiceorder.pos_adapters.fake import PROFILES

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "voiceorder" / "eval" / "scripts"
FIXTURE = Path(__file__).resolve().parents[2] / "voiceorder" / "fixtures" / "menu_taqueria.json"


def test_at_least_thirty_scripts():
    scripts = load_scripts(SCRIPTS_DIR)
    assert len(scripts) >= 30, f"only {len(scripts)} golden scripts"


def test_script_ids_unique_and_turns_well_formed():
    scripts = load_scripts(SCRIPTS_DIR)
    ids = [s.id for s in scripts]
    assert len(ids) == len(set(ids))
    for script in scripts:
        assert script.turns, f"{script.id}: no turns"
        for turn in script.turns:
            assert isinstance(turn, dict), f"{script.id}: bad turn {turn}"
            assert ("caller" in turn) != ("hangup" in turn), f"{script.id}: bad turn {turn}"


def test_expected_carts_reference_real_menu_data():
    catalog = Catalog.from_json(FIXTURE)
    scripts = load_scripts(SCRIPTS_DIR)
    for script in scripts:
        assert set(script.pos_profiles) <= set(PROFILES), f"{script.id}: bad profile"
        for line in script.expected_cart:
            item = catalog.get(line["item_ref"])
            assert item is not None, f"{script.id}: unknown ref {line['item_ref']}"
            assert line["quantity"] >= 1, f"{script.id}: bad quantity"
            if line.get("variation_id"):
                assert any(
                    v.id == line["variation_id"] for v in item.variations
                ), f"{script.id}: bad variation {line['variation_id']}"
            valid_modifiers = {
                option.id for group in item.modifier_groups for option in group.options
            }
            for modifier_id in line.get("modifier_ids") or []:
                assert modifier_id in valid_modifiers, (
                    f"{script.id}: modifier {modifier_id} not valid for {item.ref}"
                )


def test_scripts_cover_key_scenarios():
    scripts = {s.id for s in load_scripts(SCRIPTS_DIR)}
    required = {
        "burrito_corrections",      # canonical corrections flow
        "unknown_item_twice_transfer",
        "prompt_injection_free",
        "prompt_injection_discount",
        "hangup_mid_order",
        "huge_order_transfer",
        "sold_out_suggest",
        "required_choice_asked",
        "readback_correction",
        "transfer_request",
        "just_send_it",
    }
    missing = required - scripts
    assert not missing, f"missing golden scripts: {missing}"
