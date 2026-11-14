import { kindOf } from './kind';

describe('kindOf', () => {
  it.each([
    'pnpm vitest run',
    'pnpm test',
    'npm run test:unit',
    'npm run e2e',
    'uv run pytest -q',
    'uv run pytest tests/test_invoices.py',
    'python -m pytest -x',
    'go test ./...',
    'cargo test --all',
    'swift test',
    'npx jest --ci',
    'pnpm exec playwright test checkout',
    'xcodebuild test -scheme AcmeKit',
    'CI=1 pnpm vitest run --coverage',
    'cd web && pnpm test',
    "sh -c 'pnpm test'",
    'make test',
    'yarn test:e2e',
    './scripts/run-tests.sh',
    'bundle exec rspec',
  ])('reads %s as tests', (command) => expect(kindOf(command)).toBe('test'));

  it.each([
    'pnpm build',
    'npm run build',
    'vite build',
    'tsc --noEmit',
    'cargo build --release',
    'go build ./cmd/agent',
    'swift build',
    'docker build -t shop .',
    'xcodebuild -scheme AcmeKit',
    'make',
    'make check',
    'pnpm install --frozen-lockfile',
    'npm ci',
    'uv sync',
    'pnpm lint',
    'uv run mypy src',
    'uv run ruff check .',
  ])('reads %s as a build', (command) => expect(kindOf(command)).toBe('build'));

  it.each([
    'pnpm dev',
    'npm run dev -- --port 4173',
    'yarn start',
    'vite',
    'next dev',
    'uv run uvicorn app.main:app --reload',
    'python -m http.server 8000',
    'python manage.py runserver',
    'docker compose up',
    'pnpm storybook',
  ])('reads %s as a dev server', (command) => expect(kindOf(command)).toBe('server'));

  it.each([
    './scripts/backup-photos.sh --dry-run',
    'ls -la',
    'echo hello',
    '',
    '   ',
    'pnpm why react',
    'node scripts/seed.js',
    'git status',
  ])('says command for %j when it cannot tell', (command) => expect(kindOf(command)).toBe('command'));
});
