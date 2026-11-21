"""The lane egress guard: a per-lane site list and effect classes, enforced on the node
(docs/design/egress-guard.md, which has the measurements behind every choice here).

What is allowed. A document navigation (top-level or a subframe, from any source) may go to a
host that is inside the lane's site list (when it has one) and inside every egress list of the
grant driving the lane (when an agent drives it and the grant sets one). A `frame:` entry admits
its host only as a subframe (captchas, sign-in helper frames), and `<host>.google.@cc` that host on
Google's country domains (core.models.validate_sites). While a human has
control of the lane, only the lane's list applies. A lane with an empty list, driven by a grant
that sets no list, behaves exactly as before the guard existed.

The rules, and the threat each one covers:

- `lane_sites`: a document of the lane goes only to the lane's own sites. Covers an injected link
  or redirect taking the account's session (and the agent) to another site.
- `grant_egress`: while an agent drives the lane, documents go only inside its grant's egress
  list. Covers an injected instruction making the agent carry data off in a URL (AUD-09).
- `non_get`: while an agent drives the lane, a non-GET request (a form post, a fetch POST) goes
  only where a document may go. Covers exfiltration by form.
- `commit` and `write`: a request matching a commit rule in EFFECT_RULES needs a grant that allows
  commit; a write rule needs draft. Covers a plain click publishing under a grant that may only
  draft (AUD-03). Unknown requests are never blocked: many sites read through POST.
- `ask_first`: while an agent drives a lane whose "Ask me first" is on (openajar.policy.ask_first)
  and no commit gate can hold the request (an act without a route, such as a read or a wait), a
  request matching a commit rule is refused even under a grant that allows commit. During a drive
  the gate asks instead: its authorize reads the same switch, so the publish waits for a person's
  approval of exactly what it sends. An act whose gate route cannot be bound is refused before it
  runs (EngineError), so no act relies on this rule alone. A routine is not held by this rule (it
  asked at its own effect; `routine()`). A commit a person approved in a confirm dialog is not
  refused by this rule for a moment (`person_approved()`), but during a drive the gate holds it
  again on its route, for an approval of what it sends.

How it is enforced, without changing what the browser looks like:

- Before an agent acts: `check_url` (browser_navigate) and `check_action` (browser_act resolves
  from the DOM what a click, an Enter or Space, or a typed newline would follow or submit, the way
  the browser does, see TARGET_JS) refuse before anything is sent. This is prevention. On a lane
  that limits its hosts, what the check cannot read is refused too.
- Every request of the context goes past a `request` listener, which costs nothing and changes
  nothing in the browser. A refused navigation (every redirect hop, popups, frames, meta refresh,
  script) is stopped with `window.stop()` and a refused popup is closed, so the page stays where it
  was. Measured on the node, the request has already reached the host when the listener hears of
  it, so this is containment, not prevention. A refused request that is not a navigation cannot be
  stopped this way; it is recorded, and a commit outside the grant holds the lane.
- Strict lanes (`egress_strict`) add a catch-all route through the lane's RouteDispatcher, only
  while an agent's action runs. It aborts refused requests before they leave, but redirect hops
  are never routed (Juggler) and responses that arrive while the route exists are not stored in
  the HTTP cache. That is why it is off by default. The commit gate's route (below) is the one
  that holds a publish on every lane while an agent drives it.

Every block is written to the lane's egress trace (`lanes/<ws>/<name>/egress-trace.jsonl`, host,
path and method only, never a query or a body), recorded as a lane event, logged, and reported in
the result of the tool call it happened in. Repeats of one block are rate-limited in the log and
the lane events. While a human has control, a refused document also becomes a notice in the live
view (on_notice: host, path, kind, rule and the command that allows it; rate-limited). A commit the
grant does allow is recorded as `observed_commit` (host, path, method and body size; the body's shape
too while the owner observes the lane). While the owner observes a lane (`openajar lane sites <lane>
observe on`, an expiry in `lanes/<ws>/<name>/egress-observe`), every non-GET request is traced as
`observed_request` by its
shape only: the content type, an operation name and the names of the body's fields, never a value.
That is how an effect rule is checked against the real publish request of a throwaway account.

Commits are metered (openajar.policy.meters). browser_act is authorized as a draft, because what
a click does is not known before it; while an agent's act runs (`acting(meter=...)`), a request
that matches a commit rule and that the guard lets go is the click that published, and is charged
as one commit to the acting grant and the lane (`CommitMeter.charge`). Past a commit limit the act
runs under the strict route even on a lane that is not strict, so the publish is aborted before
it leaves (rule `limit_reached`, nothing debited); one that leaves anyway (a redirect hop, a
request the route could not hold) is counted, because it happened, and holds the lane. Requests
outside an act (a page's own later traffic, a person in control, a flow run, which is authorized
at its own effect) are never charged here.

The commit gate (AUD-03, openajar.node.commitgate, docs/design/egress-guard.md section 8). From an
agent's first browser act until control leaves the agent (a person takes over, a pause or a hold,
its claim ends, the lane sleeps or closes: `end_drive`, called by the lane manager), the guard keeps
one route on: a regex over the rule hosts, every path (gate_pattern), which Playwright matches in
its driver, so other hosts (CDNs) never wait for Python and GETs on the rule hosts go on at once. A
non-GET on it is classified fail closed (`_gate_rule`): a commit rule's request, a GraphQL call
with any operation that is not a known read, and a non-GET no rule reads (`<site>.unknown`) go to
the latest act's gate before they leave, and leave only once a person approved exactly what they
publish (or a standing approval bound to that content covers it); they are authorized and metered
as a commit there, so never charged twice. An unknown one that does not carry what an agent typed
on the lane (`note_typed`, in memory only) is held without asking anyone; a telemetry read rule
marked `untyped` stops being a read for a body that carries it (docs/design/egress-guard.md 8.13). Between acts the route judges with the last act's
gate: a page that publishes from a timer, after an upload, or behind a captcha is held all the
same. Any route turns the HTTP cache off for the context while it exists (measured:
scripts/spikes/commit_gate.py), so a drive costs the cache for its length. After a press the act
waits for the non-GETs it started and then for quiet (`settle`), so what the click publishes is
reported by that act. A grant below commit is refused by the `commit` rule before the gate is
asked. A person in control is never gated, charged or aborted. Uploads (draft rules) are
remembered on every path by hash, size and the ids the site answers with, so a publish that names
one binds the file. A publish that arrives by a redirect hop (never routed) during a drive stops the
navigation it carried and holds the lane (the limit is in docs/design/egress-guard.md, 8.6). An act
whose route cannot be bound is refused before it runs. Each classification of a shape no rule lists
is traced as `gate_classified` (its shape, never a value) so a rule can be added. While a flow run
holds the lane (`routine()`) the drive's route lets everything go to the listener: the flow was
authorized at its own effect.

The login window. A password in the vault is the owner's permission to use it, so a login is no
publish to hold. While the vault fills a login field (`login`, `vault_fill`), the typist tells the
guard (`open_login`), and for LOGIN_WINDOW_S after each fill a POST to the entry's registrable domain
that the gate would hold only because no rule reads it goes on, traced as `gate_classified` with the
decision `login`. The body is not matched, because Facebook and Instagram encrypt the password in the
browser. A request a commit or write rule names (any operation of a batch too) is judged as always,
a PUT, PATCH or DELETE stays gated, other domains and other lanes get nothing, and the window closes
on expiry or when control leaves the agent. Nothing else opens one: not a page, a model or a tool
argument (docs/design/egress-guard.md, 8.11).

A keepalive visit (openajar.keepalive.visit) asks for `acting(effects_route=True)` on any lane. The
planner and `--now` never start a visit on a lane an agent drives. Should a visit end a drive anyway,
`restore_drive` binds the gate's route again when the visit ends, before control returns to the agent,
unless a person or a hold has control. For the visit's duration the same route handler is bound on
the gate's pattern in its effects mode, classified as the gate classifies (`_gate_rule`): a write or
commit the visit's read-only grant refuses, and a PUT, PATCH, DELETE or POST that no rule names, is
aborted before it leaves. Nothing is gated or approved for the node. Anything else passes the route
untouched and is judged by the listener as usual, and the route is gone when the visit ends. A visit
whose route cannot be bound fails rather than running.

Effect rules match a request by host, path, method and, for GraphQL, the operation name (from a
header or the body). A rule's `verified` says when it was checked against a throwaway account's
real traffic in observe mode (a canary run of 2026-11-04); the others are unverified.

The package, by module: `constants` (limits and windows), `suggested` (suggested site lists),
`rules` (EffectRule and EFFECT_RULES), `classify` (which rule a request matches, the gate's
pattern), `shapes` (operation names and body fields, the observe window), `sites` (site-list
matching), `verdicts` (Driver, judge, Refusal, Block, FlowCommit), `trace` (EgressTrace), `page_js`
(the press-target scripts), `helpers`, and LaneGuard in `guard` with its `guard_prevent` and
`guard_records` mixins. This module re-exports every name the single file had, private ones too.
A name tests patch is read through its home module (`rules.EFFECT_RULES`, which LaneGuard reads
when it is built, `constants.NOTICE_WINDOW_S`, `sites.registrable_domain`, `sites.same_site`), so
patch it there. Modules outside the package that read `egress.EFFECT_RULES` see the package's copy.
"""

from __future__ import annotations

# What the single module had in scope, kept for attribute reach-through (node/gateshapes.py reads
# egress.globs, cli.py egress.FRAME_PREFIX).
import asyncio  # noqa: F401
import contextlib  # noqa: F401
import json  # noqa: F401
import logging
import os  # noqa: F401
import re  # noqa: F401
import threading  # noqa: F401
import time  # noqa: F401
import weakref  # noqa: F401
from collections import deque  # noqa: F401
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator, Mapping  # noqa: F401
from dataclasses import dataclass, field  # noqa: F401
from datetime import UTC, datetime  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any, Protocol  # noqa: F401
from urllib.parse import unquote, urlsplit  # noqa: F401

from openajar.core import globs  # noqa: F401
from openajar.core.errors import EgressRefused, EngineError, InvalidRequest  # noqa: F401
from openajar.core.models import (  # noqa: F401
    COUNTRY_TOKEN,
    FRAME_PREFIX,
    GOOGLE_COUNTRY_SUFFIXES,
    ActionKind,
    Effect,
    split_site,
    validate_sites,
)
from openajar.core.tools import gate_kind  # noqa: F401
from openajar.node import publish  # noqa: F401
from openajar.node.egress.classify import (  # noqa: F401 - re-exported
    _NAME_TOKEN,
    NEVER_SENT,
    SCAN_MAX,
    _anycase,
    _scan_body,
    classify,
    effects_of,
    gate_pattern,
    never_sent,
    rule_hosts,
    ungated_sites,
    unreviewed_sites,
)
from openajar.node.egress.constants import (  # noqa: F401 - re-exported
    _DOTTED_NAME_RE,
    _MULTIPART_NAME_RE,
    _SHAPE_NAME_RE,
    ACTION_TIMEOUT_MS,
    APPROVED_WINDOW_S,
    ASK_FIRST_TELL_S,
    DOCUMENT_KINDS,
    FLOW_RELOAD_S,
    HIT_EVERY_S,
    LIVE_TYPES,
    LOG_EVERY_S,
    NOTES_KEPT,
    NOTICE_BURST,
    NOTICE_EVERY_S,
    NOTICE_WINDOW_S,
    OBSERVE_EVERY_S,
    OBSERVE_MAX_MINUTES,
    OBSERVE_NAME,
    POPUP_WINDOW_S,
    SAFE_METHODS,
    SETTLE_COLD_AGE_S,
    SETTLE_COLD_S,
    SETTLE_GRACE_S,
    SETTLE_MAX_S,
    SETTLE_QUIET_S,
    SHAPE_DEPTH,
    SHAPE_DOTTED_OP_KEYS,
    SHAPE_HEADERS,
    SHAPE_JSON_FIELDS,
    SHAPE_MAX_FIELDS,
    SHAPE_OP_KEYS,
    SHAPE_PARSE_MAX,
    SITES_TTL_S,
    STOP_TIMEOUT_S,
    TRACE_EVERY_S,
    TRACE_MAX_BYTES,
    TRACE_NAME,
    UNRULED_EVERY_S,
    UPLOAD_HASH_MAX,
    UPLOADS_KEPT,
    WEB_SCHEMES,
)
from openajar.node.egress.guard import (  # noqa: F401 - re-exported
    _GUARDS,
    Events,
    LaneGuard,
    guard_for,
)
from openajar.node.egress.guard_prevent import (  # noqa: F401 - re-exported
    GuardPrevention,
)
from openajar.node.egress.guard_records import (  # noqa: F401 - re-exported
    GuardRecords,
)
from openajar.node.egress.helpers import (  # noqa: F401 - re-exported
    QUIET_SCHEMES,
    SUBMIT_KEYS,
    _in_subframe,
    _path,
    _post_data,
    _press_mode,
    _short,
    _tab_before_newline,
)
from openajar.node.egress.page_js import (  # noqa: F401 - re-exported
    _RESOLVE_JS,
    COVER_JS,
    COVER_POINTS,
    TARGET_JS,
)
from openajar.node.egress.rules import (  # noqa: F401 - re-exported
    _REGISTER_RULE,
    _RULE_ID_RE,
    ABORTED_KEEP,
    EFFECT_KINDS,
    EFFECT_RULES,
    IG_GQL,
    LINKEDIN_BEACON,
    LINKEDIN_RSC,
    LOGIN_WINDOW_S,
    META_GQL,
    REDDIT_GQL,
    SENT_KEEP,
    UNKNOWN,
    EffectRule,
)
from openajar.node.egress.shapes import (  # noqa: F401 - re-exported
    _DOC_ID_KEYS,
    _DOC_ID_RE,
    _DOC_KEYWORD,
    _MUTATION_WORD,
    _PUT_LIKE,
    _QUERY_NAME,
    OP_FIELDS,
    READ_DOC_IDS,
    READ_OPS,
    TypedStream,
    _op_name,
    _shape_paths,
    body_covered,
    body_fields,
    gql_verdict,
    normalise_text,
    operations,
    read_observe,
    request_op,
    request_shape,
    typed_in,
    typed_matches,
    unknown_rule,
    write_observe,
)
from openajar.node.egress.sites import (  # noqa: F401 - re-exported
    _PCT,
    allow_entry,
    host_of,
    in_list,
    in_sites,
    norm_path,
    site_match,
    site_scope,
)
from openajar.node.egress.suggested import (  # noqa: F401 - re-exported
    IDENTITY_SITES,
    PLATFORM_SITES,
    SUGGESTED_SITES,
)
from openajar.node.egress.trace import (  # noqa: F401 - re-exported
    EgressTrace,
)
from openajar.node.egress.verdicts import (  # noqa: F401 - re-exported
    HUMAN_KINDS,
    Block,
    CommitMeter,
    Driver,
    FlowCommit,
    PublishGate,
    Refusal,
    _body_values,
    _ceiling,
    grant_lists,
    judge,
    refusal_message,
)
from openajar.node.routes import GUARD_PATTERN  # noqa: F401
from openajar.vault.domains import registrable_domain, same_site  # noqa: F401

log = logging.getLogger("openajar.node.egress")
