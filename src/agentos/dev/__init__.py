"""A fake agentos node on a laptop: `agentos dev up` (docs/DEVELOPING.md, "Run it on your laptop").

The real daemon composition runs (policy, vault, runner, brains, workspaces, the panel and MCP) over
fakes for the parts that need a browser, a display or a network: lanes (agentos.dev.lanes, the full
LaneManager contract), tabs (agentos.dev.web, pages with snapshots, refs, actions and a captcha) and
live views (agentos.dev.display, a VNC server per lane that shows the tab). Nothing here starts a
browser, calls a model, or listens on anything but 127.0.0.1.
"""
