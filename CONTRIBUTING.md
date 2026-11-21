# Contributing to openajar

Thanks for helping. openajar is alpha: the design moves, and a short issue before a large
change saves everyone time.

## The dev loop (no node needed)

Everything except the real browser runs on a laptop (macOS or Linux, Python 3.12+, [uv](https://docs.astral.sh/uv/)):

```
uv sync                          # once: the Python env with the dev tools
uv run python -c "from camoufox.fpgen_model import ensure_fpgen_model; ensure_fpgen_model()"
                                 # once: the fingerprint model a few unit tests read (they may not reach the network)
uv run openajar dev up --seed     # a fake node on 127.0.0.1:7450 (see docs/DEVELOPING.md)
make check                       # what CI runs: lint (with layers and size limits), dead code, shell syntax, em dash check, type check, unit tests
```

The web app (`web/`) has its own loop and checks, on Node.js 22 and pnpm:
`cd web && pnpm install --frozen-lockfile && pnpm typecheck && pnpm lint && pnpm knip && pnpm test && pnpm build`.
[web/README.md](web/README.md) has the dev loop, on the mock or against the fake node.

`make check` runs the whole unit suite, which takes a while. While you work, run the tests
for the files you touch (`uv run pytest -q tests/unit/test_cli.py`) and leave the full suite
to `make check` before you push. `make merge-check` is the quick check after a merge: lint,
the route matrices, and the generated docs.

Unit tests never start a browser. They drive the real MCP server, CLI and daemon against the
fakes in `src/openajar/dev/` and `tests/unit/fakes.py`. [docs/DEVELOPING.md](docs/DEVELOPING.md)
has the details: the fake node, recorded fixtures, where things live.

## Testing on a node

Tests marked `node` (`-m node`) need Linux with Camoufox and Xvfb, and are skipped
everywhere else. To run them on an Ubuntu 24.04 box you control, put one line in
`.agentos-node.env` at the top of your checkout (it is gitignored):

```
OPENAJAR_NODE=mynode:/srv/agentos-dev
```

`mynode` is the box's name in your `~/.ssh/config`, and `/srv/agentos-dev` is a directory on it
for dev checkouts (the scripts only ever write under it). Then:

```
scripts/node-test.sh mytopic                      # sync, uv sync, uv run pytest -m node
scripts/node-test.sh mytopic tests/node/test_lanes_node.py -x -k warm
```

Never point tests at real accounts: the fixture site in `tests/fixtures/site/` and the dev web
are there for that.

## Sending a change

- One topic per pull request, in small commits. Commit subjects follow the repo's style:
  `Area: what changed, in plain words` (for example `Vault: refuse a fill on another domain`).
- Add or update tests with the change. A bug fix comes with a test that fails without it.
- A change to what an agent sees in an environment (a tool, a result, a description, the skill):
  run `make eval` before and after (Docker, free) and say what [docs/EVALS.md](docs/EVALS.md)'s
  compare shows.
- Generated docs must stay current: `make tools-doc`, `make errors-doc`, `make protocol-doc`
  (CI checks them).
- **No em dashes in anything a person reads**: UI strings, CLI output, docstrings, docs. Use a
  period, comma, colon or parentheses. `make emdash` checks the code.
- Plain words in user-facing text. The panel must make sense to someone who is not technical.
- Do not add tracking, telemetry or network calls home.

## Responsible use

openajar drives real accounts. Contributions that exist to break a platform's rules (mass
account creation, spam, ban evasion, scraping people) will not be merged. See
[docs/RESPONSIBLE-USE.md](docs/RESPONSIBLE-USE.md).

## Security issues

Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).

## Licence

By contributing you agree that your contribution is licensed under the Apache License 2.0
(see [LICENSE](LICENSE)).
