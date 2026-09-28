from __future__ import annotations

import argparse

from .agent.loop import AgentSession
from .api.main import RESTAURANTS
from .core.cart import Cart
from .core.catalog import Catalog
from .core.tools import OrderTools
from .pos_adapters.fake import FakePos


def main() -> None:
    parser = argparse.ArgumentParser(description="Terminal chat with the order-taking agent")
    parser.add_argument(
        "--number",
        default="+15551234567",
        help="restaurant phone number to simulate calling (default: the demo taqueria)",
    )
    args = parser.parse_args()

    restaurant = RESTAURANTS[args.number]
    catalog = Catalog.from_json_file(str(restaurant.catalog_path))
    pos = FakePos(profile=restaurant.pos_profile, catalog=catalog)
    tools = OrderTools(cart=Cart(call_id="terminal-chat"), catalog=catalog, pos=pos)
    session = AgentSession(tools=tools, restaurant_name=restaurant.name)

    print(f"Connected to {restaurant.name}. Type 'quit' to hang up.")
    while True:
        try:
            caller_message = input("you> ").strip()
        except EOFError:
            break
        if not caller_message:
            continue
        if caller_message.lower() in {"quit", "exit"}:
            break
        print(f"agent> {session.send(caller_message)}")


if __name__ == "__main__":
    main()
