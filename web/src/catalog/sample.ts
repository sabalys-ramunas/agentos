/** The catalog's own examples: a few runs and a test log, the components' data and nothing real. */
import type { Kind } from '@/components/ui/kind';
import type { StatusLabel } from '@/lib/labels';

export interface Row {
  id: string;
  name: string;
  cmd: string;
  kind: Kind;
  branch: string;
  status: StatusLabel;
  tests: number | null;
  failed: number;
  minutes: number;
}

export const ROWS: Row[] = [
  { id: 'r1', name: 'Cart totals round per line', cmd: 'pnpm vitest run', kind: 'test', branch: 'fix/cart-total-rounding', status: { label: '2 failed', tone: 'bad' }, tests: 440, failed: 2, minutes: 0.8 },
  { id: 'r2', name: 'Fresh install and build', cmd: 'pnpm i && pnpm build', kind: 'build', branch: 'main', status: { label: 'Running', tone: 'live', pulse: true }, tests: null, failed: 0, minutes: 3.2 },
  { id: 'r3', name: 'Check the product page', cmd: 'ui_check /products/trail-runner-2', kind: 'design', branch: 'feat/product-badges-and-a-very-long-branch-name-that-cuts', status: { label: '1 blocker', tone: 'warn' }, tests: 6, failed: 0, minutes: 0.4 },
  { id: 'r4', name: 'Dev server for the shop', cmd: 'pnpm dev', kind: 'server', branch: 'dev/web', status: { label: 'Online', tone: 'ok' }, tests: null, failed: 0, minutes: 134 },
  { id: 'r5', name: 'Orders API', cmd: 'uv run pytest -q tests/api', kind: 'test', branch: 'main', status: { label: 'Waiting', tone: 'idle' }, tests: 0, failed: 0, minutes: 0 },
];

export const LOG = [
  '\u001b[2m14:09:02\u001b[0m  RUN  v3.2.4 /srv/acme-web',
  '\u001b[32m✓\u001b[0m src/checkout/address.test.ts (22 tests) 54ms',
  '\u001b[32m✓\u001b[0m src/checkout/shipping.test.ts (17 tests) 61ms',
  '\u001b[31m✗\u001b[0m src/cart/totals.test.ts > rounds each line \u001b[1mexpected 41.99\u001b[0m',
  '  \u001b[31m- received 42\u001b[0m',
  '  \u001b[32m+ expected 41.99\u001b[0m',
  '\u001b[32m✓\u001b[0m src/account/orders.test.ts (26 tests) 72ms',
  '\u001b[33m⚠\u001b[0m 1 snapshot obsolete in src/account/login.test.ts, and this line is long enough to be cut on a phone so its full text is a title',
  '\u001b[36minfo\u001b[0m  Test Files  \u001b[31m1 failed\u001b[0m | \u001b[32m31 passed\u001b[0m (32)',
];
