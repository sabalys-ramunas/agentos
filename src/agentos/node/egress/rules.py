"""Effect rules: what a request does (a like, a post, a comment), by host, path, method and operation name."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from agentos.node.egress.constants import SAFE_METHODS
from agentos.policy import globs

# ---- effect rules ------------------------------------------------------------------------

# what the account does that others see: to others (post, comment, like, follow, join), or to its
# own public identity (profile: its name, headline, About, photo or cover)
EFFECT_KINDS = frozenset({"post", "comment", "like", "follow", "join", "profile"})
_RULE_ID_RE = re.compile(r"^[a-z0-9]+\.(post|comment|like|follow|join|profile|read|draft)(\.[a-z0-9_]+)?$")
SENT_KEEP = 4096      # the most non-GETs in flight whose sender is remembered for the ledger
ABORTED_KEEP = 512    # the most aborted requests remembered until their requestfailed comes
LOGIN_WINDOW_S = 30.0  # how long after a vault fill a POST to its domain counts as the login (open_login)
UNKNOWN = "unknown"  # the kind of the rule the gate makes for a request no rule reads (unknown_rule)
_REGISTER_RULE = re.compile(r"^[a-z0-9]+\.draft\.register")  # a media registration: its answer names the upload


@dataclass(frozen=True)
class EffectRule:
    """URL pattern and method -> read, write or commit.

    The id is `<site>.<kind>[.<detail>]`. The kind is one of EFFECT_KINDS (post, comment, like,
    follow, join: something other people see the account do; profile: a change to the account's
    own public profile, a commit like a post), `read` (a read through POST that
    must never be taken for a publish) or `draft` (a step before a publish, an upload). `op` (a
    regex) narrows a rule to requests whose operation name matches it: GraphQL clients send many
    operations to one URL, and the name comes from an operation header (SHAPE_HEADERS) or the
    body's operation key (SHAPE_OP_KEYS, also inside a form), never from what a person typed.
    `header` (name, regex) narrows a rule to a header's value. `verified` says when and where the
    rule was checked against a real account's traffic (observe mode); empty means it was not."""
    id: str
    site: str
    host: str                    # a domain pattern (globs syntax)
    path: str                    # a regex, matched from the start of the path
    method: str = "POST"
    effect: str = "commit"       # read | write | commit
    header: tuple[str, str] | None = None
    op: str | None = None        # a regex searched in the request's operation name
    verified: str = ""           # "2026-11-04 reddit.com web": checked on a throwaway account's real traffic
    note: str = ""
    # a read catch-all of a GraphQL endpoint: while an agent drives, a request it matches is a read
    # only when every operation in it is a known read (gql_verdict); else it is gated as unknown
    strict_ops: bool = False
    # a read the owner promoted for a request that names no operation (gateshapes): while an agent
    # drives, a body it matches is a read only with these field names at most, and naming no
    # operation (body_covered); else it is gated as unknown
    fields: frozenset[str] | None = None
    # a read for a body that names no operation (the sites' telemetry and page plumbing): while an
    # agent drives, a body that carries text the agent typed on the lane (LaneGuard.carries_typed) is
    # gated as unknown instead, so a request that may carry a person's words never leaves as a read
    untyped: bool = False

    @property
    def kind(self) -> str:
        """post, comment, like, follow, join, profile, read or draft (from the id)."""
        return self.id.split(".")[1]

    def matches(self, host: str, path: str, method: str, headers: dict[str, str] | None,
                op: Callable[[], str | None] | None = None) -> bool:
        # a commit or write rule takes its endpoint whatever the unsafe method (a CreatePost sent as
        # a PUT is still a publish); a read rule only its own method
        same = method == self.method or (self.effect != "read" and self.method == "POST"
                                         and method not in SAFE_METHODS)
        if not same or not globs.domain_match(self.host, host):
            return False
        if not re.match(self.path, path):
            return False
        if self.header is not None:
            value = (headers or {}).get(self.header[0])
            if value is None or re.search(self.header[1], value) is None:
                return False
        if self.op is not None:
            name = op() if op is not None else None
            return name is not None and re.search(self.op, name) is not None
        return True


REDDIT_GQL = r"/svc/shreddit/graphql$"
META_GQL = r"/api/graphql/?$"
IG_GQL = r"/(api/)?graphql(/query)?/?$"
LINKEDIN_RSC = r"/flagship-web/rsc-action/actions/server-request$"
# one path segment that changes with each build of LinkedIn's client: 12 to 24 of [A-Za-z0-9_-], with
# an upper and a lower case letter (so no word path such as /notifications or /mynetwork matches)
LINKEDIN_BEACON = r"/(?=[^/]*[A-Z])(?=[^/]*[a-z])[A-Za-z0-9_-]{12,24}$"
# every field name LinkedIn's tracking envelope had on the real node (2026-11-10): a body with any
# other field is not one (the rule's `fields`)
LINKEDIN_EVENT_FIELDS = frozenset({
    "clientContext", "clientContext.eventHeaderContext", "clientContext.eventHeaderContext.server",
    "clientContext.eventHeaderContext.serviceName", "clientContext.eventHeaderContext.publicAppVersion",
    "clientContext.eventHeaderContext.clientApplicationInstance",
    "clientContext.eventHeaderContext.clientApplicationInstance.applicationUrn",
    "clientContext.eventHeaderContext.clientApplicationInstance.version",
    "clientContext.eventHeaderContext.clientApplicationInstance.trackingId",
    "clientContext.eventHeaderContext.clientApplicationInstance.trackingId.fixedValue",
    "clientContext.eventHeaderContext.appName", "clientContext.userRequestHeaderContext",
    "clientContext.userRequestHeaderContext.theme", "clientContext.userRequestHeaderContext.interfaceLocale",
    "clientContext.userRequestHeaderContext.isBrowserPersistentRetryEnabled",
    "clientContext.userRequestHeaderContext.isFlushOnCloseBrowserTabEnabled",
    "clientContext.userRequestHeaderContext.clientDeviceType",
    "clientContext.userRequestHeaderContext.timeZoneOffsetMinutes",
    "requests", "requests[].id", "requests[].payload", "requests[].payload.name", "requests[].payload.payload",
    "requests[].payload.isPegasusEncoded", "requests[].clientBreadCrumbs", "requests[].isShadowEvent"})

# The first match wins. Verified rules were checked on 2026-11-04 against the throwaway accounts'
# real traffic in observe mode (a canary run of 2026-11-04 has each request's shape); unverified
# ones come from public descriptions of the sites' web clients. A wrong rule either misses a
# publish (the request is unknown, so it goes through as before) or treats one more request as a
# write or a commit than it should. Operation rules come before their endpoint's read catch-all.
EFFECT_RULES: tuple[EffectRule, ...] = (
    # Instagram web (www.instagram.com): every action is a GraphQL mutation named in
    # x-fb-friendly-name and in the form field fb_api_req_friendly_name
    EffectRule("instagram.like", "instagram", "www.instagram.com", IG_GQL, effect="write",
               op=r"^usePolaris(Like|Unlike)Media\w*Mutation$", verified="2026-11-04 instagram.com web",
               note="usePolarisLikeMediaXIGLikeMutation: variables.input.{media_id, actor_id, container_module}"),
    EffectRule("instagram.follow", "instagram", "www.instagram.com", IG_GQL, effect="write",
               op=r"^usePolaris(Follow|Unfollow)Mutation$", verified="2026-11-04 instagram.com web",
               note="variables.{target_user_id, container_module, nav_chain}"),
    EffectRule("instagram.comment", "instagram", "www.instagram.com", IG_GQL,
               op=r"^PolarisPostCommentInput\w*Mutation$", verified="2026-11-04 instagram.com web",
               note="PolarisPostCommentInputRevampedMutation: variables.data.{comment_text, media_id}"),
    EffectRule("instagram.read.graphql", "instagram", "www.instagram.com", IG_GQL, effect="read", strict_ops=True,
               note="every other operation that is a known read (feeds, profiles, hover cards)"),
    EffectRule("instagram.post", "instagram", "*.instagram.com", r"/api/v1/media/configure(_sidecar|_to_story)?/",
               note="publishes an uploaded photo, carousel or story"),
    EffectRule("instagram.comment.rest", "instagram", "*.instagram.com", r"/api/v1/web/comments/\d+/add/",
               note="the older web client"),
    EffectRule("instagram.draft.upload", "instagram", "*.instagram.com", r"/rupload_ig(photo|video)/", effect="write",
               note="an upload is a draft until it is configured"),
    EffectRule("instagram.like.rest", "instagram", "*.instagram.com", r"/api/v1/web/likes/\d+/(un)?like/",
               effect="write", note="the older web client"),
    # Facebook web: like Instagram, the operation is in x-fb-friendly-name and fb_api_req_friendly_name
    EffectRule("facebook.like", "facebook", "*.facebook.com", META_GQL, effect="write",
               op=r"^CometUFIFeedbackReactMutation$", verified="2026-11-04 facebook.com web",
               note="a reaction or its removal: variables.input.{feedback_id, feedback_reaction_id}"),
    EffectRule("facebook.follow", "facebook", "*.facebook.com", META_GQL, effect="write",
               op=r"^Comet(User|Page)(Follow|Unfollow)Mutation$", verified="2026-11-04 facebook.com web",
               note="following a Page or a profile (a Page follow also likes it): variables.input.subscribee_id"),
    EffectRule("facebook.post", "facebook", "*.facebook.com", META_GQL,
               op=r"^(ComposerStoryCreate|StoryCreate)Mutation$", verified="2026-11-04 facebook.com web",
               note="a profile post: variables.input.{message.text, attachments[].photo.id, audience.privacy}"),
    EffectRule("facebook.comment", "facebook", "*.facebook.com", META_GQL,
               op=r"^useCometUFICreateCommentMutation$|CommentCreate\w*Mutation$", verified="2026-11-04 facebook.com web",
               note="variables.input.{feedback_id, message.text, attachments}; typing first sends "
                    "CometUFILiveTypingBroadcastMutation (a read: others only see 'typing')"),
    EffectRule("facebook.draft.upload", "facebook", "upload.facebook.com", r"/ajax/react_composer/attachments/photo/upload",
               effect="write", verified="2026-11-04 facebook.com web",
               note="multipart {source, profile_id, upload_id}: a photo is a draft until a post names it"),
    EffectRule("facebook.draft.upload_video", "facebook", "*.facebook.com", r"/ajax/video/upload/requests/",
               effect="write", note="a video in chunks (start, receive, finish): a draft until a post names it"),
    EffectRule("facebook.draft.rupload", "facebook", "rupload.facebook.com", r"/", effect="write",
               note="the resumable upload host (videos, stories)"),
    EffectRule("facebook.read.graphql", "facebook", "*.facebook.com", META_GQL, effect="read", strict_ops=True,
               note="every other GraphQL call that is a known read"),
    EffectRule("facebook.read.graphqlbatch", "facebook", "*.facebook.com", r"/api/graphqlbatch/?$", effect="read",
               strict_ops=True, note="a batch: a read only when every query in it is one"),
    # Reddit (new web client, shreddit): one GraphQL endpoint, the operation in the JSON body
    EffectRule("reddit.post", "reddit", "www.reddit.com", REDDIT_GQL, op=r"^CreatePost$",
               verified="2026-11-04 reddit.com web",
               note="variables.input.{subredditName, title, content.richText, recaptchaToken}"),
    EffectRule("reddit.comment", "reddit", "www.reddit.com", r"/svc/shreddit/t[13]_[a-z0-9]+/create-comment$",
               verified="2026-11-04 reddit.com web",
               note="not GraphQL: a form with content, mode, richTextMedia and recaptchaToken; t3_ a post, t1_ a reply"),
    EffectRule("reddit.like.post", "reddit", "www.reddit.com", REDDIT_GQL, effect="write",
               op=r"^UpdatePostVoteState$", verified="2026-11-04 reddit.com web",
               note="an up or down vote (or its undo): variables.input.{postId, voteState}"),
    EffectRule("reddit.like.comment", "reddit", "www.reddit.com", REDDIT_GQL, effect="write",
               op=r"^UpdateCommentVoteState$", verified="2026-11-04 reddit.com web",
               note="variables.input.{commentId, voteState}"),
    EffectRule("reddit.join", "reddit", "www.reddit.com", REDDIT_GQL, effect="write",
               op=r"^UpdateSubredditSubscriptions$", verified="2026-11-04 reddit.com web",
               note="join or leave: variables.input.inputs[].{subredditId, subscribeState}"),
    # a post or comment with an image: the client leases an upload (the answer names the media id and
    # the S3 form's key), posts the file to Reddit's S3 bucket, then names the media id in the post's
    # rich text. From the web client's public behaviour, not checked on a real account
    EffectRule("reddit.draft.register", "reddit", "www.reddit.com", REDDIT_GQL, effect="write",
               op=r"^Create\w*Upload(Lease)?$", note="CreateMediaUploadLease: {mediaId, uploadLease.{uploadLeaseUrl, "
                                                     "uploadLeaseHeaders[].{header, value}}}"),
    EffectRule("reddit.draft.register_asset", "reddit", "*.reddit.com", r"/api/media/asset\.json$", effect="write",
               note="the older lease: {args.{action, fields[].{name, value}}, asset.asset_id}"),
    EffectRule("reddit.draft.upload", "reddit", "reddit-uploaded-media.s3-accelerate.amazonaws.com", r"/",
               effect="write", note="an S3 form post (key, policy, x-amz-*, file): the image bytes"),
    EffectRule("reddit.draft.upload_video", "reddit", "reddit-uploaded-video.s3-accelerate.amazonaws.com", r"/",
               effect="write", note="the same for a video"),
    EffectRule("reddit.read.graphql", "reddit", "www.reddit.com", REDDIT_GQL, effect="read", strict_ops=True,
               note="every other operation that is a known read (READ_OPS)"),
    # old.reddit.com and API clients
    EffectRule("reddit.post.oldreddit", "reddit", "*.reddit.com", r"/api/submit"),
    EffectRule("reddit.comment.oldreddit", "reddit", "*.reddit.com", r"/api/comment"),
    EffectRule("reddit.like.oldreddit", "reddit", "*.reddit.com", r"/api/vote", effect="write"),
    EffectRule("reddit.join.oldreddit", "reddit", "*.reddit.com", r"/api/subscribe", effect="write"),
    # LinkedIn web (the server-driven client): every action posts JSON to one URL and names itself in
    # requestId, a dotted name (request_op reads it)
    EffectRule("linkedin.post", "linkedin", "www.linkedin.com", LINKEDIN_RSC,
               op=r"^com\.linkedin\.sdui\.requests\.sharing\.CreatePost$", verified="2026-11-04 linkedin.com web",
               note="a member post; a photo goes first as registerMediaUpload and a PUT to /dms-uploads/"),
    # 2026-11-10, on the real site: the click on Post sent sharing.LaunchCreatePost, and the canary of
    # 2026-11-04 saw sharing.DispatchPostCreateWithDetour next to CreatePost. Neither carries the typed
    # text in a form the gate reads, so as unknown requests they were held with no card and the post
    # never left. A request the Post button sends is a publish until a real run shows otherwise: it asks.
    EffectRule("linkedin.post.dispatch", "linkedin", "www.linkedin.com", LINKEDIN_RSC,
               op=r"^com\.linkedin\.sdui\.requests\.sharing\.(LaunchCreatePost|DispatchPostCreateWithDetour)$",
               note="sent by the Post button around CreatePost; not yet confirmed which one publishes"),
    EffectRule("linkedin.comment", "linkedin", "www.linkedin.com", LINKEDIN_RSC,
               op=r"^com\.linkedin\.sdui\.comments\.createComment$", verified="2026-11-04 linkedin.com web"),
    EffectRule("linkedin.like", "linkedin", "www.linkedin.com", LINKEDIN_RSC, effect="write",
               op=r"^com\.linkedin\.sdui\.reactions\.(create|delete|update)\w*$", verified="2026-11-04 linkedin.com web",
               note="any reaction (Like, Celebrate...) or its removal"),
    EffectRule("linkedin.follow", "linkedin", "www.linkedin.com", LINKEDIN_RSC, effect="write",
               op=r"^com\.linkedin\.sdui\.requests\.mynetwork\.\w*FollowState$", verified="2026-11-04 linkedin.com web",
               note="following or unfollowing a member or a company"),
    EffectRule("linkedin.draft.upload", "linkedin", "www.linkedin.com", r"/dms-uploads/", method="PUT", effect="write",
               verified="2026-11-04 linkedin.com web", note="the image bytes, before CreatePost names them"),
    EffectRule("linkedin.draft.register", "linkedin", "www.linkedin.com", LINKEDIN_RSC, effect="write",
               op=r"(^|\.)(registerMediaUpload|RegisterImageMedia)$",
               note="the canary saw these names around the /dms-uploads/ PUT: a media upload's registration; its "
                    "answer (not recorded) is read for the urn and the upload URL (publish.registrations)"),
    EffectRule("linkedin.draft.register_voyager", "linkedin", "www.linkedin.com",
               r"/voyager/api/voyager(VideoDash)?MediaUploadMetadata$", effect="write",
               note="the older client's registerUpload: {value.{urn, singleUploadUrl}}"),
    # The member's own public profile (brief B2): TO CAPTURE. Nobody has recorded LinkedIn's profile
    # edits yet, so each op pattern is a guess at the web client's convention (a dotted requestId
    # on the rsc-action endpoint, `com.linkedin.sdui.requests.profile.<verb><Field>`), and none is
    # verified: the engine holds every profile edit (rule_unverified) until the owner edits each
    # field once on a throwaway account in observe mode, replaces the op (and path) here with the
    # name the trace shows, and writes `verified`. They are commits: the gate asks, and they are
    # metered like a post. They come after the media registrations, so a name that registers an
    # upload stays a draft. Each pattern is a verb that writes (update, save) followed by the
    # field and nothing after it, so a read or a helper around the field (editProfileNameForm,
    # saveDraftHeadlineSuggestions, updateBackgroundImageUploadPrompt) is never taken for the edit,
    # and a profile photo's upload registration is never taken for the save. The intro
    # dialog may well save the name and the headline in one request: if the capture shows that,
    # the two rules become one. The cover comes before the photo (a cover's name may say "Profile").
    EffectRule("linkedin.profile.name", "linkedin", "www.linkedin.com", LINKEDIN_RSC,
               op=r"^com\.linkedin\.sdui\.requests\.profile\.(update|save)(Profile|Member)?(Full)?Names?$",
               note="TO CAPTURE: the first and last name (a guess at the op name)"),
    EffectRule("linkedin.profile.headline", "linkedin", "www.linkedin.com", LINKEDIN_RSC,
               op=r"^com\.linkedin\.sdui\.requests\.profile\.(update|save)(Profile)?Headline$",
               note="TO CAPTURE: the headline under the name (a guess at the op name)"),
    EffectRule("linkedin.profile.about", "linkedin", "www.linkedin.com", LINKEDIN_RSC,
               op=r"^com\.linkedin\.sdui\.requests\.profile\.(update|save)(Profile)?(About|Summary)(Section)?$",
               note="TO CAPTURE: the About section (a guess at the op name)"),
    EffectRule("linkedin.profile.cover", "linkedin", "www.linkedin.com", LINKEDIN_RSC,
               op=r"^com\.linkedin\.sdui\.requests\.profile\.(update|save)(Profile)?(Background|Cover)(Photo|Image|Picture)$",
               note="TO CAPTURE: the cover (background) image, named by the urn its upload registered"),
    EffectRule("linkedin.profile.photo", "linkedin", "www.linkedin.com", LINKEDIN_RSC,
               op=r"^com\.linkedin\.sdui\.requests\.profile\.(update|save)(Profile|Display)(Photo|Picture)$",
               note="TO CAPTURE: the profile photo, named by the urn its upload registered"),
    EffectRule("linkedin.read.rsc", "linkedin", "www.linkedin.com", LINKEDIN_RSC, effect="read", strict_ops=True,
               note="every other server request that is a known read (comment prompts, hover cards, fetches)"),
    # the older Voyager client
    EffectRule("linkedin.post.voyager", "linkedin", "www.linkedin.com", r"/voyager/api/contentcreation/normShares"),
    EffectRule("linkedin.comment.voyager", "linkedin", "www.linkedin.com", r"/voyager/api/feed/comments"),
    # Medium
    EffectRule("medium.post", "medium", "medium.com", r"/_/graphql", header=("graphql-operation", r"(?i)publish")),
    EffectRule("medium.read.graphql", "medium", "medium.com", r"/_/graphql", effect="read", strict_ops=True),
    # The sites' own telemetry and page plumbing (POSTs that publish nothing): from the web clients'
    # public behaviour, not yet checked in observe mode. While an agent drives, a POST to a rule host
    # that no rule names is gated as unknown, so these keep a feed working; observe mode shows the rest.
    EffectRule("facebook.read.plumbing", "facebook", "*.facebook.com",
               r"/ajax/(bz|bulk-route-definitions|route-definition|webstorage/process_keys|qm|navigation|"
               r"haste-response|browser_error_reports)/?$", effect="read"),
    EffectRule("instagram.read.plumbing", "instagram", "*.instagram.com",
               r"/(ajax/(bz|bulk-route-definitions|qm|navigation)|logging/falco)/?$", effect="read"),
    EffectRule("reddit.read.plumbing", "reddit", "*.reddit.com", r"/svc/shreddit/(events|perfMetrics)/?$",
               effect="read"),
    EffectRule("reddit.read.reporting", "reddit", "w3-reporting.reddit.com", r"/", effect="read"),
    EffectRule("reddit.read.errors", "reddit", "error-tracking.reddit.com", r"/", effect="read"),
    EffectRule("linkedin.read.plumbing", "linkedin", "www.linkedin.com",
               r"/(li/track|sensorCollect|realtime/realtimeFrontendClientConnectivityTracking)/?", effect="read"),
    # Recorded on the real node on 2026-11-10 (its gate log,
    # names and shapes only): the sites' own background traffic while an agent acted. Each is a read,
    # telemetry or session plumbing; each lets a body go only while it carries no text the agent typed
    # on the lane (untyped), and those whose field names were all recorded are bound by them (fields).
    # LinkedIn's server-driven client: the feed's pagination, the app's config, who is online
    EffectRule("linkedin.read.pagination", "linkedin", "www.linkedin.com",
               r"/flagship-web/rsc-action/actions/(pagination|app-config)$", effect="read", untyped=True,
               verified="2026-11-10 linkedin.com web (shape only)"),
    EffectRule("linkedin.read.presence", "linkedin", "www.linkedin.com",
               r"/voyager/api/messaging/dash/presenceStatuses$", effect="read", untyped=True,
               fields=frozenset({"ids"}), verified="2026-11-10 linkedin.com web (shape only)"),
    # a page the client renders on the server (the feed, a profile) or opens in place (navigation)
    EffectRule("linkedin.read.render", "linkedin", "www.linkedin.com", r"/flagship-web/(feed|in/[^/]+)/?$",
               effect="read", untyped=True, verified="2026-11-10 linkedin.com web (shape only)",
               fields=frozenset({"requestedArguments", "requestedArguments.payload", "requestedArguments.payload.vanityName",
                                 "requestedArguments.payload.isVanityNameResolved", "requestedArguments.states",
                                 "requestedArguments.requestMetadata", "requestedArguments.requestMetadata.$type",
                                 "requestedArguments.screenId", "requestedArguments.knownTemplateIds", "isPrefetch"})),
    EffectRule("linkedin.read.navigation", "linkedin", "www.linkedin.com",
               r"/flagship-web/rsc-action/actions/navigation$", effect="read", untyped=True,
               verified="2026-11-10 linkedin.com web (shape only)",
               fields=frozenset({"clientArguments", "clientArguments.$type", "clientArguments.requestedStateKeys",
                                 "clientArguments.payload", "clientArguments.requestMetadata",
                                 "clientArguments.requestMetadata.$type", "clientArguments.states",
                                 "clientArguments.screenId", "clientArguments.knownTemplateIds", "isModal"})),
    # LinkedIn's event beacons go to a path that changes with every page build (/to11ybWSpBFqEq9mx,
    # /tapiIT3oVvopi2Lp, /E1J9Xpqg3_38diMca95): one segment of 12 to 24 letters, digits, - and _ with
    # both cases. A path like that never names a feature, so the rule reads the body instead: the
    # tracking envelope ({clientContext.eventHeaderContext, requests[].payload}, every field name of it
    # recorded), or a plain-text beacon (sendBeacon's own type), which no publish of the client uses.
    EffectRule("linkedin.read.beacon_text", "linkedin", "www.linkedin.com", LINKEDIN_BEACON, effect="read",
               untyped=True, header=("content-type", r"^\s*text/plain\b"),
               verified="2026-11-10 linkedin.com web (shape only)"),
    EffectRule("linkedin.read.beacon", "linkedin", "www.linkedin.com", LINKEDIN_BEACON, effect="read", untyped=True,
               verified="2026-11-10 linkedin.com web (shape only)", fields=LINKEDIN_EVENT_FIELDS),
    # Reddit's session token refresh (its body names the CSRF token only)
    EffectRule("reddit.read.token", "reddit", "www.reddit.com", r"/svc/shreddit/token$", effect="read", untyped=True,
               fields=frozenset({"csrf_token"}), verified="2026-11-10 reddit.com web (shape only)"),
    # Meta: the page's own resource and relay plumbing, the logging beacon, and the login sync between
    # Instagram and Facebook (an encrypted blob the account's own session sends; others see nothing)
    EffectRule("facebook.read.relay", "facebook", "*.facebook.com",
               r"/(ajax/relay-ef|static_resources/webworker/rsrc|ajax/bnzai)/?$", effect="read", untyped=True,
               verified="2026-11-10 facebook.com web (shape only)"),
    EffectRule("facebook.read.login_sync", "facebook", "www.facebook.com", r"/instagram/login_sync/update/?$",
               effect="read", untyped=True, verified="2026-11-10 instagram.com web (shape only)"),
    EffectRule("instagram.read.sync", "instagram", "www.instagram.com",
               r"/(sync/instagram|api/v1/web/fxcal/ig_sso_users)/?$", effect="read", untyped=True,
               verified="2026-11-10 instagram.com web (shape only)"),
)
