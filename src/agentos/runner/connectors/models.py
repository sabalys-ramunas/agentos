"""The connector document: one JSON file per site, data only (docs/design/connectors.md 4.2).

    {"connector": "instagram", "version": "1.4.0", "site": "instagram", "domain": "instagram.com",
     "account_languages": ["en", "lt"],
     "operations": {"post_photo": {"title": ..., "effect": "commit", "input": {...}, "output": {...},
                    "acting_as": {"kind": "account"}, "pace": {...}, "signals": {...},
                    "commit": {...}, "readback": {...}, "discard": [...], "steps": [...]}}}

These models check the shape of each part. The strict rules that need the whole operation
(the commit path, outputs, effects, hosts, fixtures) are in lint.py.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from agentos.core.models import LANE_ID_RE, ActsAs, Effect, Surface
from agentos.runner.connectors.steps import LANG_RE, SAVE_AS_RE, _regex, check_locator, validate_step
from agentos.runner.flowdoc import SendsSpec
from agentos.runner.names import PARAM_NAME_RE, RESERVED_PARAMS

LEARNED_SUFFIX = "-profile"  # a learned connector's key: <site>-profile (agentos.profiles.learn)
CONNECTOR_KEY_RE = re.compile(r"^[a-z]([a-z0-9-]{0,29}[a-z0-9])?$")
OPERATION_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
SIGNAL_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
OUTPUT_PATH_RE = re.compile(r"^[a-z_][a-z0-9_]{0,40}(\.[A-Za-z0-9_-]{1,60}){0,4}$")
GATE_FIELD_RE = re.compile(r"^(form|json|multipart|query|count):[A-Za-z0-9_.\[\]-]{1,80}$")
VAULT_REF_RE = re.compile(r"^vault:[a-z0-9][a-z0-9-]{0,40}/[A-Za-z0-9_.-]{1,80}$")
# hidden params only the connector layer fills from the binding, never the caller
BINDING_PARAMS = ("acts_as_id", "account_language")
V1_EFFECTS = (Effect.READ, Effect.DRAFT, Effect.COMMIT)
ACTING_AS_KINDS = ("account", "fb_page", "li_company", "ig_business", "lane")
WEB_SITE = "web"  # the generic site of a site API on a domain agentos.node.sites has no row for
# connector keys that would make <site>_<op> tool names clash with the node's own tools
RESERVED_KEYS = frozenset({"approval", "brain", "brains", "browser", "connector", "connectors", "env", "events",
                           "files", "flow", "flows", "grant", "grants", "howto", "human", "image", "images",
                           "lane", "lanes", "login", "memory", "policy", "run", "runs", "schedule", "service",
                           "shell", "site", "site-api", "site-apis", "sites", "surface", "surfaces", "traces",
                           "trigger", "triggers", "vault", "web", "whoami", "workspace", "workspaces"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def check_object_schema(v: Any, what: str) -> dict[str, Any]:
    if not isinstance(v, dict) or v.get("type", "object") != "object":
        raise ValueError(f"{what} must be a JSON schema of type object")
    v = {"type": "object", **v}
    try:
        Draft202012Validator.check_schema(v)
    except SchemaError as e:
        raise ValueError(f"{what} is not a valid JSON schema: {e.message}") from e
    props = v.get("properties", {})
    if not isinstance(props, dict):
        raise ValueError(f"{what}.properties must be an object")  # noqa: TRY004 - pydantic wants ValueError
    for name in v.get("required", []):
        if name not in props:
            raise ValueError(f"{what}: required {name!r} is not declared in properties")
    return v


class ActingAs(_Strict):
    """Who the operation acts as, and how that is read from structure only.

    account      the lane's own account: the site's identity cookie (agentos.node.sites), or, on
                 a site with no identity cookie (LinkedIn), the page URL (`source: url`) or an
                 element's href (`source: href`) through `pattern`, like an asset
    fb_page, li_company, ig_business
                 an asset the account administers: its id read from the page URL
                 (`source: url`) or from an element's href (`source: href`, with `locator`),
                 through `pattern` (one capture group). The expected id comes from the
                 binding (the hidden `acts_as_id` param), never from the caller.
    lane         the lane's own session, with no identity to read (a site API on a site with
                 no row in agentos.node.sites; lint refuses it anywhere else)
    """

    kind: Literal["account", "fb_page", "li_company", "ig_business", "lane"]
    source: Literal["cookie", "url", "href"] | None = None
    pattern: str | None = None
    locator: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check(self) -> ActingAs:
        if self.kind == "lane":
            if self.source or self.pattern or self.locator:
                raise ValueError("acting_as lane has no identity to read; it takes no source, pattern or locator")
            return self
        if self.kind == "account" and self.source in (None, "cookie"):
            if self.pattern or self.locator:
                raise ValueError("acting_as account reads the site's identity cookie; it takes no pattern or "
                                 "locator (set source url or href for a site with no identity cookie)")
            self.source = "cookie"
            return self
        if self.kind == "account" and not self.pattern:
            raise ValueError("acting_as account reads the site's identity cookie, or (on a site with none) the "
                             "url or an href through a pattern with one capture group: set pattern")
        if self.source not in ("url", "href") or not self.pattern:
            raise ValueError(f"acting_as {self.kind} reads the asset id from the url or an href: "
                             "set source (url or href) and pattern (one capture group)")
        _regex(self.pattern, "acting_as pattern", groups=1)
        if (self.source == "href") != (self.locator is not None):
            raise ValueError("source href needs the locator of the element whose href carries the id "
                             "(and source url takes none)")
        if self.locator is not None:
            check_locator(self.locator)
        return self


class Pace(_Strict):
    """Per-operation defaults; policy enforces the stricter of these and the lane's limits."""

    min_gap_minutes: int | None = Field(default=None, ge=0, le=10_080)
    per_day: int | None = Field(default=None, ge=1, le=1000)
    per_hour: int | None = Field(default=None, ge=1, le=1000)
    per_week: int | None = Field(default=None, ge=1, le=5000)
    active_hours: bool = True
    max_items: int | None = Field(default=None, ge=1, le=500)


class Signals(_Strict):
    """benign: interstitials the operation knows how to pass; hold: signals that stop the run
    and hold the lane. Anything not listed counts as hold."""

    benign: list[str] = Field(default_factory=list)
    hold: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Signals:
        for name in self.benign + self.hold:
            if not SIGNAL_RE.match(name):
                raise ValueError(f"signal name {name!r} must be lowercase_with_underscores")
        both = set(self.benign) & set(self.hold)
        if both:
            raise ValueError(f"signals cannot be both benign and hold: {', '.join(sorted(both))}")
        return self


class CommitRequest(_Strict):
    """The site's row in the AUD-03 commit gate: how to recognize the publish request and
    pull text and media out of its body."""

    method: Literal["POST", "PUT", "PATCH"]
    url: str
    # the operation name (a regex) when the site sends many operations to one URL (a GraphQL
    # client, LinkedIn's server requests): the name the egress guard reads (agentos.node.egress)
    op: str | None = None
    text: str | None = None
    media_count: str | None = None
    # body fields every value of which must match an anchored regex (`json:` or `form:` paths,
    # `[]` for each element of a list): the direction of a request that also undoes, like a
    # join's subscribeState. The egress guard refuses a run's request where one does not match
    # (agentos.node.egress.FlowCommit), so the operation cannot send its own undo
    match: dict[str, str] | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not v.startswith("^https://"):
            raise ValueError("the request url is an anchored regex starting with ^https://")
        _regex(v, "request url")
        return v

    @field_validator("op")
    @classmethod
    def _op(cls, v: str | None) -> str | None:
        if v is not None:
            if not v.startswith("^") or not v.endswith("$"):
                raise ValueError("the request op is an anchored regex (^...$) of the operation name")
            _regex(v, "request op")
        return v

    @field_validator("match")
    @classmethod
    def _match(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return v
        if not v:
            raise ValueError("match names at least one body field (or is left out)")
        for path, rx in v.items():
            if not GATE_FIELD_RE.match(path) or not path.startswith(("json:", "form:")):
                raise ValueError(f"match: {path!r} is a json:<path> or form:<name> field")
            if not rx.startswith("^") or not rx.endswith("$"):
                raise ValueError(f"match: the pattern for {path} is an anchored regex (^...$)")
            _regex(rx, f"match {path}")
        return v

    @field_validator("text", "media_count")
    @classmethod
    def _field(cls, v: str | None) -> str | None:
        if v is not None and not GATE_FIELD_RE.match(v):
            raise ValueError("a request field is <form|json|multipart|query|count>:<name>")
        return v


class PreflightCheck(_Strict):
    """A check run right before the commit step.

    value_equals  the element's value equals a shown param (after whitespace normalization)
    count         the number of matches equals `equals` (one image attached)
    text_differs  exactly one element is at the locator and its text is not `text` (a template
                  of shown params, "${headline}"): an edit that would set the value already there
                  stops before the commit, as already_current. `lines` compares with line breaks
                  kept (an About section)
    acting_as     the operation's acting_as check, again, right before the commit
    """

    kind: Literal["value_equals", "count", "text_differs", "acting_as"]
    locator: dict[str, Any] | None = None
    param: str | None = None
    equals: int | None = Field(default=None, ge=0, le=100)
    text: str | None = Field(default=None, min_length=1, max_length=500)
    lines: bool = False

    @model_validator(mode="after")
    def _check(self) -> PreflightCheck:
        if self.kind == "acting_as":
            if self.locator or self.param or self.equals is not None or self.text is not None or self.lines:
                raise ValueError("an acting_as check takes nothing: it repeats the operation's acting_as")
            return self
        if (self.kind == "text_differs") != (self.text is not None):
            raise ValueError("text_differs compares the element's text with `text`, and only it takes text")
        if self.kind == "text_differs" and (self.param is not None or self.equals is not None):
            raise ValueError("text_differs takes text (a template of params), not param or equals")
        if self.lines and self.kind != "text_differs":
            raise ValueError("lines belongs to a text_differs check (compare with line breaks kept)")
        if self.locator is None:
            raise ValueError(f"a {self.kind} check needs a locator")
        check_locator(self.locator)
        if self.kind == "value_equals" and (self.param is None or self.equals is not None):
            raise ValueError("value_equals compares the element's value with one shown param: set param")
        if self.kind == "count" and (self.equals is None or self.param is not None):
            raise ValueError("count needs equals (the expected number of matches)")
        return self


class Commit(_Strict):
    step: str = Field(min_length=1, max_length=60)  # the label of the only step that may publish
    preflight: list[PreflightCheck] = Field(default_factory=list)
    request: CommitRequest | None = None
    # strict scope (agentos.node.egress.FlowCommit strict): the run's page sends no non-GET on the
    # rule host but the declared request, an upload or a known read. A learned operation sets it.
    # Left out (None) it is not compiled, so a connector that never says keeps its hashes.
    strict: bool | None = None


class LiveReadback(_Strict):
    """What proves the result, and which outputs it yields: `outputs` maps an output name to
    a saved value ('post.attributes.href', 'posts.count', 'comments')."""

    save_as: str | None = Field(default=None, pattern=SAVE_AS_RE.pattern)
    outputs: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> LiveReadback:
        for name, path in self.outputs.items():
            if not PARAM_NAME_RE.match(name):
                raise ValueError(f"output name {name!r} must be lowercase_with_underscores")
            if not OUTPUT_PATH_RE.match(path):
                raise ValueError(f"output {name}: {path!r} must be a saved name and fields, like 'post.url'")
        return self


class PublicCheck(_Strict):
    """What the signed-out check (AUD-22) opens and looks for. `url`: a saved value holding the
    object's permalink ('post.url', 'post.attributes.href'). `text`: the words a visitor must see
    there, a saved value ('post.text') or a param ('param:caption')."""

    url: str = Field(pattern=OUTPUT_PATH_RE.pattern)
    text: str = Field(pattern=r"^(param:[a-z][a-z0-9_]{0,40}|" + OUTPUT_PATH_RE.pattern[1:-1] + ")$")


class ReadbackContract(_Strict):
    live: LiveReadback | None = None
    public: Literal["cookieless"] | None = None  # AUD-22: visible to a signed-out visitor
    public_check: PublicCheck | None = None  # default: the live readback's saved url and text

    @model_validator(mode="after")
    def _check(self) -> ReadbackContract:
        if self.public_check is not None and self.public is None:
            raise ValueError("public_check needs public: cookieless")
        if self.public and self.public_check is None and not (self.live and self.live.save_as):
            raise ValueError("public: cookieless needs readback.live.save_as (its url and text are checked "
                             "signed out) or a public_check naming them")
        return self

    def signed_out_check(self) -> dict[str, str] | None:
        """{"url", "text"} for the runner, or None when the operation has no signed-out check."""
        if self.public is None:
            return None
        if self.public_check is not None:
            return {"url": self.public_check.url, "text": self.public_check.text}
        save_as = self.live.save_as if self.live else None
        return {"url": f"{save_as}.url", "text": f"{save_as}.text"}


VERIFIED_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \S+")
PROOF_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
class HttpExpect(_Strict):
    min_items: dict[str, int] = Field(default_factory=dict)  # output name -> fewer means the site changed


class OperationHttp(_Strict):
    """What the /v1/sites layer and health read (docs/SITE-APIS.md): in the implementation hash,
    not the contract hash. The two field lists are output paths ('orders[].number')."""

    cache_s: int = Field(default=0, ge=0, le=86_400)
    expect: HttpExpect = Field(default_factory=HttpExpect)
    untrusted_fields: list[str] = Field(default_factory=list)
    private_fields: list[str] = Field(default_factory=list)


class Operation(_Strict):
    """One typed operation. `verified` says when and where a canary on a throwaway account
    proved it end to end ("2026-11-04 reddit.com canary: ..."), like an egress EffectRule's, and
    `verified_hash` which implementation it proved (compile.proof_hash: the whole compiled
    operation but its title, description, version and these two). In the file both are
    information only: a mark is honoured only from the node's own ledger
    (agentos.runner.connectors.marks), which the owner writes with `agentos connector verify`,
    and only while the installed operation's proof hash equals the one marked. A file mark whose
    hash is missing or differs compiles as stale (`CompiledOperation.mark`), with no `verified`
    in the compiled flow."""

    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    verified: str = Field(default="", max_length=300)
    verified_hash: str = Field(default="", max_length=80)
    effect: Effect
    input: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    output: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    acting_as: ActingAs
    pace: Pace | None = None
    signals: Signals = Field(default_factory=Signals)
    commit: Commit | None = None
    readback: ReadbackContract | None = None
    rehearsal: Literal["until_commit", "full", "none"] | None = None
    discard: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    steps: list[dict[str, Any]] = Field(min_length=1, max_length=500)
    # does it reach people (docs/WORKFLOWS.md, "Sending to people")? Installing a connector is a
    # person's act, so `not_to_people` here counts; `sends` makes it a workflow's send step only
    sends: SendsSpec | None = None
    not_to_people: bool | None = None
    http: OperationHttp | None = None  # a site API's endpoint only

    @model_validator(mode="after")
    def _reach(self) -> Operation:
        if self.sends is None:
            return self
        if self.not_to_people:
            raise ValueError("an operation either sends to people or does not: `sends` or `not_to_people`, not both")
        declared = self.input.get("properties", {})
        for param in (self.sends.to, self.sends.body):
            if param not in declared:
                raise ValueError(f"sends names {param!r}, which is not one of the operation's inputs")
        return self

    @field_validator("verified")
    @classmethod
    def _verified(cls, v: str) -> str:
        v = v.strip()
        if v and not VERIFIED_RE.match(v):
            raise ValueError("verified starts with the canary's date and where it ran: "
                             "'2026-11-04 reddit.com canary: ...' (empty: not verified)")
        return v

    @field_validator("verified_hash")
    @classmethod
    def _verified_hash(cls, v: str) -> str:
        v = v.strip()
        if v and not PROOF_HASH_RE.match(v):
            raise ValueError("verified_hash is the proof hash the canary ran: sha256:<64 hex>")
        return v

    @field_validator("effect")
    @classmethod
    def _effect(cls, v: Effect) -> Effect:
        if v not in V1_EFFECTS:
            raise ValueError(f"effect {v} is out of the v1 catalog: an operation is read, draft or commit")
        return v

    @field_validator("input")
    @classmethod
    def _input(cls, v: Any) -> dict[str, Any]:
        v = check_object_schema(v, "input")
        for name in v.get("properties", {}):
            if not PARAM_NAME_RE.match(name) or name in RESERVED_PARAMS:
                raise ValueError(f"input {name!r} must be lowercase_with_underscores and not reserved")
            if name in BINDING_PARAMS:
                raise ValueError(f"input {name!r} is a binding param: the connector layer fills it, "
                                 "never the caller")
        return v

    @field_validator("output")
    @classmethod
    def _output(cls, v: Any) -> dict[str, Any]:
        return check_object_schema(v, "output")

    @field_validator("steps", "discard")
    @classmethod
    def _steps(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for i, step in enumerate(v):
            try:
                validate_step(step)
            except ValueError as e:
                raise ValueError(f"step {i}: {e}") from e
        return v

    @property
    def rehearsal_mode(self) -> str:
        if self.rehearsal:
            return self.rehearsal
        return "until_commit" if self.effect == Effect.COMMIT else "full"


class Requires(_Strict):
    agentos: str | None = Field(default=None, pattern=r"^>=\d+\.\d+(\.\d+)?$")
    engines: list[Literal["camoufox"]] = Field(default_factory=lambda: ["camoufox"])


class Connector(_Strict):
    connector: str
    version: str
    site: str
    domain: str
    title: str | None = Field(default=None, max_length=120)
    requires: Requires = Field(default_factory=Requires)
    backends: list[Literal["browser", "api", "postiz"]] = Field(default_factory=lambda: ["browser"])
    account_languages: list[str] = Field(default_factory=lambda: ["en"], min_length=1)
    surfaces: list[str] = Field(default_factory=list)  # a site API runs on these and no other
    operations: dict[str, Operation] = Field(min_length=1, max_length=50)

    @field_validator("connector")
    @classmethod
    def _key(cls, v: str) -> str:
        if not CONNECTOR_KEY_RE.match(v):
            raise ValueError("connector must be a lowercase site key like 'instagram'")
        if v in RESERVED_KEYS:
            raise ValueError(f"connector key {v!r} is reserved: its <site>_<op> tools would clash with the node's own")
        return v

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        if not SEMVER_RE.match(v):
            raise ValueError("version must be semver MAJOR.MINOR.PATCH, like 1.4.0")
        return v

    @field_validator("domain")
    @classmethod
    def _domain(cls, v: str) -> str:
        if not DOMAIN_RE.match(v):
            raise ValueError("domain must be a lowercase registrable domain like instagram.com")
        return v

    @field_validator("account_languages")
    @classmethod
    def _langs(cls, v: list[str]) -> list[str]:
        for lang in v:
            if not LANG_RE.match(lang):
                raise ValueError(f"account language {lang!r} must be a two-letter code like en")
        if len(set(v)) != len(v):
            raise ValueError("account_languages lists a language twice")
        return v

    @model_validator(mode="after")
    def _check(self, info: ValidationInfo) -> Connector:
        local = bool(info.context and info.context.get("local"))  # parse_connector: a site API, never a document's say
        learned = bool(info.context and info.context.get("learned"))  # parse_connector: install_learned, likewise
        if learned:
            if self.connector != f"{self.site}{LEARNED_SUFFIX}":
                raise ValueError(f"a learned connector's key is <site>{LEARNED_SUFFIX}, like instagram{LEARNED_SUFFIX}")
        elif self.site != self.connector and not local:
            raise ValueError("site must equal connector: one connector per site key (agentos.node.sites)")
        if self.surfaces and not local:
            raise ValueError("surfaces belong to a site API; a catalog connector is bound with agentos connector bind")
        if "browser" not in self.backends:
            raise ValueError("phase 1 compiles the browser backend only: backends must include browser")
        for name in self.operations:
            if not OPERATION_RE.match(name):
                raise ValueError(f"operation name {name!r} must be lowercase_with_underscores")
        return self

    @property
    def version_tuple(self) -> tuple[int, int, int]:
        return version_tuple(self.version)

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


def version_tuple(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(x) for x in version.split("."))
    return major, minor, patch


# ---------------------------------------------------------------- bindings


class Fallback(_Strict):
    backend: Literal["browser", "api", "postiz"]
    lane: str | None = None


class Binding(_Strict):
    """Ties a connector to an AUD-01 surface: where its operations run and who they act as.
    Grants, approvals and schedules target the surface; the binding decides the lane."""

    surface: str
    connector: str = Field(pattern=CONNECTOR_KEY_RE.pattern)
    lane: str | None = None
    acts_as: ActsAs
    backend: Literal["browser", "api", "postiz"] = "browser"
    account_language: str = Field(default="en", pattern=LANG_RE.pattern)
    credential: str | None = None  # a vault reference, never the value
    fallback: Fallback | None = None

    @model_validator(mode="after")
    def _check(self) -> Binding:
        if not LANE_ID_RE.match(self.surface):
            raise ValueError("surface must look like 'biz-a/instagram'")
        for lane in (self.lane, self.fallback.lane if self.fallback else None):
            if lane is not None and not LANE_ID_RE.match(lane):
                raise ValueError(f"lane {lane!r} must be a lane id like 'personal/facebook'")
        if self.backend == "browser" and not self.lane:
            raise ValueError("a browser binding names the lane it runs on")
        if self.backend in ("api", "postiz") and not self.credential:
            raise ValueError(f"an {self.backend} binding names a vault credential")
        if self.credential is not None and not VAULT_REF_RE.match(self.credential):
            raise ValueError("credential is a vault reference like 'vault:biz-c/shopify-admin-token'")
        if self.fallback and self.fallback.backend == "browser" and not self.fallback.lane:
            raise ValueError("a browser fallback names its lane")
        return self

    @classmethod
    def from_surface(cls, surface: Surface) -> Binding:
        """The browser binding of an AUD-01 surface: its lane, who it acts as, its language."""
        return cls(surface=surface.id, connector=surface.site, lane=surface.lane, acts_as=surface.acts_as,
                   account_language=surface.account_language)

    def params(self) -> dict[str, str]:
        """The hidden binding params a run of this connector gets (BINDING_PARAMS)."""
        return {"acts_as_id": self.acts_as.asset_id or self.lane or "", "account_language": self.account_language}

    def check_for(self, connector: Connector, operation: str) -> None:
        """Raise ValueError if this binding cannot run `operation` of `connector`."""
        if self.connector != connector.site:
            raise ValueError(f"surface {self.surface} is bound to {self.connector}, not {connector.site}")
        op = connector.operations.get(operation)
        if op is None:
            raise ValueError(f"{connector.connector} has no operation {operation!r}")
        if op.acting_as.kind != self.acts_as.kind:
            raise ValueError(f"{connector.connector}.{operation} acts as {op.acting_as.kind}; surface "
                             f"{self.surface} is bound as {self.acts_as.kind}")
        if self.account_language not in connector.account_languages:
            raise ValueError(f"{connector.connector} covers {', '.join(connector.account_languages)}; surface "
                             f"{self.surface} is in {self.account_language}")
