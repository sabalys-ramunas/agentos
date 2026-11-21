"""Turn one recorded profile edit into a connector: a commit operation and its read
(docs/design/learn-a-site.md sections 1, 2 and 7).

    doc = generate(site="instagram", field="bio", events=recorder.stop(lane)["events"],
                   readback_locator={"css": "header section span", "within": {"role": "main"}},
                   profile_url="https://www.instagram.com/biza.coffee/", account_id="biza.coffee",
                   commit={"method": "POST", "host": "www.instagram.com", "path": "/api/v1/web/accounts/edit/",
                           "act_seq": 7},
                   limit=150, value="Coffee in Vilnius", old_value="Coffee")
    flow_ids = ConnectorService(flows).install_learned(doc)   # connector.instagram-profile.bio, ..._read

Pure: no page, no store, no model. The agent supplied only the recording (what it did in the
lane), where the value shows on the profile page (the readback locator, built by the node from a
ref) and the profile URL; the commit request comes from the node's own egress trace of the one
request the learn window released, never from the agent.

One learned connector serves every account on its site: the profile URL seen while learning
becomes a template, the learned account's id (`account_id`, as the site's acting_as reads it and
a surface binds it) replaced by `${acts_as_id}`, and so does every recorded page that carries it.
A profile URL without the id is refused. What each site's id is:

    instagram  the username, read from the href of the account's own Profile link in the navigation
    facebook   the c_user id (identity cookie): learn on https://www.facebook.com/profile.php?id=<id>
    linkedin   the /in/<id>/ slug, read from the URL /in/me/ redirects to
    reddit     the t2_ id (identity cookie): no profile page carries it yet, so reddit is refused
    medium     the uid (identity cookie): learn on https://medium.com/u/<uid>

The document has the shape of connectors/linkedin/connector.json (modelled on its
`profile_headline`) under the key `<site>-profile`, with two operations:

    <field>       commit, from fixed steps only: open the edit page (where the recording typed
                  and the save was sent from, under the site's reviewed edit pages), assert identity, wait for and fill the learned field with `${value}`
                  (`"publishes": false`), click the save (labelled `save`, the only step that may
                  publish), then reload the profile and read its text back equal to the value.
                  No other recorded click, select, key press or scroll is replayed. Name-only
                  locators are anchored in the site's edit landmark. Preflight: the field holds
                  exactly the value, and (when the value shows on the edit page itself) the shown
                  text differs from it. Commit request pinned to the traced method, URL and
                  operation name, strict scope; on a reviewed endpoint its field must hold
                  exactly the run's value and every other form field what the page loaded.

The save is the recorded act that sent the request the learn window released (`commit`'s
`act_seq`, or `act_at`: the first act that ended at or after it), and it must be a click or key
press on a button. A recording that typed into more than one field, typed anything but exactly
the value, or made a choice before saving (a select, a check, a toggle: it would ride along in
the same request on every sync) is refused.
    <field>_read  read: open the account's profile, assert identity, extract the text at the
                  readback locator, up to the field's limit (an extract, not a saved element state,
                  which keeps 300 characters).

No locator on the commit path, and not the readback locator, may name the learned value or the
old one: it would stop matching once the value changed. `name` is never learned: Facebook and
Instagram rate-lock name changes for weeks.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote, unquote_plus, urlsplit, urlunsplit

from openajar.core.errors import InvalidRequest
from openajar.core.locator import Locator
from openajar.profiles.mapping import MULTILINE
from openajar.runner.connectors.models import LEARNED_SUFFIX, OPERATION_RE, SEMVER_RE
from openajar.runner.connectors.service import READ_SUFFIX, UNLEARNABLE_FIELDS
from openajar.runner.connectors.steps import is_structural
from openajar.runner.recorder import STATE_ATTRS
from openajar.runner.util import host_in

SEND_TYPES = ("click", "press")
BUTTON_ROLES = frozenset({"button"})   # what a save is pressed on
CHOICE_ROLES = frozenset({"checkbox", "radio", "switch", "option", "combobox", "listbox", "menuitemcheckbox",
                          "menuitemradio", "slider", "spinbutton", "tab"})   # acts that choose something
TYPED = ("type", "fill")
VALUE = "value"
SAVE = "save"
ACTS_AS = "${acts_as_id}"
HOLD = ["captcha", "checkpoint", "logged_out", "blocked", "rate_limited", "wrong_account"]
DEFAULT_LIMIT = 5000   # a field with no limit in profiles.mapping
MIN_NAMED = 3          # a value shorter than this is too common a string to refuse a locator over
_TEMPLATE = "${%s}"
SINGLE_LINE = "^[^\\u0000-\\u001F\\u007F]+$"
MULTI_LINE = "^[^\\u0000-\\u0009\\u000B-\\u001F\\u007F]+$"   # line breaks kept, other control chars refused


class LearnRefused(InvalidRequest):
    """What was recorded or traced cannot become a learned operation (code invalid_request)."""


@dataclass(frozen=True)
class SiteProfile:
    """What the generator needs to know of a site beyond openajar.node.sites.

    host:       the profile pages' host (www.instagram.com)
    acting_as:  the operation's acting_as (its `names` filled per account language from `labels`)
    account:    the pattern of the account's own profile URL (proof_url's)
    anchor:     the landmark the edit form sits in, which anchors a name-only locator
    id_is:      what the account id (acts_as_id) is on this site, for an error
    identity_url: opened before assert_identity when the id is read from a redirected URL
    labels:     the acting_as locator's accessible name per account language
    """
    domain: str
    host: str
    title: str
    acting_as: dict[str, Any]
    account: str
    anchor: dict[str, Any]
    id_is: str
    identity_url: str | None = None
    labels: dict[str, str] = dc_field(default_factory=dict)


SITE_PROFILES: dict[str, SiteProfile] = {
    "instagram": SiteProfile(
        "instagram.com", "www.instagram.com", "Instagram",
        {"kind": "account", "source": "href", "locator": {"role": "link", "within": {"role": "navigation"}},
         "pattern": r"^(?:https://www\.instagram\.com)?/([A-Za-z0-9._]{1,30})/?$"},
        r"^https://www\.instagram\.com/[A-Za-z0-9._]{1,30}/", {"role": "main"},
        "the username (instagram.com/<username>/)", labels={"en": "Profile", "lt": "Profilis"}),
    "facebook": SiteProfile(
        "facebook.com", "www.facebook.com", "Facebook", {"kind": "account"},
        r"^https://www\.facebook\.com/(profile\.php\?id=[0-9]{5,20}|[A-Za-z0-9.]{5,50})", {"role": "dialog"},
        "the c_user id: learn on https://www.facebook.com/profile.php?id=<id>"),
    "linkedin": SiteProfile(
        "linkedin.com", "www.linkedin.com", "LinkedIn",
        {"kind": "account", "source": "url", "pattern": r"^https://www\.linkedin\.com/in/([A-Za-z0-9_-]+)/"},
        r"^https://www\.linkedin\.com/in/[A-Za-z0-9_-]+/", {"role": "dialog"},
        "the /in/<id>/ slug", identity_url="https://www.linkedin.com/in/me/"),
    "reddit": SiteProfile(
        "reddit.com", "www.reddit.com", "Reddit", {"kind": "account"},
        r"^https://www\.reddit\.com/(user|u)/[A-Za-z0-9_-]{3,20}/?", {"role": "main"},
        "the t2_ id of the identity cookie, which no profile page URL carries"),
    "medium": SiteProfile(
        "medium.com", "medium.com", "Medium", {"kind": "account"},
        r"^https://([a-z0-9-]+\.)?medium\.com/", {"role": "dialog"},
        "the uid of the identity cookie: learn on https://medium.com/u/<uid>"),
}


def connector_key(site: str) -> str:
    return f"{site}{LEARNED_SUFFIX}"


def flow_ids(site: str, field: str) -> tuple[str, str]:
    """(the commit operation's flow id, the read's)."""
    key = connector_key(site)
    return f"connector.{key}.{field}", f"connector.{key}.{field}{READ_SUFFIX}"


# ---------------------------------------------------------------- locators and steps


def _with_names(loc: Any, lang: str) -> Any:
    """A recorded locator (`name`) as a connector's (`names` per account language), all the way down."""
    if not isinstance(loc, dict):
        return loc
    out = {k: v for k, v in loc.items() if k != "name"}
    if loc.get("name") is not None:
        out["names"] = {lang: loc["name"]}
    if isinstance(loc.get("within"), dict):
        out["within"] = _with_names(loc["within"], lang)
    return out


def _anchored(loc: dict[str, Any], anchor: dict[str, Any]) -> dict[str, Any]:
    """A locator with a structural part: a name-only (or css-only) one goes inside the anchor."""
    if is_structural(loc):
        return loc
    return {**loc, "within": copy.deepcopy(anchor)}


def _locator(loc: Any, lang: str, anchor: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(loc, dict) or not loc:
        raise LearnRefused("a locator is an object like {role, name, within}")
    return _anchored(_with_names(loc, lang), anchor)


def _norm(text: str) -> str:
    return " ".join(str(text).split()).casefold()


def _texts(loc: Any) -> Iterator[str]:
    """Every string a locator matches on, all the way down: names, attr values, css."""
    while isinstance(loc, dict):
        for key in ("name", "css", "label"):
            if isinstance(loc.get(key), str):
                yield loc[key]
        yield from (v for v in (loc.get("names") or {}).values() if isinstance(v, str))
        yield from (v for v in (loc.get("attr") or {}).values() if isinstance(v, str))
        loc = loc.get("within")


def _names_value(loc: Any, values: list[str]) -> str | None:
    """The value (learned or old) this locator names, if any."""
    wanted = [_norm(v) for v in values if v and len(_norm(v)) >= MIN_NAMED]
    for text in _texts(loc):
        t = _norm(text)
        for v in wanted:
            if v in t:
                return text
    return None


def _when(value: Any) -> float | None:
    """A moment as epoch seconds: a number, or an ISO time (one with no zone is UTC)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            t = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return (t if t.tzinfo else t.replace(tzinfo=UTC)).timestamp()
    return None


def _role(ev: dict[str, Any]) -> str | None:
    return (ev.get("locator") or {}).get("role")


def _toggled(ev: dict[str, Any]) -> bool:
    """The act changed its own control's state (aria-pressed, aria-checked, ...)."""
    before = (ev.get("state_before") or {}).get("attributes") or {}
    after = (ev.get("state_after") or {}).get("attributes") or {}
    return any(a in after and before.get(a) != after.get(a) for a in STATE_ATTRS)


def _is_toggle(ev: dict[str, Any]) -> bool:
    """A choice rather than a step towards the field: a select, a check, a toggle."""
    if ev.get("action") == "select":
        return True
    return ev.get("action") in ("click", "press") and (_role(ev) in CHOICE_ROLES or _toggled(ev))


def _what(ev: dict[str, Any]) -> str:
    loc = ev.get("locator") or {}
    name = loc.get("name") or loc.get("label") or loc.get("css") or ""
    return f"{ev.get('action')} on {loc.get('role') or 'an element'}" + (f" {name!r}" if name else "")


def _recorded(events: Any, draft: Any) -> list[dict[str, Any]]:
    """The acts the recording kept (failed ones dropped), in order."""
    if isinstance(draft, tuple) and len(draft) == 3 and draft[2]:
        raise LearnRefused(f"the recording cannot be learned: {draft[2]}")
    if not isinstance(events, list):
        raise LearnRefused("learning builds the operation from the recorded events (Recorder.stop()['events']), "
                           "which name the act that saved")
    return [dict(e) for e in events if isinstance(e, dict) and e.get("ok") is not False]


def _save_at(events: list[dict[str, Any]], commit: dict[str, Any]) -> int:
    """The index of the act that sent the released request: the one whose sequence number is
    commit's `act_seq` (when the recorded events carry one), else the first act that ended at or
    after commit's `act_at` (it began then, or was running when the request left)."""
    acts = [i for i, e in enumerate(events) if e.get("action") != "navigate"]
    seq, at = commit.get("act_seq"), _when(commit.get("act_at"))
    numbered = any(events[i].get("act_seq", events[i].get("seq")) is not None for i in acts)
    if seq is not None and (numbered or at is None):
        hits = [i for i in acts if events[i].get("act_seq", events[i].get("seq")) == seq]
    elif at is not None:
        hits = [i for i in acts if (_when(events[i].get("at")) or float("-inf")) >= at][:1]
    else:
        raise LearnRefused("the traced save does not say which act sent it (act_seq or act_at), so the save "
                           "click cannot be told from the other clicks")
    if len(hits) != 1:
        raise LearnRefused("no recorded act sent the saved request, so nothing saved it in the recording "
                           "(record the edit again, saving it with a click)")
    return hits[0]


def _clean(loc: Any, what: str) -> dict[str, Any]:
    try:
        return Locator.model_validate(loc).model_dump(exclude_none=True)
    except ValueError as e:
        raise LearnRefused(f"the recorded {what} has no usable locator: {e}") from None


def _same_page(a: str, b: str) -> bool:
    x, y = urlsplit(a), urlsplit(b)
    return (x.netloc.lower(), x.path.rstrip("/"), x.query) == (y.netloc.lower(), y.path.rstrip("/"), y.query)


def _same_path(a: str, b: str) -> bool:
    """The same page, its query and fragment aside (the guard keeps a page's URL without them)."""
    x, y = urlsplit(a), urlsplit(b)
    return (x.netloc.lower(), x.path.rstrip("/")) == (y.netloc.lower(), y.path.rstrip("/"))


def _url_on(url: str, p: SiteProfile, what: str) -> str:
    parts = urlsplit(url or "")
    if parts.scheme != "https" or not parts.hostname or not host_in(parts.hostname, p.domain) or parts.username:
        raise LearnRefused(f"the {what} {url!r} is not an https page of {p.domain}")
    return url


def for_account(url: str, account_id: str) -> tuple[str, bool]:
    """(the URL with the account id, where it is a whole path segment or query value, replaced by
    ${acts_as_id}; whether it was there). Compared case-insensitively, as usernames are."""
    want = account_id.casefold()
    parts = urlsplit(url)
    found = False

    def sub(raw: str, decode: Any) -> str:
        nonlocal found
        if raw and decode(raw).casefold() == want:
            found = True
            return ACTS_AS
        return raw

    path = "/".join(sub(seg, unquote) for seg in parts.path.split("/"))
    query = []
    for pair in parts.query.split("&") if parts.query else []:
        key, eq, value = pair.partition("=")
        query.append(f"{key}={sub(value, unquote_plus)}" if eq else pair)
    return urlunsplit((parts.scheme, parts.netloc, path, "&".join(query), parts.fragment)), found


def _request(commit: dict[str, Any], p: SiteProfile, site: str = "", field: str = "") -> dict[str, Any]:
    """The commit gate request block from the traced request: {method, host, path, op?} (or a url).
    When the field has a reviewed save endpoint (openajar.node.sites.PROFILE_EDIT_ENDPOINTS) the
    block also pins its `body_key` to the run's own value (`match`, `${value}` filled with the
    run's escaped value when the run starts, FlowExecutor._scope_commit): a save whose field holds
    anything else is refused before it leaves, and so (the guard's strict form check) is one that
    changes any other field of the form."""
    if not isinstance(commit, dict):
        raise LearnRefused("the commit request is {method, host, path, op}")
    method = str(commit.get("method") or "").upper()
    if method not in ("POST", "PUT", "PATCH"):
        raise LearnRefused(f"the traced save is a {method or 'request with no method'}; a commit is POST, PUT or PATCH")
    host, path = commit.get("host"), commit.get("path")
    if commit.get("url"):
        parts = urlsplit(str(commit["url"]))
        if parts.scheme != "https":
            raise LearnRefused("the traced save is not https")
        host, path = parts.hostname, parts.path
    host = str(host or "").lower()
    path = str(path or "/").split("?", 1)[0].split("#", 1)[0]
    if not host or not host_in(host, p.domain):
        raise LearnRefused(f"the traced save went to {host or 'no host'}, not {p.domain}")
    if not path.startswith("/"):
        raise LearnRefused(f"the traced save's path {path!r} does not start with /")
    out: dict[str, Any] = {"method": method, "url": f"^https://{re.escape(host + path)}$"}
    if commit.get("op"):
        out["op"] = f"^{re.escape(str(commit['op']))}$"
    from openajar.node.sites import profile_edit_endpoint

    ep = profile_edit_endpoint(site, field) if site and field else None
    if ep is not None:
        if (ep.method, ep.host) != (method, host) or re.fullmatch(ep.path, path) is None \
                or (commit.get("op") or None) != ep.op:
            raise LearnRefused(f"the traced save ({method} {host}{path}) is not {p.title}'s reviewed {field} save "
                               f"({ep.method} {ep.host}{ep.path})")
        out["match"] = {ep.body_key: f"^{_TEMPLATE % VALUE}$"}
    return out


def _acting_as(p: SiteProfile, lang: str) -> dict[str, Any]:
    out = copy.deepcopy(p.acting_as)
    if isinstance(out.get("locator"), dict):
        if lang not in p.labels:
            raise LearnRefused(f"{p.title}'s account link is not known in {lang!r} yet ({', '.join(sorted(p.labels))})")
        out["locator"]["names"] = {lang: p.labels[lang]}
    return out


# ---------------------------------------------------------------- the document


def generate(*, site: str, field: str, events: list[dict[str, Any]] | None = None,
             readback_locator: dict[str, Any], profile_url: str, commit: dict[str, Any], limit: int | None,
             account_id: str, value: str, old_value: str | None = None, account_language: str = "en",
             version: str = "1.0.0", draft: Any = None, page_url: str | None = None,
             edit_paths: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
    """The connector document for one learned field: `<field>` (commit) and `<field>_read` (read).
    `events` is what the recording kept (Recorder.stop()["events"]); `commit` the traced save
    ({method, host, path, op} or a url) and the act that sent it (`act_seq` or `act_at`).
    `profile_url` is the account's profile as seen while learning and `account_id` the learned
    account's id (what the surface binds as acts_as_id); `value` the value the learning saved and
    `old_value` the one before it. `draft` (Recorder.build_checked's result) is optional: only its
    problem is read (the steps are fixed). `page_url`: the page that sent the released save, as
    the node's guard saw it (learn_result's page_url): the recording must have typed on that same
    page. `edit_paths`: the site's reviewed edit page prefixes (openajar.node.sites.profile_edit_paths
    for the pinned account): the edit page the operation opens must be under one of them; the
    learner always passes both. Raises LearnRefused with what is wrong. The caller
    compiles and installs it (ConnectorService.install_learned), which runs the strict lint with
    the commit gate."""
    p = _check_request(site, field, version, account_id, value)
    lang = account_language
    acting_as = _acting_as(p, lang)
    profile = _check_profile_url(p, acting_as, profile_url, account_id)
    values = [value, old_value or ""]
    named = _names_value(readback_locator, values)
    if named is not None:
        raise LearnRefused(f"the readback locator names {named!r}, which holds the value: it would stop matching "
                           "once the value changes. Point at the element by its place (a landmark, an attribute)")
    acts = _recorded(events, draft)
    save, typed = _check_recording(acts, commit, field, value)
    field_raw = _clean(typed[-1]["locator"], "typing")
    save_raw = _clean(save.get("locator"), "save")
    for what, loc in (("typing", field_raw), ("save", save_raw)):
        named = _names_value(loc, values)
        if named is not None:
            raise LearnRefused(f"the recorded {what} finds its element by {named!r}, which holds the value: it "
                               "would stop matching once the value changes. Record it again on a control named "
                               "for what it is")
    page = _edit_page(acts, typed, p, page_url, edit_paths)
    edit_page = for_account(page, account_id)[0]
    anchor = p.anchor
    field_loc = _locator(field_raw, lang, anchor)
    learned = _Learned(p=p, site=site, field=field, acting_as=acting_as, profile=profile, edit_page=edit_page,
                       field_loc=field_loc, shown=_locator(readback_locator, lang, {"role": "main"}),
                       most=limit or DEFAULT_LIMIT)
    save_step = _save_step(save, _locator(save_raw, lang, anchor), field_loc, dialog=anchor.get("role") == "dialog")
    return {
        "connector": connector_key(site), "version": version, "site": site, "domain": p.domain,
        "title": f"{p.title} profile (learned)", "account_languages": [lang],
        "operations": {field: _commit_operation(learned, typed[-1], save_step, commit),
                       f"{field}{READ_SUFFIX}": _read_operation(learned)},
    }


def _check_request(site: str, field: str, version: str, account_id: str, value: str) -> Any:
    """The site, the field, the version, the account id and the value, before the recording is read.
    Returns the site's profile."""
    p = SITE_PROFILES.get(site)
    if p is None:
        raise LearnRefused(f"openajar does not learn {site!r} profiles yet (it knows {', '.join(sorted(SITE_PROFILES))})")
    if field in UNLEARNABLE_FIELDS:
        raise LearnRefused(f"{field} is never learned or synced: {p.title} rate-locks name changes for weeks, "
                           "so a wrong one is hard to undo. Change it by hand")
    if not OPERATION_RE.match(field or "") or field.endswith(READ_SUFFIX):
        raise LearnRefused(f"{field!r} is not a field name (lowercase_with_underscores, not ending in {READ_SUFFIX})")
    if not SEMVER_RE.match(version):
        raise LearnRefused(f"version {version!r} is not MAJOR.MINOR.PATCH")
    if not isinstance(account_id, str) or not account_id.strip() or "/" in account_id or ACTS_AS in account_id:
        raise LearnRefused(f"the account id {account_id!r} is not an id")
    if not isinstance(value, str) or not value.strip():
        raise LearnRefused("the learned value is empty")
    return p


def _check_profile_url(p: Any, acting_as: dict[str, Any], profile_url: str, account_id: str) -> str:
    """The profile URL must be the learned account's own. Returns it as the connector opens it."""
    _url_on(profile_url, p, "profile URL")
    if acting_as.get("source") == "url":
        m = re.search(acting_as["pattern"], profile_url)
        if m is None or m.group(1).casefold() != account_id.casefold():
            raise LearnRefused(f"the profile URL {profile_url} is not the account {account_id}'s ({p.id_is})")
    profile, found = for_account(profile_url, account_id)
    if not found:
        raise LearnRefused(f"the profile URL {profile_url} does not carry the account id {account_id!r} "
                           f"({p.title}'s id is {p.id_is}), so one connector could not open another account's")
    return profile


def _check_recording(acts: list[dict[str, Any]], commit: Any, field: str,
                     value: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The save is the act that sent the released request, and nothing chosen before it is
    replayed. Returns the save and the typing acts before it."""
    at = _save_at(acts, commit if isinstance(commit, dict) else {})
    save, before = acts[at], acts[:at]
    if save.get("action") not in SEND_TYPES:
        raise LearnRefused(f"the act that sent the save was a {save.get('action')}, not a click or a key press")
    if _role(save) not in BUTTON_ROLES or _toggled(save):
        raise LearnRefused(f"the act that sent the save ({_what(save)}) is not a button: a check, a toggle or a "
                           "choice that happens to send the form is never replayed as a save")
    for ev in before:
        if _is_toggle(ev):
            raise LearnRefused(f"the recording made a choice before saving ({_what(ev)}); it would be sent with "
                               "every sync. Learn again, changing only the field")
        if ev.get("action") == "upload":
            raise LearnRefused("the recording uploads a file; photos are learned in a later slice")
        if ev.get("action") in TYPED and (ev.get("sensitive") or "text" not in ev):
            raise LearnRefused("the recording typed into a sensitive field; nothing of it is learned")
    typed = [ev for ev in before if ev.get("action") in TYPED]
    if not typed:
        raise LearnRefused("the recording typed nothing before the save, so there is no value to set")
    fields = {json.dumps(_clean(ev.get("locator"), "typing"), sort_keys=True) for ev in typed}
    if len(fields) != 1:
        raise LearnRefused(f"the recording typed into {len(fields)} fields; learning {field} types into exactly one")
    if any(str(ev.get("text")) != value for ev in typed):
        raise LearnRefused(f"the recording typed something other than exactly the value to learn for {field}")
    return save, typed


def _edit_page(acts: list[dict[str, Any]], typed: list[dict[str, Any]], p: Any, page_url: str | None,
               edit_paths: list[str] | tuple[str, ...] | None) -> str:
    """The page the recording typed on: the save's own page, and one of the site's edit pages."""
    navs = [ev.get("url") for ev in acts[: acts.index(typed[0])] if ev.get("action") == "navigate" and ev.get("url")]
    page = typed[-1].get("url") or (navs[-1] if navs else None)
    if page_url:
        seen = _url_on(str(page_url), p, "page that sent the save")
        if page and not _same_path(str(page), seen):
            raise LearnRefused(f"the recording typed on {page}, but the save was sent from {seen}: learn again, "
                               "typing and saving on the site's edit page")
        page = page or seen
    if not page:
        raise LearnRefused("the recording does not open the edit page (start recording before navigating to it)")
    page = _url_on(str(page), p, "edit page")
    if edit_paths is not None:
        from openajar.node.egress.learn_window import path_under

        if not edit_paths or not path_under(page, tuple(edit_paths)):
            raise LearnRefused(f"the edit page {page} is not one of {p.title}'s reviewed profile edit pages "
                               f"({', '.join(edit_paths) or 'none'})")
    return page


@dataclass(frozen=True)
class _Learned:
    """What both learned operations are built from."""
    p: Any
    site: str
    field: str
    acting_as: dict[str, Any]
    profile: str
    edit_page: str
    field_loc: dict[str, Any]
    shown: dict[str, Any]
    most: int


def _save_step(save: dict[str, Any], locator: dict[str, Any], field_loc: dict[str, Any], *,
               dialog: bool) -> dict[str, Any]:
    """The act that saves, and what proves it saved: the dialog closes, or the field holds the value."""
    saved_rb: dict[str, Any] = (
        {"locator": {"role": "dialog"}, "condition": {"kind": "absent"}, "timeout_ms": 30000}
        if dialog else
        {"locator": copy.deepcopy(field_loc), "condition": {"kind": "value", "equals": _TEMPLATE % VALUE},
         "timeout_ms": 15000})
    save_step: dict[str, Any] = {"type": save["action"], "label": SAVE, "writes": True,
                                 "locator": locator, "readback": saved_rb}
    if save["action"] == "press":
        save_step["key"] = str(save.get("key") or "Enter")
    return save_step


def _opening(p: Any, url: str) -> list[dict[str, Any]]:
    """Open `url` as the account: through the site's identity page first when it has one."""
    identity = {"type": "assert_identity", "label": "acting as"}
    go = {"type": "navigate", "url": url}
    return [{"type": "navigate", "url": p.identity_url}, identity, go] if p.identity_url else [go, identity]


def _commit_operation(lr: _Learned, last_typed: dict[str, Any], save_step: dict[str, Any],
                      commit: dict[str, Any]) -> dict[str, Any]:
    p, field, field_loc, shown, profile, edit_page = lr.p, lr.field, lr.field_loc, lr.shown, lr.profile, lr.edit_page
    after = last_typed.get("state_after") or {}
    filled = ({"kind": "value", "equals": _TEMPLATE % VALUE} if after.get("value") is not None or "value" not in after
              else {"kind": "text", "contains": _TEMPLATE % VALUE})
    body = [{"type": "wait_for", "locator": copy.deepcopy(field_loc)},
            {"type": "fill", "locator": copy.deepcopy(field_loc), "param": VALUE, "publishes": False,
             "readback": {"condition": filled}}]
    preflight: list[dict[str, Any]] = [{"kind": "value_equals", "locator": copy.deepcopy(field_loc), "param": VALUE}]
    if _same_page(edit_page, profile):  # the value shows where it is edited: the same value saves nothing
        preflight.append({"kind": "text_differs", "locator": copy.deepcopy(shown), "text": _TEMPLATE % VALUE})
    value_schema: dict[str, Any] = {"type": "string", "minLength": 1, "maxLength": lr.most, "show": True,
                                    "readOnly": True, "pattern": MULTI_LINE if field in MULTILINE else SINGLE_LINE}
    return {
        "title": f"Set the account's {field.replace('_', ' ')} (learned)",
        "description": (f"Learned on this node from one edit an agent made in a lane, and replayed with no model. "
                        f"Opens {edit_page}, sets {field} and saves; proved on {profile} reloaded as the account."),
        "effect": "commit",
        "input": {"type": "object", "additionalProperties": False, "required": [VALUE],
                  "properties": {VALUE: value_schema}},
        "output": {"type": "object", "additionalProperties": False, "required": ["proof_url"],
                   "properties": {"proof_url": {
                       "type": "string", "format": "uri", "x-agentos-host": p.domain, "maxLength": 300,
                       "pattern": p.account,
                       "description": "the profile page, reloaded as the account, where the readback found the value"}}},
        "acting_as": copy.deepcopy(lr.acting_as),
        "pace": {"min_gap_minutes": 60, "per_day": 2, "per_week": 5, "active_hours": True},
        "signals": {"hold": list(HOLD)},
        "commit": {"step": SAVE, "strict": True, "preflight": preflight, "request": _request(commit, p, lr.site, field)},
        "readback": {"live": {"save_as": "public", "outputs": {"proof_url": "public.url"}}},
        "discard": [{"type": "press", "key": "Escape", "publishes": False}],
        "steps": [
            *_opening(p, edit_page), *body, save_step,
            {"type": "pause", "min_ms": 2000, "max_ms": 4000},   # the save's request lands before the reload
            {"type": "navigate", "url": profile},
            {"type": "readback", "save_as": "public", "timeout_ms": 20000, "locator": copy.deepcopy(shown),
             "condition": {"kind": "text", "equals": _TEMPLATE % VALUE}},
        ],
    }


def _read_operation(lr: _Learned) -> dict[str, Any]:
    p, field, shown, profile, most = lr.p, lr.field, lr.shown, lr.profile, lr.most
    return {
        "title": f"Read the account's {field.replace('_', ' ')} (learned)",
        "description": f"Reads what the account's profile ({profile}) shows as {field}, signed in as the account.",
        "effect": "read",
        "input": {"type": "object", "additionalProperties": False, "properties": {}},
        "output": {"type": "object", "additionalProperties": False, "required": [VALUE],
                   "properties": {VALUE: {"type": "string", "maxLength": most,
                                          "description": f"the {field} the profile shows"}}},
        "acting_as": copy.deepcopy(lr.acting_as),
        "pace": {"per_hour": 4, "per_day": 60, "active_hours": True},
        "signals": {"hold": list(HOLD)},
        # an extract keeps up to max_chars (a saved element state keeps 300 characters)
        "readback": {"live": {"outputs": {VALUE: "current.items.0.value"}}},
        "steps": [*_opening(p, profile),
                  {"type": "wait_for", "locator": copy.deepcopy(shown), "timeout_ms": 20000},
                  {"type": "extract", "save_as": "current", "items": copy.deepcopy(shown), "max_items": 1,
                   "fields": {VALUE: {"read": "text", "max_chars": most, "untrusted": True}}}],
    }
