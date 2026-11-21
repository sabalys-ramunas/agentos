"""The public gauntlet: a scratch lane, through a real proxy, visits public bot-detection panels and
sites behind Cloudflare, DataDome, Akamai, F5 and friends, and each visit gets a verdict.

Targets and the ok / gated / blocked rules are ported from Ian L. Paterson's anti-detect browser
benchmark (github.com/ianlpaterson/anti-detect-browser-bench, MIT, 2026), so our numbers compare with
his published ones (Camoufox 135: 25 ok, 3 gated, 3 blocked). Panels that print a score (CreepJS,
incolumitas, BrowserScan, Pixelscan, deviceandbrowserinfo, rebrowser, Sannysoft) get it read out of
the page text. The TLS endpoints' fingerprint is kept for comparison with stock Firefox.

Every page is reached the way an agent reaches it (`browser_navigate`), and on the panels that
score behaviour the lane also moves, scrolls and hovers through node/human.py first.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---- targets ------------------------------------------------------------------------------------

PANEL, TLS, PROTECTED, CONTENT = "panel", "tls", "protected", "content"


@dataclass(frozen=True)
class Target:
    name: str
    url: str
    kind: str
    settle_s: float = 8
    behave: bool = False  # move, scroll and hover before reading (behavioural scores)
    names_a_vendor: bool = False  # its own title or page says CAPTCHA: that is not a challenge


TARGETS: list[Target] = [
    Target("sannysoft", "https://bot.sannysoft.com/", PANEL, 4),
    Target("creepjs", "https://abrahamjuliot.github.io/creepjs/", PANEL, 12),
    Target("browserleaks", "https://browserleaks.com/javascript", PANEL, 4),
    Target("browserscan-bot", "https://www.browserscan.net/bot-detection", PANEL, 10),
    Target("pixelscan-bot", "https://pixelscan.net/bot-check", PANEL, 10),
    Target("pixelscan-fp", "https://pixelscan.net/fingerprint-check", PANEL, 10),
    Target("bot-incolumitas", "https://bot.incolumitas.com/", PANEL, 16, behave=True),
    Target("rebrowser-detector", "https://bot-detector.rebrowser.net/", PANEL, 10),
    Target("deviceandbrowserinfo", "https://deviceandbrowserinfo.com/are_you_a_bot", PANEL, 10, behave=True),
    Target("recaptcha-score", "https://antcpt.com/score_detector/", PANEL, 14, behave=True, names_a_vendor=True),
    Target("fingerprint-playground", "https://demo.fingerprint.com/playground", PANEL, 12, names_a_vendor=True),
    Target("fingerprint-scan", "https://fingerprint-scan.com/", PANEL, 12, behave=True),
    Target("tls-peet", "https://tls.peet.ws/api/all", TLS, 2),
    Target("browserleaks-tls", "https://tls.browserleaks.com/json", TLS, 2),
    Target("nowsecure-cf", "https://nowsecure.nl/", PROTECTED, 12),
    Target("crunchbase-cf", "https://www.crunchbase.com/", PROTECTED, 8),
    Target("canadianinsider", "https://www.canadianinsider.com/", PROTECTED, 6),
    Target("sedarplus", "https://www.sedarplus.ca/landingpage/", PROTECTED, 8),
    Target("glassdoor", "https://www.glassdoor.com/Reviews/index.htm", PROTECTED, 8),
    Target("ceo-ca", "https://ceo.ca/scan", CONTENT, 6),
    Target("stockwatch", "https://www.stockwatch.com/News/Search?hours=24&region=C", CONTENT, 6),
    Target("newsfilecorp", "https://www.newsfilecorp.com/search?q=gold", CONTENT, 6),
    Target("medium", "https://medium.com/", CONTENT, 8),
    Target("devto", "https://dev.to/", CONTENT, 8),
    Target("reddit", "https://www.reddit.com/", CONTENT, 10),
    Target("github-explore", "https://github.com/explore", CONTENT, 8),
    Target("linkedin-jobs", "https://www.linkedin.com/jobs/search?keywords=engineer", CONTENT, 8),
    Target("amazon-product", "https://www.amazon.com/dp/B08N5WRWNW", CONTENT, 8),
    Target("google-search", "https://www.google.com/search?q=anti-detect+browser", CONTENT, 8),
    Target("instagram-post", "https://www.instagram.com/p/CwgPe2erjLY/", CONTENT, 8),
    Target("tiktok-user", "https://www.tiktok.com/@tiktok", CONTENT, 8),
    Target("booking-search", "https://www.booking.com/searchresults.html?ss=Victoria%2C+BC", CONTENT, 10),
    Target("x-explore", "https://x.com/explore", CONTENT, 10),
    Target("indeed-jobs", "https://www.indeed.com/jobs?q=engineer", CONTENT, 8),
    Target("stackoverflow", "https://stackoverflow.com/questions/tagged/python", CONTENT, 6),
]

# Camoufox 135.0.1-beta.24 in Paterson's 2026 runs (best of 3), for comparison.
PATERSON_CAMOUFOX = {"bot-incolumitas": "gated", "medium": "gated", "sedarplus": "gated",
                     "canadianinsider": "blocked", "devto": "blocked", "glassdoor": "blocked"}

# ---- verdicts (ported from Paterson's bench.py `_classify`, MIT) ------------------------------

OK, GATED, BLOCKED, ERROR = "ok", "gated", "blocked", "error"
GATE_TITLE = re.compile(
    r"(just a moment|attention required|access denied|cloudflare|verifying|performing security verification|"
    r"support id|please verify|are you a human|robot check|captcha|radware block page|shieldsquare|"
    r"access to this page has been denied|security check|incapsula|imperva|^error$|blocked by|"
    r"checking your browser)", re.IGNORECASE)
GATE_BODY = re.compile(
    r"(your support id is:|enable javascript and cookies to continue|cf-error-details|"
    r"cdn-cgi/challenge-platform|/cdn-cgi/bm/cv/|data:image/svg.*cloudflare|shieldsquare|perfdrive\.com|"
    r"aperture\.js|ssjsinternal|_incapsula_resource|datadome|px-cdn\.net|/_px/|"
    r"akamaitechnologies\.com.{0,200}block|window\.location\.reload.{0,100}cookie)", re.IGNORECASE)
BLOCKED_STATUSES = {403, 406, 429, 451, 503}


CHALLENGED = "challenged"  # a challenge (403 or "Just a moment") came first, and the real page loaded after it
BLOCK_TEXT = re.compile(
    r"(additional verification required|sorry, you have been blocked|you have been blocked|access denied|"
    r"request unsuccessful|pardon our interruption|press (?:&|and) hold|verify you are (?:a )?human|"
    r"unusual traffic|are you a robot)", re.IGNORECASE)
REAL_PAGE_CHARS = 1500  # visible text past which a page is the site itself, not a challenge or block page


def judge(status: int | None, title: str, body: str, text: str) -> tuple[str, str]:
    """Our verdict, read from what is on screen after the page settled (a solved challenge leaves the
    real page): blocked or gated when the final page is a block or a challenge; challenged when the
    first response was a challenge but the site loaded after it; else ok. Vendor script names only
    count on an untitled shim page (Radware, F5): every protected site carries its vendor's script,
    and a challenge page names itself in its title ("Just a moment...")."""
    if status is None and not title and not body and not text:
        return ERROR, "nothing loaded"
    visible = (text or "").strip()
    real = len(visible) >= REAL_PAGE_CHARS
    if m := BLOCK_TEXT.search(visible[:3000] if not real else ""):
        return BLOCKED, f"page: {m.group(0)}"
    if m := GATE_TITLE.search(title or ""):
        return GATED, f"title: {m.group(0)}"
    if not (title or "").strip() and not real and (m := GATE_BODY.search((body or "")[:50_000])):
        return GATED, f"an untitled shim page with {m.group(0)[:30]}"
    if status in BLOCKED_STATUSES:
        return (CHALLENGED, f"HTTP {status} first, then the site") if visible else (BLOCKED, f"HTTP {status}")
    return OK, ""


def classify(status: int | None, title: str, body: str) -> tuple[str, str]:
    """(verdict, why). Paterson's rules: a blocking HTTP status is blocked; a challenge title or a
    vendor's challenge markup in the first 50 KB, or a near-empty page, is gated; else ok."""
    if status is None and not title and not body:
        return ERROR, "nothing loaded"
    if status in BLOCKED_STATUSES:
        return BLOCKED, f"HTTP {status}"
    if m := GATE_TITLE.search(title or ""):
        return GATED, f"title: {m.group(0)}"
    if m := GATE_BODY.search((body or "")[:50_000]):
        return GATED, f"page: {m.group(0)[:40]}"
    if body and len(body) < 800 and "<script" not in body.lower():
        return GATED, "a near-empty page"
    return OK, ""


# ---- reading the panels' scores ------------------------------------------------------------------

def _num(pattern: str, text: str, flags: int = re.IGNORECASE) -> float | None:
    m = re.search(pattern, text, flags)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (ValueError, IndexError):
        return None


def _has(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE) is not None


def _creepjs(text: str) -> dict[str, Any]:
    return {k: v for k, v in {
        "headless_pct": _num(r"(\d+)%\s*headless", text) or _num(r"headless:?\s*(\d+)%", text),
        "like_headless_pct": _num(r"(\d+)%\s*like headless", text),
        "stealth_pct": _num(r"(\d+)%\s*stealth", text) or _num(r"stealth:?\s*(\d+)%", text),
        "lies": _num(r"lies\s*\(?(\d+)\)?", text),
    }.items() if v is not None}


def _incolumitas(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    score = _num(r"behavioralClassificationScore\W+([01](?:\.\d+)?)", text)
    if score is not None:
        out["behavioral_score"] = score
    out["fail_rows"] = len(re.findall(r"\bFAIL\b", text))
    out["failed_tests"] = re.findall(r'"(\w+)":\s*"FAIL"', text)[:10]
    if "Your Behavioral Score: ..." in text:
        out["behavioral_score"] = "not computed"
    out["ok_rows"] = len(re.findall(r"\bOK\b", text))
    return out


def _browserscan(text: str) -> dict[str, Any]:
    m = re.search(r"Test Results:?\s*(\w+)", text, re.IGNORECASE)
    return {"result": m.group(1)} if m else {}


def _pixelscan(text: str) -> dict[str, Any]:
    """Pixelscan's verdict cards (its explainer text below them repeats the same words, so only the
    verdict phrases count)."""
    out: dict[str, Any] = {}
    if _has(r"you're definitely a human|no automated behavior detected", text):
        out["bot"] = "human"
    elif _has(r"you're (definitely |likely )?a bot|automated behavior detected", text):
        out["bot"] = "bot"
    m = re.search(r"Your Browser Fingerprint is\s+(\w+)", text, re.IGNORECASE)
    if m:
        out["fingerprint"] = m.group(1).lower()
    if _has(r"masking detected", text):
        out["masking"] = "detected"
    if _has(r"no proxy detected", text):
        out["proxy"] = "not detected"
    elif _has(r"proxy detected", text):
        out["proxy"] = "detected"
    return out


def _deviceandbrowserinfo(text: str) -> dict[str, Any]:
    if _has(r"you are (a )?human", text):
        return {"verdict": "human"}
    if _has(r"you are a bot|bot detected", text):
        return {"verdict": "bot"}
    return {}


CHROME_ONLY = re.compile(r"Chrome\s*(?:\(New\))?\s*missing \(failed\)", re.IGNORECASE)


def _sannysoft(text: str) -> dict[str, Any]:
    """Failed rows, leaving out the Chrome-only row every Firefox fails (window.chrome is missing)."""
    fails = len(re.findall(r"\(failed\)|\bfailed\b", text, re.IGNORECASE))
    benign = len(CHROME_ONLY.findall(text))
    return {"failed": fails - benign, "chrome_only_failed": benign}


def _count_fails(text: str) -> dict[str, Any]:
    return {"failed": len(re.findall(r"\bfail(?:ed)?\b", text, re.IGNORECASE))}


def _recaptcha(text: str) -> dict[str, Any]:
    return {"score": _num(r"your score is:\s*([\d.]+)", text)}


def _fingerprint(text: str) -> dict[str, Any]:
    """Fingerprint's Smart Signals: each row is its name, then "Not detected" or what it found."""
    names = ("Bot", "VPN", "Browser Tampering", "Virtual Machine", "Proxy Detection", "Privacy Settings",
             "Rare Device", "IP Blocklist", "Developer Tools")
    found = [n for n in names if (m := re.search(rf"^{n}\s*\n\s*(.+)$", text, re.MULTILINE))
             and "not detected" not in m[1].lower()]
    return {"suspect_score": _num(r"Suspect Score\s*\n?\s*(\d+)", text), "found": found}


def _fingerprint_scan(text: str) -> dict[str, Any]:
    return {"bot_score": _num(r"Bot score\s*(\d+)/100", text)}


PARSERS: dict[str, Callable[[str], dict[str, Any]]] = {
    "recaptcha-score": _recaptcha, "fingerprint-playground": _fingerprint, "fingerprint-scan": _fingerprint_scan,
    "creepjs": _creepjs, "bot-incolumitas": _incolumitas, "browserscan-bot": _browserscan,
    "pixelscan-bot": _pixelscan, "pixelscan-fp": _pixelscan, "deviceandbrowserinfo": _deviceandbrowserinfo,
    "sannysoft": _sannysoft, "rebrowser-detector": _count_fails,
}


def tls_summary(body: str) -> dict[str, Any]:
    """The fingerprints a TLS endpoint reports (tls.peet.ws or browserleaks), from its JSON."""
    m = re.search(r"\{.*\}", body or "", re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {}
    tls = data.get("tls") if isinstance(data.get("tls"), dict) else data
    h2 = data.get("http2") if isinstance(data.get("http2"), dict) else {}
    out = {k: tls.get(k) for k in ("ja3_hash", "ja4", "ja4_r", "peetprint_hash") if tls.get(k)}
    if h2.get("akamai_fingerprint_hash"):
        out["akamai_h2"] = h2["akamai_fingerprint_hash"]
    for k in ("ja3_hash", "ja4", "akamai_hash", "akamai_text"):
        if data.get(k) and k not in out:
            out[k] = data[k]
    return out


# ---- a visit --------------------------------------------------------------------------------------

@dataclass
class Visit:
    target: str
    kind: str
    verdict: str
    why: str = ""
    status: int | None = None
    title: str = ""
    scores: dict[str, Any] = field(default_factory=dict)
    signal: str | None = None
    error: str | None = None
    paterson: str | None = None  # the same visit under Paterson's first-response rules, for comparison

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "", {})}


PROXY_REFUSED = "NS_ERROR_PROXY_FORBIDDEN"
PROXY_RETRY_S = (45, 90)  # a proxy that refuses under load (a connection cap) usually recovers in a minute


async def visit(ix: Any, page: Any, t: Target, out: Path | None = None) -> Visit:
    from openajar.node import human

    for attempt, wait_s in enumerate((0, *PROXY_RETRY_S)):
        if wait_s:
            await page.wait_for_timeout(wait_s * 1000)
        try:
            result = await ix.navigate(page, t.url)
            break
        except Exception as e:  # noqa: BLE001 - a failed load is a verdict
            message = str(e).splitlines()[0][:200]
            if PROXY_REFUSED not in message or attempt == len(PROXY_RETRY_S):
                return Visit(t.name, t.kind, ERROR, error=message)
    status = getattr(result, "status", None)
    if t.behave:
        try:
            await human.glance(page)
            await human.wheel_down(page)
        except Exception:  # noqa: BLE001, S110 - behaviour is best effort; the reading still counts
            pass
    await page.wait_for_timeout(int(t.settle_s * 1000))
    try:
        title = await page.title()
        body = await page.content()
        text = await page.inner_text("body")
    except Exception as e:  # noqa: BLE001
        return Visit(t.name, t.kind, ERROR, status=status, error=f"read: {type(e).__name__}")
    verdict, why = judge(status, title, body, text)
    if t.names_a_vendor and verdict == GATED:
        verdict, why = OK, ""
    v = Visit(t.name, t.kind, verdict, why, status, title[:120], signal=getattr(result, "signal", None),
              paterson=classify(status, title, body)[0])
    if t.name in PARSERS:
        v.scores = PARSERS[t.name](text)
    elif t.kind == TLS:
        v.scores = tls_summary(text)
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{t.name}.txt").write_text(text[:200_000])
        try:
            await page.screenshot(path=str(out / f"{t.name}.png"), full_page=False)
        except Exception:  # noqa: BLE001, S110
            pass
    return v


async def run(ix: Any, page: Any, targets: list[Target], *, out: Path | None = None,
              say: Callable[[str], None] = lambda _: None, rng: random.Random | None = None) -> list[Visit]:
    order = list(targets)
    (rng or random.Random()).shuffle(order)  # no fixed order a site could key on
    visits = []
    for i, t in enumerate(order):
        v = await visit(ix, page, t, out)
        say(f"[{i + 1}/{len(order)}] {t.name}: {v.verdict} {v.why} {json.dumps(v.scores) if v.scores else ''}")
        visits.append(v)
        await page.wait_for_timeout(random.randint(2000, 5000))
    return visits


def summary(visits: list[Visit]) -> dict[str, Any]:
    counts = {k: sum(1 for v in visits if v.verdict == k) for k in (OK, CHALLENGED, GATED, BLOCKED, ERROR)}
    paterson = {k: sum(1 for v in visits if v.paterson == k) for k in (OK, GATED, BLOCKED)}
    differs = {v.target: f"{PATERSON_CAMOUFOX.get(v.target, OK)} -> {v.paterson}" for v in visits
               if v.paterson and v.paterson != PATERSON_CAMOUFOX.get(v.target, OK)}
    return {"counts": counts, "paterson_rules": paterson, "vs_paterson_camoufox135": differs}


def format_visits(visits: list[Visit]) -> str:
    s = summary(visits)
    c = s["counts"]
    p = s["paterson_rules"]
    lines = [(f"gauntlet: {c[OK]} ok, {c[CHALLENGED]} passed a challenge, {c[GATED]} gated, {c[BLOCKED]} blocked, "
             f"{c[ERROR]} error"),
             (f"  by Paterson's first-response rules: {p[OK]} ok, {p[GATED]} gated, {p[BLOCKED]} blocked "
              "(his Camoufox 135: 25 ok, 3 gated, 3 blocked)"), ""]
    for kind in (PANEL, TLS, PROTECTED, CONTENT):
        rows = [v for v in visits if v.kind == kind]
        if not rows:
            continue
        lines.append(kind)
        for v in sorted(rows, key=lambda v: v.target):
            extra = f" {v.why}" if v.why else ""
            scores = f"  {json.dumps(v.scores)}" if v.scores else ""
            lines.append(f"  {v.verdict:8} {v.target:22}{extra}{scores}")
        lines.append("")
    if s["vs_paterson_camoufox135"]:
        lines.append("by his rules, different from his Camoufox 135: " + ", ".join(
            f"{k} {d}" for k, d in sorted(s["vs_paterson_camoufox135"].items())))
    return "\n".join(lines).rstrip()
