"""The egress guard's limits, windows and the request-shape parser's keys."""

from __future__ import annotations

import re

TRACE_NAME = "egress-trace.jsonl"
TRACE_MAX_BYTES = 4 * 1024 * 1024
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
WEB_SCHEMES = ("http", "https", "ws", "wss")
NOTES_KEPT = 50
LOG_EVERY_S = 60.0       # one log line and one lane event per (rule, host) a minute, with a count
TRACE_EVERY_S = 1.0      # one trace row per (rule, host) a second, with a count
SITES_TTL_S = 2.0        # how long a site list read from the store is trusted
POPUP_WINDOW_S = 3.0     # a popup opening this soon after its refused request is closed
STOP_TIMEOUT_S = 5.0
APPROVED_WINDOW_S = 30.0  # what a confirm dialog a person approved sends, right after it is answered
FLOW_RELOAD_S = 10.0     # a flow that starts during an agent's drive reloads its page first, at most this long
ASK_FIRST_TELL_S = 3600.0  # one Needs you card a lane an hour for publishes `ask_first` refused
NOTICE_EVERY_S = 30.0    # one live-view notice per (rule, host, kind) every 30 s while a human has control
NOTICE_BURST = 5         # and at most 5 notices a lane in any 30 s
NOTICE_WINDOW_S = 30.0
# the block kinds that are documents (a notice explains them to the human in the live view)
DOCUMENT_KINDS = frozenset({"navigation", "subframe", "popup", "redirect", "committed"})
ACTION_TIMEOUT_MS = 1500
OBSERVE_NAME = "egress-observe"  # beside the trace: an expiry (epoch seconds) while the owner observes
OBSERVE_MAX_MINUTES = 240
OBSERVE_EVERY_S = 1.0    # one observed_request row per (host, path, method, operation, field set) a second
HIT_EVERY_S = 1.0        # one rule_hit row per write rule a second (rule health: when a rule last matched)
UNRULED_EVERY_S = 60.0   # one unruled_request row per shape a minute (a lane's own host no rule names)
SHAPE_PARSE_MAX = 256 * 1024   # bodies larger than this (uploads) are sized, not parsed
SETTLE_QUIET_S = 2.0     # after an agent's press that sent a non-GET: wait for this much quiet...
SETTLE_MAX_S = 10.0      # ...at most this long, so a publish that lags the click (a captcha first) is gated
SETTLE_GRACE_S = 0.25    # and always this long after a press: the browser tells the driver of a request late
# ...or up to this long while the browser has not yet told the driver of any fetch since it launched:
# Camoufox's content blocker (uBlock Origin) holds a page's requests until its filter lists load, about
# 1.5 s after launch, so the request a cold first click sent reaches the driver 1.2 s late (measured:
# scripts/spikes/commit_gate.py `first`). The wait ends as soon as the held requests arrive.
SETTLE_COLD_S = 2.5
# ...and only once a browser, for its first press, while it is younger than this (the lists load in
# about 1.5 s): a page that never fetches does not make every act wait SETTLE_COLD_S
SETTLE_COLD_AGE_S = 5.0
LIVE_TYPES = frozenset({"fetch", "xhr", "beacon"})  # a request the content blocker would have held
UPLOADS_KEPT = 20        # uploads remembered per lane, so a publish naming one binds its hash
UPLOAD_HASH_MAX = 64 * 1024 * 1024
SHAPE_MAX_FIELDS = 80
SHAPE_DEPTH = 6
# headers that name an operation (never a value the user typed)
SHAPE_HEADERS = ("x-fb-friendly-name", "x-apollo-operation-name", "x-graphql-operation-name",
                 "graphql-operation", "x-restli-method")
# body keys that name an operation
SHAPE_OP_KEYS = ("operationName", "operation", "fb_api_req_friendly_name", "queryName")
# form fields whose value is JSON: their own field names are part of the shape (Meta's `variables`)
SHAPE_JSON_FIELDS = ("variables", "variables_json", "input")
_SHAPE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:@$-]{1,80}$")
# a body key whose value names the operation only when it is a dotted name (LinkedIn's web client
# posts every action to one URL with requestId "com.linkedin.sdui.requests...."); never a uuid
SHAPE_DOTTED_OP_KEYS = ("requestId",)
_DOTTED_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$]*(\.[A-Za-z0-9_$]+){2,}$")
_MULTIPART_NAME_RE = re.compile(rb'Content-Disposition:[^\r\n]*?\bname="([^"\r\n]{1,80})"', re.IGNORECASE)
