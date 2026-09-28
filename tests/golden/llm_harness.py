from __future__ import annotations

from dataclasses import dataclass

import anthropic

from voiceorder.agent.loop import AgentSession
from voiceorder.core.cart import Cart
from voiceorder.core.catalog import Catalog
from voiceorder.core.tools import OrderTools
from voiceorder.pos_adapters.fake import FakePos

from .harness import ExpectedLine

CALLER_MODEL = "claude-sonnet-5"
END_CALL_MARKER = "[[END CALL]]"


@dataclass
class CallerScript:
    """A persona for a caller LLM to play, and the cart it should end with.
    Mirrors gstack's eval idea: run the agent headless, grade the transcript."""

    name: str
    category: str
    persona: str
    expected_cart: list[ExpectedLine]
    expect_submitted: bool = True


def _caller_line(client: anthropic.Anthropic, persona: str, transcript: list[dict]) -> str | None:
    response = client.messages.create(
        model=CALLER_MODEL,
        max_tokens=300,
        system=(
            f"{persona}\n\nWhen the call is over -- your order is confirmed, or you're "
            f"done for any other reason -- send exactly {END_CALL_MARKER} as your entire "
            "message, with nothing else."
        ),
        messages=transcript,
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    if END_CALL_MARKER in text:
        return None
    return text


def run_golden_call(
    catalog: Catalog, profile: str, script: CallerScript, max_turns: int = 10
) -> OrderTools:
    client = anthropic.Anthropic()
    pos = FakePos(profile=profile, catalog=catalog)
    call_id = f"llm-golden-{script.name}-{profile}"
    tools = OrderTools(cart=Cart(call_id=call_id), catalog=catalog, pos=pos)
    agent = AgentSession(tools=tools, restaurant_name="Taqueria Demo", client=client)

    caller_transcript: list[dict] = []
    agent_reply = agent.send("Hi, thanks for calling, what can I get started for you?")
    caller_transcript.append({"role": "user", "content": agent_reply})

    for _ in range(max_turns):
        caller_line = _caller_line(client, script.persona, caller_transcript)
        if caller_line is None:
            break
        caller_transcript.append({"role": "assistant", "content": caller_line})
        agent_reply = agent.send(caller_line)
        caller_transcript.append({"role": "user", "content": agent_reply})

    return tools


def assert_golden_call(tools: OrderTools, script: CallerScript) -> None:
    actual = [
        ExpectedLine(description=line.describe(), quantity=line.quantity)
        for line in tools.cart.lines
    ]
    assert actual == script.expected_cart, (
        f"[{script.name}] expected {script.expected_cart}, got {actual}"
    )
    was_submitted = len(tools.pos._orders) > 0
    assert was_submitted == script.expect_submitted, (
        f"[{script.name}] expected submitted={script.expect_submitted}, got {was_submitted}"
    )
