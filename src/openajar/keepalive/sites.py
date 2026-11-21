"""Where a keepalive visit goes on each site, what counts as an item worth opening, and how often.

One row per site of openajar.node.sites. `home` is the page a person opens first (the feed); `items`
is a path regex for links to one post or article (searched in the link's path, so anchor it with ^);
`cadence` is the range in days between visits when nothing else used the lane. A lane whose site is
not here (or a blog) visits its own first site's front page and opens no item.

The cadences sit well inside every session's known life: Instagram, Facebook and LinkedIn keep a web
session for months but ask an account that returns after weeks to confirm it is them; Reddit and
Medium are more patient. Every visit is drawn inside the range (openajar.keepalive.planner), so no two
lanes keep the same rhythm.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from openajar.core.models import LaneSpec, split_site
from openajar.node import sites as node_sites


@dataclass(frozen=True, slots=True)
class Entry:
    key: str                         # the openajar.node.sites key, or "site" for the lane's own first site
    name: str
    home: str                        # the first page of a visit
    hosts: tuple[str, ...]           # where an item link may lead
    cadence: tuple[float, float]     # days between visits, drawn inside this range
    items: str | None = None         # path regex of one post or article

    def is_item(self, url: str, base: str | None = None) -> bool:
        """A link to one item on this site (never another site, never an action URL)."""
        if self.items is None:
            return False
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        if parts.scheme and parts.scheme not in ("http", "https"):
            return False
        host = (parts.hostname or urlsplit(base or self.home).hostname or "").lower()
        if not any(node_sites.host_is(host, h) for h in self.hosts):
            return False
        return re.search(self.items, parts.path or "/") is not None


TABLE: dict[str, Entry] = {e.key: e for e in (
    Entry("instagram", "Instagram", "https://www.instagram.com/", ("instagram.com",), (2.0, 4.0),
          items=r"^/(?:p|reel)/[\w-]+/?$"),
    Entry("facebook", "Facebook", "https://www.facebook.com/", ("facebook.com",), (3.0, 5.0),
          items=r"^/(?:[\w.]+/posts/[\w-]+|permalink\.php|photo/?|watch/?)$"),
    Entry("linkedin", "LinkedIn", "https://www.linkedin.com/feed/", ("linkedin.com",), (3.0, 6.0),
          items=r"^/feed/update/urn:li:activity:\d+/?$"),
    Entry("reddit", "Reddit", "https://www.reddit.com/", ("reddit.com",), (3.0, 6.0),
          items=r"^/r/\w+/comments/\w+(?:/[\w-]*)?/?$"),
    Entry("medium", "Medium", "https://medium.com/", ("medium.com",), (4.0, 7.0),
          items=r"^/(?:@[\w.-]+|p)/[\w-]+-?[0-9a-f]{8,12}/?$"),
    Entry("wordpress", "WordPress", "https://wordpress.com/reader", ("wordpress.com",), (5.0, 9.0)),
    Entry("x", "X", "https://x.com/home", ("x.com", "twitter.com"), (3.0, 5.0),
          items=r"^/\w+/status/\d+/?$"),
    Entry("google", "Google", "https://myaccount.google.com/", ("google.com",), (5.0, 9.0)),
)}
OWN_SITE_CADENCE = (4.0, 7.0)


def _own_site(spec: LaneSpec) -> str | None:
    """The host of the lane's first page site (not a frame-only entry, not a pattern)."""
    for pattern in spec.sites:
        frame_only, host = split_site(pattern)
        host = host.removeprefix("*.")
        if not frame_only and host and "*" not in host and "@" not in host:
            return host
    return None


def entry_for(spec: LaneSpec) -> Entry | None:
    """The lane's keepalive target: the site it is signed in to (its account, else its name), else its
    own first site's front page, else None (nothing to keep alive). A blog (WordPress) whose lane lists
    its own domain visits that domain: a self-hosted blog is not on wordpress.com."""
    expect = node_sites.expectation_for(spec)
    own = _own_site(spec)
    for key in sorted(expect.sites):
        if key == "wordpress" and own and not node_sites.host_is(own, "wordpress.com"):
            break
        if key in TABLE:
            return TABLE[key]
    if own is None:
        return None
    known = node_sites.site_for(f"https://{own}/")
    if known is not None and known.key in TABLE:
        return TABLE[known.key]
    return Entry("site", own, f"https://{own}/", (own,), OWN_SITE_CADENCE)
