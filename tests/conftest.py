from __future__ import annotations

from pathlib import Path

import pytest

from voiceorder.core.catalog import Catalog

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def catalog() -> Catalog:
    return Catalog.from_json_file(str(FIXTURES_DIR / "menu_taqueria.json"))
