"""The panel and app HTTP API (docs/API.md) and the built web panel.

    app = build_api(runtime)            # Starlette; serve with uvicorn next to the MCP endpoint
    runtime.events.publish("lane", lane)  # live updates over GET /api/events
    create_login_code(home).url         # what `openajar panel login` prints
"""

from openajar.api.app import ApiSettings, build_api
from openajar.api.auth import LoginLink, PanelStore, create_login_code
from openajar.api.events import Event, EventBus

__all__ = ["ApiSettings", "Event", "EventBus", "LoginLink", "PanelStore", "build_api", "create_login_code"]
