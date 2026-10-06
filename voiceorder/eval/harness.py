"""Harness: run one golden script (scripted caller vs. agent) and grade it."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..agent.llm import LLMClient
from ..agent.loop import AgentSession
from ..core.cart import Cart
from ..core.catalog import Catalog
from ..core.ports import RestaurantContext
from ..pos_adapters.fake import PROFILES, FakePos
from ..voice_adapters.text import TextAdapter
from .grader import GradeResult, grade


@dataclass
class Script:
    id: str
    description: str = ""
    persona: str = ""
    pos_profiles: list[str] = field(default_factory=lambda: sorted(PROFILES))
    pos_setup: dict = field(default_factory=dict)
    turns: list[dict] = field(default_factory=list)
    expected_cart: list[dict] = field(default_factory=list)
    expected_submit: bool = False
    expected_transfer: bool = False
    expected_total: float | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "Script":
        return cls(
            id=raw["id"],
            description=raw.get("description", ""),
            persona=raw.get("persona", ""),
            pos_profiles=list(raw.get("pos_profiles", sorted(PROFILES))),
            pos_setup=dict(raw.get("pos_setup", {})),
            turns=list(raw.get("turns", [])),
            expected_cart=list(raw.get("expected_cart", [])),
            expected_submit=bool(raw.get("expected_submit", False)),
            expected_transfer=bool(raw.get("expected_transfer", False)),
            expected_total=raw.get("expected_total"),
        )


def load_scripts(directory: str | Path) -> list[Script]:
    scripts = []
    for path in sorted(Path(directory).glob("*.yaml")):
        with open(path) as fh:
            raw = yaml.safe_load(fh)
        scripts.append(Script.from_dict(raw))
    return scripts


class ScriptedCaller:
    """Deterministic caller: emits the script's utterances verbatim, then hangs up."""

    def __init__(self, script: Script):
        self._turns = list(script.turns)

    def next_utterance(self, transcript: list[dict]) -> str | None:
        if not self._turns:
            return None  # hang up
        turn = self._turns.pop(0)
        if turn.get("hangup"):
            return None
        return str(turn.get("caller", ""))


class CallerLLM:
    """LLM-driven caller with a persona and script (needs an LLM key)."""

    def __init__(self, llm: LLMClient, script: Script, model: str):
        self.llm = llm
        self.script = script
        self.model = model
        self.system = (
            f"You are a restaurant customer calling in a pickup order. Persona: {script.persona}\n"
            f"Your scripted goal for this call: {script.description}\n"
            "Speak like a real caller: short, casual, sometimes vague. "
            "Answer the agent's questions to complete your goal. "
            "When your goal is done (or you want to hang up), reply with exactly: HANGUP"
        )

    def next_utterance(self, transcript: list[dict]) -> str | None:
        messages = [
            {"role": "user" if t["role"] == "agent" else "assistant", "content": t["text"]}
            for t in transcript
            if t["role"] in ("caller", "agent")
        ]
        # NOTE: roles are flipped so the caller LLM speaks as the user.
        response = self.llm.complete(
            system=self.system, messages=messages, tools=[], model=self.model
        )
        text = response.text.strip()
        return None if text == "HANGUP" else text


def make_context(profile: str, restaurant_name: str = "Taqueria Demo") -> RestaurantContext:
    return RestaurantContext(
        restaurant_id="taqueria-demo",
        restaurant_name=restaurant_name,
        pos_profile=profile,
        voice_platform="text",
        transfer_number="+15550134200",
    )


def run_script(
    script: Script,
    *,
    profile: str,
    llm: LLMClient,
    model: str,
    catalog: Catalog,
    outdir: str | Path,
    run_index: int = 0,
    caller_kind: str = "scripted",
) -> dict:
    """Run one script once. Returns a result record; saves the transcript."""
    setup = script.pos_setup or {}
    pos = FakePos(
        profile=profile,
        catalog=catalog,
        mode=setup.get("mode", "ok"),
        sold_out=tuple(setup.get("sold_out", [])),
    )
    ctx = make_context(profile)
    cart = Cart(
        cart_id=uuid.uuid4().hex[:12],
        restaurant_id=ctx.restaurant_id,
        idempotency_key=uuid.uuid4().hex,
    )
    adapter = TextAdapter()
    greeting = adapter.call_start_response(ctx)["greeting"]
    session = AgentSession(llm, catalog, pos, ctx, cart, model)
    caller = (
        CallerLLM(llm, script, model)
        if caller_kind == "llm"
        else ScriptedCaller(script)
    )

    transcript: list[dict] = [{"role": "agent", "text": greeting, "tool_calls": []}]
    while True:
        utterance = caller.next_utterance(transcript)
        if utterance is None:
            transcript.append({"role": "system", "text": "caller hung up", "tool_calls": []})
            break
        transcript.append({"role": "caller", "text": utterance, "tool_calls": []})
        agent_turn = session.handle_caller_message(utterance)
        transcript.append(
            {
                "role": "agent",
                "text": agent_turn["text"],
                "tool_calls": agent_turn["tool_calls"],
            }
        )
        if cart.transferred or cart.state.value == "submitted":
            break

    result: GradeResult = grade(script.__dict__, cart, transcript)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    transcript_path = outdir / f"{script.id}__{profile}__run{run_index}.json"
    transcript_path.write_text(
        json.dumps(
            {
                "script": script.id,
                "profile": profile,
                "run": run_index,
                "passed": result.passed,
                "failures": result.failures,
                "final_cart": [line.to_dict() for line in cart.lines],
                "cart_state": cart.state.value,
                "transferred": cart.transferred,
                "transcript": transcript,
            },
            indent=2,
        )
    )
    return {
        "script": script.id,
        "profile": profile,
        "run": run_index,
        "passed": result.passed,
        "failures": result.failures,
        "transcript": str(transcript_path),
    }
