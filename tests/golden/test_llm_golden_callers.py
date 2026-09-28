from __future__ import annotations

import os

import pytest

from voiceorder.pos_adapters.fake import PROFILES

from .callers import CALLER_SCRIPTS
from .llm_harness import assert_golden_call, run_golden_call

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="needs ANTHROPIC_API_KEY: this is the deliberate, paid LLM tier (build plan section 7)",
)


@pytest.mark.parametrize("profile", list(PROFILES))
@pytest.mark.parametrize("script", CALLER_SCRIPTS, ids=lambda s: s.name)
def test_llm_golden_caller(catalog, profile, script):
    tools = run_golden_call(catalog, profile, script)
    assert_golden_call(tools, script)
