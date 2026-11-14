"""The strict lint for connectors (docs/design/connectors.md 4.4).

Beyond the flow lint (agentos.runner.flows.lint), an operation must be safe to replay on a
real account with no model watching. Errors block compiling; warnings are shown. The rules:

- the site is a key of agentos.node.sites.SITES and the domain is one of its hosts;
- the declared effect is at least what the steps do (input steps count as commit unless
  marked "publishes": false, which the signed catalog vouches for), read/draft/commit only;
- typed contracts: input and output schemas forbid unknown properties; every string output
  is constrained (pattern, format, enum or maxLength); a uri output names its host inside the
  connector's domain; every output is mapped from a saved value and every mapped value is an
  output;
- no param decides a host: every URL has a literal https host inside the connector's domain, read
  as a browser reads it (no user part, no backslash);
- `nth` only with an anchor; css-only locators are warnings, and on the commit path errors;
- on a site API, a param reaches a page only through a URL, a fill or type value, or a select whose
  param has an enum (param_problems): never a locator, an extract, a read_json, a regex or a file;
- a read_json's url has a literal https host inside the connector's domain, like any URL;
- a commit operation has a labelled top-level commit step (click or press) that is the only
  input that may publish, a non-empty preflight, at least one assert_identity before the
  commit, a readback with outputs, pace limits, and a commit gate request block (a warning
  until AUD-03 lands, an error with `commit_gate=True`);
- on the commit path (every step up to and including the commit, and the preflight) no
  locator is css-only or name-only;
- no input step inside a for_each, which only iterates over a list saved earlier;
- a `names` map covers every account language of the connector;
- fixtures exist for every operation and account language (when compiled from a directory).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agentos.core.models import Effect
from agentos.core.template import TEMPLATE_RE, template_roots
from agentos.runner.connectors.models import BINDING_PARAMS, WEB_SITE, Connector, Operation
from agentos.runner.connectors.steps import is_input, is_structural, step_locators, step_urls, walk
from agentos.runner.urlpattern import url_pattern
from agentos.runner.util import host_in

_STRING_CONSTRAINTS = ("pattern", "format", "enum", "const", "maxLength")
_AUTHORITY_RE = re.compile(r"([a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?::\d{1,5})?")
SCOPE_ROOTS = ("vars", "trigger")
# names that resolve on the node's own network, whatever is behind them, and names that put an
# IP address in a host name. A resolved private address is the lane's guard's to refuse
# (agentos.node.egress); these can be refused before anything runs.
PRIVATE_SUFFIXES = ("localhost", "local", "localdomain", "internal", "intranet", "lan", "home", "corp",
                    "home.arpa", "in-addr.arpa", "ip6.arpa", "onion", "nip.io", "sslip.io", "xip.io")


@dataclass(frozen=True, slots=True)
class Issue:
    level: Literal["error", "warning"]
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.level}: {self.where}: {self.message}"


def _is_public_suffix(domain: str) -> bool:
    from agentos.vault.domains import registrable_domain

    return registrable_domain(f"x.{domain}") == f"x.{domain}"


def site_hosts(sites: Mapping[str, tuple[str, ...]] | None = None) -> dict[str, tuple[str, ...]]:
    """site key -> registrable hosts. Default: agentos.node.sites.SITES, the one site table."""
    if sites is not None:
        return dict(sites)
    from agentos.node.sites import SITES

    return {s.key: s.hosts for s in SITES}


class _Lint:
    def __init__(self, connector: Connector, *, sites: Mapping[str, tuple[str, ...]] | None,
                 root: Path | None, commit_gate: bool, local: bool = False):
        self.c = connector
        self.local = local
        self.sites = site_hosts(sites)
        self.root = root
        self.commit_gate = commit_gate
        self.issues: list[Issue] = []

    def error(self, where: str, message: str) -> None:
        self.issues.append(Issue("error", where, message))

    def warn(self, where: str, message: str) -> None:
        self.issues.append(Issue("warning", where, message))

    # ---- the connector ----
    def connector(self) -> None:
        c = self.c
        hosts = self.sites.get(c.site)
        if self.local and c.site == WEB_SITE:  # any domain: its hosts are `domain` and its subdomains
            if any(host_in(c.domain, s) for s in PRIVATE_SUFFIXES):
                self.error("domain", f"{c.domain} is a private name; a site API reaches a public site")
            elif _is_public_suffix(c.domain):
                self.error("domain", f"{c.domain} is shared by unrelated sites; name the site's own domain")
        elif hosts is None:
            self.error("site", f"{c.site!r} is not a site agentos knows (agentos.node.sites.SITES); add the "
                               "site there first, through the same review")
        elif not any(host_in(c.domain, h) for h in hosts):
            self.error("domain", f"{c.domain} is not one of the {c.site} hosts ({', '.join(hosts)})")
        for b in c.backends:
            if b != "browser":
                self.warn("backends", f"the {b} backend is declared but not compiled before phase 5")
        if self.root is None and not self.local:
            self.warn("fixtures", "compiled without a directory, so fixtures were not checked")
        for name, op in c.operations.items():
            self.operation(name, op)

    # ---- one operation ----
    def operation(self, name: str, op: Operation) -> None:
        w = f"operations.{name}"
        if op.acting_as.kind == "lane" and not self.local:
            self.error(f"{w}.acting_as", "acting_as lane is only for a site API (saved_by kind local)")
        if op.acting_as.kind == "lane" and (any(s.get("type") == "assert_identity" for _, s in walk(op.steps)) or
                                            any(c.kind == "acting_as" for c in (op.commit.preflight if op.commit else []))):
            self.error(f"{w}.acting_as", "acting_as lane has no identity to check: drop assert_identity and the acting_as check")
        if op.http is not None and not self.local:
            self.error(f"{w}.http", "http belongs to a site API endpoint")
        self.contract(w, op)
        self.locators_and_urls(w, op)
        self.effect(w, op)
        self.loops(w, op)
        self.outputs(w, op)
        if op.effect == Effect.COMMIT:
            self.commit(w, op)
        elif op.commit is not None:
            self.error(f"{w}.commit", f"only a commit operation has a commit block (this one is {op.effect})")
        else:
            if op.pace is None:
                self.warn(f"{w}.pace", "no pace limits: reads are capped per call and paced per day")
            if op.acting_as.kind != "lane" and not any(s.get("type") == "assert_identity" for _, s in walk(op.steps)):
                self.warn(f"{w}.steps", "no assert_identity step, so the run does not check whose account it reads")
        self.fixtures(w, name)

    def contract(self, w: str, op: Operation) -> None:
        if op.input.get("additionalProperties") is not False:
            self.error(f"{w}.input", "set additionalProperties: false, so a caller cannot pass unknown params")
        for pname, prop in op.input.get("properties", {}).items():
            if isinstance(prop, dict) and prop.get("format") == "agentos-file" and not (
                    prop.get("media") and prop.get("max_bytes")):
                self.error(f"{w}.input.{pname}", "a file param lists its media types and max_bytes")
        if op.output.get("additionalProperties") is not False:
            self.error(f"{w}.output", "set additionalProperties: false: an operation returns declared fields only")
        for path, prop in _schema_strings(op.output, "output"):
            if not any(k in prop for k in _STRING_CONSTRAINTS):
                self.error(f"{w}.{path}", "a string output needs a pattern, format, enum or maxLength, so "
                                          "structured fields cannot carry prose")
            if prop.get("format") == "uri":
                host = prop.get("x-agentos-host")
                if not host or not host_in(str(host), self.c.domain):
                    self.error(f"{w}.{path}", f"a uri output names its host (x-agentos-host) inside {self.c.domain}")

    def locators_and_urls(self, w: str, op: Operation) -> None:
        read = self.local and op.effect == Effect.READ  # discovery often has no landmark to anchor a list to
        for part, steps in (("steps", op.steps), ("discard", op.discard)):
            for path, step in walk(steps):
                where = f"{w}.{part}.{path}"
                for what, loc in step_locators(step):
                    # an extract field resolves inside its item, which anchors it
                    self._locator(f"{where} {what}", loc, field=what.startswith("field "), read=read)
                for url in step_urls(step):
                    self._url(where, url)
                if step.get("type") == "read_json":
                    self._url(where, url_pattern(step["url"]).origin)
                if part == "discard" and is_input(step) and step.get("publishes") is not False:
                    self.error(where, "a discard step closes the composer without saving: mark it "
                                      '"publishes": false')
        for i, check in enumerate(op.commit.preflight if op.commit else []):
            if check.locator is not None:
                self._locator(f"{w}.commit.preflight.{i}", check.locator)
        if op.acting_as.locator is not None:
            self._locator(f"{w}.acting_as", op.acting_as.locator)
        if self.local:
            for where, why in param_problems(op):
                self.error(f"{w}.{where}", why)

    def _locator(self, where: str, loc: dict[str, Any], *, field: bool = False, read: bool = False) -> None:
        """`field`: an extract field reads under its item, so the item is its anchor. `read`: a site
        API's read touches nothing, and its `expect` notices when page order moves."""
        cur: dict[str, Any] | None = loc
        while isinstance(cur, dict):
            if cur.get("nth") is not None and cur.get("within") is None and not (field and cur is loc):
                (self.warn if read else self.error)(where, "nth without an anchor (within) depends on page order")
            names = cur.get("names")
            if isinstance(names, dict):
                missing = [lang for lang in self.c.account_languages if lang not in names]
                if missing:
                    self.error(where, f"names has no entry for {', '.join(missing)} (account_languages)")
            if not cur.get("role") and cur.get("attr") is None:
                self.warn(where, "css-only locator; prefer role or attr, which survive redesigns")
            cur = cur.get("within")

    def _url(self, where: str, url: str) -> None:
        if not url.startswith("https://"):
            self.error(where, f"{url!r}: a connector URL is https with a literal host")
            return
        rest = url[len("https://"):]
        authority = re.split(r"[/?#\\]", rest, maxsplit=1)[0]  # a browser ends the host at a backslash too
        m = _AUTHORITY_RE.fullmatch(authority)
        if TEMPLATE_RE.search(authority) or "$" in authority:
            self.error(where, f"{url!r}: no param decides a host")
        elif m is None or "\\" in rest.split("?", 1)[0].split("#", 1)[0]:
            self.error(where, f"{url!r}: a connector URL has a plain host (no user, no backslash)")
        elif not host_in(m.group(1), self.c.domain):
            self.error(where, f"{url!r} is outside the connector's domain {self.c.domain}")
        if self.local:
            for root in template_roots(url):
                if root in SCOPE_ROOTS:
                    self.error(where, f"{url!r}: a site API URL takes the caller's params only, never ${{{root}...}}, "
                                      "so what a page says cannot steer the lane")

    def effect(self, w: str, op: Operation) -> None:
        worst, why = Effect.READ, None
        for path, step in walk(op.steps):
            if not is_input(step):
                continue
            eff = Effect.DRAFT if step.get("publishes") is False else Effect.COMMIT
            if eff.rank > worst.rank:
                worst, why = eff, f"step {path} ({step['type']})"
        if worst.rank > op.effect.rank:
            self.error(f"{w}.effect", f"declared {op.effect}, but {why} is {worst}: declare {worst} or change "
                                      "the step (a connector is never lower than its steps)")
        if op.effect == Effect.DRAFT and worst == Effect.READ:
            self.warn(f"{w}.effect", "declared draft, but no step writes anything")

    def loops(self, w: str, op: Operation) -> None:
        saved: set[str] = set()
        self._loops(w, op.steps, "", saved)

    def _loops(self, w: str, steps: list[Any], prefix: str, saved: set[str]) -> None:
        for i, step in enumerate(steps):
            path = f"{prefix}{i}"
            if step.get("type") == "for_each":
                if step.get("over") not in saved:
                    self.error(f"{w}.steps.{path}", f"for_each over {step.get('over')!r}, which is not saved "
                                                    "by an earlier step")
                for sub_path, sub in walk(step.get("steps") or [], f"{path}.steps."):
                    if is_input(sub):
                        self.error(f"{w}.steps.{sub_path}", "no input steps inside for_each: a loop only reads")
                inner = saved | {step.get("as")}
                self._loops(w, step.get("steps") or [], f"{path}.steps.", inner)
            elif step.get("type") == "branch":
                self._loops(w, step.get("then") or [], f"{path}.then.", saved)
                self._loops(w, step.get("else") or [], f"{path}.else.", saved)
            if step.get("save_as"):
                saved.add(step["save_as"])
            if step.get("type") in ("extract", "read_json"):
                for fname, field in (step.get("fields") or {}).items():
                    if field.get("host") and not host_in(field["host"], self.c.domain):
                        self.error(f"{w}.steps.{path}.fields.{fname}",
                                   f"host {field['host']} is outside {self.c.domain}")
                    if field.get("type", "string") != "string":
                        continue  # a number, an integer or a boolean is typed and capped by what it is
                    if not field.get("pattern") and not field.get("format") and not field.get("max_chars"):
                        self.error(f"{w}.steps.{path}.fields.{fname}", "a field needs a pattern, a format or "
                                                                     "max_chars, so values stay typed and capped")

    def outputs(self, w: str, op: Operation) -> None:
        live = op.readback.live if op.readback else None
        mapping = live.outputs if live else {}
        props = op.output.get("properties", {})
        saved = {s["save_as"] for _, s in walk(op.steps) if s.get("save_as")}
        for name, path in mapping.items():
            if name not in props:
                self.error(f"{w}.readback.live.outputs.{name}", "is not declared in the output schema")
            if path.split(".", 1)[0] not in saved:
                self.error(f"{w}.readback.live.outputs.{name}", f"{path!r} is not saved by any step (save_as)")
        for name in op.output.get("required", []):
            if name not in mapping:
                self.error(f"{w}.output", f"required output {name!r} is not mapped from a saved value")
        if live and live.save_as and live.save_as not in saved:
            self.error(f"{w}.readback.live.save_as", f"{live.save_as!r} is not saved by any step")
        if props and not mapping:
            self.error(f"{w}.readback", "the output schema declares fields but the readback maps none")

    def commit(self, w: str, op: Operation) -> None:
        cw = f"{w}.commit"
        if op.commit is None:
            self.error(cw, "a commit operation names its commit step, preflight and request")
            return
        idx = self._commit_step(cw, op)
        self._only_commit_publishes(w, op, idx)
        self._commit_preflight(cw, op)
        if idx is not None:
            self._commit_path(w, op, idx)
        if op.commit.request is None:
            msg = "no commit gate request block (AUD-03): how the publish request is recognized"
            (self.error if self.commit_gate else self.warn)(f"{cw}.request", msg)
        live = op.readback.live if op.readback else None
        if live is None or not live.outputs:
            self.error(f"{w}.readback", "a commit operation reads the live object back and maps its outputs")
        if op.pace is None or op.pace.per_day is None or op.pace.min_gap_minutes is None:
            self.error(f"{w}.pace", "a commit operation sets per_day and min_gap_minutes")
        if op.rehearsal_mode == "until_commit" and not op.discard:
            self.warn(f"{w}.discard", "no discard steps, so a rehearsal cannot close the composer (phase 2)")

    def _commit_step(self, cw: str, op: Operation) -> int | None:
        """The one top-level step labelled as the commit step (a click or press that publishes), or None."""
        assert op.commit is not None
        labelled = [(p, s) for p, s in walk(op.steps) if s.get("label") == op.commit.step]
        top = [i for i, s in enumerate(op.steps) if s.get("label") == op.commit.step]
        if len(labelled) != 1 or len(top) != 1:
            self.error(f"{cw}.step", f"exactly one top-level step is labelled {op.commit.step!r} "
                                     f"(found {len(labelled)}, {len(top)} at the top level)")
            return None
        step = op.steps[top[0]]
        if step.get("type") not in ("click", "press"):
            self.error(f"{cw}.step", f"the commit step is a click or press, not {step.get('type')}")
        if step.get("publishes") is False:
            self.error(f"{cw}.step", 'the commit step publishes: it cannot be "publishes": false')
        return top[0]

    def _only_commit_publishes(self, w: str, op: Operation, idx: int | None) -> None:
        for path, step in walk(op.steps):
            if idx is not None and path == str(idx):
                continue
            if is_input(step) and step.get("publishes") is not False:
                self.error(f"{w}.steps.{path}", 'only the commit step may publish: mark this input "publishes": '
                                                "false (the signed catalog vouches for it)")

    def _commit_preflight(self, cw: str, op: Operation) -> None:
        """The preflight checks the composer, on shown inputs and structural locators."""
        assert op.commit is not None
        if not op.commit.preflight:
            self.error(f"{cw}.preflight", "a commit operation checks the composer right before the commit")
        shown = op.input.get("properties", {})
        for i, check in enumerate(op.commit.preflight):
            if check.kind == "value_equals":
                prop = shown.get(check.param)
                if not isinstance(prop, dict):
                    self.error(f"{cw}.preflight.{i}", f"param {check.param!r} is not an input")
                elif prop.get("show") is not True:
                    self.error(f"{cw}.preflight.{i}", f"param {check.param!r} must be shown (\"show\": true), "
                                                      "so the owner approves the exact text")
            if check.kind == "text_differs":
                for root in template_roots(check.text or ""):
                    prop = shown.get(root)
                    if not isinstance(prop, dict) or prop.get("show") is not True:
                        self.error(f"{cw}.preflight.{i}", f"text_differs names ${{{root}}}, which is not a shown "
                                                          "input (\"show\": true)")
            if check.locator is not None and not is_structural(check.locator):
                self.error(f"{cw}.preflight.{i}", "commit path locators need a structural part (attr, within, "
                                                  "or a role without a name), not a name or css alone")

    def _commit_path(self, w: str, op: Operation, idx: int) -> None:
        """Up to the commit step: acting as is asserted, and every locator is structural."""
        if op.acting_as.kind != "lane" and not any(s.get("type") == "assert_identity" for _, s in walk(op.steps[:idx])):
            self.error(f"{w}.steps", "a commit operation checks acting as (assert_identity) before the commit")
        for path, step in walk(op.steps[: idx + 1]):
            for what, loc in step_locators(step):
                if not is_structural(loc):
                    self.error(f"{w}.steps.{path} {what}", "commit path locators need a structural part "
                                                           "(attr, within, or a role without a name)")

    def fixtures(self, w: str, name: str) -> None:
        if self.root is None:
            return
        for lang in self.c.account_languages:
            base = self.root / "fixtures" / name
            if not ((base / lang).exists() or any(base.glob(f"{lang}.*"))):
                self.error(f"{w}.fixtures", f"no fixture for account language {lang} "
                                            f"(fixtures/{name}/{lang} or fixtures/{name}/{lang}.*)")


def _templated(value: Any) -> bool:
    """A `${...}` anywhere in this value, as `render` would fill it."""
    if isinstance(value, str):
        return TEMPLATE_RE.search(value) is not None
    if isinstance(value, dict):
        return any(_templated(v) for v in value.values())
    return isinstance(value, list) and any(_templated(v) for v in value)


def _conditions(step: dict[str, Any]) -> list[dict[str, Any]]:
    holders = (step, step.get("readback"), step.get("if"))
    return [h["condition"] for h in holders if isinstance(h, dict) and isinstance(h.get("condition"), dict)]


def param_problems(op: Any) -> list[tuple[str, str]]:
    """(where, why) for each place a site API lets a value into something other than a URL, a fill
    or type value, or a select with an enum. A css selector or a name is structure: one value there
    picks a different element on the caller's say. `op` is an Operation or a site API Endpoint. The
    catalog is reviewed and does not come here."""
    enums = {k for k, p in (op.input.get("properties") or {}).items() if isinstance(p, dict) and p.get("enum")}
    out: list[tuple[str, str]] = []
    for part, steps in (("steps", op.steps), ("discard", op.discard)):
        for path, step in walk(steps):
            where = f"{part}.{path}"
            for what, loc in step_locators(step):
                if not what.startswith("field ") and _templated(loc):
                    out.append((f"{where} {what}", "no param goes in a locator: use the URL, a fill or a type value"))
            for name, field in (step.get("fields") or {}).items():
                if _templated(field):
                    out.append((f"{where} field {name}", "no param goes in an extract: it reads what the steps opened"))
            if step.get("type") == "read_json" and _templated([step.get("url"), step.get("items")]):
                out.append((where, "no param goes in a read_json: its url and paths stay as written"))
            if any(_templated(c.get("matches")) for c in _conditions(step)):
                out.append((where, "a condition's regex stays as written: no param goes in it"))
            if step.get("type") == "select":
                names = {step["param"]} if step.get("param") else template_roots(str(step.get("option") or ""))
                if names - enums:
                    out.append((where, f"a select takes a param only with an enum: {', '.join(sorted(names - enums))}"))
            if step.get("type") == "upload":
                out.append((where, "a site API has no upload step: a caller's param would pick the file"))
    for i, check in enumerate(op.commit.preflight if op.commit else []):
        if _templated(check.locator):
            out.append((f"commit.preflight.{i}", "no param goes in a locator: compare it with value_equals"))
    return out


def _schema_strings(schema: dict[str, Any], path: str):
    """(path, schema) for every string-typed schema under an output schema."""
    t = schema.get("type")
    types = t if isinstance(t, list) else [t]
    if "string" in types:
        yield path, schema
    for name, prop in (schema.get("properties") or {}).items():
        if isinstance(prop, dict):
            yield from _schema_strings(prop, f"{path}.{name}")
    items = schema.get("items")
    if isinstance(items, dict):
        yield from _schema_strings(items, f"{path}[]")


def lint_connector(connector: Connector, *, sites: Mapping[str, tuple[str, ...]] | None = None,
                   root: Path | None = None, commit_gate: bool = False, local: bool = False) -> list[Issue]:
    lint = _Lint(connector, sites=sites, root=root, commit_gate=commit_gate, local=local)
    lint.connector()
    return lint.issues


def template_problems(op: Operation) -> list[str]:
    """Templates in extension parts (attr values, names) that name an undeclared param; the
    runner checks the rest when it validates the compiled flow."""
    declared = set(op.input.get("properties", {})) | set(BINDING_PARAMS) | {"vars", "trigger"}
    out: list[str] = []
    for path, step in walk(op.steps + op.discard):
        for what, loc in step_locators(step):
            cur: dict[str, Any] | None = loc
            while isinstance(cur, dict):
                texts = [v for v in (cur.get("attr") or {}).values() if isinstance(v, str)]
                texts += [v for v in (cur.get("names") or {}).values() if isinstance(v, str)]
                for text in texts:
                    for root in template_roots(text):
                        if root not in declared:
                            out.append(f"step {path} {what}: ${{{root}}} is not a declared param")
                cur = cur.get("within")
    return out
