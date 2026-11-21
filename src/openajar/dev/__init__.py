"""A fake openajar node on a laptop: `openajar dev up` (docs/DEVELOPING.md, "Run it on your laptop").

The real daemon composition runs (policy, vault, runner, brains, workspaces, the panel and MCP) over
fakes for the parts that need a browser, a display or a network: lanes (openajar.dev.lanes, the full
LaneManager contract), tabs (openajar.dev.web, pages with snapshots, refs, actions and a captcha) and
live views (openajar.dev.display, a VNC server per lane that shows the tab). Nothing here starts a
browser, calls a model, or listens on anything but 127.0.0.1.
"""
