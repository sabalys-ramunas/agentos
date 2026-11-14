# Workspaces, templates and the login queue

Scaling to 29 accounts by hand is slow, and the expensive mistake is a proxy mix-up: two
accounts that share an IP look linked to the platforms, and that hurts their reputation. This
package makes the set-up a template you apply to each company, and it refuses the mistakes
that link accounts by IP. It then keeps a queue of the accounts you still have to log into.

Code: `src/agentos/workspaces/`. Runtime service: `workspaces`. Storage: `$AGENTOS_HOME/workspaces.db`.

## The model

- **Workspace**: one business or person. It has a name, an optional colour, a default proxy
  label, notes and a creation time. A workspace can also exist only as the prefix of lane ids
  made before this package (`acme/instagram`). It then shows up with a derived name ("Acme")
  and `derived: true`, so nothing is hidden.
- **Lane**: one account in one browser profile, with its own proxy. Lanes stay in the node's
  lane store. This package adds what the node does not know about a lane: its platform, the
  template surface it came from, its login URL and its readiness.
- **Template**: a list of surfaces ("for each company: these six surfaces"). Applying it
  creates one lane per surface in each chosen workspace.
- **Plan**: what applying would do, computed with no side effects. Each step is `create`,
  `exists` (already there and matching) or `conflict` (with the reason).
- **Readiness**: `needs_login` (the default), `ready`, or `needs_attention` (with a note).
  It is set explicitly by the owner (CLI, API) or by an agent after the owner says so. Login
  state is never detected, and nothing in this package visits a site.

Every workspace id and lane name follows the lane id rules: a workspace id is 1 to 31
characters and a surface or lane name is 1 to 41, so any valid pair makes a valid lane id.

## Quick start

```
agentos workspace create acme --name "Acme" --proxy lt1
agentos workspace plan social-6 -w acme            # shows six lanes, creates nothing
agentos workspace apply social-6 -w acme           # asks, then creates them
agentos workspace queue                            # who still needs to log in, with login URLs
agentos workspace ready acme/instagram             # after the owner has logged in
```

Lanes from `apply` start as `needs_login`. Log in through the lane's live view, then mark it
ready. The queue shows "18 of 29 ready" overall and per workspace.

## Templates

Built in:

- `social-6`: instagram, facebook, reddit, medium, blog and linkedin, for one company.
- `personal`: instagram, facebook, reddit and linkedin, for your own accounts.

A user template is a TOML file in `$AGENTOS_HOME/templates/<id>.toml`. The file name is the
template id (1 to 41 characters, lowercase letters, digits and dashes). A user file may not
reuse a built-in name.

```toml
title = "Shop"
description = "Store, marketplace and support for one shop"

[[surfaces]]
name = "store"                                  # lane name: <workspace>/store
platform = "shopify"                            # proxy rules: one account per platform per IP
home_url = "https://shop.example.com/"          # optional
login_url = "https://shop.example.com/admin"    # optional, opened in the live view for login

[[surfaces]]
name = "support"
platform = "helpdesk"
engine = "camoufox"                             # optional, the default and the only engine today
```

Rules:

- `title` and `description` are optional. Unknown top-level keys are an error.
- Each surface needs `name` and `platform`. Unknown keys are an error.
- Surface names must be unique within a template. Two surfaces with the same name are refused.
- URLs must be `https://`, with no username or password in them, and at most 500 characters.
  A blog's address belongs to each company, so the built-in `blog` surface leaves its URLs
  unset. Set them in your own template if the blog address is fixed.
- At most 50 surfaces per template.

A bad file never stops listing. `agentos workspace templates` (and `GET /api/templates`)
shows the valid templates and, under "Not usable", each problem with its file name. Asking for
a broken template by name says why it cannot be used.

## Plan and apply

For each workspace and surface, the proxy label comes from the first of these that is set:

1. a per-lane label (`--lane-proxy acme/blog=lt2`; `none` means no proxy),
2. a per-workspace label (`--proxy acme=lt1`),
3. the workspace's default proxy,
4. no proxy.

A plan checks every step against the lanes the node has now, and against the lanes the plan
has accepted so far. Apply refuses while the plan has any conflict, and then creates nothing.
When it does run, it:

- re-checks each step against the live state just before it creates the lane,
- creates lanes through the node's own lane manager, so the engine and proxy checks still apply,
- records each lane's platform, surface, login URL and readiness,
- stops at the first failure and reports exactly what it created, what is still to do and why
  it stopped. Running the same apply again finishes the rest.

Apply is idempotent. A lane that already exists and matches is left alone (its login URL is
filled in if it was missing), and its readiness is never reset.

Agents never create lanes without the owner. `workspace_apply` asks for approval first. The
approval shows a summary written by the node (the lanes and their proxy labels) and binds to
the exact plan. Reasons for an override are part of that plan, so an approval cannot be reused
for a different one.

## Proxy rules and why

A proxy label is an account's IP identity. Two rules protect accounts from being linked by IP:

- **(a) One proxy, one business.** A proxy label used by lanes in two different workspaces
  links two businesses. A new lane is refused if its proxy is already used in another workspace.
- **(b) One account per platform per IP.** Two lanes on the same platform behind one proxy look
  like two accounts sharing an address. A new lane is refused if a lane on that proxy already
  uses its platform, in any workspace. A business can still have an Instagram and a Facebook on
  one proxy, because those are different platforms.

Other points:

- **Existing lanes are never moved.** If a lane exists on another proxy, or with another
  platform, the plan reports a conflict. No override changes it.
- **Lanes with no proxy are allowed, with a warning.** They exit on the node's own IP, which
  every other lane without a proxy shares.
- **Platform comes from the template.** A lane created by apply records its platform. A lane
  made some other way has no record, so its platform is its lane name. That is the right guess
  for `instagram` or `reddit`, and a wrong one for a lane called `main`. Give such lanes a
  workspace record through apply, or accept the name rule.
- **Unknown proxy labels are an error,** and only labels are ever read or shown. Credentials
  stay in the node's proxy file.
- **Overrides are explicit and recorded.** `--override-proxy-rules` (or `override_proxy_rules`
  in the API and tools) bypasses rules (a) and (b) for one call, and needs a `--reason`. Each
  overridden lane is recorded with its rules, the reason, who did it and when. List them with
  `agentos workspace overrides` or `GET /api/overrides`.

Changing a workspace's default proxy only affects future plans. It never moves an existing lane.

## Readiness and the login queue

- A lane without a record is `needs_login`. Applying a template also records it as
  `needs_login`.
- `ready` means the owner has logged the account in.
- `needs_attention` needs a note saying what is wrong, for example "phone code asked".
- Setting readiness on a lane the node does not have is `lane_not_found`.

The queue lists lanes that are not ready, with `needs_attention` first. Each queued lane
carries its login URL, so the panel can open it in the live view. Counts cover every lane in
scope, including ready ones.

Workspace health: `blocked` when a lane is quarantined, `attention` when
a lane has a note for the owner, `ok` when every lane is ready, and `idle` when there are no lanes
or the owner is still logging in.

## Moving a workspace: export and import

"Self-hosters trust products they can leave." One workspace goes into one encrypted file, and that
file comes back on another node, or on the same node under a new id, or stays on a laptop as a
copy you control.

```bash
# on the old node
agentos workspace export acme --to /root/acme.agentos          # asks for a passphrase twice
agentos workspace export acme --to /root/ -r age1...   # your key from age-keygen; a dated name
scp mynode:/root/acme.agentos .                                  # or keep it on the laptop

# on the new node
agentos workspace import acme.agentos --dry-run                 # verify every checksum, print the plan
agentos workspace import acme.agentos                           # plan, ask, import as one unit
agentos workspace import acme.agentos --as acme-eu --map-proxy lt1=de1 --reissue-grants
```

The file is age v1 (a passphrase, or one or more `age1...` recipients from `age-keygen`), so
`age -d acme.agentos | tar -tz` lists it without agentos. Inside (agentos.workspaces.transfer.export):

| Member | Holds |
|---|---|
| `agentos-export/format.json` | format `agentos-workspace-export` version 1, the workspace, the source node and host, when, which agentos |
| `workspace/workspace.json` | the workspace record (null for a workspace that only exists as a lane-id prefix) and, per lane, its spec (engine, proxy label, locale, account) and readiness row |
| `workspace/vault.json` | the vault entries bound inside the workspace (`acme/<lane>` or `acme/*`), values included; bindings to other workspaces are left off |
| `workspace/flows.json` | flows on the workspace's lanes, every saved version and which one is current; plus a cross-workspace flow (`*/reddit`) when one of these schedules runs it |
| `workspace/schedules.json` | their cron, watch and webhook schedules, without webhook tokens and secrets |
| `workspace/agenda.json` | format 3: the agenda entries that have not run yet (draft, proposed, scheduled), without approvals, runs or history (docs/AGENDA.md). Entries that ran, closed windows and entries that fire a trigger stay home |
| `workspace/grants.json` | root grants whose whole scope is this workspace, as definitions: subject, scope, ceiling, specials, budget, expiry. Never tokens |
| `workspace/proxies.json` | each proxy label the lanes use, with a SHA-256 of its host, port and user; host, port and credentials only with `--with-proxies` |
| `workspace/lanes/<name>/profile/...` | the browser profile: cookies, logins, local storage, IndexedDB. No caches, no lock files, no symlinks |
| `workspace/lanes/<name>/fingerprint.json` | the pinned fingerprint, so the lane stays the same device |
| `agentos-export/manifest.json` | last: every member with its size and SHA-256, the counts, each lane and how it was copied (`cold` or `paused`), notes, what was left out |

**Consistency.** Each lane is copied while its browser is down. A warm lane is paused through the
daemon (sent cold, copied straight into the encrypted stream, warmed again), like a backup. With no
daemon to pause it the export refuses, and a browser that starts during the copy fails it.

**No plaintext on disk.** The only file an export writes is the encrypted one (as
`.partial-<name>` until it is complete). Vault values exist in plaintext only in memory and inside
the encrypted stream. An import stages profiles under `AGENTOS_HOME/tmp` (700, removed afterwards)
and keeps `vault.json` in memory until it is sealed into this node's vault.

**Never exported:** grant tokens (re-issue with `--reissue-grants`; the new tokens are printed
once), webhook tokens and secrets (new ones are made and printed once), proxy hosts and credentials
unless `--with-proxies`, machine secrets (they belong to the node), runs, journals, proofs,
screenshots and recordings, override records, browser caches, each lane's `shots/` and `files/`,
and terminal and service lanes (only browser lanes move for now), approvals of agenda entries.

**Import rules** (agentos.workspaces.transfer.import_):

- The whole file is decrypted and every checksum compared with the manifest before anything
  else. A wrong passphrase, a damaged, cut or altered file is refused with nothing left behind.
- An existing lane id or workspace is refused: an import never merges and never replaces.
  `--as NEW` brings the workspace in under a new id, and lane ids, flow lanes, flow ids that start
  with the workspace id (`acme.instagram.post` becomes `acme-eu.instagram.post`), vault bindings,
  grant scopes and schedule lanes all follow.
- A lane is never created without its proxy, because it would then use this node's own IP. A
  label this node lacks must be added first (`agentos proxy add`), mapped (`--map-proxy OLD=NEW`),
  carried in the file (`--with-proxies` on export), or, with `--allow-missing-proxy`, created as a
  placeholder that points at `proxy-missing.invalid`, so the lane fails closed until you set the
  real one. A label that exists here but points at a different proxy is a warning: the account
  will come from a new IP.
- Vault entries are sealed into this node's vault in one transaction. No vault yet, or a sealed
  vault (a passphrase vault with no passphrase given, or one the daemon reports sealed), refuses
  the import. Same-named entries refuse it too, unless `--on-vault-conflict rename|skip`.
- Grants are re-issued from their definitions only with `--reissue-grants`, with the time they
  had left. Narrowed grants made from them are not exported; their holders narrow again.
- Schedules come in turned off, so the two copies never both fire; `--enable-schedules` keeps
  them as they were. A schedule whose grant is not re-issued here stays off either way.
- Agenda entries come in as drafts, whatever they were on the source: an approval binds to the
  source node's grants and is used up there, so approvals never travel. The plan and the import's
  output say so; propose each again (`agentos agenda propose <id>`) once the source copy is
  cancelled. An entry on a connector operation this node has not installed is left out, with a note.
- Profiles come in cold. Then, everything is added as one unit: proxies, lanes, the workspace
  record and readiness, vault entries, grants, flows, schedules, agenda entries. If one step fails, everything the
  import added is removed again.

**One device, two places.** The export records where it came from, and never deletes anything
there. The import warns, loudly, when the lanes still exist on another node: running both copies
at once looks to the sites like one device in two places and can trigger checkpoints. Delete or
archive the source lanes (`agentos lane delete` once it exists), or at least keep them cold. An
import under `--as` on the same node gets the same warning: it makes a second copy of the same
devices.

## Storage

`$AGENTOS_HOME/workspaces.db` (SQLite, WAL, `synchronous=FULL`, mode 600). It holds workspace
records, one row per lane this package has seen (platform, surface, template, login URL,
readiness and note, and who last changed readiness) and the override record. It holds no
credentials. The schema version is in a `schema_version` table. A file from a newer agentos is
refused, and a damaged file gives a clean `unavailable` error that says the lanes are fine. Lanes
live in the node's own store and are not affected by a damaged workspaces file.

## CLI

`agentos workspace` (mounted by the node CLI):

| Command | Does |
|---|---|
| `list [--json]` | workspaces with readiness and health |
| `create ID [--name] [--color] [--proxy LABEL] [--notes]` | make a workspace |
| `show ID [--json]` | one workspace with its lanes |
| `update ID [--name] [--color] [--no-color] [--proxy LABEL] [--no-proxy] [--notes]` | change a workspace |
| `delete ID [--yes]` | delete, refused while it has lanes |
| `templates [--json]` | templates, and any problems with user files |
| `plan TEMPLATE -w WS [--proxy WS=LABEL] [--lane-proxy LANE=LABEL] [--override-proxy-rules --reason TEXT] [--json]` | preview; exits 1 on conflicts |
| `apply TEMPLATE -w WS ... [--yes]` | show the plan, ask, create; `--yes` skips the question |
| `queue [WS] [--json]` | the login queue with login URLs |
| `ready LANE` | mark logged in |
| `unready LANE [--attention --note TEXT]` | mark needs_login, or needs_attention with a note |
| `overrides [--limit N]` | recorded proxy-rule overrides |
| `export WS --to FILE\|DIR [--passphrase-file F \| -r age1...] [--with-proxies] [--vault-passphrase-file F] [--json]` | one encrypted file (see "Moving a workspace") |
| `import FILE [--as NEW] [--dry-run] [--reissue-grants] [--allow-missing-proxy] [--map-proxy OLD=NEW] [--on-vault-conflict error\|rename\|skip] [--enable-schedules] [--passphrase-file F \| -i IDENTITY] [--vault-passphrase-file F] [--yes] [--json]` | verify, plan, import as one unit |

## API

Under `/api`, errors use the documented shape `{ "error": { "code", "message" } }`.

| Method and path | Does | Errors |
|---|---|---|
| `GET /api/workspaces` | list with lanes | |
| `POST /api/workspaces` `{id, name?, color?, default_proxy?, notes?}` | create (201) | 400, 409 |
| `GET /api/workspaces/{id}` | one workspace | 404 |
| `PATCH /api/workspaces/{id}` | change any of name, color, default_proxy, notes | 400, 404 |
| `DELETE /api/workspaces/{id}` | delete | 404, 409 |
| `GET /api/workspaces/{id}/queue` | login queue for one workspace | 404 |
| `GET /api/queue` | login queue for all workspaces | |
| `POST /api/lanes/{ws}/{name}/readiness` `{state, note?}` | set readiness | 400, 404 `lane_not_found` |
| `GET /api/templates` | `{templates, problems}` | |
| `POST /api/templates/plan` `{template, workspaces, proxy_per_workspace?, proxy_per_lane?, override_proxy_rules?, reason?}` | plan, read only | 400, 404 |
| `POST /api/templates/apply` (same body) | create; the owner's click is the approval | 400, 404, 409 when there are conflicts |
| `GET /api/overrides?limit=` | recorded overrides | |
| `POST /api/workspaces/{id}/export` `{passphrase}` or `{recipients: ["age1..."]}`, `with_proxies?` | owner only: streams the encrypted file (`application/octet-stream`, a dated file name, a JSON summary in `X-Agentos-Export`) | 400, 404, 409 (a warm lane that cannot be paused, a sealed vault) |
| `POST /api/workspaces/import?as=&dry_run=&reissue_grants=&allow_missing_proxy=&map_proxy=OLD=NEW&on_vault_conflict=&enable_schedules=` | owner only: the file is the body, the secret is in `X-Agentos-Passphrase` or `X-Agentos-Identity`. 200 with the plan (`applied: false`) for a dry run, 409 with the plan when blocked, 201 with the plan and the result (new tokens and webhook secrets, once) | 400 `bad_export`, 409, 500 `import_failed` (rolled back) |

Apply returns `{status: "complete" | "partial", created, existing, overridden, failed,
remaining, warnings}`. A partial run is a 200 with `status: "partial"`. A refused plan is a 409
with nothing created.

## Tools

| Tool | Effect | Does |
|---|---|---|
| `workspace_list` | read | workspaces and their lanes, filtered by the grant |
| `workspace_queue` | read | the login queue, filtered by the grant |
| `workspace_plan` | read | plan a template; checks the grant lane by lane |
| `workspace_apply` | commit | refuses conflicts, asks for approval, then creates |
| `lane_set_readiness` | draft | set ready, needs_login or needs_attention, when the owner says so |

## Limits and open questions

- **Blog URLs.** The `blog` surface has no fixed address. Its lane gets no login URL until a
  user template sets one. Per-company blog addresses are an open question.
- **Platforms of hand-made lanes** come from their names (see the proxy rules above).
- **Two processes.** Apply serialises within one process. Two separate CLI and daemon applies
  racing on different lanes can each pass the check before either creates. Re-checking each step
  against the live state makes that rare, but it is not impossible.
- **Moving lanes.** Terminal and service lanes are not exported yet, nor a lane's `files/`. The lane
  manager has no `delete` yet: the import's rollback removes a lane it made from the lane store and
  its directory directly, and the source lanes are left for the owner to delete by hand. The lane
  manager also cannot create a lane whose proxy label is missing, which is why
  `--allow-missing-proxy` adds a placeholder proxy rather than a lane with a dangling label.
- **Built-in URLs** were written from the public login pages. They have not been checked
  against the live sites, so check them before the first apply.
