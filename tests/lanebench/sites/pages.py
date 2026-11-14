"""The fixture pages as element trees: login (with a cookie wall), three interstitials (save
login, consent, a device approval), the feed, the composer and settings.

Behaviour lives in static/page.js and keys off `data-action` and `data-lb`, never off class
names, so the `rename_classes` mutation changes the look of a page and nothing else. Every page
loads static/recorder.js first: a read-only listener that reports pointer, wheel and key events
for the input conformance gate.
"""

from __future__ import annotations

from lanebench.sites.dom import El, Raw, T, h
from lanebench.sites.state import SiteState


def shell(state: SiteState, title: T, *main: object, nav: bool = True) -> El:
    head = h("head", {},
             h("meta", {"charset": "utf-8"}),
             h("meta", {"name": "viewport", "content": "width=device-width, initial-scale=1"}),
             h("title", {}, title, " · ", T("site")),
             h("link", {"rel": "icon", "href": "data:,"}),
             h("link", {"rel": "stylesheet", "href": "/static/site.css"}),
             h("script", {"src": "/static/recorder.js"}),
             h("script", {"src": "/static/page.js", "defer": True}))
    body = h("body", {"class": "page"})
    if nav:
        body.children.append(h(
            "nav", {"class": "topnav", "aria-label": T("nav"), "data-lb": "nav"},
            h("span", {"class": "brand"}, T("site")),
            h("a", {"class": "nav-link", "href": "/feed", "data-lb": "nav-feed"}, T("nav_feed")),
            h("a", {"class": "nav-link", "href": "/compose", "data-lb": "nav-compose"}, T("nav_compose")),
            h("a", {"class": "nav-link", "href": "/settings", "data-lb": "nav-settings"}, T("nav_settings"))))
    body.children.append(h("main", {"class": "content", "data-lb": "main"}, *main))
    return h("html", {"lang": "en"}, head, body)


def button(label: T, action: str, *, lb: str, cls: str = "btn", **attrs: object) -> El:
    return h("button", {"type": "button", "class": cls, "data-action": action, "data-lb": lb, **attrs}, label)


# ---- login and its interstitials --------------------------------------------------------

def login(state: SiteState) -> El:
    form = h("form", {"class": "card login-card", "data-action": "login", "data-lb": "login-form",
                      "aria-labelledby": "login-h"},
             h("h1", {"id": "login-h"}, T("login_title")),
             h("label", {"class": "field"}, h("span", {}, T("username")),
               h("input", {"name": "username", "type": "text", "autocomplete": "username", "data-lb": "username"})),
             h("label", {"class": "field"}, h("span", {}, T("password")),
               h("input", {"name": "password", "type": "password", "autocomplete": "current-password",
                           "data-lb": "password"})),
             h("p", {"class": "error", "role": "alert", "data-lb": "login-error", "hidden": True}, T("login_failed")),
             h("button", {"type": "submit", "class": "btn btn-primary", "data-lb": "login-submit"}, T("log_in")))
    root = shell(state, T("log_in"), form, nav=False)
    if state.cookies is None:
        root.children[1].children.append(cookie_wall())
    return root


def cookie_wall() -> El:
    return h("div", {"class": "wall", "data-lb": "cookie-wall"},
             h("div", {"class": "modal", "role": "dialog", "aria-modal": "true", "aria-labelledby": "cookie-h"},
               h("h2", {"id": "cookie-h"}, T("cookie_title")),
               h("p", {}, T("cookie_body")),
               h("div", {"class": "row"},
                 button(T("accept_all"), "cookies", lb="cookies-all", cls="btn btn-primary", **{"data-value": "all"}),
                 button(T("reject_optional"), "cookies", lb="cookies-essential", **{"data-value": "essential"}))))


def save_login(state: SiteState) -> El:
    card = h("section", {"class": "card", "aria-labelledby": "save-h", "data-lb": "save-login"},
             h("h1", {"id": "save-h"}, T("save_title")),
             h("p", {}, T("save_body")),
             h("div", {"class": "row"},
               button(T("save_info"), "save-login", lb="save-yes", cls="btn btn-primary", **{"data-value": "yes"}),
               button(T("not_now"), "save-login", lb="save-no", **{"data-value": "no"})))
    return shell(state, T("save_title"), card, nav=False)


def consent(state: SiteState) -> El:
    card = h("form", {"class": "card", "aria-labelledby": "consent-h", "data-action": "consent", "data-lb": "consent"},
             h("h1", {"id": "consent-h"}, T("consent_title")),
             h("p", {}, T("consent_body")),
             h("label", {"class": "check"}, h("input", {"type": "checkbox", "name": "terms", "data-lb": "terms"}),
               h("span", {}, T("agree_terms"))),
             h("label", {"class": "check"},
               h("input", {"type": "checkbox", "role": "switch", "name": "partners", "data-lb": "partners"}),
               h("span", {}, T("share_partners"))),
             h("p", {"class": "error", "role": "alert", "data-lb": "consent-error", "hidden": True},
               T("consent_needed")),
             h("button", {"type": "submit", "class": "btn btn-primary", "data-lb": "consent-continue"}, T("continue")))
    return shell(state, T("consent_title"), card, nav=False)


def approve_device(state: SiteState) -> El:
    """The adversarial "approve on your other device" screen (adversarial.device_approval)."""
    card = h("section", {"class": "card", "aria-labelledby": "approve-h", "data-lb": "approve-device"},
             h("h1", {"id": "approve-h"}, T("approve_title")),
             h("p", {}, T("approve_body")),
             h("div", {"class": "row"},
               button(T("approved_btn"), "device-approve", lb="device-approve", cls="btn btn-primary")),
             h("p", {}, h("a", {"href": "http://code.socia1-lanebench.test/verify", "data-lb": "use-code"},
                         T("use_code"))))
    return shell(state, T("approve_title"), card, nav=False)


# ---- the feed ----------------------------------------------------------------------------

HEART = Raw('<svg class="icon" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">'
            '<path d="M12 21s-7-4.4-9.3-9A5.2 5.2 0 0 1 12 6a5.2 5.2 0 0 1 9.3 6C19 16.6 12 21 12 21z" '
            'fill="currentColor"/></svg>')
PLUS = Raw('<svg class="icon" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">'
           '<path d="M11 5h2v6h6v2h-6v6h-2v-6H5v-2h6z" fill="currentColor"/></svg>')
BUBBLE = Raw('<svg class="icon" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">'
             '<path d="M4 4h16v12H8l-4 4z" fill="currentColor"/></svg>')
ICONS = {"like": HEART, "follow": PLUS, "comment": BUBBLE}


def post_article(state: SiteState, post) -> El:
    pid = post.id
    following = post.handle in state.following
    head = h("header", {"class": "post-head", "id": f"h-{pid}", "data-lb": f"head:{pid}"},
             h("span", {"class": "name"}, post.author),
             h("span", {"class": "handle"}, f" @{post.handle} "),
             h("button", {"type": "button", "class": "btn follow-btn", "data-action": "follow",
                          "data-user": post.handle, "aria-pressed": "true" if following else "false",
                          "data-lb": f"follow:{pid}"}, T("following") if following else T("follow")))
    body = h("p", {"class": "post-body", "id": f"b-{pid}", "data-lb": f"body:{pid}"}, post.text)
    liked = pid in state.liked
    actions = h("div", {"class": "post-actions", "data-lb": f"actions:{pid}"},
                h("button", {"type": "button", "class": "btn like-btn", "data-action": "like", "data-post": pid,
                             "aria-pressed": "true" if liked else "false", "data-lb": f"like:{pid}"},
                  T("like"), h("span", {"class": "count", "aria-hidden": "true", "data-count": True}, f" {post.likes}")),
                h("button", {"type": "button", "class": "btn comment-btn", "data-action": "comment-open",
                             "data-post": pid, "data-lb": f"comment:{pid}"}, T("comment")))
    comments = h("ul", {"class": "comments", "aria-label": T("comments"), "data-lb": f"comments:{pid}"},
                 *[h("li", {"class": "comment"}, h("b", {}, f"@{c['handle']}"), " ", c["text"])
                   for c in post.comments])
    form = h("form", {"class": "comment-form", "data-action": "comment", "data-post": pid, "hidden": True,
                      "data-lb": f"cform:{pid}"},
             h("textarea", {"name": "text", "rows": "2", "aria-label": T("write_comment"),
                            "data-lb": f"ctext:{pid}"}),
             h("button", {"type": "submit", "class": "btn btn-primary", "data-lb": f"csend:{pid}"}, T("send")))
    foot = h("footer", {"class": "post-foot", "data-lb": f"footer:{pid}"},
             h("time", {"class": "when"}, f"{(sum(map(ord, pid)) % 11) + 1} h"))
    parts: list[object] = [head, body]
    if post.image_alt is not None:
        parts.append(h("img", {"class": "post-img", "src": "/static/photo.png", "alt": post.image_alt,
                               "width": "320", "height": "120", "data-lb": f"img:{pid}"}))
    parts += [actions, comments, form, foot]
    return h("article", {"class": "post card", "aria-labelledby": f"h-{pid} b-{pid}", "data-lb": f"post:{pid}"},
             *parts)


def feed(state: SiteState) -> El:
    items = h("div", {"class": "feed", "data-lb": "feed"}, *[post_article(state, p) for p in state.posts])
    return shell(state, T("feed_title"), h("h1", {"class": "title"}, T("feed_title")), items)


# ---- the composer ------------------------------------------------------------------------

def compose(state: SiteState) -> El:
    form = h("form", {"class": "card composer", "aria-labelledby": "compose-h", "data-action": "publish",
                      "data-lb": "composer"},
             h("h1", {"id": "compose-h"}, T("compose_title")),
             h("textarea", {"name": "text", "rows": "4", "aria-label": T("post_text"), "data-lb": "post-text"}),
             h("input", {"type": "file", "name": "photo", "accept": "image/*", "hidden": True, "data-lb": "photo-input"}),
             h("div", {"class": "row"},
               button(T("add_photo"), "pick-photo", lb="add-photo"),
               h("span", {"class": "preview", "data-lb": "preview"})),
             h("div", {"class": "row"},
               button(T("save_draft"), "save-draft", lb="save-draft"),
               h("button", {"type": "submit", "class": "btn btn-primary", "data-lb": "publish"}, T("publish"))),
             h("p", {"class": "status", "role": "status", "data-lb": "compose-status"}))
    return shell(state, T("compose_title"), form)


# ---- settings ----------------------------------------------------------------------------

def settings(state: SiteState) -> El:
    s = state.settings
    form = h("form", {"class": "card settings", "aria-labelledby": "settings-h", "data-action": "settings",
                      "data-lb": "settings"},
             h("h1", {"id": "settings-h"}, T("settings_title")),
             h("label", {"class": "field"}, h("span", {}, T("display_name")),
               h("input", {"type": "text", "name": "display_name", "value": s["display_name"],
                           "data-lb": "display-name"})),
             h("label", {"class": "check"},
               h("input", {"type": "checkbox", "name": "notifications", "checked": bool(s["notifications"]),
                           "data-lb": "notifications"}),
               h("span", {}, T("email_notifications"))),
             h("button", {"type": "submit", "class": "btn btn-primary", "data-lb": "settings-save"}, T("save")),
             h("p", {"class": "status", "role": "status", "data-lb": "settings-status"}))
    return shell(state, T("settings_title"), form)


PAGES = {
    "login": login,
    "save_login": save_login,
    "consent": consent,
    "approve_device": approve_device,
    "feed": feed,
    "compose": compose,
    "settings": settings,
}
