"""The replay as one HTML file (`openajar audit replay --html`): what an agent did, step by step, for
someone with no access to the node.

The page is rendered here, on the node, and carries everything it shows: inline CSS, images as
data URIs, no JavaScript, nothing fetched (its Content-Security-Policy refuses all of it). Every value
in it came from the action log or a page an agent read, so every value is escaped and none becomes
markup: a URL is shown as text, never as a link. What it shows is what a share link shows (no grant
ids, screenshots only when asked for), and less: a file cannot be taken back once it is sent, so by
default it leaves out what an agent typed, the arguments' values and the query part of every address
(a one-time code, a session id, an OAuth code). Only the vault's own values were scrubbed from those
when they were written. `include_args` puts them in.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from datetime import datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from openajar.core import toolnames

CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'"
ARGS_CHARS = 4000      # past this, a step's arguments are cut (and the cut says so)
PREVIEW_CHARS = 2000   # the approval card's preview, as the approver saw it (the card cut it at the same size)
TEXT_CHARS = 160       # typed text and URLs inside the one-line description
EMBED_BYTES = 50_000_000  # all the screenshots in one file together, as base64; past it the rest are left out
EMBED_MB = EMBED_BYTES // 1_000_000

EFFECT_WORDS = {"read": "Read", "draft": "Draft", "commit": "Commit"}
STATUS_WORDS = {"ok": "Done", "error": "Failed", "denied": "Refused", "pending": "Waited for approval",
                "unfinished": "Did not finish", "started": "Started"}
CHAIN_WORDS = {
    "ok": "Verified: every step is covered by a signed checkpoint and nothing was changed.",
    "unprovable": "Partly verified: some steps were written after the last signed checkpoint, so they "
                  "cannot be proven yet.",
    "unchained": "Not verifiable: some steps were written before this node's log was chained.",
    "tampered": "Changed after it was written: the log does not match its own hashes or signatures.",
}
HOW_WORDS = {"passkey": "with a passkey", "code": "with a one-time code", "session": "signed in to the panel",
             "cli": "on the node's command line", "push_link": "from a notification link", "chat": "in chat",
             "telegram": "from Telegram",
             "owner_token": "with the node token", "token": "with a token", "device": "on a paired device",
             "standing": "ahead of time, as a standing approval"}
LEVEL_WORDS = {"chat": "answered in chat", "signed_in": "a signed-in person", "present": "a person proved they "
               "were there", "device_key": "a device key signed this exact content"}
ACT_WORDS = {"click": "Clicked", "hover": "Pointed at", "scroll": "Scrolled to", "type": "Typed", "fill": "Filled in",
             "select": "Chose", "press": "Pressed"}
TOOL_WORDS = {"browser_snapshot": "Read the page", "browser_screenshot": "Took a screenshot of the page",
              "browser_scroll": "Scrolled down the page", "browser_wait": "Waited for the page",
              "browser_tabs": "Looked at the open tabs", "browser_upload": "Uploaded a file",
              "browser_dialog": "Looked at an open dialog",
              "browser_claim": "Took the browser for a few acts", "browser_release": "Let go of the browser",
              "browser_open": "Opened the browser", "browser_close": "Closed the browser", "browser_view": "Asked for the live view",
              "vault_login": "Logged in with a login from the vault (the password never left the node)",
              "login": "Logged in with a login from the vault (the password never left the node)"}
# events shown as their own line inside a step (the rest are kept in the log, not on the page)
EVENT_KINDS = ("commit_gate", "commit_observed", "egress_blocked", "link_click", "act_read_back")


# an address's query and fragment (`?code=...`, `#access_token=...`), inside any text
_URL_TAIL = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s?#\"'<>]*)[?#][^\s\"'<>]*")
LEFT_OUT = "?[left out]"


def plain_url(text: str) -> str:
    """`text` with the query and fragment of every address in it replaced by LEFT_OUT."""
    return _URL_TAIL.sub(lambda m: m.group(1) + LEFT_OUT, text)


def esc(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


def _cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "..."


def _local(at: str | None, tz: ZoneInfo, fmt: str = "%H:%M:%S") -> str:
    if not at:
        return ""
    try:
        return datetime.fromisoformat(at).astimezone(tz).strftime(fmt)
    except ValueError:
        return at


def describe(step: dict, *, include_args: bool = False) -> str:
    """What the step did, in plain words, from its tool and arguments (as text, never markup). What
    was typed, and an address's query, only with `include_args`."""
    tool = toolnames.current(step.get("tool") or "")  # a trace from before the rename keeps the old name
    args = step.get("args") if isinstance(step.get("args"), dict) else {}
    if tool == "browser_navigate" and isinstance(args.get("url"), str):
        url = args["url"] if include_args else plain_url(args["url"])
        return f"Opened {_cut(url, TEXT_CHARS)}"
    if tool == "browser_act":
        verb = ACT_WORDS.get(str(args.get("action")), f"Did {args.get('action') or 'something'} on")
        ref, text = args.get("ref"), args.get("text")
        if args.get("action") in ("type", "fill") and text and not include_args:
            n = len(str(text))
            return f"{verb} {n} character{'' if n == 1 else 's'} into element {ref}"
        if args.get("action") in ("type", "fill") and text:
            return f'{verb} "{_cut(str(text), TEXT_CHARS)}" into element {ref}'
        if args.get("action") == "select" and text:
            return f'{verb} "{_cut(str(text), TEXT_CHARS)}" in element {ref}'
        if args.get("action") == "press" and text:
            return f"{verb} {_cut(str(text), 40)}"
        return f"{verb} element {ref}"
    if tool == "browser_dialog" and isinstance(args.get("accept"), bool):
        return "Answered a dialog"
    if tool == "browser_tabs" and isinstance(args.get("select"), int):
        return f"Switched to tab {args['select']}"
    if tool == "flow_run":
        return f"Ran the saved flow {args.get('flow') or args.get('id') or ''}".rstrip()
    if tool in TOOL_WORDS:
        return TOOL_WORDS[tool]
    if tool:
        return f"Used {tool}"
    return step.get("summary") or str(step.get("kind") or "An event")


def _who(steps: list[dict]) -> str:
    seen: list[str] = []
    for s in steps:
        p = s.get("principal") or {}
        if p.get("id"):
            name = str(p["id"]) + (f" ({p['kind']})" if p.get("kind") and p.get("kind") != "agent" else "")
            if name not in seen:
                seen.append(name)
    if not seen:
        return "Nobody recorded"
    return ", ".join(seen[:6]) + (f" and {len(seen) - 6} more" if len(seen) > 6 else "")


def _by(who: Any) -> str:
    """Who decided, as the approvals table records it ("owner", "owner:cli", "chat:ana"), in words."""
    text = str(who or "someone")
    head, _, how = text.partition(":")
    where = {"cli": "on the node", "panel": "in the panel", "push": "from a notification"}.get(how, how)
    if head == "owner":
        return "the owner" + (f" ({where})" if where else "")
    if head == "telegram" and how == "owner":  # the owner's own Telegram chat; HOW_WORDS says "from Telegram"
        return "the owner"
    return f"{how} (in chat)" if head == "chat" and how else text


def _approval(a: dict, tz: ZoneInfo) -> str:
    if not a.get("found"):
        kind = "a standing approval" if a.get("route") == "standing" else "a person's approval"
        return (f'<div class="appr"><p><b>Allowed by {kind}.</b> Its record is no longer on this node, so '
                "who decided is not shown.</p></div>")
    when = _local(a.get("at"), tz, "%Y-%m-%d %H:%M:%S")
    if a.get("route") == "standing":
        head = (f"<b>Allowed by a standing approval</b> that {esc(_by(a.get('by')))} made on {esc(when)}, "
                f"for {esc(a.get('summary'))}.")
        note = f"<p>Its note: {esc(a['note'])}</p>" if a.get("note") else ""
        return f'<div class="appr"><p>{head}</p>{note}</div>'
    how = HOW_WORDS.get(str(a.get("how")), f"through {a['how']}" if a.get("how") else "")
    level = LEVEL_WORDS.get(str(a.get("level")))
    parts = [f"<b>Approved by {esc(_by(a.get('by')))}</b>"]
    if how:
        parts.append(esc(how))
    parts.append(f"at {esc(when)}" if when else "")
    head = " ".join(p for p in parts if p) + "."
    lines = [f"<p>{head}</p>"]
    if level:
        lines.append(f"<p>Confirmation: {esc(level)}"
                     + (f" (this action needed {esc(LEVEL_WORDS.get(str(a['requires']), a['requires']))})"
                        if a.get("requires") else "") + ".</p>")
    if a.get("after_untrusted"):
        lines.append(f"<p>The card said this followed {esc(a['after_untrusted'])}.</p>")
    lines.append(f"<p>They saw: {esc(a.get('summary'))}</p>")
    if a.get("preview"):
        lines.append(f"<pre>{esc(_cut(str(a['preview']), PREVIEW_CHARS))}</pre>")
    if a.get("edited"):
        lines.append(f"<p>They edited it before approving: {esc(', '.join(map(str, a['edited'])))}.</p>")
    return f'<div class="appr">{"".join(lines)}</div>'


def _image(img: dict | None, step: dict) -> str:
    cap = step.get("capture")
    if img is None:
        if step.get("screenshot_left_out") == "size":
            return ('<p class="muted">A screenshot was kept for this step and left out of this file: the file '
                    f"already holds {EMBED_MB} MB of screenshots, the most it carries.</p>")
        if step.get("screenshot_left_out") or cap:
            return '<p class="muted">A screenshot was kept for this step and left out of this file.</p>'
        return ""
    if img.get("missing"):
        return f'<p class="warn">{esc(img["missing"])}</p>'
    note = {"ok": "This image matches the hash the log recorded when it was taken.",
            "mismatch": "This image does not match the hash the log recorded: it was changed after it was "
                        "taken.",
            "unrecorded": "The log recorded no hash for this image (a flow run's proof), so it cannot be "
                          "checked against the log."}[img["check"]]
    cls = "warn" if img["check"] == "mismatch" else "muted"
    data = base64.b64encode(img["data"]).decode()
    alt = esc(img.get("alt") or "Screenshot of the page after this step")
    return (f'<figure><img src="data:{img["mime"]};base64,{data}" alt="{alt}">'
            f'<figcaption class="{cls}">{esc(note)}</figcaption></figure>')


def _step(n: int, s: dict, tz: ZoneInfo, img: dict | None, include_args: bool = False) -> str:
    def clean(text: Any) -> str:
        return str(text) if include_args else plain_url(str(text))

    dl = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in _step_rows(s, clean))
    approvals = "".join(_approval(a, tz) for a in s.get("approvals") or [])
    events = [e for e in s.get("events") or [] if e.get("kind") in EVENT_KINDS and e.get("summary")]
    ev = ""
    if events:
        ev = "<ul class=\"ev\">" + "".join(
            f"<li>{esc(_local(e.get('at'), tz))} {esc(clean(e['summary']))}</li>" for e in events[:20]) + "</ul>"
    args = _step_args(s, include_args)
    return (f'<li class="step" id="s{n}"><div class="head"><span class="time">{esc(_local(s.get("at"), tz))}</span>'
            f'{"".join(_step_tags(s))}<span class="n">Step {n} (seq {esc(s.get("seq"))})</span></div>'
            f"<h2>{esc(describe(s, include_args=include_args))}</h2><dl>{dl}</dl>{approvals}{_image(img, s)}{ev}{args}</li>")


def _step_tags(s: dict) -> list[str]:
    """Its status, its effect, and the two warnings: changed after it was written, after untrusted text."""
    status = STATUS_WORDS.get(str(s.get("status")), str(s.get("status") or ""))
    effect = s.get("effect")
    tags = [f'<span class="tag">{esc(status)}</span>']
    if effect:
        tags.append(f'<span class="tag e-{esc(effect)}">{esc(EFFECT_WORDS.get(effect, effect))}</span>')
    if s.get("chain", {}).get("status") == "tampered":
        tags.append('<span class="tag bad">Changed after it was written</span>')
    if s.get("after_untrusted"):
        tags.append('<span class="tag bad">After untrusted text</span>')
    return tags


def _step_rows(s: dict, clean: Callable[[Any], str]) -> list[tuple[str, str]]:
    """The step's facts, label and HTML: result, error, who, the grant, untrusted text, its run, the
    page after it, how long it took."""
    rows: list[tuple[str, str]] = []
    if s.get("summary"):
        rows.append(("Result", esc(clean(s["summary"]))))
    if s.get("error"):
        rows.append(("Error", f'<span class="warn">{esc(clean(s["error"]))}</span>'))
    who = (s.get("principal") or {}).get("id")
    if who:
        rows.append(("By", esc(who)))
    if s.get("grant"):
        rows.append(("Allowed by", esc((s["grant"] or {}).get("label") or "a grant (its label is not on this node)")))
    if s.get("after_untrusted"):
        rows.append(("Untrusted text", (f"This step came after the agent read {esc(s['after_untrusted'])}. Text on "
                                       "a page can carry instructions, so check it was what you wanted.")))
    run = s.get("run")
    if run:
        proof = (run.get("proof") or {}).get("url")
        rows.append(("Run", esc(f"{run.get('flow') or run.get('id')}: {run.get('status')}")
                     + (f"<br>Proof of done: {esc(clean(proof))}" if proof else "")))
    cap = s.get("capture")
    if cap and cap.get("url"):
        rows.append(("Page after", esc(_cut(clean(cap["url"]), 300))
                     + (f"<br>{esc(_cut(str(cap['title']), 200))}" if cap.get("title") else "")))
    if s.get("duration_ms") is not None:
        ms = int(s["duration_ms"])
        rows.append(("Took", f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f} s"))
    return rows


def _step_args(s: dict, include_args: bool) -> str:
    """Its arguments: their values only with include_args, else their names."""
    if s.get("args") is not None and include_args:
        text = json.dumps(s["args"], indent=1, ensure_ascii=False, default=str)
        return (f"<details><summary>Arguments (redacted when they were written)</summary>"
                f"<pre>{esc(_cut(text, ARGS_CHARS))}</pre></details>")
    if isinstance(s.get("args"), dict) and s["args"]:
        names = ", ".join(str(k) for k in list(s["args"])[:30])
        return f'<p class="muted">Arguments (names only, their values are left out): {esc(names)}</p>'
    return ""

STYLE = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1b1d21;--mute:#5d6470;--line:#dde1e6;--card:#f6f7f9;
--warn:#a3260f;--warnbg:#fdecea;--ok:#1d6b35;--okbg:#e7f4ea;--tag:#eceff3}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--fg:#e6e8eb;--mute:#9aa2ad;--line:#2c3138;
--card:#1a1d21;--warn:#ff8a73;--warnbg:#3a1d17;--ok:#7bd88f;--okbg:#17301e;--tag:#262b31}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,
sans-serif;overflow-wrap:anywhere}
main{max-width:860px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:1.5rem;margin:0 0 4px}h2{font-size:1.05rem;margin:6px 0 8px}
.muted{color:var(--mute)}.warn{color:var(--warn)}
.meta{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;margin:16px 0}
.meta dt,dl dt{color:var(--mute)}dl dd,.meta dd{margin:0}
.chain{padding:10px 12px;border-radius:8px;background:var(--okbg);color:var(--ok);margin:12px 0}
.chain.bad{background:var(--warnbg);color:var(--warn)}
.notice{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:12px 0;color:var(--mute)}
ol.steps{list-style:none;padding:0;margin:24px 0}
.step{border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:0 0 14px;background:var(--card);
break-inside:avoid}
.head{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.time{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.9rem}
.n{margin-left:auto;color:var(--mute);font-size:.85rem}
.tag{background:var(--tag);border-radius:999px;padding:1px 9px;font-size:.8rem}
.tag.e-commit{background:var(--warnbg);color:var(--warn)}.tag.bad{background:var(--warnbg);color:var(--warn)}
.step dl{display:grid;grid-template-columns:max-content 1fr;gap:2px 14px;margin:0}
.appr{border-left:3px solid var(--ok);padding:2px 10px;margin:10px 0}.appr p{margin:4px 0}
pre{white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px;
font-size:.82rem;max-height:none}
figure{margin:10px 0}img{max-width:100%;height:auto;border:1px solid var(--line);border-radius:6px}
figcaption{font-size:.85rem}ul.ev{margin:8px 0;padding-left:18px;font-size:.9rem}
details summary{cursor:default;color:var(--mute)}
footer{border-top:1px solid var(--line);margin-top:24px;padding-top:12px;font-size:.88rem;color:var(--mute)}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.85rem}
@media (max-width:560px){.meta,.step dl{grid-template-columns:1fr}.n{margin-left:0}}
@media print{:root{--bg:#fff;--fg:#000;--card:#fff;--mute:#444}main{max-width:none;padding:0}}
"""


def render(view: dict, *, steps: list[dict], images: dict[str, dict], generated_at: str, tz: str,
           verify: dict, include_args: bool = False) -> str:
    """The whole page. `view` is the replay (or one trace) as the share view returns it; `images` maps
    a step id to its picture ({data, mime, check}); `verify` says how to check the page (footer).
    `include_args` puts in what was typed, the arguments' values and whole addresses."""
    zone = ZoneInfo(tz)
    lane = view.get("lane") or "a lane"
    chain = view.get("chain") or {}
    status = str(chain.get("status") or "unprovable")
    if view.get("day"):
        when = f"{view['day']} ({view.get('timezone') or tz})"
        title = f"{lane} on {view['day']}"
    else:
        first = steps[0].get("at") if steps else None
        when = f"{_local(first, zone, '%Y-%m-%d %H:%M:%S')} ({tz})"
        title = f"One step on {lane}"
    meta = [("Lane", esc(lane)), ("When", esc(when)), ("Who acted", esc(_who(steps))),
            ("Steps", esc(len(steps)) + (" (the day had more; only these are shown)" if view.get("truncated") else "")),
            ("Made", esc(f"{_local(generated_at, zone, '%Y-%m-%d %H:%M:%S')} ({tz})"))]
    findings = [f for f in chain.get("findings") or [] if f.get("level") == "tampered"]
    chain_html = (f'<p class="chain{" bad" if status in ("tampered", "unchained") else ""}">'
                  f"Log check when this file was made: {esc(CHAIN_WORDS.get(status, status))}</p>")
    if findings:
        chain_html += "<ul class=\"warn\">" + "".join(f"<li>{esc(f.get('text'))}</li>" for f in findings[:10]) + "</ul>"
    mismatched = sum(1 for i in images.values() if i.get("check") == "mismatch")
    if mismatched:
        chain_html += (f'<p class="chain bad">{mismatched} screenshot(s) in this file do not match the hash the log '
                       "recorded. They are marked below.</p>")
    body = "".join(_step(i + 1, s, zone, images.get(s["id"]), include_args) for i, s in enumerate(steps))
    if not steps:
        body = '<li class="muted">Nothing was recorded on this lane that day.</li>'
    proven = verify.get("proven_through") or {}
    checks = []
    if proven:
        checks.append(f"Checkpoint {esc(proven.get('mark'))} of chain {esc(verify.get('chain') or 'unknown')} covers the "
                      f"log through row seq {esc(proven.get('seq'))}, signed {esc(proven.get('at'))}"
                      + (f" with audit key {esc(verify['kid'])}" if verify.get("kid") else "") + ".")
    else:
        checks.append("No signed checkpoint covered these rows when this file was made.")
    if verify.get("seq"):
        checks.append(f"The steps are rows seq {esc(verify['seq'][0])} to {esc(verify['seq'][1])} of the log.")
    checks.append("This file is a copy and proves nothing by itself. To check it, ask the node's owner for the "
                  f"signed export of the same day (<code>{esc(verify.get('export'))}</code>) and check that "
                  "with <code>openajar audit verify --bundle FILE</code>, or run "
                  f"<code>{esc(verify.get('verify'))}</code> on the node.")
    foot = "".join(f"<p>{c}</p>" for c in checks)
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{esc(CSP)}">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>{esc('Replay · ' + title)}</title><style>{STYLE}</style></head><body><main>"
        f"<h1>What the agent did: {esc(title)}</h1>"
        '<p class="muted">A replay from the openajar action log, read-only.</p>'
        f'<dl class="meta">{"".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in meta)}</dl>{chain_html}'
        '<p class="notice">Anyone who has this file can read it. It shows what the agent did on this account. '
        + ("Grant ids and secrets are left out. What the agent typed and its arguments are shown as they were "
           "redacted when written.</p>" if include_args else
           "Grant ids, secrets, what the agent typed, the arguments' values and the query part of addresses "
           "are left out.</p>")
        + f'<ol class="steps">{body}</ol><footer>{foot}</footer></main></body></html>\n'
    )
