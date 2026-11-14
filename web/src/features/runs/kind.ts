/**
 * What kind of work a run is, read from its command: tests, a build, a dev server, or a plain
 * command when the words do not say. Only the command is read; nothing is guessed from the output.
 */
import type { RunKind } from './search';

/** Words that start a command without being the program: `uv run`, `npx`, `pnpm exec`, `FOO=1`. */
const WRAPPERS: string[][] = [
  ['uv', 'run'], ['poetry', 'run'], ['pipenv', 'run'], ['hatch', 'run'], ['pdm', 'run'], ['rye', 'run'],
  ['pnpm', 'exec'], ['pnpm', 'dlx'], ['yarn', 'exec'], ['yarn', 'dlx'], ['npm', 'exec'], ['bun', 'x'],
  ['python', '-m'], ['python3', '-m'], ['bundle', 'exec'], ['dotenv', '--'], ['env'], ['npx'], ['bunx'],
  ['time'], ['nice'], ['sudo'], ['xvfb-run'], ['timeout'],
];

/** Package managers whose next word names a script: `pnpm test`, `npm run build`. */
const SCRIPT_RUNNERS = new Set(['npm', 'pnpm', 'yarn', 'bun', 'make', 'just', 'task', 'mise', 'deno', 'turbo', 'nx', 'rake', 'composer']);

const TEST = /^(test|tests|testing|spec|specs|e2e|unit|integration|check:test|vitest|jest|pytest|ctest|cypress|playwright)([:_-].*)?$/;
const TEST_PROGRAMS = new Set(['pytest', 'py.test', 'vitest', 'jest', 'mocha', 'ava', 'tap', 'rspec', 'phpunit', 'tox', 'nox', 'ctest', 'unittest', 'nosetests', 'karma', 'jasmine']);
/** A program whose own `test` word makes it a test run: `go test`, `cargo test`, `swift test`. */
const TEST_SUBCOMMAND = new Set(['go', 'cargo', 'swift', 'dotnet', 'mvn', 'gradle', './gradlew', 'mix', 'deno', 'bun', 'zig', 'flutter', 'dart', 'sbt', 'lein', 'stack', 'cabal']);

const BUILD = /^(build|compile|bundle|dist|package|release|install|ci|lint|typecheck|type-check|types|tsc|check|format|fmt|migrate|migration|generate|codegen|prepare)([:_-].*)?$/;
const BUILD_PROGRAMS = new Set(['uv', 'poetry', 'cargo', 'go', 'swift', 'tsc', 'webpack', 'rollup', 'esbuild', 'tsup', 'turbo', 'nx', 'gcc', 'clang', 'cmake', 'ninja', 'make', 'docker', 'eslint', 'ruff', 'mypy', 'pyright', 'biome', 'prettier', 'xcodebuild', 'pip', 'pnpm', 'npm', 'yarn', 'bun', 'alembic']);
const BUILD_SUBCOMMAND = new Set(['build', 'compile', 'install', 'ci', 'sync', 'lint', 'check', 'archive', 'package', 'publish', 'vet', 'clippy', 'migrate', 'fmt']);

const SERVER = /^(dev|serve|server|start|preview|watch|storybook|develop)([:_-].*)?$/;
const SERVER_PROGRAMS = new Set(['uvicorn', 'gunicorn', 'hypercorn', 'flask', 'rails', 'nodemon', 'http-server', 'live-server', 'storybook', 'caddy', 'nginx']);

const INTERPRETERS = new Set(['python', 'python3', 'node', 'ruby', 'bash', 'sh', 'zsh', 'perl', 'php', 'tsx', 'ts-node']);

const base = (word: string) => word.replace(/^.*\//, '').toLowerCase();

function words(command: string): string[] {
  // Only the first command of a chain says what the run is for: `cd app && pnpm test` is the test.
  const parts = command
    .split(/\s*(?:&&|\|\||;|\|)\s*/)
    .map((part) => part.trim())
    .filter(Boolean)
    .filter((part) => !/^(cd|export|source|\.)\s/.test(part) && !/^set\s+-/.test(part));
  const first = parts[0] ?? '';
  return first.split(/\s+/).filter(Boolean).map((word) => word.replace(/^['"]|['"]$/g, ''));
}

/** Drops env assignments, `sh -c` and wrappers, and returns the program and what follows it. */
function program(command: string): string[] {
  let rest = words(command);
  for (let guard = 0; guard < 8; guard += 1) {
    while (rest[0] && /^[A-Za-z_][A-Za-z0-9_]*=/.test(rest[0])) rest = rest.slice(1);
    if ((rest[0] === 'sh' || rest[0] === 'bash' || rest[0] === 'zsh') && rest[1] === '-c') {
      rest = words(rest.slice(2).join(' '));
      continue;
    }
    // `python manage.py runserver`, `node scripts/seed.js`: the script is the program.
    if (INTERPRETERS.has(base(rest[0] ?? '')) && rest[1] && !rest[1].startsWith('-')) {
      rest = rest.slice(1);
      continue;
    }
    const wrapper = WRAPPERS.find((w) => w.every((word, index) => rest[index] === word));
    if (!wrapper) break;
    rest = rest.slice(wrapper.length);
    // `timeout 600 cmd`, `nice -n 10 cmd`: skip the wrapper's own arguments.
    while (rest[0] && /^(-|\d)/.test(rest[0]) && rest[0] !== '--') rest = rest.slice(1);
    if (rest[0] === '--') rest = rest.slice(1);
  }
  return rest;
}


/** The kind of a run from its command. `command` when the words do not say. */
export function kindOf(command: string): RunKind {
  const argv = program(command);
  const head = argv[0];
  if (!head) return 'command';
  const name = base(head);
  const next = (argv[1] ?? '').toLowerCase();
  const flagless = argv.slice(1).filter((word) => !word.startsWith('-')).map((word) => word.toLowerCase());

  if (TEST_PROGRAMS.has(name)) return 'test';
  if (name === 'playwright' || name === 'cypress') return next === 'test' || next === 'run' ? 'test' : 'command';
  if (TEST_SUBCOMMAND.has(name) && next === 'test') return 'test';
  if (name === 'xcodebuild') return argv.some((word) => word === 'test' || word === 'test-without-building') ? 'test' : 'build';
  if (name === 'swift' && next === 'build') return 'build';

  if (SCRIPT_RUNNERS.has(name)) {
    // `npm run test:unit`, `pnpm test`, `make check`, `yarn dev`.
    const script = (next === 'run' || next === 'run-script' ? argv[2] : argv[1])?.toLowerCase() ?? '';
    if (TEST.test(script)) return 'test';
    if (SERVER.test(script)) return 'server';
    if (BUILD.test(script)) return 'build';
    if (name === 'make' && !script) return 'build';
    return 'command';
  }

  if (/(^|[-_.])tests?([-_.]|$)|e2e|spec/.test(name) && /\.(sh|py|js|ts|rb)$|^[^.]+$/.test(name)) return 'test';
  if (SERVER_PROGRAMS.has(name)) return 'server';
  if (name === 'vite' || name === 'next' || name === 'nuxt' || name === 'astro' || name === 'remix' || name === 'svelte-kit') {
    if (next === 'build') return 'build';
    if (next === 'test') return 'test';
    return 'server';
  }
  if (name === 'http.server' || (name === 'manage.py' && next === 'runserver') || (name === 'php' && argv.includes('-S'))) return 'server';
  if (name === 'manage.py' && next === 'test') return 'test';
  if (name === 'docker' && next === 'compose' && argv[2] === 'up') return 'server';
  if (BUILD_PROGRAMS.has(name) && (BUILD_SUBCOMMAND.has(next) || name === 'tsc' || name === 'webpack' || name === 'esbuild' || name === 'rollup' || name === 'tsup' || name === 'mypy' || name === 'pyright' || name === 'eslint' || name === 'ruff')) return 'build';
  if (['go', 'cargo', 'swift', 'dotnet', 'mvn', 'gradle', './gradlew'].includes(name) && flagless.some((word) => BUILD_SUBCOMMAND.has(word) || word === 'package' || word === 'assemble')) return 'build';
  if (/(^|[-_.])(build|install|deploy|release|lint)([-_.]|$)/.test(name)) return 'build';
  return 'command';
}

/** The kind's word for a person. */
export const KIND_WORD: Record<RunKind, string> = {
  test: 'Tests',
  build: 'Build',
  server: 'Dev server',
  command: 'Command',
};
