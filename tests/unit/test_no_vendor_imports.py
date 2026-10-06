"""Lint rule (Phase 1 gate): core/ must never import a vendor SDK.

The order engine stays vendor-free by construction; this test proves it by
parsing every module under voiceorder/core and rejecting imports that are
neither stdlib nor local.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent / "voiceorder" / "core"

BANNED_TOP_LEVELS = {
    "squareup", "square",       # Square SDK
    "clover",                    # Clover SDK
    "toast",                     # Toast API client
    "elevenlabs",                # ElevenLabs
    "vapi",                      # Vapi
    "twilio",                    # Twilio
    "stripe", "adyen",           # payments (card data never touches us)
}


def imported_top_levels(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: local by definition
                continue
            if node.module:
                found.add(node.module.split(".")[0])
    return found


def test_core_imports_no_vendor_sdk():
    stdlib = set(sys.stdlib_module_names)
    offenders: list[str] = []
    for path in sorted(CORE_DIR.rglob("*.py")):
        for mod in sorted(imported_top_levels(path)):
            if mod in BANNED_TOP_LEVELS:
                offenders.append(f"{path.name}: banned vendor import '{mod}'")
            elif mod not in stdlib and mod != "voiceorder":
                offenders.append(f"{path.name}: non-stdlib import '{mod}'")
    assert not offenders, "core/ must stay vendor-free:\n" + "\n".join(offenders)


def test_banned_list_covers_known_vendors():
    assert {"squareup", "elevenlabs", "vapi", "twilio"} <= BANNED_TOP_LEVELS
