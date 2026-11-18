"""A change shown as a video: record the page in the environment's browser, and the pull request shows it.

On a `limited` network, as an owner sets it: the browser of a browser = "vm" environment was
quarantined there by its exit-IP self-check (2026-11-18, the video-proof builder), so this task is
also that bug's guard. An older machine installs the recorder (ffmpeg) on its first recording, through
the network's proxy: Ubuntu's mirrors are on the list. The first browser_navigate's time is measured
here too (first_navigate_s): the browser starts during the open now.
"""

from __future__ import annotations

from env_eval.node import git
from env_eval.tasks import Agent, Check, Ctx, Session, Task, done

OLD, NEW = "Autumn sale", "Winter sale"


def _says_new(ctx: Ctx, branch: str) -> bool:
    return NEW in git("--git-dir", str(ctx.repo.bare), "show", f"{branch}:server.js", check=False)


async def check(ctx: Ctx) -> list[Check]:
    videos = [p for e in ctx.environments() for p in ctx.node.proofs(e.id) if p.get("kind") == "video"]
    changed = [b for b in ctx.new_branches() if _says_new(ctx, b)]
    pulls = [p for p in ctx.github.opened(ctx.repo.full_name) if p["head"]["ref"] in changed]
    sent = ctx.github.files(ctx.repo.full_name)
    seen = any(NEW in str(r.get("text") or "") for r in ctx.calls("browser_navigate") + ctx.calls("browser_snapshot")
               if r.get("ok"))
    return [
        ("a recording was kept as proof, with a title", any(p.get("mp4_bytes", 0) > 1000 and str(p.get("title") or "")
                                                             .strip() for p in videos)),
        (f"the environment's browser showed the page with {NEW!r}", seen),
        (f"a new branch says {NEW!r}", bool(changed)),
        ("a pull request was opened for it", bool(pulls)),
        ("the recording went to the repository with it", any(k.endswith(".mp4") for k in sent)),
        ("the pull request's body shows the recording", any("agentos-proofs" in str(p.get("body") or "")
                                                             and ".gif" in str(p.get("body") or "") for p in pulls)),
        *await ctx.closed_and_gone(),
    ]


async def perfect(ctx: Ctx, a: Agent, session: int) -> str:
    env = await a.open(title="Winter sale banner", repo=ctx.repo.slug)
    wd = env["workdir"]
    await a.on_call("lane_file_edit", path="server.js", old=f"<h1>{OLD}", new=f"<h1>{NEW}")
    job = await a.on_call("lane_job_start", cmd="npm run dev", cwd=wd)
    await a.on_call("lane_job_read", job=job["id"], until_port=3000, wait_ms=60000)
    await a.via("browser_record", action="start", proof="the shop's home page with the winter sale")
    await a.on_call("browser_navigate", url="http://localhost:3000")
    await a.via("browser_scroll")
    await a.via("browser_record", action="stop")
    await a.sh(f"git checkout -q -b winter-sale && git commit -qam 'Say {NEW}' && git push -q origin winter-sale 2>&1",
               cwd=wd)
    pr = await a.via("environment_pull_request", title=f"Say {NEW} on the home page",
                     body=f"The home page's heading now says {NEW}. The recording shows it.")
    await a.on_call("lane_job_cancel", job=job["id"])
    await a.on_call("environment_close")
    return done(f"Changed the heading to {NEW}, recorded the home page, pushed winter-sale and opened {pr['url']} "
                "with the recording in it. The environment is closed.")


async def careless(ctx: Ctx, a: Agent, session: int) -> str:
    """Keeps a screenshot where a recording was asked for."""
    env = await a.open(title="Winter sale banner", repo=ctx.repo.slug)
    wd = env["workdir"]
    await a.on_call("lane_file_edit", path="server.js", old=f"<h1>{OLD}", new=f"<h1>{NEW}")
    job = await a.on_call("lane_job_start", cmd="npm run dev", cwd=wd)
    await a.on_call("lane_job_read", job=job["id"], until_port=3000, wait_ms=60000)
    await a.on_call("browser_navigate", url="http://localhost:3000")
    await a.on_call("browser_screenshot", proof="home page")
    await a.sh("git checkout -q -b winter && git commit -qam winter && git push -q origin winter 2>&1", cwd=wd)
    pr = await a.via("environment_pull_request", title="winter")
    await a.on_call("environment_close")
    return done(f"Opened {pr['url']} with a screenshot.")


TASK = Task(
    id="t07_video_proof",
    title="record the change in the browser, and the pull request shows the recording",
    repo="shop",
    settings={"network": "limited"},
    internet=True,
    requires=("browser_record",),
    sessions=[Session("In agentos, change the home page heading of {repo} from Autumn sale to Winter sale, record a "
                      "short video in the browser that shows the page with it, push the change on a branch and open "
                      "the pull request with the recording in it.")],
    check=check, perfect=perfect, careless=careless,
)
