# Design: the engagement engine (posting, likes, comments, follows as a system, not an agent)

Status: design, 2026-11-04. Owner direction, in their words: "We don't want agent doing this
job, we want our agentos have a system for interactions, posting, following and etc and agent
simply select levers, more sophisticated than just agent uses the browser."

Builds on: connectors (`docs/design/connectors.md`, `src/agentos/runner/connectors/`), the
runner and its schedules (`src/agentos/runner/`), the egress guard and its effect rules
(`docs/design/egress-guard.md`, `src/agentos/node/egress.py`), signals and holds
(`src/agentos/node/sites.py`, `holds.py`), the lane check (`agentos.stealth.lanecheck`), the events
core (`src/agentos/events/`), approvals (`src/agentos/policy/`), brains
(`src/agentos/brains/`), workspace memory (`docs/design/workspace-memory.md`) and the pilot
(`docs/design/browser-and-computer-agents.md`). Folds in the canary findings of 2026-11-04.

---

## 1. One page for the owner

**What you get.** You (or your agent) write one sentence per account, for example "biz-a
Instagram: warm up for 10 days, then 15 likes and 3 comments a day on coffee topics, I
approve comments". agentos turns it into a **program**: a plan of short sessions at random
times in that account's waking hours, with browsing in between, real limits that ramp up
slowly, comments drafted by your own brain and shown to you before they go out, and a daily
report. No agent clicks anything. The engine runs typed, tested operations (connectors) and
checks that every like, follow and comment really happened.

**What the field taught us.**

- Social media managers (Buffer, Hootsuite, Sprout, Later, Metricool, Publer, Postiz) only
  post and reply, because the official APIs only allow that. None of them like or follow for
  you: Instagram removed those API endpoints in 2018 and started deleting likes and follows
  made by growth apps.
- Growth tools (Jarvee, which closed in 2022, PhantomBuster, Kicksta, LinkedIn tools such as
  Waalaxy, Dripify, Expandi and Linked Helper) do engage, through a browser or a phone. The
  good ones share a few habits: limits belong to the account rather than the campaign, limits
  ramp up slowly, work happens only in working hours with random delays, and the account
  pauses at the first sign of trouble.
- **No platform publishes how many likes, follows or comments are safe.** Every number you
  read online ("200 follows a day") is vendor advice or folklore. We use those numbers as an
  upper ceiling and start far below them.

**The honest risk.** Automating likes, follows and comments in a browser is against the
terms of Instagram, Facebook, LinkedIn and Medium, and Reddit forbids software choosing what
to vote on. No timing model changes that. A careful engine lowers the chance of trouble, but
some accounts may still get temporary blocks. So the design does three things:

1. It uses the **official API wherever one exists**: posting to Instagram professional
   accounts, Facebook Pages, LinkedIn and your blog, and replying to comments on your own
   posts. That part is allowed and safe.
2. It keeps browser engagement at **one real person's pace** (well under the folklore
   numbers), never has your own accounts like or comment on each other, and stops an account
   for days the moment a platform pushes back.
3. It lets you choose, per account, how much risk to take: `careful` (the default), `normal`,
   or `off`. Your most valuable accounts start on `careful` with comments always approved by
   you.

**What is new to build.** One node service, `agentos.engage`: programs, a planner that draws
each account's sessions, a session runner that drives connector operations in one lane, a
target pool, a comment drafter on your brain, and a safety governor. Everything else
(connectors, effect rules, holds, approvals, schedules, events, brains, memory) exists.

**First step (no input from you).** Brief E1: the program format, the planner and the
timing model, run as a dry run that writes plans and a report but clicks nothing. It ships
before any engagement touches a real account. The engagement connectors (Brief E2) follow
the canary, which today verified the real Instagram like, follow and comment requests and
Reddit votes and joins on throwaway accounts.

**What we need from you** (section 10 has the defaults we use otherwise): which accounts
are switched to Instagram professional (for API posting), whether you will create a Meta app
and a LinkedIn app (we write the steps), and a risk setting per account if you want anything
other than `careful`.

---

## 2. How the field does it

Labels used below: **[DOC]** the platform documents it, **[VENDOR]** a tool vendor publishes
it as its own guidance, **[FOLK]** blogs and forums with no platform source.

### 2.1 Social media management tools (official APIs only)

| Tool | Levers | Engagement offered | Notes |
|---|---|---|---|
| Buffer | queue per channel, posting slots, AI assistant, basic approvals | replies in a limited inbox | approvals appear single-step ([Sked comparison](https://skedsocial.com/best-approval-workflows-social-media-tools.md)) |
| Hootsuite | calendar, OwlyWriter AI (learns tone from past posts), unified inbox, roles and approval workflows | inbox replies | tiering of full workflows disputed ([Planable](https://planable.io/blog/hootsuite-vs-sprout-social/), [Genesys Growth](https://genesysgrowth.com/blog/hootsuite-owlywriter-vs-buffer-ai-vs-sprout-social-ai)) |
| Sprout Social | multi-step approval chains: named steps, approvers per step, "any one" or "all", even full publishers cannot skip | inbox replies | best documented approvals ([Sprout help](https://support.sproutsocial.com/hc/en-us/articles/205974715), [product guide](https://media.sproutsocial.com/uploads/Sprout-Social-Message-Approval-Product-Guide.pdf)) |
| Later | visual Instagram planner, best time to post, caption AI | conversations on higher plans | ([FS Poster](https://www.fs-poster.com/blog/buffer-vs-hootsuite-vs-later)) |
| Metricool, Publer | bulk CSV, RSS automations, auto-scheduling, watermarks | replies | ([PostPlanify](https://postplanify.com/compare/metricool-vs-publer)) |
| Postiz (open source) | calendar, per-platform providers, "plugs" (rules), public API, CLI, MCP server | replies, auto-repost or auto-reply when a post passes N likes (X) | architecture below ([docs](https://docs.postiz.com/self-host/architecture.md)) |

"Best time to post" is marketed by all of them, but none documents how it is computed. Read
it as a histogram of the account's own engagement by hour of the week.

None of them likes or follows. In 2018 Instagram removed its likes, follows and relationship
endpoints and banned "like/share/comment/follower exchange" programs
([VentureBeat](https://venturebeat.com/social/new-third-party-instagram-apps-cant-follow-or-like-on-users-behalf/)).
In November 2018 it started removing likes, follows and comments made by third-party growth
apps, found with machine-learning classifiers, and forced password changes on those accounts
([Instagram](https://about.instagram.com/blog/announcements/reducing-inauthentic-activity-on-instagram/)).

**Postiz is the closest open architecture** (read from the
[gitroomhq/postiz-app](https://github.com/gitroomhq/postiz-app) source):

- One provider file per platform (`*.provider.ts`, about 37). Each implements authentication
  (`generateAuthUrl`, `refreshToken`, analytics) and integration (`post`, optional
  `postPending` with `checkPostStatus` and `finalizePost` for asynchronous publishing,
  optional `comment`).
- An idempotency contract: once `finalizePost` went through, `checkPostStatus` must say
  `completed`, so a retry after an unknown outcome can never post twice. Our connectors have
  the same rule (no half posts); we keep it for every engagement action too.
- Temporal workflows replace cron and workers. Each platform has its own task queue with its
  own concurrency: Reddit 1, LinkedIn 8, Instagram 400, Facebook 500.
- One error classifier per provider: `refresh-token`, `bad-body`, `retry` or `disconnect`.
  HTTP 429 is retried. Platform strings become user messages ("Page posting for today is
  limited, please try again tomorrow").
- "Plugs": declared rules with a trigger, an interval and a maximum number of runs. This is
  the shape we copy for community rules ("thank people who comment").

### 2.2 Engagement and growth automation

**Jarvee** was a desktop tool for Instagram, Facebook, Twitter and others with per-account
settings for follows, likes, comments, delays and sleep times. It went private around
2022-09-20 without a public reason. Its predecessor MassPlanner closed in 2017 after an
Instagram cease-and-desist letter ([Proxyway](https://proxyway.com/news/jarvee-shuts-down),
[Owlead](https://owlead.com/jarvee-alternatives-what-works-now/)). The market then moved to
phone and device farms (Onimator drives Android devices and emulators,
[Capterra](https://www.capterra.com/p/10039110/Onimator/)) and to cloud tools. "JarveePro"
(2025) has no known link to the original team
([Product Hunt](https://www.producthunt.com/products/jarveepro-social-media-automation-tool/launches)).

**PhantomBuster** runs single-purpose scripts ("phantoms") as scheduled launches, with a
batch size per launch so a daily total is spread over several launches. It calls its numbers
"recommended, not enforced", and users report warnings below them
([Emelia review](https://emelia.io/software/phantombuster)).

- Instagram auto-follow [VENDOR]: at most 5 profiles per launch, under 200 follows a day,
  launches hours apart. Auto-liker: 1 like an hour over 12 hours (from PhantomBuster support
  articles, which refused our fetcher; numbers from search snippets).
- LinkedIn [VENDOR]: about 80 profile visits a day (150 on Sales Navigator). Start at 20 to
  40 visits a day for 3 to 5 days, then raise only without warnings. Connection requests go
  from 5 to 10 a day in week 1 to 20 to 25 a day by week 4
  ([PhantomBuster blog](https://phantombuster.com/blog/linkedin-automation/linkedin-limits-workaround/)).

**Apify** sells scraper actors priced per result (official Instagram scraper about $1.50 per
1,000 posts, [use-apify](https://use-apify.com/docs/how-to-use-apify/scrape-instagram)). In
growth stacks it is the targeting source, kept separate from the account that acts. We do not
copy this (section 5.3).

**Instagram growth services** (Kicksta, SocialCaptain, Plixi, Nitreo):

- Kicksta: you pick target accounts (competitors recommended), then filter by post count,
  follower count, engagement rate and last post date. These filters cannot apply to private
  profiles. It keeps allow and block lists, can "like after follow", and separates targeted
  growth from engagement pods ([targets](https://intercom.help/kicksta-9807bc/en/articles/12414771-best-practices-for-choosing-the-right-targets),
  [filters](https://intercom.help/kicksta-9807bc/en/articles/12413995-filter-your-audience),
  [lists](https://intercom.help/kicksta-knowledge-base/en/articles/12690365-allow-block-lists),
  [pods](https://intercom.help/kicksta-9807bc/en/articles/12414450-targeted-growth-vs-growth-pods-network)).
  Reviews mention gender, language and profanity filters
  ([Software Advice](https://www.softwareadvice.com.au/software/239482/kicksta)).
- SocialCaptain: hashtags, locations and demographics with "smart auto targeting"
  ([Creati](https://creati.ai/ai-tools/socialcaptain-com/)).
- Shared pattern: a source pool (competitor followers, hashtags, locations), then filters,
  then actions (follow, then like a few posts), then allow and block lists. None publishes a
  daily rate.

**LinkedIn automation tools** (the most mature timing and limit models):

- Waalaxy [VENDOR]: "200 per week" invitations, 80 to 100 a day within that. When the cap is
  hit, queued invites show "Postponed" and resume next Monday. Wait 3 weeks before
  re-inviting someone whose pending invite you withdrew
  ([quotas](https://intercom.help/waalaxy/en/articles/5463827-daily-quotas-and-linkedin-restrictions)).
- Expandi [VENDOR]: 80 to 100 invites a week for most accounts, a warm-up toggle that raises
  limits gradually, and a refusal to go over limits
  ([Expandi](https://expandi.io/blog/linkedin-connections-limit)). A review says it pauses on
  LinkedIn security challenges ([SyncGTM](https://syncgtm.com/blog/expandi-review)).
- Dripify [VENDOR]: limits per action, hold them 1 to 2 weeks, then raise 10 to 20 percent a
  week. Working hours with a time zone (campaigns suspend at the end of the window and resume
  at the start), random delays, and "Activity Control" that adjusts limits from connection
  count, pending invites, account type and history
  ([Dripify help](https://help.dripify.com/en/articles/8201637-how-dripify-daily-limits-safeguard-your-linkedin-account)).
- Linked Helper (richest timing): global working hours and per-action working hours (both
  must allow), delays between workflow steps (days) and inside actions (clicks, typing),
  batches with a pause between batches, randomized start times, "smart daily limits" that
  vary day to day, and nested caps (24 hours plus hourly)
  ([action timing](https://www.linkedhelper.com/features/action-timing-control)). Campaigns
  "sleep" outside hours or after the daily cap
  ([Linked Helper blog](https://medium.com/linked-helper/why-is-my-campaign-shown-as-sleeping-when-will-it-start-59213799fb16)).
- HeyReach: sender rotation. A campaign is split over several accounts, and limits are set per
  sender and shared by all that sender's campaigns
  ([HeyReach help](https://help.heyreach.io/en/articles/9897768-multiple-linkedin-senders-on-one-campaign-sender-rotation)).
- Octopus CRM: a Chrome extension with basic daily limits and no smart throttling
  ([ZoomInfo review](https://pipeline.zoominfo.com/sales/octopus-crm-review)).
- Restriction behavior [FOLK]: the "weekly invitation limit" stops invites but not browsing
  or commenting; reported waits are 3 to 7 days
  ([Cleverly](https://cleverly.co/blog/linkedin-youve-reached-the-weekly-connection-limit-now-what)).

**Reddit tools**:

- Postpone schedules posts with title autocomplete. Its help describes the escalation ladder
  (removals, warning, 3 to 7 day suspension, permanent ban), the spam signals (account age,
  low karma overall and per subreddit, unverified email, reports, identical posts across
  subreddits, mod removals), and a shadowban check: signed in, visit reddit.com/appeal; "nothing
  to appeal" means clean
  ([Postpone help](https://help.postpone.app/en/scheduling-posts/spam-suspensions-and-shadowbans-on-reddit)).
- ReplyGuy watches keywords and drafts replies that mention your product, with auto-post or
  review first. Reviewers call auto-post a ban risk
  ([Redship](https://redship.io/reddit-tool/replyguy)).
- "Spend 2 to 4 weeks and 500 karma before promoting" is [FOLK]
  ([example](https://gyfc.vercel.app/blog/avoid-reddit-shadowban)).

### 2.3 Anti-detect browsers

- **Multilogin X**: attaches Selenium, Puppeteer or Playwright to a running profile over
  remote WebDriver or CDP ([FAQ](https://multilogin.helpjuice.com/en_US/automation-faq)). A
  "CookieRobot" spends about 3 minutes per site building history on a fresh profile
  ([cookie robot](https://multiloginx.helpjuice.com/en_US/script-runner-predefined-scripts/cookie-robot-script)).
  Its warm-up guide refuses a universal schedule: "a few days of gradual activity" for
  Instagram, 2 to 3 weeks on Reddit before promotion, finish the profile before outreach,
  avoid identical engagement patterns across groups of accounts, and do not log in "like
  clockwork" ([warm-up](https://multiloginx.helpjuice.com/en_US/start/how-to-warm-up-a-profile-in-multilogin-x)).
- **GoLogin**: no official day-by-day guide. Forum advice is 10 to 15 minutes a day of
  general browsing per profile [FOLK]
  ([BlackHatWorld](https://www.blackhatworld.com/seo/browser-profile-warmups.1731824/)).
- **AdsPower RPA**: a recorder replays clicks, typing and scrolling as a flow, with
  random-interval waits and scroll steps
  ([extension](https://chromewebstore.google.com/detail/hkefjlonaegglofbhbjblbhmhfhmnfjf)).
  Community Facebook warm-up flows: browse the feed, like posts, later create pages and
  publish. We did not verify the official templates.
- **Dolphin Anty, Octo Browser**: visual scenario builders and local APIs that hand back a CDP
  endpoint ([Dolphin](https://intercom.help/dolphinteam/en/articles/7127390-basic-automation-dolphin-anty)).
- **Proxy hygiene** (all vendors agree): choose the proxy first, then set timezone, language
  and geolocation to match it; watch WebRTC, IPv6 and DNS leaks; do not stack a VPN on the
  proxy ([Pixelscan](https://pixelscan.net/blog/avoid-location-leak/)). Our lane check already
  covers this (proxy alive, exit IP and geo against the pinned timezone and locale, WebRTC and
  DNS, fingerprint pin).

Warm-up schedules published by vendors conflict, which itself shows they are folklore:
5 days for Facebook ([IPHTML](https://iphtml.com/blog/A-Comprehensive-Guide-to-Creating-and-Warming-Up-Facebook-Accounts-Safely-27)),
week 1 at 25 to 30 likes and 5 to 10 follows a day for Instagram
([Picmim](https://blog.picmim.com/blog/warm-up-new-social-media-account-30-day-strategy)),
or 2 weeks starting at 3 to 5 likes and 1 to 2 follows a day
([DEV](https://dev.to/xavier_fok/account-warm-up-strategies-that-keep-your-accounts-alive-5934)).

### 2.4 How they model human timing

Every serious tool uses the same five pieces, and none of them publishes a model:

1. Working hours per account in its time zone, sometimes per action (Linked Helper, Dripify).
2. Random delays between actions, as uniform min and max ranges (AdsPower `randomInterval`).
3. Batches or launches separated by long pauses (PhantomBuster, Linked Helper).
4. Daily limits that vary from day to day (Linked Helper "smart daily limits").
5. Ramps: hold, then plus 10 to 20 percent a week (Dripify), or 2 to 5 weeks overall.

Research shows that uniform random delays are not what humans look like:

- Human activity is bursty: long gaps between bursts, heavy-tailed rather than Poisson
  ([Barabasi, Nature 2005](https://arxiv.org/abs/cond-mat/0505371)). The data fits a
  log-normal at least as well as a power law
  ([Eckmann et al.](https://arxiv.org/pdf/physics/0510216)).
- Time on a page follows a Weibull with "negative aging": the longer you stay, the less likely
  you leave ([Liu, White, Dumais, SIGIR 2010](https://microsoft.com/en-us/research/wp-content/uploads/2010/10/dwelltime.pdf)).
- Activity follows strong daily rhythms, with weekend mornings about 2 hours later
  ([Golder and Macy, Science 2011](https://www.aaas.org/news/science-tweets-reveal-role-circadian-rhythms-mood)).
- Users switch between active and inactive states; a two-state model fits posting better than
  a single self-exciting process ([arXiv 2002.08521](https://arxiv.org/abs/2002.08521v1)).
- Detectors look at timing (Botometer's temporal features,
  [Kudugunta et al.](https://arxiv.org/abs/1802.04286)), but the strongest signal for a fleet
  is **synchronization across accounts**: DeBot flags accounts whose activity stays correlated
  for a long time, because "humans cannot be highly synchronous for a long duration"
  ([DeBot, ICDM 2016](https://www.cs.unm.edu/~chavoshi/debot/)). Meta enforces against
  networks of assets controlled by the same people
  ([Meta inauthentic behavior policy](https://transparency.meta.com/policies/community-standards/inauthentic-behavior)).

### 2.5 How they detect and react to blocks

- Instagram private API clients classify responses: `feedback_required` ("This action was
  blocked. Please try again later."), `challenge_required`, `checkpoint_required`,
  `login_required`, "Please wait a few minutes"
  ([instagrapi exceptions](https://subzeroid.github.io/instagrapi/3.0.20/exceptions/),
  [handling](https://subzeroid.github.io/instagrapi/usage-guide/handle_exception.html)).
  Its best practices: retry transport errors only, cool down on feedback blocks, stop for a
  human after repeated challenges
  ([best practices](https://subzeroid.github.io/instagrapi/usage-guide/best-practices.html)).
- Instagram web dialogs: "Try Again Later", "Action Blocked", "We restrict certain activity to
  protect our community" ([Vista Social](https://support.vistasocial.com/hc/en-us/articles/5462397210011-Instagram-error-explained-We-restrict-certain-activity-to-protect-our-community)).
  Durations [FOLK]: "try again later" hours, first block 24 to 48 hours, repeat 3 to 7 days, up
  to 30 days for repeat offenders, and retrying during a block extends it
  ([Proxidize](https://proxidize.com/blog/instagram-action-block),
  [Lilach Bullock](https://www.lilachbullock.com/instagram-says-try-again-later-how-long/)).
- Facebook: "It looks like you were misusing this feature by going too fast" (API error 368,
  subcode 1390008) is what schedulers see
  ([Agorapulse](https://support.agorapulse.com/en/articles/8773638-facebook-publishing-errors)).
  "A day to a week" is folklore ([SmarterQueue](https://help.smarterqueue.com/article/238-temporary-facebook-block)).
- LinkedIn documents the triggers (many invites in a short time, ignored or spam-marked
  invites, suspected automation) and that "most restrictions will automatically be removed
  within one week" ([LinkedIn Help 4800](https://www.linkedin.com/help/linkedin/answer/4800)).
- Reddit returns `RATELIMIT` ("you are doing that too much, try again in X minutes"); PRAW
  waits only for short ones and raises for long ones
  ([PRAW](https://praw.readthedocs.io/en/latest/getting_started/ratelimits.html)).
- Tools react by pausing the action or the whole account, never by retrying (Expandi,
  instagrapi, Waalaxy "Postponed").

---

## 3. Platform rules and realities

### 3.1 What the official APIs allow

| Platform | Post | Reply on own posts | Like | Follow | Comment on others | Source |
|---|---|---|---|---|---|---|
| Instagram (professional account) | yes: images (JPEG), video, reels, carousels up to 20, stories | yes: reply, hide, delete, turn comments off | **no** | **no** | **no**, except replying to a comment that @mentions you | [content publishing](https://developers.facebook.com/docs/instagram-platform/content-publishing), [comment moderation](https://developers.facebook.com/docs/instagram-platform/comment-moderation), [mentions](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/mentions) |
| Facebook Page | yes, as the Page (scheduled 10 minutes to 30 days out) | yes, as the Page | yes, a Page can like posts and comments it can see | no | not documented for posts the Page does not own | [posts](https://developers.facebook.com/docs/pages-api/posts), [comments](https://developers.facebook.com/docs/pages-api/comments-mentions), [likes](https://developers.facebook.com/docs/graph-api/reference/object/likes) |
| Facebook personal profile | **no** (`publish_actions` removed 2018) | no | no | no | no | [Meta 2018](https://developers.facebook.com/blog/post/2018/07/31/platform-update-publish-permission/) |
| Facebook Groups | **no** (Groups API removed 2024) | no | no | no | no | [v19](https://developers.secure.facebook.com/blog/post/2024/01/23/introducing-facebook-graph-and-marketing-api-v19/) |
| LinkedIn member | yes (`w_member_social`, self-serve "Share on LinkedIn", 150 requests per member a day) | yes | yes (Reactions API) | no | yes (Comments API) | [Posts API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api), [Share on LinkedIn](https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/share-on-linkedin), [reactions](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/reactions-api), [comments](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/comments-api) |
| LinkedIn company page | yes (`w_organization_social`, admin roles, Community Management API is vetted) | yes | yes | no | yes | [Community Management](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/community-management-overview) |
| Reddit | yes, but new OAuth apps need approval (Responsible Builder Policy, 2025-11-11) | yes | API votes "must be cast by humans": a one-for-one proxy of a human's choice is fine, software choosing is not | subscribe exists | yes (same approval) | [RBP announcement mirror](https://r.datuan.dev/r/redditdev/comments/1oug31u/introducing_the_responsible_builder_policy_new/noc31sg), [vote rule quoted](https://docs.zernio.com/connect/vote-reddit-thing) |
| Medium | **no**: API unsupported, no new tokens since 2025-01-01; the import tool sets a canonical URL | no | no | no | no | [GitHub](https://github.com/Medium/medium-api-docs), [import](https://help.medium.com/hc/en-us/articles/214550207) |
| Blog | WordPress REST and Ghost Admin API: yes | WordPress: yes (`/wp/v2/comments` with `parent`); Ghost: no comments endpoint | n/a | n/a | n/a | [WordPress](https://developer.wordpress.org/rest-api/reference/comments/), [Ghost](https://docs.ghost.org/admin-api/) |

Details that shape the design:

- **Instagram publishing quota disagrees with itself**: "100 API-published posts within a
  24-hour moving period" in one place, "50" elsewhere, and the `content_publishing_limit`
  reference says the quota is "currently 50"
  ([reference](https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/content_publishing_limit.md)).
  Read the live value per account; it is far above our posting pace anyway.
- **Instagram needs a professional account** for any API use. Basic Display for personal
  accounts ended on 2024-12-04. Standard Access (accounts with a role on the app) is enough
  because all accounts are the owner's; Advanced Access needs App Review
  ([overview](https://developers.facebook.com/docs/instagram-platform/overview)). Hashtag
  search is read-only and capped at 30 unique hashtags per 7 days
  ([hashtag search](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-hashtag-search)).
- **Facebook rate limits** are API quotas (Pages: 4800 times engaged users per 24 hours), not
  anti-spam pacing ([rate limiting](https://developers.facebook.com/docs/graph-api/overview/rate-limiting)).
- **LinkedIn API rate limits** are per endpoint, unpublished, shown in the developer portal,
  reset at midnight UTC ([rate limits](https://learn.microsoft.com/en-us/linkedin/shared/api-guide/concepts/rate-limits)).
  Comment creation has a short per-member limit that returns 429. Pin a current
  `Linkedin-Version`: 202511 sunsets on 2026-11-16.
- **Reddit API** free tier is 100 queries a minute per OAuth client, averaged over 10 minutes
  [DOC]. Since 2026-03-31 approved automated accounts carry an "[App]" label, and accounts
  that look automated without it may be asked to prove a human is behind them
  ([Help Net Security](https://www.helpnetsecurity.com/2026/03/26/reddit-human-verification-changes/)).

### 3.2 Terms of service on automation

- **Instagram Terms 4.2**: "You can't attempt to create accounts or access or collect
  information in unauthorized ways. This includes creating accounts or accessing or collecting
  information in an automated way without our express permission"
  ([terms archive](https://github.com/OpenTermsArchive/pga-versions/blob/main/Instagram/Terms%20of%20Service.md)).
  Community Guidelines: "not artificially collecting likes, followers, or shares, posting
  repetitive comments or content" (quoted via [Scroll.in](https://scroll.in/announcements/902809/scroll_in)).
- **Facebook Terms 3.2.3**: no access or collection "using automated means (without our prior
  permission)"; "Platform APIs are the only allowable means to access data programmatically"
  ([Meta](https://developers.facebook.com/docs/development/terms-and-policies/automated-data-collection/)).
- **Meta inauthentic behavior**: a connected network of assets controlled by the same people
  delivering engagement is "fake engagement"
  ([policy](https://transparency.meta.com/en-gb/policies/community-standards/inauthentic-behavior/)).
  In 2025 Meta announced lower reach for networks posting identical content and lower
  visibility for coordinated comments ([Tubefilter](https://www.tubefilter.com/2025/04/24/meta-content-moderation-facebook-spam-posts-comments/)).
  **This is the rule 29 accounts owned by one person most easily break**, by liking and
  commenting on each other.
- **LinkedIn User Agreement 8.2**: no "bots or other unauthorized automated methods" to add
  contacts, send messages, or "create, comment on, like, share, or re-share posts, or otherwise
  drive inauthentic engagement" ([agreement](https://www.linkedin.com/legal/user-agreement),
  [prohibited software](https://www.linkedin.com/help/linkedin/answer/a1341387)). The API is
  the authorized route; browser automation is the prohibited one. In 2025 and 2026 LinkedIn
  started limiting the visibility of automated comments and testing a "seems like AI slop"
  report ([SocialMediaToday](https://www.socialmediatoday.com/news/linkedin-limit-visibility-of-comments-made-via-automation-tools/758207/),
  [Campaign](https://campaignlive.com/article/linkedin-cracks-down-automated-content-new-seems-ai-slop-detection-button/1966416)).
- **Reddit Rules**: "do not cheat or engage in content manipulation (including spamming, vote
  manipulation, ban evasion, or subscriber fraud)" ([archived policy](https://webarchive.library.unt.edu/web/20201218091247/https://www.reddit.com/help/contentpolicy)).
  "It's perfectly fine to be a redditor with a website, it's not okay to be a website with a
  reddit account." The "10 percent rule" is an old heuristic, demoted in 2017. Subreddit rules
  are what is enforced, often by AutoModerator checks on account age and karma
  ([AutoModerator wiki](https://lr.eu.psf.lt/r/AutoModerator/wiki/can_do)).
- **Medium Rules**: spam includes "a disproportionately large number of interactions,
  particularly by automated means" ([Medium Rules](https://policy.medium.com/medium-rules-30e5502c4eb4)).

### 3.3 Action blocks and pacing: what is documented

| Claim | Status |
|---|---|
| Instagram API publishing quota | [DOC] 50 a day (also stated as 100), read live |
| Instagram or Facebook likes, follows, comments per hour or day | not published; every number is [VENDOR] or [FOLK] |
| Meta finds growth-app activity with machine learning | [DOC] |
| LinkedIn about 100 invitations a week | [FOLK]; LinkedIn documents triggers and "about a week" durations, not the number |
| LinkedIn withdrawn invite cannot be resent for 3 weeks | [DOC] |
| Reddit 100 queries a minute per client | [DOC] |
| Reddit karma and age gates | per subreddit AutoModerator rules; no sitewide threshold published |
| New accounts are throttled harder than aged ones | every platform ties limits to history; no numbers; treat as true |

Folklore ceilings collected by the field research (used in section 5.5 only as ceilings we
stay well below):

| Platform | Action | New account | Aged account | Source |
|---|---|---|---|---|
| Instagram | follows a day | 50 to 100 | 150 to 200 | [FOLK], PhantomBuster says under 200 [VENDOR] |
| Instagram | likes a day | about 250 | 300 to 1000 | [FOLK] ([Metricool](https://metricool.com/instagram-limits/)) |
| Instagram | comments a day | 30 to 50 | 100 to 150 | [FOLK] |
| Instagram | total following | 7,500 cap | same | widely cited to Instagram help, unverified |
| LinkedIn | invites a week | 15 to 25 | 80 to 200 | [VENDOR] Expandi, Waalaxy |
| LinkedIn | profile views a day | 20 to 40 | about 80 | [VENDOR] PhantomBuster |
| Reddit | warm-up before promoting | 2 to 4 weeks | n/a | [FOLK] |

### 3.4 What the canary verified today (2026-11-04)

The canary builder drives throwaway accounts on `test/reddit`, `test/instagram`,
`test/facebook` and `test/linkedin` through MCP only, with egress observe mode tracing every
non-GET request by shape (never values). Findings that matter here:

- **Verified request shapes** (commit 2569f6a, `src/agentos/node/egress.py`):

  | Rule | Request | Shape |
  |---|---|---|
  | `instagram.like` | GraphQL `usePolaris(Like\|Unlike)Media*Mutation` | `variables.input.{media_id, actor_id, container_module}` |
  | `instagram.follow` | GraphQL `usePolaris(Follow\|Unfollow)Mutation` | verified on web |
  | `instagram.comment` | GraphQL `PolarisPostCommentInput*Mutation` | `variables.data.{comment_text, media_id}` |
  | `reddit.post` | `POST /svc/shreddit/graphql`, `CreatePost` | `variables.input.{subredditName, title, content.richText, recaptchaToken}` |
  | `reddit.like.post` | same endpoint, `UpdatePostVoteState` | `variables.input.{postId, voteState}` |
  | `reddit.like.comment` | `UpdateCommentVoteState` | `variables.input.{commentId, voteState}` |
  | `reddit.join` | `UpdateSubredditSubscriptions` | `variables.input.inputs[].{subredditId, subscribeState}` |

  Facebook (`CommentCreate*Mutation`, post), LinkedIn (`normShares`) and Medium rules are still
  unverified. Effect rules now match GraphQL operations by name, and likes, follows and joins
  are classed `write` with an effect kind (`post`, `comment`, `like`, `follow`, `join`), which
  is exactly the hook the engine needs to count and verify actions per kind.
- The old `reddit.submit` rule (`/api/submit`) did not match the real request, so before the
  fix a click under a draft-only grant could have published unnoticed. **Lesson: no
  engagement operation ships until its effect rule is verified on a throwaway account.**
- Reddit's post runs invisible reCAPTCHA Enterprise (`reload`, `clr`) before the publish and
  passed. Invisible checks are normal; only a visible challenge is a hold (sites.py already
  says this).
- After Post, Reddit went to `/r/test/`, not the permalink, so readback must find the new
  object rather than trust the landing page.
- A signed-out check (`readback.public: cookieless`) is declared but nothing runs it, and a
  plain HTTP read cannot stand in on Reddit (JavaScript shell, login wall, 403 from the node's
  IP). Shadowban probes in this design need the cookieless browser context through the lane's
  proxy that the canary added to the backlog. (Built since for readbacks: `lanes.read_signed_out`,
  a throwaway browser with the lane's launch options, agentos/node/signed_out.py; probes can call it.)

---

## 4. Principles the engine follows

These come from the research and from `docs/PRINCIPLES.md`.

1. **The system acts, agents set levers.** No model chooses a click. Agents and the owner
   write programs; the engine runs connector operations; a model only drafts comment text,
   which deterministic checks and (by default) the owner approve.
2. **Official API first.** Posting and replies on own posts go through the API wherever the
   owner can use one. The browser backend is for actions no API offers.
3. **One real person's pace.** Caps start at a fraction of the folklore ceilings, ramp slowly,
   and can only be raised by the owner up to a hard ceiling in the catalog. This is principle
   1 ("parity with normal use ... not there to defeat detection at scale"), not stealth.
4. **No fleet-internal engagement.** The owner's accounts never like, comment on, follow or
   vote for each other's content unless the owner turns on a named exception per pair (for
   example a founder's personal account liking the company's launch post), capped at once per
   post. This is the coordinated-behavior line in Meta's and Reddit's rules.
5. **Verify every effect.** Each action is proven by the effect rule seeing the request and
   the readback seeing the result on the target's own state (the heart is filled after a
   reload, the comment is on the post under the right author). A toast is never proof.
6. **Stop at the first push-back.** A block on one action pauses that action for days; a
   challenge holds the lane for a human; nothing is retried inside a block.
7. **Say it is automated where asked.** Reddit's "[App]" labeling and the EU AI Act disclosure
   rule apply to software-run accounts. Comments in `auto` mode on platforms that ask for
   disclosure are off until the owner decides (open question 4).

---

## 5. The engine

### 5.1 Action library (connector operations)

Every action is a connector operation (`docs/design/connectors.md` 4.2) with a contract, an
effect, an acting-as check, structural locators, a verified effect rule, a readback, signals
and pace. The engine never calls `browser_act`. Backends: `api` where it exists, `browser`
otherwise.

| Operation | Effect | Backend | Effect rule | Readback |
|---|---|---|---|---|
| `instagram.post_photo`, `post_carousel`, `post_reel`, `post_story` | commit | api (professional account), browser fallback | `instagram.post` (`media/configure`) | permalink, author |
| `instagram.like` / `unlike` | write | browser | `instagram.like` **verified** | heart state after reload |
| `instagram.follow` / `unfollow` | write | browser | `instagram.follow` **verified** | button state on profile |
| `instagram.comment` | commit | browser | `instagram.comment` **verified** | comment under own author on the post |
| `instagram.reply_own` (reply to a comment on own media) | commit | **api** | n/a (API) | API returns the reply id |
| `instagram.hide_comment` (own media) | write | api | n/a | API |
| `instagram.view_story` | read (counts toward the session, visible to the author) | browser | read | n/a |
| `instagram.browse_feed`, `open_post`, `open_profile`, `open_hashtag` | read | browser | read | n/a |
| `facebook.page_post` | commit | **api** (Pages API) | n/a | API post id plus permalink read |
| `facebook.page_reply_own` | commit | api | n/a | API |
| `facebook.page_like_comment` (comments on own Page posts) | write | api | n/a | API |
| `facebook.like`, `facebook.comment` (personal profile) | write, commit | browser | `facebook.comment` unverified | after canary |
| `linkedin.post`, `linkedin.company_post` | commit | api (`w_member_social`, `w_organization_social`), browser fallback | `linkedin.post` unverified | API URN |
| `linkedin.react`, `linkedin.comment`, `linkedin.reply_own` | write, commit | **api** (Reactions and Comments APIs) | n/a | API |
| `linkedin.follow`, `linkedin.connect` | write | browser | to capture | button state |
| `reddit.post_text`, `post_link` | commit | browser (RBP approval for a new OAuth app is uncertain) | `reddit.post` **verified** | find the new post on the profile, permalink, signed-out check |
| `reddit.comment`, `reddit.reply_own` | commit | browser | `reddit.comment.oldreddit` (new client to capture) | permalink, signed-out check |
| `reddit.vote` | write | browser | `reddit.like.post` / `like.comment` **verified** | only one-for-one: the owner picked this item (section 5.4) |
| `reddit.join` | write | browser | `reddit.join` **verified** | joined state |
| `medium.import_story` (from the blog URL) | commit | browser | `medium.post` unverified | story URL with canonical link |
| `medium.clap`, `medium.follow` | write | browser | to capture | low priority |
| `blog.post`, `blog.reply_own` | commit | api (WordPress or Ghost) | n/a | API |

Rules for the library:

- **Undo is part of the contract** for writes (`unlike`, `unfollow`, leave), so a wrong action
  can be reversed by the engine itself when the owner asks.
- **"Already done" is a success with no request.** Every like and follow first reads the
  current state; if the heart is filled or the button says following, the operation returns
  `already` and costs nothing. This fixes the PoC's double like on Facebook comments and
  matches the owner's rule "skip things the human already reacted to".
- **Read operations are real browsing.** `browse_feed` scrolls with the mouse wheel, dwells,
  opens some posts. They exist because a session that only acts is the least human thing
  there is.
- Each operation declares `kind` from the effect rule (`like`, `follow`, `comment`, `post`,
  `join`), and budgets count per kind.

### 5.2 Programs: what the owner and agents set

A **program** is the only thing an agent or the owner writes. It is data, validated by a
pydantic model, hashed, stored on the node, and approved once by the owner (section 5.8).

```yaml
program: biz-a-ig-coffee
surface: biz-a/instagram
risk: careful                    # careful | normal | off (off = posting and own-post replies only)
phases:
  - kind: warm_up                # section 5.4
    days: 10
  - kind: steady
    daily:                       # targets, not promises; the governor may give less
      like: 15
      comment: 3
      follow: 2
      story_view: 10
    targets:
      - {source: hashtags, values: [specialtycoffee, vilniuscoffee, latteart]}
      - {source: own_engagers, lookback_days: 14}
      - {source: own_feed}
    filters: {language: [lt, en], min_account_age_days: 90, max_following_ratio: 3,
              exclude_topics: [politics, religion, health_claims, minors]}
comments:
  mode: approve                  # approve | auto | off
  voice: memory:biz-a/voice      # tone notes in workspace memory
  topics: [coffee, cafe life, brewing]
  max_chars: 140
community:
  reply_to_comments: {mode: approve, within_hours: 6}
  thank_new_followers: false
posting:
  calendar: biz-a/instagram      # the content queue, section 5.7
report: {daily: true, to: owner}
```

The same program can be written as one sentence. An agent passes the sentence to
`engage_program_draft`, which returns the YAML and a plain summary, and nothing runs until the
owner approves it. Translation from words to YAML is done by the brain, and the validated
YAML, not the words, is what the owner approves.

### 5.3 Targeting sources and filters

| Source | What it reads | Default |
|---|---|---|
| `own_feed` | the account's home feed, as a person scrolls it | on |
| `own_engagers` | people who liked or commented on the account's posts (API where it exists, else the post's own lists) | on |
| `own_post_comments` | comments on own posts (for replies) | on |
| `hashtags` / `topics` | the hashtag or topic page, top and recent | on when listed |
| `communities` | Reddit subreddits, LinkedIn groups, Facebook groups the account is in | on when listed |
| `locations` | Instagram location pages | off |
| `accounts` | posts of named partner or competitor accounts (their posts, not their follower lists) | on when listed |
| `followers_of` | a named account's follower list | **off**, and small when on |

**Why `followers_of` is off.** It is the classic growth-tool source (Kicksta, Jarvee), and it
is exactly the bulk read the connectors design ruled out ("no list followers of X"). It is
also the most detectable: opening a follower list and following from it is the growth-bot
signature. When the owner turns it on, the engine reads at most one screen of the list per
session (what a person sees), never exports it, and never uses a scraping service.

All targeting reads happen **inside the account's own sessions, at session pace**. There is no
background crawler and no separate scraping account. The target pool is a small store per
surface: candidate posts and people with where they came from, when seen, and why they were
kept or dropped. Candidates expire after 3 days, so the engine acts on fresh content as a
person would.

**Quality filters** (all deterministic, from structure and the post's own data):

- account age where visible, posts count, follower and following counts and their ratio;
- private, verified, business or creator (a private account cannot be judged before
  following, so skip it unless `own_engagers`);
- language of the caption (a small local language detector, not a model);
- last post date (skip inactive accounts);
- sensitive topics excluded by keyword lists and the account's own block list: politics,
  religion, tragedy, health claims, anything involving minors, adult content, gambling,
  giveaways and engagement bait ("comment YES");
- **never the owner's own accounts** (principle 4), never someone the account interacted with
  in the last 30 days for the same action kind, never a person who blocked or reported (when
  visible);
- an allow list (always eligible) and a block list (never) per workspace, as Kicksta does.

### 5.4 Campaigns and programs

**Warm-up** (new accounts, and accounts coming back from a block). Vendors disagree, so the
engine uses the most conservative shape and verifies as it goes:

| Days | Sessions a day | Allowed |
|---|---|---|
| 1 to 3 | 1 to 2, 5 to 12 minutes | browse feed, view stories, open posts; no writes |
| 4 to 7 | 2 | plus up to 5 likes a day, 1 follow a day, join 1 community |
| 8 to 14 | 2 to 3 | likes up to 10, follows up to 3, first comment (approve mode) every other day |
| 15 onward | as the steady phase | steady targets, ramp continues (section 5.5) |

Warm-up advances only if the previous phase had no soft or hard signal. Any signal sends it
back one phase. Aged accounts (the owner's existing ones) start at the steady phase with the
ramp, not at day 1.

**Steady engagement**: daily targets per kind, spread over 2 to 4 sessions, mostly browsing.

**Launch push** (a product launch or important post): for up to 3 days, the posting calendar
holds the launch post, community replies come faster (`within_hours: 1` during active hours),
and steady targets may rise by up to 30 percent but never above the ramp's current cap. A
launch push never adds cross-account engagement, except the named owner-approved exceptions.

**Community management** (the safest and most valuable part): replies to comments on own posts
(API on Instagram professional, Facebook Pages, LinkedIn, WordPress; browser on Reddit), likes
on those comments (Facebook Page API; browser on Instagram), optional thanks to new followers
(off by default, because mass thank-you messages are spam-shaped). These run in `approve` mode
by default, and they are the first engagement to move to `auto`.

**Reddit is special.** No program chooses what to upvote: Reddit's rule is that votes must be
cast by a human, and software amplifying a vote is not allowed. Reddit programs do: join
listed communities, browse them, draft comments for the owner to approve, reply on own posts,
and vote only one-for-one on items the owner or the drafter's approval card explicitly lists.
Posting follows each subreddit's rules (read once and kept in memory) and a per-subreddit
gap of at least 7 days.

### 5.5 The timing model

Everything is drawn per account from its own seeded random generator, so two accounts never
share a clock.

**Persona.** At program creation the engine draws a persona per account and keeps it (it
drifts by a few percent a week): wake time and sleep window (a 6 to 9 hour trough, jittered),
weekday and weekend shift (weekends about 2 hours later, after Golder and Macy), a preferred
session count, a session length scale, a "scroll speed", and a "liker" factor (how often it
acts per item seen). The persona lives in workspace memory with `node` trust.

**Daily plan** (drawn each night for the next day, in the proxy's local time zone):

1. **Quiet day?** With probability 1 in 7 (persona-dependent, 0.08 to 0.2), the account does
   nothing but reply to comments on own posts. At most 2 quiet days in a row.
2. **Daily volume**: the program's target times a draw from a log-normal around 1 (sigma
   0.3), clipped to the current cap. So "15 likes" is 9 one day and 19 another, never above
   the cap.
3. **Sessions**: 2 to 4 starts drawn from a non-homogeneous Poisson process whose rate follows
   the persona's daily curve (morning, lunch, evening bumps), with a minimum gap of 45 minutes.
   Starts are never on round minutes and never at the same time as yesterday plus or minus 10
   minutes (Multilogin's "like clockwork" warning).
4. **Split**: actions are divided among sessions unevenly (a Dirichlet draw), so one session
   may hold most of the day's likes.

**Inside a session** (driven by the session runner, section 6):

- Length: log-normal, median 9 minutes, rarely over 25.
- A two-state rhythm: "browse" (scroll, dwell, open) and "engage" (act on the item in view),
  switching with low probability per item, so actions come in short bursts.
- Dwell per item: Weibull with shape below 1 (most items skimmed, some read long), scaled by
  the persona; a video is watched for a random share of its length.
- Gap between actions: log-normal, median 20 seconds, with a long tail; never a fixed or
  uniform delay.
- **Action ratio**: most items seen get no action. For likes, about 1 in 4 to 1 in 8 items
  seen; comments far rarer. A session where the account acts on everything is refused by the
  planner.
- Input is the existing human layer: mouse-wheel scrolling, a pause before acting, a random
  point inside the target with a real press, typing one character at a time with uneven
  rhythm (`human.py`, `typist.py`). Comments are typed, never pasted.

**Caps** (hard limits the governor enforces regardless of the program):

| Platform | Kind | `careful` start | `careful` ceiling | `normal` ceiling | Hourly cap |
|---|---|---|---|---|---|
| Instagram | like | 10 a day | 40 | 80 | 15 |
| Instagram | follow | 2 | 10 | 20 | 5 |
| Instagram | comment | 1 | 5 | 10 | 3 |
| Instagram | story view | 10 | 40 | 80 | 30 |
| Facebook (profile) | like | 5 | 25 | 50 | 10 |
| Facebook (profile) | comment | 1 | 3 | 6 | 2 |
| LinkedIn | react | 5 | 20 | 30 | 8 |
| LinkedIn | comment | 1 | 3 | 5 | 2 |
| LinkedIn | connect | 2 a day, 10 a week | 5 a day, 25 a week | 10 a day, 50 a week | 3 |
| Reddit | comment | 1 | 4 | 8 | 2 |
| Reddit | join | 1 | 2 | 3 | 1 |
| Reddit | post | 1 a week per subreddit | same | same | n/a |
| Medium | clap, follow | 3 | 10 | 20 | 5 |
| any | post (feed) | per calendar | 2 a day | 3 a day | 1 |

These are a quarter to a half of the folklore ceilings in section 3.3, on purpose. The
`normal` ceiling never exceeds about half of the lowest folklore figure for an aged account.
Agents cannot raise a ceiling; only the owner can, and only up to the `normal` column.

**Ramp**: the cap starts at the `start` value and grows by 15 percent a week (Dripify's 10 to
20 percent), only after a week with no signal, until it reaches the program's target or the
ceiling. Any soft signal freezes the ramp for 2 weeks; a hard signal halves the cap.

**Correlation across a company's accounts** (DeBot's lesson):

- Accounts of one company never start sessions within 20 minutes of each other on the same
  node, and never act on the same target post or person within 48 hours.
- No trigger fans out to several accounts at once (a launch push schedules each account's
  extra sessions independently).
- A nightly self-audit computes the lagged correlation of each pair's hourly action series
  over 14 days. A pair above a threshold (0.5, open question 7) gets its personas redrawn and
  a notice in the report.
- This matters more because of the device decision (AUD-02): if a company's lanes share one
  device and proxy, the platform already links them, and synchronized behavior is the next
  signal.

### 5.6 Comments: where the words come from

Comments need language, so they need a model. The model only writes text; it never sees a
page, a tool or a secret.

**Where it runs**, in order of preference:

1. **The node brain on the owner's subscription** (`agentos.brains`, `claude -p` or
   `codex exec`, own use on own node, as the brains README describes). Called as a pure text
   task with **no tools**: input is a small JSON record, output is up to 3 candidate comments
   as JSON. Cost: one short turn per comment, a few dozen a day across all accounts, well
   inside a subscription.
2. **A local model** (`ollama`, already supported) as the fallback when the subscription is
   busy or down, and for languages where it is good enough. Its drafts always go to `approve`.
3. **Templates**, only for thank-you replies on own posts ("Thanks, {first_name}!" style
   with variants), never for comments on other people's posts. Templates are the most
   detectable text there is.

**What the drafter gets**: the post's caption or text, alt text and visible image
description where the site provides one, the top 3 comments, the target's language, the
account's voice notes and topics from workspace memory, and the last 20 comments this account
wrote (so it does not repeat itself). All page content is labelled untrusted (the existing
taint model); a caption that says "ignore your instructions" can only change the draft's
words, which the checks and the owner see.

**Deterministic checks on every draft** (a draft that fails any is dropped, never "fixed"):

- language equals the post's language (or the account's allowed list);
- length within the platform cap and the program's `max_chars`;
- no links, no @mentions, no hashtags, no emoji-only, no phone numbers or emails;
- grounding: shares at least one specific content word or named thing with the post, and is
  not a stock phrase ("Great post", "Love this", "Thanks for sharing", per language lists);
- not too similar to the account's or the fleet's recent comments (n-gram overlap below a
  threshold), and never identical to another account's comment;
- no sales pitch, price, or product name unless the program allows it (Reddit: never);
- topic checks: the program's topic list and the sensitive-topic list (section 5.3);
- subreddit rules: skip communities whose rules ban AI or self-promotion.

**Approve or auto, per account and per kind**:

- `approve` (default everywhere): the draft becomes an approval card (the post preview, the
  draft, two alternatives, edit, skip). The card expires with the session window; an expired
  card is skipped, not posted late.
- `auto`: allowed per account only after 30 approved drafts with under 20 percent edited and
  no signal in 30 days, and only on Instagram and Facebook. LinkedIn and Reddit stay
  `approve`, because LinkedIn now targets comments posted "without human oversight" and
  Reddit communities ban AI text. `auto` drops back to `approve` on any complaint, removal or
  signal.

### 5.7 Posting calendar and queue

- A **content queue** per surface: posts with media, caption per platform, earliest and
  latest time, and approval state. Agents and the owner add to it (`engage_post_add`); the
  brain may draft captions, which go to approval like any post.
- The planner places queued posts into the account's active hours at a drawn time (the
  persona's evening or lunch bump), respecting pace (`min_gap_minutes`, `per_day`) and the
  calendar's spacing across a company's accounts (no two accounts of one company posting the
  same content within 2 hours, and never identical text across accounts).
- Posting uses the API backend where bound, with the existing exact-content approvals:
  posting is a commit, so it needs the owner's approval of the exact content, or a standing
  approval bound to the contract hash and the content digest.
- After a post, community management watches its comments more often for 24 hours (events
  core watch, already defaulted to at most every 20 minutes, 08:00 to 22:00).
- Cross-posting the blog to Medium uses Medium's import (canonical URL), one story at a time.

### 5.8 Feedback and safety

The **safety governor** sits between the planner and the session runner and has the last word.

**Signals it reads** (all structural or from responses, never page text, as sites.py
requires):

| Signal | Where it comes from | Reaction |
|---|---|---|
| captcha, checkpoint, blocked, logged out, wrong account | sites.py `classify_page`, lane check | existing lane hold until the owner hands back; program paused; one notification |
| rate limited (429, LinkedIn 999) | status rules | existing 6 hour backoff, extended by the ladder below |
| action refused | the effect rule saw the request, and the response body says it failed (Instagram GraphQL error or `feedback_required` shape, Reddit `RATELIMIT`, Facebook error 368), read by shape on the node | **operation hold** for that kind on that surface (new), ladder below |
| silent failure | effect rule saw the request, but readback after reload shows no effect (the like did not stick) | counts as action refused; this is how Instagram's soft blocks often look |
| unknown dialog | a modal appeared after an action that no benign recipe knows | stop the session, screenshot, hold the operation, ask the owner (the pilot may classify it later, section 6.3) |
| removal | own comment or post missing on the signed-out probe | strike on the surface; `auto` comments return to `approve` |
| reach drop | own post impressions (API insights) below 40 percent of the 30-day median for 3 posts | notice and ramp freeze; possible shadowban |

**Backoff ladder per operation kind and surface** (the field's practice, made strict):

1. Soft limit with a stated wait (Reddit "try again in X minutes"): wait 1.5 times X plus
   jitter, at most once a day; a second one in a day is step 2.
2. First action refused: stop that kind for 48 hours, halve its cap, freeze the ramp 2 weeks.
3. Second within 14 days: stop **all writes** on the account for 7 days, cap stays halved,
   warm-up restarts at days 8 to 14.
4. Third within 30 days, or any challenge: lane hold for the owner (existing `needs_human`),
   program paused until the owner resumes it.

Nothing is retried inside a hold. Pace and holds cannot be overridden by an approval.

**Shadowban and visibility probes** (weekly per surface, plus after any removal):

- Reddit: the account's profile and its last 3 comments signed out, through a cookieless
  browser context on the lane's own proxy (the canary's backlog item; a plain HTTP read does
  not work on Reddit). Profile missing signed out means a likely sitewide shadowban: lane hold
  and owner notice.
- Instagram: read Settings, Account Status (signed in, read only) for "not eligible for
  recommendation"; compare non-follower reach from the Insights API.
- LinkedIn: comment visibility from a cookieless view where public; impressions per post
  from the API.

**Owner notices** go through the existing notifier and Needs you: one message per event, in
plain words ("biz-a Instagram: likes paused for 2 days because Instagram refused one. Nothing
for you to do."), and the daily report.

**Freeze**: the existing kill switch stops all programs at once.

### 5.9 Levers: MCP, CLI and panel

Agents get tools that set and read, never tools that click. All are under the normal grant
model; writing a program is a `draft` effect, and only the owner's approval activates it.

| MCP tool | Effect | What it does |
|---|---|---|
| `engage_program_draft(surface, text or yaml)` | draft | validate, return YAML, plain summary, the caps it will actually get, and warnings |
| `engage_program_activate(id)` | needs owner approval | shows the program card on the owner's phone |
| `engage_program_set(id, changes)` | draft | change targets, topics, daily targets within caps; a change is a new version that needs approval if it raises anything |
| `engage_program_pause(id)` / `resume` | write (pause), owner approval (resume after a hold) | |
| `engage_status(surface or workspace)` | read | today's plan, done counts per kind, caps, ramp stage, holds, next session window (as a window, not an exact time) |
| `engage_report(surface, days)` | read | actions, approvals, signals, follower and reach changes |
| `engage_targets_add(surface, source, values)` / `block(...)` | draft | add hashtags, partner accounts, communities; block people or topics |
| `engage_post_add(surface, media, caption, window)` | draft | add to the content queue; posting itself needs the usual approval |
| `engage_comment_queue(surface)` | read | pending comment drafts (for an agent to summarize, not approve) |

CLI mirrors the same: `agentos engage program draft|activate|set|pause|resume|show`,
`agentos engage status [--all]`, `agentos engage report`, `agentos engage plan <surface>
--dry-run` (prints the drawn day without acting).

Panel, in the everyday names the design council chose: each account under **Accounts** gets
a **Routine** card that reads as sentences ("Likes about 15 coffee posts a day, mostly in the
evening. Comments 3 times a day after you approve them. Warming up: day 6 of 10.") with a
small chart of the last 14 days and a big pause button. Comment approvals appear in **Needs
you** as real post previews with the draft underneath. Holds appear in plain words with what
happens next. **History** lists every action with a link to the target.

### 5.10 What agents cannot do

- Click, type or browse on an engagement surface while a program runs on it (the session
  runner holds the lane claim; an agent's `browser_act` waits or is refused).
- Raise a cap, change `risk`, turn on `followers_of`, turn on `auto` comments, or add a
  cross-account exception. These are owner-only.
- Approve their own comment drafts or programs.

---

## 6. Architecture inside agentos

### 6.1 What exists and is reused

| Part | Used for |
|---|---|
| Connectors (`runner/connectors`: models, compile, lint, pace, health, surfaces, taint) | the action library; pace per surface and operation; surfaces and bindings decide the lane and backend |
| Runner (flows, executor, proof, store, schedules with jitter) | each action is a run of `connector.<site>.<op>` with its journal and proof; the planner's session starts are one-off schedules |
| Flow driver and human layer (`flow_driver.py`, `human.py`, `typist.py`) | human-like input |
| Egress guard and effect rules (`egress.py`) | proof the request happened, kinds per action, observe mode for capturing new shapes, refusing a commit the grant does not allow |
| Signals, holds, lane check (`sites.py`, `holds.py`, `stealth.lanecheck`) | lane-level holds; lane check before every session (proxy, geo, WebRTC, fingerprint, signed in, right account) |
| Policy (grants, approvals, standing approvals, quiet hours, notifier, freeze, audit log) | program approval, comment cards, the kill switch, the signed record |
| Events core (watches, routes, rate limits, circuit breaker) | new comments on own posts, new followers, signals as events |
| Brains (`brains/`) | comment and caption drafts, program translation |
| Workspace memory | personas, voice notes, subreddit rules, interaction history summaries |
| Panel | Routine card, Needs you, History |

### 6.2 What is new: `agentos.engage` (node package)

```
programs (pydantic, hashed, versioned)  --owner approval-->  active program
        |
   planner (nightly, per account, seeded)  ->  day plan: sessions, budgets, posts
        |
   governor (caps, ramp, ladder, operation holds, correlation audit)  <- signals, readbacks
        |
   session runner (one lane claim per session)
        |  lane check -> browse reads -> pick targets from pool -> connector ops (runs) -> readback
        |                                       ^
   target pool (per surface, 3-day expiry)  ----+
        |
   drafter (brain, no tools)  ->  checks  ->  approval card or auto
        |
   reporter  ->  events, daily report, panel API
```

- **Store**: one SQLite file on the node (`engage.db`), tables for programs and versions,
  personas, day plans, sessions, actions (kind, target id, run id, outcome, readback), target
  pool, drafts, operation holds, counters. Interaction history is kept 90 days to enforce
  "not the same person twice in 30 days".
- **Session runner**: claims the lane like a flow does, runs the lane check, then a loop of
  read operations and, by the persona's rhythm, write operations, each as a normal connector
  run with `parent=session_id`. Pace counting stays in connectors (`pace.py`); the governor's
  caps are a second, stricter layer. The session ends at its drawn length, at the first
  signal, or when the owner takes the lane (the existing human hand-over wins).
- **Operation holds** are new: today holds are per lane. A refused like should stop likes, not
  the owner's own posting. Add `OpHold(surface, kind, until, step)` in the engage store,
  checked by the governor, shown by `lane_check` and `engage_status`. Lane holds stay as they
  are and still stop everything.
- **Response reading**: the egress guard today sees requests. To read "action refused", add
  response shape reading for verified engagement rules only (status, and a small set of
  structural keys such as `errors[].code`, `feedback_required`, `RATELIMIT`), on the node,
  never logging values. This extends observe mode rather than adding a route.
- **API backends**: Instagram Graph (professional accounts, own app with Standard Access),
  Facebook Pages, LinkedIn Posts, Reactions and Comments, WordPress or Ghost. Tokens in the
  vault, bound to the surface, refreshed by a small job (Postiz's `refreshToken` lesson).

### 6.2.1 The program executor as built (brief B1)

The session runner landed as `agentos.engage.executor.Executor`. It talks to the node through
four ports (`agentos.engage.ports`), each with a production adapter (`engage/adapters.py`) and a
fake (`engage/fakes.py`): `OperationPort` (connector runs through the runner; approve-mode text
parks on an approval card, and nothing is submitted with `authorized=True`), `TargetPort`
(`ConnectorTargets`, section 6.2.5), `ContentPort` (drafts from B3 and owner items from B4 plug in
here: `take` reserves, `settle` uses or hands back) and `LanePort` (a lane claim per window, renewed
during long pauses; the window's own connector runs pass it through `Run.parent = "claim:<holder>"`,
where the holder key carries a random nonce per window and the flow driver honors it only while
that holder claims the run's lane). Operation idempotency keys carry an HMAC tag made with a
per-node secret, and the adapter adopts an existing run under a key only when the engine submitted
it for that window with the same flow and params (anything else is `foreign_run`). A run the
runner re-queues after a signal (`rate_limited`, `signal_*`) is that signal: the adapter cancels it
and the executor holds and reschedules. The day's and hour's caps are the saved plan's, never above
the owner's current cap. A window cut short by a stop closes as `aborted`; one a crash left
`running` is recovered at the next start (`interrupted`). The operation
registry (`engage/operations.py`) links (site, kind) to a connector flow and an egress rule id; an
operation is valid only when its flow is installed, its rule is a verified `EffectRule` and the
operation has a canary mark (section 6.2.5).
engage.db schema 2 adds `sessions`, `ops`, `holds` (engine holds per surface or per kind) and
`history` (a general per-surface log later briefs also write). Windows fire from the engage
service's loop only while the node switch `engage_sessions` is on (off by default).

### 6.2.2 The items inbox as built (brief B4)

`agentos.engage.items`: engage.db schema 3 adds `items` (queued, reserved, used, expired,
withdrawn). Every item is submitted by the owner and runs with `Content.approved`, so an agent's
`engage_item_submit` authorizes `commit` with no lane and the exact item as content: that always
routes to a one-off approval (no standing approval covers a call with no lane), and only the
approved retry queues it, with the requesting principal kept in `requested_by` and the history.
The production `ContentPort` is `ChainedContent([ItemsContent(store)])`: `take` reserves the
oldest unexpired items in one IMMEDIATE transaction and names the surface's running session;
the service tick and nightly sweep reservations whose session ended back to the queue and expire
queued items past `expires_at`. The drafter (B3) plugs in as a further port of the chain, after
items; settle goes back to the port that produced the content.

### 6.2.3 The draft provider as built (brief B3)

`agentos.engage.drafts`: node.toml names a provider (`engage_draft_provider`: off by default,
passthrough for offline tests, or http with `engage_draft_endpoint`, `engage_draft_secret_ref` and
`engage_draft_timeout_s`). The http provider POSTs `{kind, topics, languages, max_length, n,
target, voice}` and expects `{candidates: [str]}`; the bearer token is a vault machine secret read
per request (`get_secret(ref, purpose="engage draft provider")`, the ref defaulting to
`engage/draft-provider/token`); https is required except to loopback; the client ignores proxy
settings from the environment; the whole call has one deadline (`asyncio.wait_for`) and the body
is streamed with a 256 KB cap; the answer is validated strictly (at most `n` strings; each NFC
normalized and trimmed, at most `max_length` code points, no control characters and no format
(Cf) characters except an emoji's zero width joiner; nothing is repaired) and any failure is
`ProviderUnavailable`. This is the "where the words come from" of section 5.6 as a pluggable
endpoint: the node brain or a local model can sit behind it; the deterministic checks of 5.6
beyond length and characters are still to come. engage.db schema 4 adds `drafts` (pending,
alternative, used, approved, denied, returned, expired, failed; one provider answer is a `batch`;
`offers` counts the windows that took a draft). A draft settles like an item: it goes back only
when its run provably did not act (`OpResult.acted is False`) and is offered again once, within
24 hours of its return; a draft whose run may have acted, or whose window stopped mid-run (the op
still `attempted`), is `failed`, and no draft whose newest op has a run is ever re-offered. A
used draft is `approved` only once its op is verified.
`DraftsContent` is the last port of the production chain, after items, wired by the service when
a provider is configured. It is the one port that calls out, so the ContentPort gained an optional
`async atake`; plain `take` only re-offers returned drafts. Its content is never `approved`, so
approve mode always parks it on an approval card through `RunnerOperations`; auto mode (Instagram
and Facebook comments and replies) sends an http draft without a card. Passthrough text carries
`Content.review`, which always asks, and the passthrough provider is not wired (nor can an auto
program be approved) while an approved program uses auto. When the provider is unavailable, the
executor skips that window's text operations (`draft_unavailable`), records `drafts_unavailable`
in the surface history and one notice per 12 hours, and writes a hold row of kind
`draft_provider` until the surface's next window starts (else an hour). Only `_take` reads that
kind (text from the provider is not asked for while it is in force); no operation has that kind
and the surface checks only kind-less holds, so the non-text operations and items still run. A
surface hold was not used because it would stop every operation, and an op-kind hold would also
stop items.

### 6.2.4 The daily digest as built (brief B5)

`agentos.engage.digest`: `day_record(store, surface, day)` is a pure read of engage.db into a
pydantic `DayRecord` (`schema` 1; fields only ever added): the day's windows from the saved plan
and the session rows, operations per kind from the op rows (`attempted` is the executor's SPENT
set, with the failed and held runs' reasons), cap use (`used` is `store.count_ops(..., day=)`
exactly as the executor counts; `cap` and `hourly` come from `caps.day_limits`, the one function
`Executor._limits` also calls: the plan's caps never above the owner's current cap or the hard
ceiling, the hour's never above the day's), drafts awaiting approval (ops of the day in
`awaiting_approval` joined to their draft) and the drafts that failed or expired that day, items
(with failed ones and those awaiting a card now), holds overlapping the day (each with its scope:
the surface, a kind, or `drafts` for the `draft_provider` pause, which the week strip and the
panel's Held chip do not count), signals from the history's op entries (an op row can lose its
reason to a reschedule) and the day's notices. The day is local to the surface's time zone as the
service resolves it for the executor. `render_text` renders the same record. Surfaces: the
`engage_digest` READ tool (grant filtered like `engage_status`, never draft text), `agentos engage
digest`, and `GET /api/engage/digest[/{ws}/{name}]` (member-visible like a lane's keepalive; only
the owner gets an 80-character preview of each waiting draft). engage.db version 5 adds the
indexes it reads by: `notices(surface, at)` and `items(surface, settled_at)`.

### 6.2.5 Targets, canary marks and profile edits as built

Each connector operation has a mark in the node's own ledger (`agentos.runner.connectors.marks`),
set by the owner after a canary (`agentos connector verify`) and naming the implementation the
canary ran. The registry reports `op_unverified` for an operation without a mark, and with the
reason `verified_stale` when the installed operation is no longer the one the canary ran, so
operations written from notes hold instead of running (the `no_valid_rule` hold's detail names
each kind's problem). A `verified` text inside a connector is information only. The connectors
for Reddit (comment, join, browse_subreddit, post_text) and LinkedIn (post, comment, react,
follow, feed, and the five profile edits) exist (`docs/design/connectors.md` 4.13). The
production TargetPort is `engage/targets.py` `ConnectorTargets`: it reads candidates with
`reddit.browse_subreddit` (the `communities` source) and `linkedin.feed` (`own_feed`) inside the
window's claim (a read's idempotency key carries the window's claim parent, so a read is never
another window's run), keeps a small pool per surface and kind for 3 days, and never returns the
owner's own accounts or a target the surface already acted on for that kind. A target an
operation's params builder refuses is skipped as `bad_target`, and its content is handed back.

Profile edits (brief B2, `agentos.engage.profile`): a program's optional `profile:` names the
public profile the account should show. The planner plans `profile_edit` operations under their
own cap (1 a day at either risk to start, at most 3, 1 an hour; none in warm-up), the executor
resolves each to the next due field's operation, asks the owner every time (its params are
readOnly on the card), and holds the `profile_edit` kind alone when a due field's operation may
not run. A field is known only from a verified edit or an `already_current` run (the preflight
found exactly one element already showing the value). The owner's profile_edit cap applies at
once through `caps.day_limits`, like every kind, and `Executor.reconcile` settles profile rows
with the rest: `awaiting_approval` rows by their run, recent `unverified` rows when a later check
proves them, and recent `attempted` rows (a window that stopped mid-run) by the run their
HMAC-tagged key started (`OperationPort.lookup`). `agentos engage profile reset` forgets a
field's state.

### 6.3 Where the pilot (browser and computer-use agents) helps

From `docs/design/browser-and-computer-agents.md`, the pilot is a node-side model that
proposes and never commits on its own. In the engine it helps in three places, all off the
action path:

1. **Repair**: when a connector's locator breaks (the preflight fails), the pilot proposes a
   locator patch on a throwaway lane; it ships through the normal reviewed connector update.
2. **Triage of unknown dialogs**: the session has stopped and holds the operation; the pilot
   looks at the screenshot and suggests "benign interstitial, add a dismiss recipe" or "this
   is a block", for the owner or a maintainer to confirm.
3. **Target judgment for edge cases**: whether a post is on topic when the keyword filters
   are unsure, as a read-only classifier with no tools, the same way the drafter works.

The pilot never likes, follows or comments. If it ever does, the engine is back to "an agent
uses the browser", which is what the owner said no to.

---

## 7. Phased plan

| Phase | What | Needs the owner | Size |
|---|---|---|---|
| E1 | program format, planner, timing model, governor caps, store, dry-run reports; no clicks | nothing | about 1 builder-week |
| E2 | engagement connector operations on throwaway accounts: Instagram like, follow, comment, view story, browse; Reddit join, comment, vote (one-for-one), browse; "already done" reads; readbacks | throwaway accounts (exist) | about 1.5 builder-weeks, in parallel with E1 |
| E3 | session runner, operation holds, response shape reading, backoff ladder; runs on throwaway accounts only for a week | nothing | about 1 builder-week |
| E4 | drafter (brain, no tools), checks, comment approval cards, Routine card and Needs you in the panel | Claude login on the node (also needed for agent lanes) | about 1 builder-week |
| E5 | API backends: Instagram Graph publishing and own-comment replies, Facebook Pages, LinkedIn Posts, Reactions and Comments, WordPress or Ghost | a Meta app and a LinkedIn app (steps written by us), professional mode on IG accounts | about 1.5 builder-weeks |
| E6 | first real accounts: one company, `careful`, community management first, then steady engagement; correlation audit on; weekly review with the owner | pick the company | ongoing, 4 weeks of watching |
| E7 | Facebook profile and LinkedIn browser operations, Medium import, cookieless visibility probes, the pilot's triage | the canary for those sites | about 1.5 builder-weeks |

E1 and E2 start now in parallel. Nothing touches a real account before E6.

### 7.1 Builder briefs

**Brief E1: programs, planner and timing (dry run).**

> Build `src/agentos/engage/` on branch `engage-e1` from `m1-node-lanes-mcp`. Read
> docs/design/engagement-engine.md sections 5.2, 5.4, 5.5 and 6.2 first. Deliver:
> (1) pydantic models for `Program` (surface, risk, phases warm_up and steady, daily targets
> per kind, targets, filters, comments, community, posting, report), canonical hashing and
> versioning like `FlowDefinition`; (2) `Persona` drawn from a per-account seed (wake and sleep
> window, weekend shift, session count, length scale, liker factor) with slow weekly drift;
> (3) a planner that draws a day plan (quiet day, log-normal daily volume clipped to the cap,
> 2 to 4 session starts from a non-homogeneous Poisson process on the persona's curve, min gap
> 45 minutes, uneven split) in the lane's time zone; (4) a governor with the caps table for
> `careful` and `normal`, the 15 percent weekly ramp, warm-up phases, and the cross-account
> rules (20-minute start separation per company, nightly lagged correlation audit over 14
> days); (5) an SQLite store (`engage.db`) for programs, personas, plans and counters; (6) CLI
> `agentos engage program draft|show`, `agentos engage plan <surface> --dry-run [--days N]`,
> and MCP `engage_program_draft`, `engage_status` (read only). No connector calls and no
> lane claims in this brief. Tests: seeded determinism, caps never exceeded over 365 simulated
> days, no two accounts of one company within 20 minutes, quiet day frequency, correlation
> audit flags a synthetic synchronized pair. Run only the tests you touch. New errors need
> entries in src/agentos/core/error_catalog.toml and `make errors-doc`; new NodeConfig fields
> need a description in core/config_report.py; run `make -s merge-check` before handing back.
> No em dashes in any user-facing string.

**Brief E2: engagement connector operations (throwaway accounts only).**

> On branch `engage-e2`, add connector operations for Instagram (`like`, `unlike`, `follow`,
> `unfollow`, `comment`, `view_story`, `browse_feed`, `open_post`, `open_profile`,
> `open_hashtag`) and Reddit (`join`, `leave`, `comment`, `vote`, `browse_subreddit`,
> `open_post`) on `test/instagram` and `test/reddit`, driven only through MCP like the canary
> (scripts/canary_call.py). Use the verified effect rules in src/agentos/node/egress.py
> (`instagram.like`, `instagram.follow`, `instagram.comment`, `reddit.like.*`, `reddit.join`)
> and capture the new-client Reddit comment request in observe mode before writing its rule.
> Each write first reads current state and returns `already` without a request when done.
> Readback after a reload proves the effect. Structural locators only, language maps where a
> name is unavoidable. Record request shapes and outcomes with each canary run. Never act
> on a real account; never act on another throwaway account's content from these lanes.

Briefs E3 to E7 are written when E1 and E2 land, from what they learn.

---

## 8. Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Terms of service: browser engagement breaks Instagram, Facebook, LinkedIn and Medium terms | certain, by the letter | API for everything an API allows; engagement only on the owner's own accounts at one person's pace; `risk: off` per account keeps it to posting and replies; the owner chooses per account |
| Account restriction or ban | moderate for engagement, low for posting via API | caps at a quarter to half of folklore; ramp; operation holds and the ladder; stop at first challenge; most valuable accounts on `careful` with approve-only comments; start with one company |
| Linked accounts punished together (coordinated behavior) | moderate if accounts engage each other or move in sync | no fleet-internal engagement by default; independent personas; correlation audit; no identical content across accounts |
| Proxy reputation: an ISP IP associated with blocks hurts every account on it | moderate if several accounts share a proxy (AUD-02 proposal: one per company) | one account's hold slows the others on the same proxy (governor shares a per-proxy strike count and halves their ramp); never more than one session at a time per proxy; no scraping traffic ever on these IPs |
| AI comments read as spam or "AI slop" | high without checks | grounding and stock-phrase checks, approve mode by default, LinkedIn and Reddit approve-only, low comment share |
| Reddit vote manipulation | high if software picks votes | no program-chosen votes; one-for-one only |
| Prompt injection through captions | moderate | drafter has no tools; checks are deterministic; approval shows the post and the draft |
| Platform changes break operations silently | high over time | verified effect rules plus readback make silent failure impossible; canaries and connector health; pilot repair proposals |
| Engine bug acts too much | low | caps in two layers (connector pace and governor), hard ceilings in code, freeze, dry-run plans visible before the first live day |
| Reputation of the owner's brands (a bad comment under a company name) | moderate | approve mode, sensitive topic lists, no sales pitch in comments, undo operations |

---

## 9. What this design does not do

- No account creation, no buying or warming accounts for others, no follower lists exported,
  no scraping services, no follow-unfollow churn (unfollow exists only to undo or to clean up
  on the owner's request, capped like follows).
- No engagement pods, no exchanges, no owner accounts boosting each other.
- No CAPTCHA solving; a visible challenge is always a hold for the owner.
- No direct messages in v1 (cold messages are the fastest route to LinkedIn and Instagram
  restrictions); replies to messages the account received may come later in approve mode.

---

## 10. Open questions, with the default builders use

1. **Risk per account.** Default: every account `careful`; personal accounts `off` (posting
   and replies only) until the owner turns them on.
2. **Which company goes first.** Default: the company with the smallest audience, so a block
   costs least; the owner may pick another.
3. **Instagram professional mode.** Default: switch business accounts to professional for API
   posting and replies; personal accounts stay personal and post through the browser.
4. **Disclosure.** Default: `auto` comments stay off on every platform until the owner decides
   how to disclose (Reddit's [App] label applies to approved API apps; for browser-driven
   accounts the comments are the owner's own approved words).
5. **Comment brain.** Default: the node's Claude subscription brain with no tools, local
   model fallback with approve-only.
6. **Cross-account exceptions.** Default: none. The owner may add named pairs (personal account
   likes the company's launch post), at most once per post and never within the first 10
   minutes of posting.
7. **Correlation threshold.** Default 0.5 lagged correlation over 14 days of hourly counts;
   revisit after E6 data.
8. **Device per company (AUD-02).** Default: one device and proxy per company; the governor treats
   accounts sharing a proxy as one risk group.
9. **Reddit API.** Default: browser for the owner's Reddit accounts; apply for Responsible
   Builder approval only if the owner wants API posting, since approval for new apps is
   uncertain and brings the [App] label.
10. **Follower targeting.** Default: `followers_of` off for all programs.
