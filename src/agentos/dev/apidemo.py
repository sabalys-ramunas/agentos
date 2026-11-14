"""Serve the real API and the built panel against in-memory fakes, for a look without a node.

    uv run python -m agentos.dev.apidemo            # http://localhost:7401, prints a sign-in link
"""

from __future__ import annotations

import argparse

from agentos.api.app import ApiSettings, build_api
from agentos.api.auth import PanelStore, new_login_code
from agentos.dev.apifakes import demo_runtime


def main() -> None:
    parser = argparse.ArgumentParser(description="agentos panel demo (fake services)")
    parser.add_argument("--port", type=int, default=7401)
    parser.add_argument("--code", help="use this sign-in code instead of a random one (tests)")
    args = parser.parse_args()

    import uvicorn

    from agentos.node import vnc_relay

    vnc_relay.TEST_TCP_UPSTREAM = True  # the fakes' live views are TCP ports; this server runs no node
    store = PanelStore(None)
    code = args.code or new_login_code()
    store.add_code(code, ttl_seconds=3600)
    app = build_api(demo_runtime(), ApiSettings(store=store, owner_token="demo-owner-token"))
    print(f"Sign in: http://localhost:{args.port}/login#code={code}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
