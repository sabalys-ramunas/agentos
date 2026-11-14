"""The panel and app HTTP API (docs/API.md) and the built web panel.

    app = build_api(runtime)            # Starlette; serve with uvicorn next to the MCP endpoint
    runtime.events.publish("lane", lane)  # live updates over GET /api/events
    create_login_code(home).url         # what `agentos panel login` prints
"""

from agentos.api.app import ApiSettings, build_api
from agentos.api.auth import LoginLink, PanelStore, create_login_code
from agentos.api.events import Event, EventBus

__all__ = ["ApiSettings", "Event", "EventBus", "LoginLink", "PanelStore", "build_api", "create_login_code"]
