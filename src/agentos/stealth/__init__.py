"""Stealth: whether a lane looks like one ordinary person's browser, and stays safe to use.

Modules: lanecheck (`agentos lane check` and the `lane_check` tool: proxy, exit IP and location,
WebRTC and DNS leaks, profile and pin, signed in, the expected account, holds), stealth (the
findings on pinned devices and on what a page's own script sees; `agentos lane audit`, with
stealth_probe.html), stealthcheck (`agentos lane stealth-check`: scratch lanes on a test slot),
fingerprint (browser version drift and `agentos lane refresh-fingerprint`), tools (the MCP tool),
redteam (`agentos lane redteam`: a detector site and detectors that try to catch our own lanes;
docs/design/redteam.md). docs/design/stealth.md says what each finding means.
"""
