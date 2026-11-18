"""`agentos environments replay <id>`: an environment's replay written to a folder, from the node's shell.

It reads the replay's files (agentos.environments.replay_store) and the environment's record straight
from AGENTOS_HOME, so it works after the VM is gone and with agentosd stopped. The folder gets:

    frames/<frame id>.jpg   every kept frame
    timeline.json           the environment, and its frames and calls by time (the web API's shape,
                            without links)
    index.html              a contact sheet: a static page that steps through the frames with their
                            captions and the calls between them. No external assets, no network.
"""

from __future__ import annotations

import html
import json
import shutil
from pathlib import Path
from typing import Any

from agentos.core import clock
from agentos.core.errors import NotFound
from agentos.environments.replay_store import ReplayStore, iso_ms, public


def export(home: Path, env_id: str, out: Path) -> dict[str, Any]:
    """Write the replay of environment `env_id` into `out`. Returns what it wrote."""
    from agentos.environments.store import EnvironmentStore

    db = Path(home) / "environments.db"
    env = None
    if db.exists():
        try:
            env = EnvironmentStore(db).get(env_id)
        except (NotFound, ValueError, KeyError):
            env = None
    store = ReplayStore(home)
    frames = store.frames(env_id) if env is not None or _has_folder(store, env_id) else None
    if frames is None:
        raise NotFound(f"no environment {env_id} on this node, and no replay of one")
    calls = store.calls(env_id)
    if not calls and env is not None:
        calls = [{"at": iso_ms(c.at), "tool": c.tool, "summary": c.summary, "ok": c.ok, "error": c.error,
                  "exit_code": c.exit_code, "lane": None} for c in env.calls]
    out.mkdir(parents=True, exist_ok=True)
    (out / "frames").mkdir(exist_ok=True)
    items: list[dict] = []
    copied = 0
    for r in frames:
        src = store.frame_path(env_id, r["id"])
        if src is None:
            continue
        shutil.copyfile(src, out / "frames" / f"{r['id']}.jpg")
        copied += 1
        items.append({"kind": "frame", **public(r), "file": f"frames/{r['id']}.jpg"})
    items.extend({"kind": "call", **c} for c in calls)
    items.sort(key=lambda i: (str(i.get("at") or ""), i["kind"] == "frame"))
    about = {"id": env_id, "title": env.title if env is not None else env_id,
             "state": env.state if env is not None else "unknown",
             "created_at": clock.iso_or_none(env.created_at, z=True) if env is not None else None,
             "closed_at": clock.iso_or_none(env.closed_at, z=True) if env is not None else None}
    timeline = {"environment": about, "timeline": items}
    (out / "timeline.json").write_text(json.dumps(timeline, indent=2))
    (out / "index.html").write_text(contact_sheet(about, items))
    return {"environment": env_id, "folder": str(out), "frames": copied, "calls": len(calls),
            "bytes": sum(int(r.get("bytes") or 0) for r in frames), "page": str(out / "index.html")}


def _has_folder(store: ReplayStore, env_id: str) -> bool:
    try:
        return store.folder(env_id).is_dir()
    except ValueError:
        return False


def contact_sheet(about: dict, items: list[dict]) -> str:
    """The static page: one frame large with its caption, the timeline beside it, keys to step."""
    data = json.dumps({"environment": about, "timeline": items}).replace("</", "<\\/")
    title = html.escape(f"Replay of {about.get('title') or about.get('id')}")
    return PAGE.replace("__TITLE__", title).replace("__DATA__", data)


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  --bg: #fafafa; --panel: #ffffff; --text: #171717; --muted: #6b6b6b; --line: #e5e5e5;
  --accent: #2563eb; --agent: #2563eb; --person: #c2410c; --change: #6b7280; --end: #15803d; --bad: #b91c1c;
  --stage: #f0f0f0;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0a0a0a; --panel: #141414; --text: #ededed; --muted: #a1a1a1; --line: #262626;
    --accent: #60a5fa; --agent: #60a5fa; --person: #fb923c; --change: #9ca3af; --end: #4ade80; --bad: #f87171;
    --stage: #1c1c1c;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
header { padding: 20px 24px 12px; border-bottom: 1px solid var(--line); }
h1 { margin: 0; font-size: 18px; font-weight: 600; letter-spacing: -0.01em; }
.meta { color: var(--muted); margin-top: 4px; font-size: 13px; }
main { display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 0; min-height: calc(100vh - 70px); }
.stage { padding: 20px 24px; display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.shot { background: var(--stage); border: 1px solid var(--line); border-radius: 10px; overflow: hidden;
  aspect-ratio: 16 / 9; display: flex; align-items: center; justify-content: center; }
.shot img { width: 100%; height: 100%; object-fit: contain; display: block; }
.empty { color: var(--muted); padding: 40px; text-align: center; }
.caption { display: flex; flex-direction: column; gap: 4px; min-height: 64px; }
.caption .line1 { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.pill { font-size: 12px; font-weight: 600; padding: 2px 8px; border-radius: 999px; color: #fff; }
.pill.agent { background: var(--agent); } .pill.person { background: var(--person); }
.pill.change { background: var(--change); } .pill.save, .pill.close { background: var(--end); }
.what { font-weight: 600; overflow-wrap: anywhere; }
.when { color: var(--muted); font-variant-numeric: tabular-nums; font-size: 13px; }
.page { color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
.controls { display: flex; align-items: center; gap: 8px; }
button { font: inherit; background: var(--panel); color: var(--text); border: 1px solid var(--line);
  border-radius: 8px; padding: 6px 12px; cursor: pointer; }
button:hover { border-color: var(--muted); }
button:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
input[type=range] { flex: 1; accent-color: var(--accent); min-width: 0; }
.count { color: var(--muted); font-variant-numeric: tabular-nums; font-size: 13px; min-width: 72px; text-align: right; }
.hint { color: var(--muted); font-size: 12px; }
aside { border-left: 1px solid var(--line); background: var(--panel); overflow-y: auto; max-height: calc(100vh - 70px);
  position: sticky; top: 0; }
aside ol { list-style: none; margin: 0; padding: 8px 0; }
aside li { padding: 7px 16px; display: grid; grid-template-columns: 62px 1fr; gap: 8px; cursor: default; font-size: 13px; }
aside li.frame { cursor: pointer; }
aside li.frame:hover { background: var(--stage); }
aside li.on { background: var(--stage); box-shadow: inset 3px 0 0 var(--accent); }
aside .t { color: var(--muted); font-variant-numeric: tabular-nums; }
aside .call .s { color: var(--muted); overflow-wrap: anywhere; display: -webkit-box; -webkit-box-orient: vertical;
  -webkit-line-clamp: 3; line-clamp: 3; overflow: hidden; }
aside .call.failed .s { color: var(--bad); }
aside .frame .s { overflow-wrap: anywhere; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: 1px; }
.dot.agent { background: var(--agent); } .dot.person { background: var(--person); }
.dot.change { background: var(--change); } .dot.save, .dot.close { background: var(--end); }
@media (max-width: 860px) {
  main { grid-template-columns: 1fr; }
  aside { border-left: 0; border-top: 1px solid var(--line); max-height: none; position: static; }
  header, .stage { padding-left: 16px; padding-right: 16px; }
}
</style>
</head>
<body>
<header>
  <h1 id="title"></h1>
  <div class="meta" id="meta"></div>
</header>
<main>
  <section class="stage">
    <div class="shot" id="shot"></div>
    <div class="caption" id="caption"></div>
    <div class="controls">
      <button id="prev" aria-label="Previous frame">Previous</button>
      <button id="play" aria-label="Play">Play</button>
      <button id="next" aria-label="Next frame">Next</button>
      <input type="range" id="scrub" min="0" max="0" value="0" aria-label="Frame">
      <span class="count" id="count"></span>
    </div>
    <div class="hint">Left and right arrows step through the frames, space plays them.</div>
  </section>
  <aside><ol id="list"></ol></aside>
</main>
<script type="application/json" id="data">__DATA__</script>
<script>
(function () {
  var data = JSON.parse(document.getElementById("data").textContent);
  var env = data.environment, items = data.timeline;
  var frames = items.filter(function (i) { return i.kind === "frame"; });
  var WORDS = {agent: "Agent", person: "Person in control", change: "Page changed", save: "Saved", close: "Closed"};
  var at = 0, timer = null;
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls;
    if (text != null) e.textContent = text; return e; }
  function time(s) { return s ? s.slice(11, 19) : ""; }
  function day(s) { return s ? s.slice(0, 10) + " " + s.slice(11, 16) + " UTC" : ""; }
  function said(f) {
    if (f.cause === "agent") return (f.tool || "a call") + (f.summary ? ": " + f.summary : "");
    if (f.cause === "person") return "A person had control in the live view";
    if (f.cause === "change") return "The page changed on its own";
    if (f.cause === "save") return "The environment was saved";
    return "The environment closed";
  }
  document.getElementById("title").textContent = "Replay of " + (env.title || env.id);
  document.getElementById("meta").textContent = env.id + " \\u00b7 " + frames.length + " frames \\u00b7 " +
    (items.length - frames.length) + " calls" + (env.created_at ? " \\u00b7 opened " + day(env.created_at) : "") +
    (env.closed_at ? " \\u00b7 closed " + day(env.closed_at) : "");
  var list = document.getElementById("list"), rows = [];
  items.forEach(function (i) {
    var li = el("li", i.kind + (i.kind === "call" && i.ok === false ? " failed" : ""));
    li.appendChild(el("span", "t", time(i.at)));
    var s = el("span", "s");
    if (i.kind === "frame") {
      s.appendChild(el("span", "dot " + i.cause));
      s.appendChild(document.createTextNode(WORDS[i.cause] + (i.cause === "agent" && i.summary ? ": " + i.summary : "")));
      var n = frames.indexOf(i);
      li.addEventListener("click", function () { show(n); });
      rows[n] = li;
    } else {
      s.textContent = i.tool + (i.summary ? " " + i.summary : "") + (i.ok === false ? " (" + (i.error || "failed") + ")" : "");
    }
    li.appendChild(s);
    list.appendChild(li);
  });
  var scrub = document.getElementById("scrub");
  scrub.max = Math.max(0, frames.length - 1);
  scrub.addEventListener("input", function () { show(+scrub.value); });
  function show(n) {
    var shot = document.getElementById("shot"), cap = document.getElementById("caption");
    shot.textContent = ""; cap.textContent = "";
    if (!frames.length) { shot.appendChild(el("div", "empty", "No frames were kept for this environment."));
      document.getElementById("count").textContent = ""; return; }
    at = Math.max(0, Math.min(frames.length - 1, n));
    var f = frames[at], img = el("img");
    img.src = f.file; img.alt = said(f);
    if (f.width && f.height) shot.style.aspectRatio = f.width + " / " + f.height;
    shot.appendChild(img);
    var l1 = el("div", "line1");
    l1.appendChild(el("span", "pill " + f.cause, WORDS[f.cause]));
    l1.appendChild(el("span", "what", said(f)));
    cap.appendChild(l1);
    cap.appendChild(el("div", "when", f.at ? f.at.slice(0, 10) + " " + time(f.at) + " UTC" : ""));
    if (f.page_title || f.page_url) cap.appendChild(el("div", "page", [f.page_title, f.page_url].filter(Boolean).join(" \\u00b7 ")));
    scrub.value = at;
    document.getElementById("count").textContent = (at + 1) + " of " + frames.length;
    rows.forEach(function (r) { r.classList.remove("on"); });
    if (rows[at]) { rows[at].classList.add("on"); rows[at].scrollIntoView({block: "nearest"}); }
  }
  function play() {
    var b = document.getElementById("play");
    if (timer) { clearInterval(timer); timer = null; b.textContent = "Play"; b.setAttribute("aria-label", "Play"); return; }
    b.textContent = "Pause"; b.setAttribute("aria-label", "Pause");
    timer = setInterval(function () { if (at >= frames.length - 1) { play(); return; } show(at + 1); }, 900);
  }
  document.getElementById("prev").addEventListener("click", function () { show(at - 1); });
  document.getElementById("next").addEventListener("click", function () { show(at + 1); });
  document.getElementById("play").addEventListener("click", play);
  document.addEventListener("keydown", function (e) {
    if (e.target && e.target.tagName === "INPUT") return;
    if (e.key === "ArrowLeft") { show(at - 1); e.preventDefault(); }
    else if (e.key === "ArrowRight") { show(at + 1); e.preventDefault(); }
    else if (e.key === " ") { play(); e.preventDefault(); }
  });
  show(0);
})();
</script>
</body>
</html>
"""
