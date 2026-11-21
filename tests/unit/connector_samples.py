"""Sample connectors for the runner's fake social site (openajar.runner.fakes.social_site)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from openajar.runner.fakes import BASE

SITES = {"social": ("social.example",)}  # the fake site, in place of openajar.node.sites.SITES
LANE = "biz-a/social"
BINDING = {"acts_as_id": "biz-a", "account_language": "en"}  # what the surface below fills in
# the AUD-01 surface the sample runs act as: its own account, through the lane of the same name
SURFACE = {"id": LANE, "site": "social", "lane": LANE, "acts_as": {"kind": "account", "user_id": "biz-a"},
           "account_language": "en"}

MAIN = {"role": "main"}
DIALOG = {"role": "dialog"}


def read_op() -> dict[str, Any]:
    """Read the account's own profile: post count, the posts label and the profile URL."""
    return {
        "title": "Profile summary",
        "description": "Reads the account's own profile.",
        "effect": "read",
        "input": {"type": "object", "additionalProperties": False, "properties": {}},
        "output": {
            "type": "object", "additionalProperties": False,
            "required": ["post_count", "posts_label", "profile_url"],
            "properties": {
                "post_count": {"type": "integer", "minimum": 0, "description": "posts on the profile"},
                "posts_label": {"type": "string", "pattern": "^[0-9]+ posts$"},
                "profile_url": {"type": "string", "format": "uri", "x-agentos-host": "social.example"},
            },
        },
        "acting_as": {"kind": "account"},
        "pace": {"per_hour": 6, "max_items": 1},
        "signals": {"hold": ["captcha", "checkpoint", "logged_out"]},
        "readback": {"live": {"save_as": "account", "outputs": {
            "post_count": "posts.count", "posts_label": "status.text", "profile_url": "account.url"}}},
        "steps": [
            {"type": "navigate", "url": f"{BASE}/u/${{acts_as_id}}"},
            {"type": "readback", "locator": {"role": "heading", "name": "${acts_as_id}", "within": MAIN},
             "condition": {"kind": "exists"}, "save_as": "account"},
            {"type": "readback", "locator": {"role": "status", "within": MAIN},
             "condition": {"kind": "text", "matches": "^[0-9]+ posts$"}, "save_as": "status"},
            {"type": "wait_for", "state": "any", "locator": {"role": "article", "within": MAIN}, "save_as": "posts"},
        ],
    }


def page_op() -> dict[str, Any]:
    """read_op acting as a Page (fb_page): the Page id is read from the page URL, so a surface
    can act as it through a login in another workspace."""
    op = read_op()
    op["title"] = "Page summary"
    op["acting_as"] = {"kind": "fb_page", "source": "url", "pattern": "^https://social\\.example/u/([a-z0-9-]+)$"}
    op["steps"].insert(1, {"type": "assert_identity"})
    return op


def commit_op() -> dict[str, Any]:
    """Post a photo with a caption: uses every Brief B capability (attr, names,
    assert_identity, the preflight), so it compiles with pending markers."""
    return {
        "title": "Post a photo with a caption",
        "effect": "commit",
        "input": {"type": "object", "additionalProperties": False, "required": ["image", "caption"],
                  "properties": {
                      "image": {"type": "string", "format": "agentos-file", "media": ["image/jpeg", "image/png"],
                                "max_bytes": 8_000_000},
                      "caption": {"type": "string", "maxLength": 2200, "show": True}}},
        "output": {"type": "object", "additionalProperties": False, "required": ["post_url"],
                   "properties": {"post_url": {"type": "string", "format": "uri", "x-agentos-host": "social.example"}}},
        "acting_as": {"kind": "account"},
        "pace": {"min_gap_minutes": 90, "per_day": 3, "active_hours": True},
        "signals": {"benign": ["save_login_info"], "hold": ["captcha", "checkpoint", "logged_out", "wrong_account"]},
        "commit": {
            "step": "share",
            "preflight": [
                {"kind": "value_equals", "locator": {"role": "textbox", "within": DIALOG}, "param": "caption"},
                {"kind": "count", "locator": {"role": "img", "within": DIALOG}, "equals": 1},
                {"kind": "acting_as"},
            ],
            "request": {"method": "POST", "url": "^https://social\\.example/api/posts$", "text": "form:caption",
                        "media_count": "count:upload_id"},
        },
        "readback": {"live": {"save_as": "post", "outputs": {"post_url": "post.url"}}, "public": "cookieless"},
        "discard": [{"type": "press", "key": "Escape", "publishes": False}],
        "steps": [
            {"type": "navigate", "url": f"{BASE}/"},
            {"type": "assert_identity", "label": "acting as"},
            {"type": "click", "locator": {"role": "button", "name": "New post", "within": MAIN}, "publishes": False},
            {"type": "upload", "locator": {"role": "button", "attr": {"name": "type", "equals": "file"},
                                           "within": DIALOG}, "param": "image", "publishes": False},
            {"type": "fill", "locator": {"role": "textbox", "within": DIALOG}, "param": "caption",
             "publishes": False},
            {"type": "click", "label": "share", "writes": True,
             "locator": {"role": "button", "names": {"en": "Share", "lt": "Bendrinti"}, "within": DIALOG}},
            {"type": "readback", "url": f"{BASE}/u/${{acts_as_id}}",
             "locator": {"role": "article", "within": MAIN, "nth": 0},
             "condition": {"kind": "exists"}, "save_as": "post"},
        ],
    }


def connector(ops: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    doc = {"connector": "social", "version": "1.0.0", "site": "social", "domain": "social.example",
           "account_languages": ["en", "lt"],
           "operations": ops if ops is not None else {"profile_summary": read_op()}}
    doc.update(kw)
    return copy.deepcopy(doc)


POST_PARAMS = {"caption": "Autumn at the lake", "image": "photo.jpg"}  # post_photo's input; the binding adds the rest


class Inbox:
    """The runtime "files" service as far as connector approvals need it: each lane's inbox
    directory (openajar.node.flow_driver.inbox_dir), on disk under `root`."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def inbox_dir(self, lane: str) -> Path:
        d = self.root / lane / "inbox"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def put(self, lane: str, name: str, data: bytes) -> Path:
        f = self.inbox_dir(lane) / name
        f.write_bytes(data)
        return f


def bind(h: Any, **fields: Any):
    """Add a surface (SURFACE with `fields` replaced) to a Harness, as the owner."""
    from openajar.runner.fakes import OWNER

    surfaces = h.runner.surfaces
    surfaces.sites = surfaces.sites or SITES
    return surfaces.put({**SURFACE, **fields}, by=OWNER)[0]


def write_dir(root: Path, doc: dict[str, Any], *, fixtures: bool = True) -> Path:
    """A connectors/<site>/ directory with connector.json and (by default) a fixture per
    operation and account language."""
    d = root / doc["connector"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "connector.json").write_text(json.dumps(doc, indent=2))
    if fixtures:
        for op in doc["operations"]:
            for lang in doc.get("account_languages", ["en"]):
                f = d / "fixtures" / op / f"{lang}.html"
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("<main></main>")
    return d


# ---------------------------------------------------------------- a site for running connectors

SHARE = {"en": "Share", "lt": "Bendrinti"}
COMMENTS = {
    "1": [("t1_a", "ana", "2026-11-04T08:12:00Z", "/p/1/c/t1_a", "First comment on one", [
        ("t1_ar", "bo", "2026-11-04T08:20:00Z", "/p/1/c/t1_ar", "A reply inside it", [])]),
          ("t1_b", "cy", "2026-11-04T09:00:00Z", "https://evil.example/phish", "Click my link", [])],
    "2": [("t1_c", "dee", "2026-11-04T10:00:00Z", "/p/2/c/t1_c", "On the second post", [])],
}


SAMPLE_POSTS = [{"id": "1", "caption": "Autumn"}, {"id": "2", "caption": "Lake"}]


def connector_site(*, lang: str = "en", account: str = "biz-a", posts: list[dict[str, Any]] | None = None):
    """The fake social site as a connector sees it: a composer whose file control carries
    type=file, an image once a file is chosen, a Share button named in the account's
    language, profile posts with data-id and links, and post pages whose comments carry
    machine attributes (thingid, author, a time with datetime) and nest their replies."""
    from openajar.runner.fakes import El, FakeSite

    site = FakeSite()
    site.state = {"posts": copy.deepcopy(posts or []), "account": account, "lang": lang,
                  "comments": copy.deepcopy(COMMENTS)}
    site.identity = {"site": "social", "user_id": account, "source": "cookie uid"}

    def home(s, d):
        return El("document", "Home").add(El("main").add(
            El("heading", "Feed"), El("button", "New post", on_click=lambda s, el, d: f"{BASE}/compose")))

    def compose(s, d):
        def share(s, el, d):
            s.state["posts"].append({"id": str(len(s.state["posts"]) + 1), "caption": d.values.get("caption", ""),
                                     "image": d.values.get("file")})
            return f"{BASE}/u/{s.state['account']}"

        dialog = El("dialog", "Create new post").add(
            El("button", "Select from computer", key="file", attrs={"type": "file"}))
        if d.values.get("file"):
            dialog.add(El("img", "Selected photo"))
        dialog.add(El("textbox", "Write a caption...", key="caption"),
                   El("button", SHARE[s.state["lang"]], on_click=share))
        return El("document", "Create post").add(dialog)

    def profile(s, d):
        main = El("main", s.state["account"]).add(
            El("heading", s.state["account"]), El("status", "Posts", text=f"{len(s.state['posts'])} posts"))
        for p in s.state["posts"]:
            main.add(El("article", f"Post {p['id']}", text=p["caption"], attrs={"data-id": p["id"]}).add(
                El("link", "Open", attrs={"href": f"/p/{p['id']}"})))
        return El("document", s.state["account"]).add(main)

    def comment(cid, author, when, link, body, replies):
        el = El("article", f"Comment by {author}", attrs={"thingid": cid, "author": author}).add(
            El("time", "", attrs={"datetime": when}), El("paragraph", "", text=body),
            El("link", "Permalink", attrs={"href": link}))
        for r in replies:
            el.add(comment(*r))
        return el

    def post(s, d):
        pid = d.url.rsplit("/", 1)[1]
        main = El("main").add(El("heading", f"Post {pid}"))
        for c in s.state["comments"].get(pid, []):
            main.add(comment(*c))
        return El("document", f"Post {pid}").add(main)

    site.route(f"{BASE}/", home)
    site.route(f"{BASE}/compose", compose)
    site.route(f"{BASE}/u/*", profile)
    site.route(f"{BASE}/p/*", post)
    return site


def live(driver) -> None:
    """Re-render the fake page after every input, like a real page reacting to it."""
    driver.after_hooks.append(lambda op, target, d: d._render() if op in ("upload", "fill", "type") else None)


def comments_op() -> dict[str, Any]:
    """List comments on the account's own posts: extract the posts, then for each post page
    extract its comments (machine attributes, own text, a host-checked permalink)."""
    comment_item = {
        "type": "object", "additionalProperties": False, "required": ["id", "author", "created_at", "body"],
        "properties": {
            "id": {"type": "string", "pattern": "^t1_[a-z0-9]+$"},
            "author": {"type": "string", "pattern": "^[a-z]{2,20}$"},
            "created_at": {"type": "string", "format": "date-time"},
            "permalink": {"type": ["string", "null"], "format": "uri", "x-agentos-host": "social.example"},
            "body": {"type": "string", "maxLength": 40}}}
    return {
        "title": "List comments on our posts",
        "effect": "read",
        "input": {"type": "object", "additionalProperties": False, "properties": {}},
        "output": {"type": "object", "additionalProperties": False, "required": ["comments", "posts_seen"],
                   "properties": {"comments": {"type": "array", "items": comment_item},
                                  "posts_seen": {"type": "integer", "minimum": 0}}},
        "acting_as": {"kind": "account"},
        "pace": {"per_hour": 6, "max_items": 50},
        "readback": {"live": {"outputs": {"comments": "comments.items", "posts_seen": "posts.count"}}},
        "steps": [
            {"type": "navigate", "url": f"{BASE}/u/${{acts_as_id}}"},
            {"type": "assert_identity"},
            {"type": "extract", "save_as": "posts", "max_items": 2,
             "items": {"role": "article", "attr": {"name": "data-id", "regex": "^[0-9]+$"}, "within": MAIN},
             "fields": {"id": {"attr": "data-id", "pattern": "^[0-9]{1,6}$"},
                        "url": {"locator": {"role": "link"}, "attr": "href", "format": "uri", "host": "social.example"}}},
            {"type": "for_each", "over": "posts", "as": "post", "max_items": 2, "steps": [
                {"type": "navigate", "url": f"{BASE}/p/${{vars.post.id}}"},
                {"type": "extract", "save_as": "comments", "max_items": 50,
                 "items": {"role": "article", "attr": {"name": "thingid", "prefix": "t1_"}, "within": MAIN},
                 "fields": {
                     "id": {"attr": "thingid", "pattern": "^t1_[a-z0-9]+$"},
                     "author": {"attr": "author", "pattern": "^[a-z]{2,20}$", "untrusted": True},
                     "created_at": {"locator": {"attr": {"name": "datetime", "regex": "."}}, "attr": "datetime",
                                    "format": "date-time"},
                     "permalink": {"locator": {"role": "link"}, "attr": "href", "format": "uri", "host": "social.example"},
                     "body": {"locator": {"role": "paragraph"}, "read": "own_text", "max_chars": 12,
                              "untrusted": True}}}]},
        ],
    }
