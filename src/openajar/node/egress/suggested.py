"""Suggested site lists for a lane: the shared sign-in and challenge hosts, and each platform's own."""

from __future__ import annotations

# ---- suggested site lists ----------------------------------------------------------------

# The sign-in and challenge hosts a login can need, whichever platform it is for. Pages and redirect
# hops of the identity providers are plain entries; captcha widgets and sign-in helper frames load
# only as subframes (`frame:`), so an agent can never open them as a page. Built from the providers'
# public docs (docs/design/egress-guard.md, section 5), not from watching a logged-in account.
IDENTITY_SITES: tuple[str, ...] = (
    # Google sign-in: the popup and its redirects, and SetSID on the exit IP's country domain
    "accounts.google.com", "accounts.google.@cc",
    # the cookie consent page Google can put in front of a sign-in from the EU
    "consent.google.com",
    # Google's sign-in helper: a CheckConnection frame, and a SetSID hop for YouTube's cookies
    "accounts.youtube.com",
    # Sign in with Apple: the popup or redirect, and the frame its sign-in form runs in
    "appleid.apple.com", "frame:idmsa.apple.com",
    # reCAPTCHA (www.google.com/recaptcha, and recaptcha.net where google.com is not reachable)
    "frame:www.google.com", "frame:recaptcha.google.com", "frame:www.recaptcha.net", "frame:recaptcha.net",
    # hCaptcha
    "frame:hcaptcha.com", "frame:*.hcaptcha.com",
    # Cloudflare Turnstile and its managed challenge
    "frame:challenges.cloudflare.com",
    # Arkose Labs (FunCaptcha): client-api and per-customer <company>-api hosts
    "frame:*.arkoselabs.com", "frame:*.funcaptcha.com",
)

# Document hosts each platform needs, with its CDNs and login hosts, plus IDENTITY_SITES.
# Suggestions only: `openajar lane sites <lane> suggest <platform>` applies one, nothing applies them
# on its own. Built from the platforms' public domains, not from watching a logged-in account.
PLATFORM_SITES: dict[str, tuple[str, ...]] = {
    "instagram": ("instagram.com", "*.instagram.com", "cdninstagram.com", "*.cdninstagram.com",
                  "facebook.com", "*.facebook.com", "fbcdn.net", "*.fbcdn.net", "fbsbx.com", "*.fbsbx.com"),
    "facebook": ("facebook.com", "*.facebook.com", "fbcdn.net", "*.fbcdn.net", "facebook.net",
                 "*.facebook.net", "fbsbx.com", "*.fbsbx.com", "fb.com", "*.fb.com", "messenger.com",
                 "*.messenger.com"),
    "reddit": ("reddit.com", "*.reddit.com", "redd.it", "*.redd.it", "redditstatic.com",
               "*.redditstatic.com", "redditmedia.com", "*.redditmedia.com",
               # the media buckets a post's image or video is uploaded to (a POST, never a page)
               "frame:reddit-uploaded-media.s3-accelerate.amazonaws.com",
               "frame:reddit-uploaded-video.s3-accelerate.amazonaws.com"),
    "linkedin": ("linkedin.com", "*.linkedin.com", "licdn.com", "*.licdn.com", "lnkd.in",
                 # LinkedIn's bot-defence frame (HUMAN Security, seen refused on the real node on
                 # 2026-11-10): blocking it makes the account look like a bot, so it loads as a frame
                 "frame:li.protechts.net"),
    "medium": ("medium.com", "*.medium.com"),
}
SUGGESTED_SITES: dict[str, tuple[str, ...]] = {
    platform: hosts + IDENTITY_SITES for platform, hosts in PLATFORM_SITES.items()}
