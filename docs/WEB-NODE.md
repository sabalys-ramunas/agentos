# The web app on a node

`web/` is the one UI. It speaks one API, the cloud's: `/api/orgs/{org}/...` (`web/src/api/client.ts`).
A node speaks that API too, so the same screens run on a person's own computer with nothing branching
in them. To the app, a node is one org, `local`, with one computer in it.

A node serves `web/dist` (or the build `OPENAJAR_PANEL_DIR` points at). The older `panel/` UI was removed
on 2026-11-14.

## Build it and open it

Node.js 22 or later, and pnpm 10.33.0 (`corepack enable && corepack prepare pnpm@10.33.0 --activate`, or
`npx --yes pnpm@10.33.0` in front of each command):

```bash
cd web && pnpm install --frozen-lockfile && pnpm build   # writes web/dist
```

Start openajar again, then run `openajar panel login` on the
node for a one-time sign-in link; from your computer, reach a remote node first with
`ssh -N -L 7400:localhost:7400 <your node>`. `openajar dev up` prints its own link. The node's installer
(`deploy/install-node.sh`) builds `web/` by itself whenever its sources changed and Node.js is there, and
keeps the last good build when a new one fails. Without a build, `/` says how to make one. The dev loop
on the mock or against a dev node: [web/README.md](../web/README.md).

## Where things go

| What | Where |
|---|---|
| A route of the contract | `src/openajar/api/web/<area>.py`, with `routes(api)`. Found by itself, so no registry to edit |
| Shared helpers | `src/openajar/api/web/base.py` |
| Its tests | `tests/unit/web/test_<area>.py`, using the `client` and `node` fixtures |
| What the app expects | `web/src/api/client.ts` (calls), `web/src/api/types.ts` (shapes), `web/src/mocks/` (how it behaves) |
| Design rules | `web/DESIGN.md`. Read it before touching a screen |

A path no module claims goes through to the node unchanged: `/api/orgs/local/x` is the node's `/api/x`.
That is right only when the node's shape already matches the type. Otherwise write a contract route.

## A contract route

Reshape what the node really answers. Never re-implement who may do what: `read` and `write` run the real
node route inside this request, with the same cookie and CSRF header, so auth, policy and tracing stay
where they are, and a refusal goes back to the app as it was.

```python
from openajar.api.web.base import org_route, read, write

def routes(api):
    async def workspaces(request):
        rows = await read(request, "/api/workspaces")
        return [{"id": row["id"], "name": row["name"], "lanes": row["lanes_total"]} for row in rows]

    return [org_route(api, "/workspaces", workspaces, ["GET"])]
```

`org_route` takes `endpoint`'s `owner=True`, `cap="..."` and `public=True`. Use them when a route has no
node route under it to do the checking. A person-only route refuses tokens (see `routes_worker.py`).

If the node has no route for a call, add a real one on the node first (`src/openajar/api/routes_*.py`,
or a service method) and test it. Then shape it here.

## Telling the truth

- **Never invent data.** No sample rows, no made-up numbers, no fixed values that look measured.
- A field the node cannot know: use `null` if the type allows it. If the type does not, make the field
  optional in `types.ts` and make the screen say so calmly, as it does for a report a node does not send
  (`reported` in `http.ts`). Adding an optional field is fine. Do not change a field the cloud relies on.
- An empty list is a fine answer. A feature the node really lacks answers an honest empty state.
- Do not edit `src/mocks` to make a test pass, and do not weaken a test.

## Checking your work

```bash
export OPENAJAR_PYTHON=/path/to/openajar/.venv/bin/python
cd web && pnpm install --frozen-lockfile --offline

node scripts/dev-node.mjs --port 7461 &               # real services, fake lanes, no browser
node scripts/contract.mjs --port 7461 --only /lanes   # every GET checked against its type
PYTHONPATH=../src $OPENAJAR_PYTHON ../tests/unit/web/test_every_call.py lanes   # writes too: is it served?

cd .. && PYTHONPATH=src $OPENAJAR_PYTHON -m pytest tests/unit/web -q
cd web && pnpm typecheck && pnpm lint && pnpm test
```

Three checks go further than tests, against a dev node serving the real build
(`pnpm build`, then `OPENAJAR_PANEL_DIR=$PWD/dist node scripts/dev-node.mjs --port 7461`):

- `node scripts/contract.mjs --port 7461`: every GET against the type its screen reads.
- `node scripts/walk-node.mjs --port 7461 --app http://localhost:7461 --widths 1440,375`: every page in a real
  browser, and what went wrong on it.
- `node scripts/journey-node.mjs --port 7461`: what a person really does, step by step. It changes the dev
  node's data, so start a fresh one for each run.

Restart the dev node after changing Python. To see it in a browser, sign in with a code from the dev
node's output: `OPENAJAR_API_TARGET=http://127.0.0.1:7461 VITE_API_MODE=live pnpm dev --port 5321`, then open
`http://localhost:5321/login#code=...`. Use your own ports so agents do not collide.

Run targeted tests while you work. The full suites are for gates. Port 5310, Vite's port here, is where a
local stack you use may already listen: never point a test at it. `pnpm test:e2e` starts its own server on 5392, or on `E2E_PORT`.

## Rules that do not bend

- **A hosted page never holds a secret.** No password or key field in `web/`. A form that takes a secret
  is served by the node itself, as `GET /api/mail/form/{token}` is.
- Models never see a secret. Never print, log or commit a key.
- A login never asks when its password is in the vault. The vault's own checks stay.
- Money and the irreversible always ask.
- Only a person turns on a worker or a looser setting. A token never can.
- No em dash in anything a person reads. Everyday words from `web/src/lib/vocabulary.ts`.
- The rail is the menu the owner approved with Mono, in five groups (web/DESIGN.md, "The shell"): Work (Home,
  Environments, Runs, Checks), Where it runs (Machines), Who it is for (Brand, Accounts), Automations (Routines, Email, Site
  APIs) and You (Needs you, History, Settings). New things go under one of those places.
- Never point a test or a script at real accounts, or at a node or stack someone uses.
