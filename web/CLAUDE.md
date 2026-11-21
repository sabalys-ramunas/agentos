# web/ for Claude Code

Read [AGENTS.md](AGENTS.md) first: it is the same guide for every agent, with the commands. Then read
[DESIGN-SYSTEM.md](DESIGN-SYSTEM.md) before you write or change any UI, and the group index in
[design-system/](design-system/) for each component you use.

In short:

- Compose screens from `src/components/` (the catalog at `/$org/design` shows all of them). Style only with
  token classes and type roles. A hand-drawn card, dot, spinner, table or error fails `pnpm design:check`,
  and the failure says what to use.
- Run `pnpm design:check` while you work, and `pnpm typecheck && pnpm lint && pnpm knip && pnpm test`
  before you commit. Start the dev server with `OPENAJAR_API_TARGET=http://127.0.0.1:9` so it stays on the mock.
- With the openajar-studio tools, `ui_check` the page you changed, in both themes, at 390 and 1440.
- Never raise a count in `src/test/design-allowlist.json`, and never write an em dash in text a person reads.
