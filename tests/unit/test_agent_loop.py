"""Agent loop mechanics with a scripted fake LLM (no API key needed)."""
import uuid
from pathlib import Path

from voiceorder.agent.llm import LLMResponse
from voiceorder.agent.loop import AgentSession
from voiceorder.core import tools
from voiceorder.core.cart import Cart
from voiceorder.core.catalog import Catalog
from voiceorder.core.ports import RestaurantContext
from voiceorder.pos_adapters.fake import FakePos

FIXTURE = Path(__file__).resolve().parents[2] / "voiceorder" / "fixtures" / "menu_taqueria.json"


class FakeLLM:
    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def complete(self, *, system, messages, tools, model):
        self.calls.append(
            {"system": system, "n_messages": len(messages), "tools": tools, "model": model}
        )
        return self._responses.pop(0)


def _session(responses: list[LLMResponse]) -> tuple[AgentSession, FakeLLM]:
    catalog = Catalog.from_json(FIXTURE)
    ctx = RestaurantContext(
        restaurant_id="taqueria-demo",
        restaurant_name="Taqueria Demo",
        pos_profile="square_like",
        voice_platform="text",
        transfer_number="+15550134200",
    )
    pos = FakePos(profile="square_like", catalog=catalog)
    cart = Cart(
        cart_id="testcart", restaurant_id=ctx.restaurant_id, idempotency_key=uuid.uuid4().hex
    )
    llm = FakeLLM(responses)
    return AgentSession(llm, catalog, pos, ctx, cart, model="fake-model"), llm


def _call(name: str, arguments: dict) -> LLMResponse:
    return LLMResponse(tool_calls=[{"id": "c1", "name": name, "arguments": arguments}])


def test_loop_runs_tool_calls_then_replies():
    session, llm = _session(
        [
            _call("search_menu", {"query": "chicken taco"}),
            _call("add_item", {"item_ref": "T1", "quantity": 2}),
            LLMResponse(text="Got it, two chicken tacos."),
        ]
    )
    turn = session.handle_caller_message("two chicken tacos please")
    assert turn["text"] == "Got it, two chicken tacos."
    assert [c["name"] for c in turn["tool_calls"]] == ["search_menu", "add_item"]
    assert all(c["ok"] for c in turn["tool_calls"])
    assert len(session.cart.lines) == 1
    assert session.cart.lines[0].item_ref == "T1"
    assert session.cart.lines[0].quantity == 2
    assert len(llm.calls) == 3  # one LLM call per loop iteration


def test_loop_passes_all_tool_schemas_and_system_prompt():
    session, llm = _session([LLMResponse(text="hi")])
    session.handle_caller_message("hi")
    call = llm.calls[0]
    assert [t["name"] for t in call["tools"]] == tools.TOOL_NAMES
    assert "Taqueria Demo" in call["system"]
    assert call["model"] == "fake-model"


def test_loop_surfaces_tool_errors_to_llm():
    session, llm = _session(
        [
            _call("add_item", {"item_ref": "NOPE"}),
            LLMResponse(text="I couldn't find that, what else?"),
        ]
    )
    turn = session.handle_caller_message("one nope please")
    assert turn["tool_calls"][0]["ok"] is False
    assert turn["tool_calls"][0]["error_code"] == "unknown_item"
    assert turn["text"] == "I couldn't find that, what else?"
    assert session.cart.is_empty()
