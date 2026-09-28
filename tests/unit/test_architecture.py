from __future__ import annotations

import ast
import sys
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent.parent / "voiceorder" / "core"
ALLOWED_ROOTS = set(sys.stdlib_module_names) | {"__future__"}


def _absolute_import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_core_imports_no_vendor_sdk():
    """core/ talks to the LLM and the POS only through ports.py; it must never
    import a vendor SDK (fastapi, a POS client, a voice-platform client, ...)."""
    violations = {}
    for path in sorted(CORE_DIR.glob("*.py")):
        extra = _absolute_import_roots(path) - ALLOWED_ROOTS
        if extra:
            violations[path.name] = extra
    assert not violations, f"voiceorder/core must import only stdlib, found: {violations}"
