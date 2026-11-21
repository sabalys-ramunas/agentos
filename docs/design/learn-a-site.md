# Design: learning a site's profile edit as a verified connector operation

Status: built on branch t3/profiles (Nov 2026), then hardened after an adversarial review: only
reviewed per-field save endpoints (`PROFILE_EDIT_ENDPOINTS`, Instagram bio and website today) can be
learned; the window checks the field's body key, the other form fields against the site's form
data, the agent's typed text and its grant; the commit operation is built from fixed steps only; a
learned save is proved on its own account before any other; lessons are capped and bound to the
agent that started them. Where this document and the code differ, the code and docs/PROFILES.md win.
Original status: Owner decisions: an agent learns how to change a
profile field on a site by doing it once in the account's lane; the node turns what it did into a
connector operation, checks it with no model in the loop, marks it verified, and syncs that field
from then on. No approval cards anywhere in this loop: the owner's consent is given when they add
the account's login and when they save (or approve) the profile.

## 1. Recording vs connector op

`Recorder.build_checked` (`src/openajar/runner/recorder.py`) yields a FlowDefinition draft:
steps navigate, wait_for, click, fill/type (with a param and inline readback), select, press,
scroll_to; `Locator` (`runner/models.py`) with `name`, not per-language `names`; params
schema; effect `draft`. Events come from `record_act` (`node/flow_driver.py`), which records
click/type/fill/select/press/scroll only (`flow_driver.py`): uploads are not recorded (photos
come in a second slice).

The node generates everything else, modelled on `connectors/linkedin/connector.json`
`profile_headline`: connector meta (site, domain, version, account_languages); input schema with
`show`, `readOnly`, `maxLength` (from `profiles/mapping.py`), `pattern`; output `proof_url` pinned
to the site host; `acting_as` and an `assert_identity` step before the commit; `publishes: false`
on every input step but the Save click (labelled commit); preflight `value_equals` on the filled
field plus `text_differs` where the value shows on the same page; readback; discard (Escape);
pace; signals hold list. Strict lint (`runner/connectors/lint.py`) wants structural
locators on the commit path: the generator wraps name-only locators in `within` (dialog, form or
main).

The agent supplies only: the field (fixed by the learn session) and where the new value shows on
the profile page (a ref, turned into a locator by the node with `structural_locator_for`). The
commit request is never written by the agent (`browser_network` withholds bodies,
`node/devtools_tools.py`): the node takes it from its own egress trace of the one request the
learn window released (host, path, method, operation name; the shape `gateshapes.log` builds,
`node/gateshapes.py`).

## 2. Connector lifecycle

- Install: only `install_dir` exists (unsigned, owner-only, whole connector,
  `runner/connectors/service.py`); `adopt` is not built. Add
  `ConnectorService.install_learned(doc)` under its own connector key `<site>-profile` (e.g.
  `connector.instagram-profile.bio`), refusing a clashing tool name, with a new `saved_by` kind
  `learned` in `INSTALL_KINDS` (`runner/models.py`) that is NOT counted by `is_owner`
  (`models.py`). `may_publish` (`runner/executor.py`) honours `publishes: false` for a
  `learned` op only under strict FlowCommit scope (section 3).
- Verified = egress rule `verified` non-empty, flow installed, ledger mark whose proof hash equals
  the installed op's
  (`runner/connectors/marks.py`); the ledger is owner-only from a shell. Add provenance
  `by: "replay"`, written only by the in-process `ProfileLearner` after its model-free replays
  pass (no MCP tool or API route writes it). `ProfileLearner.state` accepts a replay mark only when
  the flow's `saved_by.kind == "learned"`; catalog ops still need the owner's mark.

## 3. Proof rules: data-defined, node-written

`EFFECT_RULES` is code (`node/egress/rules.py`); per-node gateshapes promotions are data but
owner-only and only ever stricter (`gateshapes.py`). Add
`node/learned_rules.py`, a gateshapes-shaped store (`learned-rules.json`, 600) written by the
learner from trace evidence: id `<site>.profile.learned_<field>`, kind fixed `profile`, exact
host/path/method/op seen, `verified` set by the replay. Compiled after built-in commit rules and
before read catch-alls, so it never reclassifies a post/comment/like/follow; for agent drives it is
still a commit. Hosts must already be known to built-in rules (`gateshapes.py`): facebook,
instagram, linkedin, medium, reddit today; x, threads, tiktok, youtube need reviewed base rules and
`node/sites.py` entries first (lint refuses unknown sites, `lint.py`).

Strict scope: `FlowCommit(strict=True)` for learned ops. Today `problem` lets an unmatched request
go (`node/egress/verdicts.py`); strict aborts any non-GET on the rule host that is not the
declared request, an upload `draft` rule or a known read.

## 4. Approvals, removed narrowly

| Forces a card | Where | Change |
|---|---|---|
| Ask-me-first and taint on connector runs | `policy/ROUTING.md` | When the owner saves/approves a profile in person, mint a bound standing approval per (surface, field, value) whose content is the digest of op key + params (`ROUTING.md`). |
| Commit gate during learning (`<site>.unknown` + taint) | `egress-guard.md` 8.1, 8.7 | A learn window (below), modelled on the vault login window (`node/egress/guard.py`, `_gate_rule`). |

Learn window: `LaneGuard.open_learn(field, value, path_prefix, grant, ttl=15m)` releases one
request that the agent's own press sent, whose body carries exactly the approved value (the
`carries_typed` machinery, `guard.py`), classified only `<site>.unknown` or
`<site>.profile.*` (never post/comment/like/follow/join/pay/irreversible), from a page under a
reviewed per-site edit path (`profile_edit_paths` in `node/sites.py`), on the lane's pinned
account. Traced `learn_commit`; that trace row is the op's commit request. Agent posts, comments
and pilot runs never get a learn window.

## 5. Sync runner

`profiles/sync.py`. Submits connector runs through the runner with `authorized=False` and no card of
its own; lane holds, connector `pace`,
`lane_paced`, lane claims apply. Wakes on profile save and every 30 minutes; per due
(surface, field): run the read op (`<field>_read`); if it shows the value, `in_sync`; else run the
commit op; record in `synced(surface, field, value_hash, run_id, status)`. A signal holds the
account (never a relearn); `connector_broken`, `preflight_mismatch` or `connector_contract_broken`
(`runner/connectors/health.py`) sets the field `relearn`, at most 2 relearns per field per week.

## 6. The learning agent

The connected MCP agent (first slice) or a brain task (`claude -p` on the node) following a howto
recipe. Tools: `profile_learn_start(surface, field)` (checks attached, approved, field manual or
relearn, lane not held; starts the recorder, opens the learn window; returns value and edit-path
hint) and `profile_learn_finish(surface, field, readback_ref, profile_url)` (node builds the op,
compiles, installs, replays). The agent uses `login`, `browser_navigate`, `browser_snapshot`,
`browser_act` in between. Panel: a "Teach openajar this field" button per manual field.

## 7. Sequence

learn_start -> agent edits and saves -> window releases the request, traces its shape -> node
reloads the profile URL and reads the readback locator (must equal the value, else abandon and
hold) -> `profiles/learn.py` generates `<field>` (commit) and `<field>_read` (read), compiled with
strict lint -> learned rule (unverified) + `install_learned` -> model-free replay: commit op
rehearsal (up to the commit with preflight passing, then discard) and the read op returning the
value -> rule `verified` + replay mark -> sync runner uses it -> on breakage, relearn.

A full commit replay right after learning would end `already_current` (the value is live), so the
commit's proof is the learning save itself (node-traced, node-read-back) and the first real sync
is the first model-free commit; if it fails, the field relearns.

## 8. Invariants

1. No model at sync time: only compiled, strictly linted ops.
2. The agent never writes the commit request, the rule or the mark.
3. Values come only from the approved profile version (`mapping.resolve`); inputs `readOnly`;
   `value_equals` binds the form to the value.
4. A learned rule's kind is fixed `profile` and is ordered after the built-in commit rules.
5. Strict FlowCommit: a learned op's run sends only its declared request and uploads.
6. Catalog ops still need the owner's mark; the gate-shapes file stays owner-only.
7. Revoke: `openajar connector verify --revoke`, `connector remove`, a per-lane learn switch.

`name` is excluded from automatic learning and sync: Facebook and Instagram rate-lock name changes
for weeks (hard to undo, PRINCIPLES.md:59-60).

## 9. First slice

Instagram bio (text; `instagram.com` has rules; edit page `/accounts/edit/`; readback on
`/<username>/`), learned by the connected MCP agent through a howto. Ships `profiles/learn.py`, the
two tools, the learn window, `learned_rules.py`, strict FlowCommit, the `learned` install kind,
replay marks, and `profiles/sync.py` for that field. Photos second (upload hook in `record_act`,
learned upload draft rules, `changed_from` readback).
