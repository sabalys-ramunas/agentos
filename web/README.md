# The openajar web app

The screens a person uses to watch and steer what agents do on a node: every environment with its
live browser, the commands its terminal ran and its git (branch, changes, what was pushed), runs,
studio's checks, brand, the machines, accounts, routines, Needs you (approvals), history and
settings. React 19, TanStack Router and Query, Tailwind 4, built with Vite.

It speaks one API, `/api/orgs/{org}/...` (`src/api/client.ts`). A node answers it as the org `local`,
so the same screens run on your own node with nothing branching in them. How the node serves that
API, and how to add a route: [docs/WEB-NODE.md](../docs/WEB-NODE.md). The design rules every screen
follows: [DESIGN.md](DESIGN.md); how to build a screen with the design system, its component groups and
its checks: [DESIGN-SYSTEM.md](DESIGN-SYSTEM.md) (agents: [AGENTS.md](AGENTS.md)).

## What you need

Node.js 22 or later and pnpm 10.33.0, the version that wrote `pnpm-lock.yaml`. Corepack, which comes
with Node.js, gives you that pnpm:

```bash
corepack enable && corepack prepare pnpm@10.33.0 --activate
```

Or put `npx --yes pnpm@10.33.0` in front of each command instead of `pnpm`, as the node's installer
does.

## Dev loop

```bash
cd web
pnpm install --frozen-lockfile
pnpm dev --port 5321          # mock mode: no node needed
```

`pnpm dev` runs on the mock backend (`.env.development` sets `VITE_API_MODE=mock`): Mock Service
Worker answers every call with a believable, fictional demo world (the org `northwind-group`), so you
can work on any screen without a node. `?mock=loading`, `?mock=error` and the other scenarios in
DESIGN.md show the states. The mock never reaches a build: `vite build` refuses to run in mock mode.

To work against a real node, start a dev node from the repository root (fake account lanes, real
everything else, nothing leaves your machine), then point the app at it:

```bash
uv run openajar dev up                      # prints a sign-in link on 127.0.0.1:7450
OPENAJAR_API_TARGET=http://127.0.0.1:7450 VITE_API_MODE=live pnpm dev --port 5321
```

Sign in with the code from the link the dev node printed, on the app's port:
`http://localhost:5321/login#code=...`. A code works once and for ten minutes.
`node scripts/dev-node.mjs --port 7451` starts a dev node, signs in for the scripts in `scripts/`, and
prints a second link for the browser.

## Build

```bash
pnpm build                    # writes dist/, in live mode
```

A node serves `web/dist` at `/` beside its API, as soon as `dist/` is built (`OPENAJAR_PANEL_DIR`
points it at another build). A checkout's own build comes first; an openajar installed without a
checkout serves the prebuilt copy `openajar web install` unpacks into `~/.openajar/web/app`
([docs/LOCAL.md](../docs/LOCAL.md#the-web-app)).

Each release carries that copy: the `web-release` workflow builds `web/` in live mode and packs
`dist/` with `scripts/web_asset.py` into `openajar-web-<version>.tar.gz` and its `.sha256`. To make
the same archive here (it is reproducible: the same build packs to the same bytes):

```bash
make web-asset                # pnpm build, then dist-release/openajar-web-<version>.tar.gz and .sha256
```

The packer refuses a `dist/` with a link in it or the mock backend in it. The node's installer (`deploy/install-node.sh`) builds `web/` whenever its sources changed since the last
good build, and keeps that build if a new one fails.

To open it on a node, run `openajar panel login` there: it prints a one-time sign-in link. From your
computer, reach the node first with `ssh -N -L 7400:localhost:7400 <your node>`.

## Tests

```bash
pnpm typecheck && pnpm lint && pnpm knip && pnpm test && pnpm build
pnpm test:e2e                 # real browser flows on the mock, in system Chrome
```

`pnpm lint` holds every file to 500 lines and every function to a complexity of 25; the files still
over are listed in `eslint.config.js`, and that list only shrinks. `pnpm knip` finds unused files,
exports and dependencies (`knip.json`).

`pnpm test` (Vitest, jsdom) includes the design rules in `src/test/design-system.test.ts` (tokens only,
components where they exist, a catalog entry for every component; `pnpm design:check` runs just those), the source rules in `src/test/source-rules.test.ts` (no em dash,
no hex colour in screens, screens never import the mock, no password field) and the token checks,
which run `python3 tokens/build-tokens.py --check`. `pnpm test:e2e` starts its own dev server on
port 5392, or on `E2E_PORT`.

`pnpm test:e2e catalog` photographs the design system's catalog by group, light and dark, at 1440 and
390. The baselines are not in git (`e2e/__screenshots__/` is ignored): a machine's first run records
them and passes, and later runs compare against them, so take the first run on a clean `main` before
you change anything. After a change you meant, `pnpm test:e2e catalog --update-snapshots` records the
new pictures. Fonts and anti-aliasing differ between machines, so a baseline only means something on
the machine that took it.

To prove a change draws nothing different (a codemod, a token rename), `scripts/visual-compare.mjs`
photographs the catalog and the main screens on the mock and compares two sets pixel by pixel:

```bash
OPENAJAR_API_TARGET=http://127.0.0.1:9 pnpm dev --port 5343 &
node scripts/visual-compare.mjs shoot --out /tmp/before      # before the change
node scripts/visual-compare.mjs shoot --out /tmp/after       # after it
node scripts/visual-compare.mjs diff /tmp/before /tmp/after --out /tmp/diff
```

`scripts/codemod-legacy-tokens.mjs` rewrites the pre-Mono class names (`bg-bg`, `text-13`) to the Mono
ones; a branch made before 2026-11-19 runs it once to pass `pnpm design:check`.

From the repository root, `uv run pytest -q tests/unit/web` checks the node's side: every call in
`src/api/client.ts` must be a route the node serves (`tests/unit/web/test_every_call.py`).
