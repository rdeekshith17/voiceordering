"""Shared fixtures for the VoiceOrderAI test suite."""
from __future__ import annotations

import os

# Hermetic POS profile: the suite must never touch a real vendor account.
# _load_dotenv() does not override existing env vars, so setting this here
# (before any test module imports voiceorder.api.main) forces the fake
# adapter even when the developer's .env points at production
# Square/Toast/Clover. The real adapters are covered by their own mocked
# unit tests.
os.environ.setdefault("POS_PROFILE", "square_like")
# Hermetic restaurant identity: greeting/SMS copy in tests must not depend
# on the developer's real .env.
os.environ.setdefault("RESTAURANT_NAME", "Taqueria Demo")
# Hermetic database: importing voiceorder.api.main seeds the default tenant
# into this DB -- it must never be the production database.
os.environ.setdefault("VOICEORDER_DB", f"/tmp/voiceorder_test_{os.getpid()}.db")
# Hermetic tenant key: tests must never read/write the production key file
# (and therefore can never decrypt production tenant secrets).
os.environ.setdefault(
    "TENANT_MASTER_KEY", "mJR3GX77FPHPjJwFdiG2mHaqvs_qdwccrDkPZ_iinec="
)

from pathlib import Path

import pytest

from voiceorder.core.cart import Cart
from voiceorder.core.catalog import Catalog
from voiceorder.core.ports import RestaurantContext
from voiceorder.pos_adapters.fake import PROFILES, FakePos

FIXTURE = Path(__file__).resolve().parent.parent / "voiceorder" / "fixtures" / "menu_taqueria.json"


@pytest.fixture(scope="session")
def catalog() -> Catalog:
    return Catalog.from_json(FIXTURE)


@pytest.fixture()
def ctx() -> RestaurantContext:
    return RestaurantContext(
        restaurant_id="taqueria-demo",
        restaurant_name="Taqueria Demo",
        pos_profile="square_like",
        voice_platform="text",
        transfer_number="+15550134200",
    )


@pytest.fixture(params=sorted(PROFILES))
def pos(request, catalog) -> FakePos:
    return FakePos(profile=request.param, catalog=catalog)


@pytest.fixture()
def cart(ctx) -> Cart:
    return Cart(cart_id="cart_test", restaurant_id=ctx.restaurant_id, idempotency_key="key_test")
