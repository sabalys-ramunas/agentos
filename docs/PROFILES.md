# Profiles

A profile is one person or one business: its name, headline, bios, phone, email, links, photos,
work history and the voice it writes in. You write it once. Every account attached to it (a
LinkedIn, an Instagram, an X lane) is brought in line from it, and every agent that writes on
those accounts writes in its voice.

A profile can also be a **product** (a thing being built, with its brand and rules), a
**reviewer** (a judging job an agent does) or an **audience** (a kind of user). Those are for
agents that build and design: see "Products, reviewers and audiences" below. An agent learns who
the work is for in one call, `profile_use`.

```yaml
profile: acme                      # a short name, unique in the workspace
workspace: acme
kind: business                     # person or business
display_name: Acme Coffee
headline: Coffee gear, made in Vilnius
bio:
  short: Loud grinders. Honest coffee.          # X, Instagram, Reddit, TikTok bios
  long: |                                       # LinkedIn About, Facebook and YouTube descriptions
    We make coffee grinders in Vilnius.
phone: "+370 600 12345"
website: https://acme.example
links: {shop: https://acme.example/shop}
photos: {avatar: sha256:..., cover: sha256:...}   # agentos profile photo acme/acme avatar me.jpg
voice:
  personality: Dry, confident, roasts itself before anyone else can.
  tone: short and a little cheeky
  do: [answer the actual question first]
  dont: [discounts in replies, politics]
  emoji: sparing                   # none, sparing or often
  examples:
    - {context: "Your grinder is loud", reply: "It is. That is 40 mm burrs doing their job."}
accounts:
  - surface: acme/linkedin
  - surface: acme/instagram
    overrides: {bio: "Loud grinders. Honest coffee. DMs open."}   # only this account
  - surface: acme/x
```

## What happens when you change it

1. You save (panel, or `agentos profile set acme/acme bio.short "..."`). Your save is live at once.
   An agent's save (`profile_update`) is a draft that waits for you: read it with
   `agentos profile show acme/acme --version N`, then approve it in the panel or with
   `agentos profile approve acme/acme --version N`. Your own edits build on the approved version,
   so a waiting draft never goes live with them.
2. The save says what changes where: "bio changes on 2 accounts".
3. Each account then gets its fields, with no approval card (your save and the login you saved
   are your consent; "Ask me first" on a lane still makes that lane wait):
   - **Learned fields change by themselves.** Once an agent has taught agentos how a site's field
     is changed (see "Learning a site"), every account on that site gets it whenever you save.
   - **Not learned yet:** `agentos profile status acme/acme` (or `profile_sync_status`) lists them.
     An agent can teach one, or change it by hand and mark it done (`profile_mark_synced`).

## Learning a site

An agent teaches agentos a field once: `profile_learn_start(surface, field)`, then in the account's
lane it logs in from the vault, opens the site's edit page, sets exactly the value, saves, opens the
account's profile page and calls `profile_learn_finish` with the element that shows the new value.
The node keeps what it saw itself (the recorded field and save click, the one save request it let
through, the value read back), builds the site's operation from fixed steps (open the edit page,
fill the field, save, read back), and replays the read with no model. The save is proved by its
first real run on the account it was learned on (the next time the field changes); only then does
every other account on the site get it. If the site changes, the next sync stops before saving and
the field reads "Site changed", to be taught again.

What keeps a lesson to exactly one profile edit, with no approval card:

- Only a reviewed save request can be learned (`PROFILE_EDIT_ENDPOINTS` in
  `src/agentos/node/sites.py`): today Instagram's bio and website. Other sites and fields read
  "learnable: false" until their save request is reviewed and added there.
- The one request let through must be that endpoint, carry exactly the value under the field's own
  key, and leave every other form field as the site said it was; the agent may type only that one
  value, and only the agent that started the lesson may act in the lane meanwhile.
- A lesson is bound to the agent and grant that started it, never stops someone else's recording,
  and is capped (3 a day per lane, 3 a week per field). A field agentos already knows is taught
  again only after a sync saw the site change, or by you.
- On a site that names accounts in its URLs (Instagram), the account comes from the handle you set
  on the lane, never from a URL the agent typed.
- A lane with "Ask me first" on is never taught: the lesson's save is refused there (it would need
  a card). The save counts toward the lane's posting pace and limits like any publish.
- A learned operation is never an agent tool: only the sync runs it, and a save not proved yet runs
  only on the account it was learned on. Each of its saves must carry that run's own value under the
  field's key and leave every other form field as the page loaded it, as during the lesson.

Names are never learned (sites lock them for weeks), photos come later, and LinkedIn keeps its own
operations. Design: docs/design/learn-a-site.md.

A value too long for a site is refused when you save, with the account and the limit named (an
Instagram bio takes 150 characters, an X bio 160). Shorten it or give that account an override.
Nothing is ever cut short.

## Accounts and their logins

An account is a lane (`acme/instagram`). In the panel, Profiles, Add account: pick a lane of the
workspace or make a new one, choose the site, and enter its login (username, password and, if the
account has two-factor sign-in, its authenticator key). The login is saved in the vault on the
node, bound to that lane, and agents sign in with it later (`login(on="acme/instagram")`). Agents
never see the password. An account with no login yet shows "No login" and a Save login button.

Nothing in a profile is hidden: agents read every field (phone and email included), and every
field a site has is filled in on it.

## The voice

`voice` is turned into plain instructions (`voice_prompt` in `profile_get`). Agents writing a post,
comment or reply on an attached account are told to follow it.

## Products, reviewers and audiences

```yaml
profile: shop
workspace: acme
kind: product                      # product, reviewer or audience
display_name: Acme Shop
headline: Booking for small salons
brand:
  tokens:                          # Design Tokens (DTCG 2025.10), at most 64 KB of JSON
    color:
      $type: color
      brand: {"500": {$value: "#3366cc"}}            # stored as {colorSpace: srgb, components, hex}
      text: {primary: {$value: "{color.brand.500}"}}  # an alias must name a token in the set
    space: {$type: dimension, "2": {$value: 8px}}
  fonts: [{family: Inter, role: body, weights: [400, 600], file: sha256:...}]
  logo: sha256:...                 # an SVG or image, kept like a photo (profile_photo_put)
  assets: {mark: sha256:...}
  components:                      # how a known component is recognised in a page
    - {name: Button, attr: data-slot=button}
    - {name: Card, class_pattern: "^card(-|$)"}
  theme: {how: attr, name: data-theme, light: light, dark: dark}   # how it turns dark (or media)
  widths: [360, 768, 1280]
  engine: chromium                 # chromium, firefox or webkit
  ignore: [".shop-preview"]        # regions studio leaves out: another product drawn in the page
guidelines:                        # at most 100
  - {id: text-contrast, text: Body text is easy to read, check: contrast.text, threshold: 4.5}
  - {id: calm, text: One accent colour per screen, advisory: true}
references:                        # products to compare against, at most 30
  - {name: Linear, url: https://linear.app, note: the density we want}
environments:                      # where it runs, and the vault entries its keys are in
  - {name: local, url: http://localhost:3000}
  - {name: staging, url: https://staging.acme.example, keys: {stripe: acme/stripe-test}}
```

- A **guideline** is either checked (`check` is a studio check id, `threshold` optional) or
  `advisory: true`, never both. `ui_check` measures the checked ones.
- **Environments** name vault entries, never values. Anything in these sections that looks like
  a credential (an API key, a token, a password, a login in a URL) is refused with
  `secret_in_profile`, and the message names the field, never the value.
- A **reviewer** profile has `reviewer: {cares_about, rubric: [{id, question, weight 1 to 5}],
  examples: [{situation, verdict, why}]}`. It is words only: nothing in it names a tool or grants
  a permission, and anything else is refused.
- An **audience** profile has `audience: {screen_width, language (like lt or en-GB), network
  (offline_often, slow_3g, fast_3g, 4g, broadband), goals, hard}`.
- `brand` belongs to products and businesses, `environments` to products, `guidelines` and
  `references` to people, businesses and products. Products, reviewers and audiences have no
  accounts (they are never shown on a site) and none of a person's own fields (name, birth
  date, work history, education).
- Every text is cleaned and capped like the rest of a profile, so text an agent brought in from a
  page reaches you as plain, visible words.
- Logos, brand images and fonts are uploaded like photos (`profile_photo_put` or `agentos profile
  photo`): SVG, PNG, JPEG, WebP, GIF, and WOFF2, WOFF, TTF or OTF. An SVG that runs code (a script,
  an event handler) is refused. A photo field takes only a photo, a font field only a font.

### Working as a profile: profile_use

`profile_use(profile, task)` gives an agent the approved version, sliced for one task:

| task | gets |
|---|---|
| design | the brand (tokens as CSS values, components, fonts, theme, widths), guidelines (`checks` and `advice`), references |
| build | environments and the names of their vault entries |
| write | the voice (`voice_prompt`), the accounts, the headline and bios |
| review | the reviewer's rubric, or the product's guidelines and references |

An audience profile's section goes to every task. The result says what it left out and how big
each part is (`left_out`, `size`); a large token set is left out of the summary and comes with
`detail="full"`. Private fields (birth date, phone, email, location, a person's name) are never
in it. The workspace's own rules are not copied: `workspace_rules` points at `memory_context`.
Only the approved version is served, never a waiting draft.

### Proposing a change: profile_propose

`profile_propose(profile, change, reason)` is how an agent changes a brand, guidelines or
references. The brand merges like `profile_update`; guidelines and references take a whole list
or `{add: [...], remove: [ids or names]}`. It is always a draft, even from you, and it carries
the agent's reason and a readable list of what changes. `agentos profile show acme/shop
--version N` prints both above the profile; approve it as any other draft. An agent cannot
approve it.

### Learning a brand from the site: profile_learn_brand

Onboarding a product is "point it at the URL, review the draft".
`profile_learn_brand(profile, url, pages)` opens the site (and up to 4 more pages of it) in the
review browser, in light and dark at 360 and 1280 px, and reads it the way `ui_check` does,
in reverse:

- custom properties the site declares become tokens with their own names, with how often its
  CSS uses each (`$extensions.agentos.css_rules`) and how many of the page's values sit on them;
- where it declares none, the values it uses are clustered: colours (by delta E OK) into a
  palette with roles guessed from use (text, background, border, accent), spacing into a scale
  with its step, radii, and families, sizes and weights into a type scale. Each such token says
  `heuristic` in its `$description` and `$extensions.agentos`;
- how it turns dark (prefers-color-scheme, a data-theme or data-mode attribute, a `dark` class,
  a stored theme), tried one by one and read back from the page;
- component hints: `data-component` and `data-slot` values, and repeated class names (labelled
  heuristic).

The result is small: per category where the tokens came from, how many, how much of the page
they cover and the one-off values that fit nothing; the palette; what is uncertain; and the
proposal's version. The draft is saved only through `profile_propose`, with a reason that says
what was read and from where, so it waits for you like any proposal. A site that holds to no
system (low coverage, many one-offs) gets no proposal, and the result says so. Only names and
values are kept: no page text.

### Text that reads like an instruction

Guidelines, a reviewer's rubric and examples, and reference notes are served to other agents, so
when an agent saves them they are screened the way workspace memory screens an agent's notes:
text that addresses agents, uses an override phrase ("ignore previous", "you are now"), carries
a link or an encoded blob is marked in the draft's reason ("Screened: ...") and in the result
(`screened`). Nothing is refused for it (a rule may well say "always"); you decide when you
approve. Your own saves and text already approved are not screened.

## Commands and tools

| CLI (`agentos profile ...`, also `agentos workspace profile ...`) | MCP tool |
|---|---|
| `list [workspace]` | `profile_list` |
| `show acme/acme [--version N]` | `profile_get` |
| none | `profile_use` (the profile sliced for design, build, write or review) |
| none | `profile_propose` (a draft of brand, guidelines or references, with a reason) |
| none | `profile_learn_brand` (a brand drafted from the live site, proposed as above) |
| `save profile.yaml` | `profile_save` |
| `set acme/acme <field> <value>`, `edit acme/acme changes.yaml` | `profile_update` |
| `photo acme/acme avatar me.jpg` | `profile_photo_put` (from a lane's inbox) |
| `attach acme/acme acme/x [--override bio=...]`, `detach` | `profile_attach`, `profile_detach` |
| `approve`, `reject` | none: only you approve |
| `status acme/acme` | `profile_sync_status`, `profile_mark_synced` |

The tools are in the `profiles` toolset (`tools_more(toolset="profiles")`), and `howto(topic=
"update_profile")` walks an agent through a change.

## Sites

Each site's profile fields and limits are in `src/agentos/profiles/mapping.py`: linkedin, facebook,
instagram, reddit, x, threads, tiktok, youtube, medium. An account's site comes from its name
(`acme/instagram`), or set `site:` on the account. A field becomes automatic once an agent has
taught agentos how it is changed on that site (see "Learning a site").
