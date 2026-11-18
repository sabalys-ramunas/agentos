# Working in web/

The agentos web app (React 19, TanStack Router and Query, Tailwind 4, Vite). Most of its UI is written by
coding agents, and its design system is built so that the right way is the easy way.

## Read first

1. [DESIGN-SYSTEM.md](DESIGN-SYSTEM.md): how to build a screen in five steps, the tokens, the component
   groups and the checks. Short; read all of it before you write UI.
2. The index of each group you use, in [design-system/](design-system/).
3. [DESIGN.md](DESIGN.md) when you need a rule's detail or its reason.
4. [README.md](README.md) for the dev loop and the mock backend.

## Rules that a test enforces

- Colour, type, space, radius, depth, motion and layers come from tokens. No hex, `rgb()`, named colour,
  Tailwind palette class (`text-red-500`), arbitrary design value (`p-[7px]`) or `style={{ color }}` in a screen.
- Use the components for what they cover: `Card`, `PageHeader`, `Table`, `StatTile`, `Status`, `Spinner`,
  `InlineError`, `CopyButton`. The test names the one to use.
- A new file under `src/components/` needs an entry in `src/catalog/registry.tsx` and a line in its group's index.
- Words a person reads come from `src/lib/vocabulary.ts`. Never an em dash (U+2014).
- Screens never import `src/mocks`. The hosted app has no password field.
- `src/test/design-allowlist.json` only shrinks. Never raise a count to make a test pass: fix the finding.

## Commands

```bash
pnpm install --frozen-lockfile
AGENTOS_API_TARGET=http://127.0.0.1:9 pnpm dev --port <yours>   # the mock; the catalog is /northwind-group/design
pnpm design:check                                            # the design rules, in seconds
pnpm typecheck && pnpm lint && pnpm knip && pnpm test         # before you commit
pnpm test:e2e                                                # real browser flows and the catalog screenshots
python3 tokens/build-tokens.py                               # after changing a token (never edit the outputs)
(cd .. && make emdash)
```

Changed a component on purpose? `pnpm test:e2e catalog --update-snapshots`, and look at the new pictures.
