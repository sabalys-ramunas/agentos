/**
 * The stories the environment concepts tell (concept stage, mock only): how each one opened, its app, its
 * network and secrets, its checkpoints, who watches it, and the saves an owner can resume.
 *
 * - e7k2 Black Friday offer page: a warm, prepared machine, open in 4.1 s with `pnpm dev` up on 3000.
 * - h5ap Date picker on the booking form: a cold open, setup ran here, and the app never listened on
 *   5173 (Vite took 3000 from the repository's own config), with the log's end.
 * - n3lm Shipping rates from the carrier: limited network, four blocked hosts (one got a placeholder
 *   that was never swapped), two app secrets.
 * - c5kp Cart totals with discounts: five checkpoints and a rollback to the third.
 * - r9cl Guest checkout, end to end: closed, with a 47 minute replay (mocks/env-replay.ts).
 * - q3mn (starting): its bring-up as it goes, step by step.
 * - Saved: a paused machine on Morph (back in seconds with its dev server) and the dark mode snapshot
 *   (about a minute); Resume wakes either from the app (mocks/env-concepts.ts).
 */
import type { EnvBringUp, EnvBringUpStep, EnvCheckpoint, EnvCheckpointChange, EnvDetail, EnvSave } from '@/api/types';
import { MEDIUM, SMALL, at, call, command, job, proof, summaryOf, videoProof } from './environments';
import type { MockEnv } from './environments';
import { scenario } from './scenario';

const SECOND = 1_000;
const MINUTE = 60_000;
const HOUR = 60 * MINUTE;

/** A step as planned: when it begins after the open and how long it takes. */
export type Plan = EnvBringUpStep[];

const s = (step: string, atS: number, seconds: number, extra: Partial<EnvBringUpStep> = {}): EnvBringUpStep => ({ step, at_s: atS, seconds, ok: true, ...extra });
/** A step the open did not need, as the node marks it: no seconds of its own, and what it took when it last ran. */
const skip = (step: string, why: string, saved: number | null): EnvBringUpStep => ({ step, at_s: 0, seconds: 0, ok: true, skipped: why, saved_s: saved });

export const PLANS: Record<string, Plan> = {
  e7k2: [
    skip('VM create', 'warm machine', 9.1),
    skip('VM ready', 'warm machine', 23.6),
    s('tunnels and git', 0, 0.9),
    s('browser', 0.9, 1.6),
    skip('clone', 'prepared machine', 6.4),
    s('checkout', 0.9, 0.6),
    s('refresh', 1.5, 1.4),
    skip('setup', 'prepared machine', 192.4),
    s('app', 2.9, 1.2),
  ],
  h5ap: [
    s('VM create', 0, 8.6),
    s('VM ready', 8.6, 23.9),
    s('setup check', 32.5, 3.1),
    s('tunnels and git', 35.6, 1.4),
    s('browser', 37, 3.8),
    s('clone', 37, 5.2),
    s('setup', 42.2, 51.6),
    s('app', 93.8, 30, { ok: false }),
  ],
  n3lm: [
    s('VM create', 0, 9.1),
    s('VM ready', 9.1, 21.7),
    s('network', 30.8, 1.9),
    s('tunnels and git', 32.7, 1.1),
    s('browser', 33.8, 2.2),
    skip('clone', 'prepared machine', 5.8),
    s('checkout', 33.8, 0.8),
    s('refresh', 34.6, 6.3),
    skip('setup', 'prepared machine', 171.2),
    s('app', 40.9, 3.4),
  ],
  c5kp: [
    s('VM create', 0, 8.2),
    s('VM ready', 8.2, 22.4),
    s('setup check', 30.6, 2.9),
    s('tunnels and git', 33.5, 1.3),
    s('browser', 34.8, 3.5),
    s('clone', 34.8, 4.4),
  ],
  r9cl: [
    skip('VM create', 'warm machine', 8.8),
    skip('VM ready', 'warm machine', 22.9),
    s('tunnels and git', 0, 1.1),
    s('browser', 1.1, 1.9),
    skip('clone', 'prepared machine', 6.1),
    s('checkout', 1.1, 0.7),
    s('refresh', 1.8, 1.6),
    skip('setup', 'prepared machine', 188),
    s('app', 3.4, 1.4),
  ],
  q3mn: [
    s('VM create', 0, 9.4),
    s('VM ready', 9.4, 24.1),
    s('setup check', 33.5, 3),
    s('tunnels and git', 36.5, 1.2),
    s('browser', 37.7, 3.6),
    s('clone', 37.7, 8.1),
    s('setup', 45.8, 58.2),
    s('app', 104, 4.5),
  ],
  /** Waking a disk snapshot: a new machine from the image, then the usual. */
  snapshot: [
    s('VM create', 0, 11.2),
    s('VM ready', 11.2, 34.8),
    s('tunnels and git', 46, 1.3),
    s('browser', 47.3, 3.1),
    s('restore', 47.3, 2.1),
  ],
  /** Waking a paused machine: the same machine, memory and all, so the dev server never stopped. */
  paused: [
    s('VM create', 0, 1.2),
    s('VM ready', 1.2, 1.6),
    s('tunnels and git', 2.8, 0.9),
    s('browser', 3.7, 1.6),
    s('restore', 5.3, 0.6),
  ],
};

const endOf = (step: EnvBringUpStep) => step.at_s + step.seconds;
const tenths = (value: number) => Math.round(value * 10) / 10;

/** The bring-up `elapsed` seconds in, as the node serves it: the steps done, and the ones running now. */
export function bringupAt(plan: Plan | undefined, elapsed: number, restoreFrom?: 'snapshot' | 'paused'): EnvBringUp {
  plan ??= [];
  const ran = plan.filter((step) => !step.skipped);
  const total = ran.length ? Math.max(...ran.map(endOf)) : 0;
  const over = elapsed >= total;
  const steps = plan.filter((step) => step.skipped || over || endOf(step) <= elapsed);
  const running = over ? [] : ran.filter((step) => step.at_s <= elapsed && endOf(step) > elapsed).map((step) => ({ step: step.step, at_s: step.at_s, so_far_s: tenths(elapsed - step.at_s) }));
  return { total_s: tenths(over ? total : elapsed), ok: over ? plan.every((step) => step.ok !== false) : null, steps, running, ...(restoreFrom ? { restore_from: restoreFrom } : {}) };
}

/** The time a plan takes from the open to ready. */
export const planSeconds = (plan: Plan | undefined) => Math.max(0, ...(plan ?? []).filter((step) => !step.skipped).map(endOf));

const done = (id: string): EnvBringUp => bringupAt(PLANS[id], Number.POSITIVE_INFINITY);

const change = (files: [number, number, number], added: number, removed: number): EnvCheckpointChange => ({ files_added: files[0], files_changed: files[1], files_removed: files[2], lines_added: added, lines_removed: removed });

/** One repository of a checkpoint: what it held and what changed since. */
const repo = (dir: string, head: string, files: number, since: EnvCheckpointChange | null): EnvCheckpoint['repos'][number] => ({ dir: `/work/${dir}`, head, files, diff: since });

function checkpoint(now: number, id: number, minutesAgo: number, note: string, repos: EnvCheckpoint['repos'], auto: string | null = null): EnvCheckpoint {
  return { id, at: at(now, -minutesAgo * MINUTE), note, auto, repos };
}

const VITE_TAIL = [
  '> booking@2.4.0 dev /work/booking',
  '> vite',
  '',
  '  VITE v6.0.7  ready in 812 ms',
  '',
  '  \u001b[32m➜\u001b[39m  \u001b[1mLocal\u001b[22m:   \u001b[36mhttp://localhost:\u001b[1m3000\u001b[22m/\u001b[39m',
  '  \u001b[32m➜\u001b[39m  \u001b[2mNetwork: use --host to expose\u001b[22m',
  '  \u001b[32m➜\u001b[39m  \u001b[2mpress h + enter to show help\u001b[22m',
  '\u001b[33m[vite] (client) the server.port in vite.config.ts is 3000; PORT=5173 is ignored\u001b[39m',
].join('\n');

/** The fields the concepts add to the environments the mock already had. */
export function decorate(envs: MockEnv[], now: number) {
  const byId = new Map(envs.map((item) => [item.detail.id, item]));
  const offer = byId.get('e7k2')?.detail;
  if (offer) {
    Object.assign(offer, {
      bringup: done('e7k2'),
      prepared: {
        used: true,
        from: { commit: '3f9a1c0e72b4', built_at: at(now, -2 * 24 * HOUR), age: '2 days' },
        checkout: { ok: true, seconds: 0.6, commit: 'c81e07d2a4f9' },
        refresh: { command: 'pnpm install --frozen-lockfile --offline', ok: true, exit_code: 0, seconds: 1.4 },
        setup_s: { build: 192.4, here: 1.4, saved: 191 },
        warm: "this machine came from the node's warm pool",
      },
      app: { command: 'pnpm dev', port: 3000, listening: true, job_id: 'job-8c1f0e2a3b', seconds: 1.2, link: '/api/orgs/northwind-group/environments/e7k2/ports/3000/' },
      network: { policy: 'full', allowed: { all: true }, blocked: [], secrets: ['STRIPE_TEST_SECRET'] },
      watched: { since: at(now, -6 * MINUTE), viewers: 1, live_views: 1, port_connections: 0 },
      replay: { frames: 9, since: offer.created_at, bytes: 9 * 48_000, kept_until: null },
      checkpoints: [
        checkpoint(now, 2, 4, 'Struck-through yearly prices, checked at 360 and 1280', [repo('shop', 'c81e07d2a4f9', 5, change([0, 0, 0], 0, 0))]),
        checkpoint(now, 1, 12, 'The offer banner on the home page, before the pricing table', [repo('shop', 'c81e07d2a4f9', 3, change([1, 2, 0], 26, 15))]),
      ],
    } satisfies Partial<EnvDetail>);
  }
  const flaky = byId.get('q3mn');
  if (flaky) {
    const elapsed = scenario.has('still') ? 47 : (now - Date.parse(flaky.detail.created_at)) / SECOND;
    if (!scenario.has('still')) flaky.plan = { id: 'q3mn', start: now - elapsed * SECOND };
    flaky.detail.bringup = bringupAt(PLANS.q3mn, elapsed);
    flaky.detail.prepared = { used: false, why: 'a prepared machine for this repository is being built; the next open starts from it' };
  }
  const invoices = byId.get('w8tp')?.detail;
  if (invoices) {
    invoices.network = { policy: 'limited', allowed: { preset: 'package registries and code hosts', hosts: [], secret_hosts: [] }, blocked: [], secrets: [] };
    invoices.watched = { since: at(now, -18 * MINUTE), viewers: 2, live_views: 1, port_connections: 1 };
  }
  const dark = byId.get('d1dm');
  if (dark) {
    dark.detail.keep = 'save';
    dark.detail.closed_reason = 'idle';
    // it was saved when it idled, three hours ago, after four hours of work
    dark.detail.closed_at = at(now, -3 * HOUR);
    dark.detail.created_at = at(now, -7 * HOUR);
    dark.detail.last_call_at = at(now, -3 * HOUR - 10 * MINUTE);
    dark.detail.replay = { frames: 64, since: dark.detail.created_at, bytes: 64 * 52_000, kept_until: at(now, 11 * 24 * HOUR) };
  }
}

/** The saves the concepts add: a paused machine on Morph that wakes in seconds. */
export function storySaves(now: number): EnvSave[] {
  return [
    {
      id: 'sv_k7pz_2', title: 'Checkout redesign', repo: 'github.com/harbor-supply/shop', branch: 'feat/checkout-redesign', head: '5e1b7c9a0d3f2468a1c3e5f7b9d0a2c4e6f8b0d2',
      saved_at: at(now, -2 * HOUR), kind: 'save', snapshot_gb: null, files_bytes: 284 * 1024 * 1024, paused: true, cost_eur_month: 2.4, provider: 'morph', server_type: 'morph-4x8', price_hourly_eur: 0.048,
      note: 'The address step is done and pnpm dev is running. Next: the payment step, then the e2e test for a guest.',
      agent: { name: 'Claude Code', client: 'T3 Code' }, prune_at: at(now, 12 * 24 * HOUR),
      services: [{ cmd: 'pnpm dev', port: 3000 }, { cmd: 'docker compose up db', port: 5432 }],
    },
  ];
}

/** New environments, one per story. */
export function storyEnvironments(now: number): MockEnv[] {
  const claude = { name: 'Claude Code', client: 'T3 Code' };
  const datePicker = summaryOf(now, {
    id: 'h5ap', title: 'Date picker on the booking form', state: 'working', repo: 'github.com/meridian-travel/booking', branch: 'feat/date-picker',
    age: 9, lastCall: 50, left: 111, machine: MEDIUM, agent: claude,
  });
  const devJob = job(now, 'h5ap', 'job-3a9f0c1d7e', 'pnpm dev', '/work/booking', 'running', 7, null, VITE_TAIL);
  const rates = summaryOf(now, {
    id: 'n3lm', title: 'Shipping rates from the carrier', state: 'idle', repo: 'github.com/harbor-supply/shop', branch: 'feat/carrier-rates',
    age: 41, lastCall: 12 * 60, left: 79, machine: SMALL, agent: { name: 'Codex', client: null }, keep: 'save',
  });
  const cart = summaryOf(now, {
    id: 'c5kp', title: 'Cart totals with discounts', state: 'working', repo: 'github.com/harbor-supply/shop', branch: 'fix/cart-discounts',
    age: 58, lastCall: 15, left: 62, machine: SMALL, agent: claude,
  });
  const guest = summaryOf(now, {
    id: 'r9cl', title: 'Guest checkout, end to end', state: 'closed', repo: 'github.com/harbor-supply/shop', branch: 'feat/guest-checkout',
    age: 3 * 60 + 20, ran: 47, lastCall: 0, left: 0, machine: SMALL, agent: claude,
  });
  const guestEnd = Date.parse(guest.closed_at!);
  const guestStart = Date.parse(guest.created_at);
  return [
    {
      jobs: [devJob],
      detail: {
        ...datePicker,
        ports: [{ port: 3000, process: 'node (vite)', link: null }],
        git: { branch: 'feat/date-picker', ahead: 1, dirty: 2, pushed: [] },
        calls: [
          call(now, 50, 'lane_exec', 'grep -n "port" vite.config.ts'),
          call(now, 95, 'lane_job_read', 'job-3a9f0c1d7e, 9 lines'),
          call(now, 140, 'browser_navigate', 'http://localhost:5173/book: could not connect', false),
          call(now, 520, 'environment_open', 'Date picker on the booking form, github.com/meridian-travel/booking'),
        ],
        commands: [command(now, devJob.job.id, 'pnpm dev', 'running', 7)],
        viewer: { path: '/api/orgs/northwind-group/lanes/env/h5ap-browser/vnc' },
        closed_reason: null,
        unsaved: { dirty_files: ['src/booking/date-picker.tsx', 'src/booking/date-picker.test.tsx'], unpushed: ['2d7e4a1 Booking: a date picker that knows the closed days'] },
        bringup: done('h5ap'),
        prepared: {
          used: false,
          why: 'no prepared machine for this repository and its lockfiles yet, so setup ran here',
          setup: { command: 'pnpm install', ok: true, exit_code: 0, seconds: 51.6 },
          build: { started: true },
        },
        app: {
          command: 'pnpm dev', port: 5173, listening: false, job_id: devJob.job.id, seconds: 30,
          why: 'it runs, but port 5173 did not listen within 30 s (check it listens on that port, on 0.0.0.0 or 127.0.0.1)',
          log_tail: VITE_TAIL, listening_instead: [3000], next: `lane_job_read(job="${devJob.job.id}") shows its whole output`,
        },
        network: { policy: 'full', allowed: { all: true }, blocked: [], secrets: [] },
        watched: null,
        checkpoints: [],
      },
    },
    {
      jobs: [],
      detail: {
        ...rates,
        ports: [{ port: 3000, process: 'next-server', link: '/api/orgs/northwind-group/environments/n3lm/ports/3000/' }],
        git: { branch: 'feat/carrier-rates', ahead: 0, dirty: 0, pushed: [{ branch: 'feat/carrier-rates', commit: 'b7e1d04a9c3f5e8c1a9b6d2e4f70c3a8b5d1e96f', at: at(now, -14 * MINUTE) }] },
        calls: [
          call(now, 12 * 60, 'lane_exec', 'git push origin feat/carrier-rates'),
          call(now, 13 * 60, 'lane_job_start', 'pnpm vitest run src/shipping'),
          call(now, 16 * 60, 'lane_exec', 'curl -sS https://api.segment.io/v1/batch: refused by the network', false),
          call(now, 19 * 60, 'browser_navigate', 'http://localhost:3000/cart'),
          call(now, 24 * 60, 'lane_exec', 'pnpm add @parcelpoint/rates'),
          call(now, 40 * 60, 'environment_open', 'Shipping rates from the carrier, github.com/harbor-supply/shop, network limited'),
        ],
        commands: [command(now, 'exec-4a1', 'git push origin feat/carrier-rates', 'passed', 12, 0), command(now, 'exec-3b2', 'pnpm add @parcelpoint/rates', 'passed', 24, 0)],
        viewer: { path: '/api/orgs/northwind-group/lanes/env/n3lm-browser/vnc' },
        closed_reason: null,
        unsaved: null,
        bringup: done('n3lm'),
        prepared: {
          used: true,
          from: { commit: '91c2e5f0a7b3', built_at: at(now, -5 * HOUR), age: '5 h' },
          checkout: { ok: true, seconds: 0.8, commit: 'b7e1d04a9c3f' },
          refresh: { command: 'pnpm install --frozen-lockfile', ok: true, exit_code: 0, seconds: 6.3 },
          setup_s: { build: 171.2, here: 6.3, saved: 164.9 },
        },
        app: { command: 'pnpm dev', port: 3000, listening: true, seconds: 3.4, link: '/api/orgs/northwind-group/environments/n3lm/ports/3000/' },
        network: {
          policy: 'limited',
          allowed: { preset: 'package registries and code hosts', hosts: ['sandbox.parcelpoint.example'], secret_hosts: ['api.parcelpoint.example'] },
          blocked: [
            { host: 'api.segment.io', count: 41, last: at(now, -2 * MINUTE) },
            { host: 'collector.ratesdash.example', count: 3, last: at(now, -16 * MINUTE), why: 'the placeholder of PARCELPOINT_API_KEY was sent here: never swapped' },
            { host: 'fonts.googleapis.com', count: 12, last: at(now, -19 * MINUTE) },
            { host: 'o4501.ingest.sentry.io', count: 7, last: at(now, -31 * MINUTE) },
          ],
          secrets: ['PARCELPOINT_API_KEY', 'SENTRY_DSN'],
          fix: 'the owner allows a host with `agentos environments allow --host api.segment.io`',
        },
        watched: null,
        checkpoints: [],
      },
    },
    {
      jobs: [],
      detail: {
        ...cart,
        ports: [{ port: 3000, process: 'next-server', link: '/api/orgs/northwind-group/environments/c5kp/ports/3000/' }],
        git: { branch: 'fix/cart-discounts', ahead: 2, dirty: 3, pushed: [] },
        calls: [
          call(now, 6 * 60, 'environment_checkpoint', 'made 6, "Fixed discounts never take the total under zero"'),
          call(now, 70, 'lane_job_start', 'pnpm vitest run src/cart'),
          call(now, 9 * 60, 'lane_exec', 'git commit -m "Cart: a fixed discount never takes the total under zero"'),
          call(now, 18 * 60, 'environment_rollback', 'back to 3, "Percent and fixed discounts, with tests" (kept the work as 5 first)'),
          call(now, 19 * 60, 'lane_job_start', 'pnpm vitest run src/cart: 6 failed', false),
          call(now, 25 * 60, 'environment_checkpoint', 'made 4, "Try: discounts per line item"'),
          call(now, 37 * 60, 'environment_checkpoint', 'made 3, "Percent and fixed discounts, with tests"'),
          call(now, 44 * 60, 'environment_checkpoint', 'made 2, "Discount codes apply to the subtotal"'),
          call(now, 52 * 60, 'environment_checkpoint', 'made 1, "Before anything: the cart as it was"'),
          call(now, 57 * 60, 'environment_open', 'Cart totals with discounts, github.com/harbor-supply/shop'),
        ],
        commands: [command(now, 'exec-7c1', 'pnpm vitest run src/cart', 'passed', 1, 0), command(now, 'exec-6b2', 'git commit -m "Cart: a fixed discount never takes the total under zero"', 'passed', 9, 0)],
        viewer: { path: '/api/orgs/northwind-group/lanes/env/c5kp-browser/vnc' },
        closed_reason: null,
        unsaved: { dirty_files: ['src/cart/totals.ts', 'src/cart/totals.test.ts', 'src/cart/summary.tsx'], unpushed: ['8e2f1c0 Cart: a fixed discount never takes the total under zero', '4a7d9b3 Cart: percent and fixed discounts'] },
        bringup: done('c5kp'),
        prepared: { used: false, why: 'the repository has no .agentos/environment.toml, so there is nothing to prepare' },
        network: { policy: 'full', allowed: { all: true }, blocked: [], secrets: [] },
        watched: { since: at(now, -3 * MINUTE), viewers: 1, live_views: 1, port_connections: 0 },
        checkpoints: [
          // the agent went back to 3 eighteen minutes ago: agentos kept the work as it was first, as 5
          checkpoint(now, 6, 6, 'Fixed discounts never take the total under zero', [repo('shop', '8e2f1c0a9b3d', 2, change([0, 1, 0], 9, 2)), repo('ui-kit', '4a7d9b3c2e10', 0, change([0, 0, 0], 0, 0))]),
          checkpoint(now, 5, 18, 'automatic, before the rollback to checkpoint 3', [repo('shop', '4a7d9b3c2e10', 11, change([1, 9, 2], 102, 391)), repo('ui-kit', '4a7d9b3c2e10', 2, change([0, 2, 0], 9, 31))], 'before rollback to 3'),
          checkpoint(now, 4, 25, 'Try: discounts per line item', [repo('shop', '4a7d9b3c2e10', 11, change([1, 9, 2], 102, 391)), repo('ui-kit', '4a7d9b3c2e10', 2, change([0, 2, 0], 9, 31))]),
          checkpoint(now, 3, 37, 'Percent and fixed discounts, with tests', [repo('shop', '4a7d9b3c2e10', 6, change([0, 3, 0], 41, 6)), repo('ui-kit', '4a7d9b3c2e10', 1, change([0, 0, 0], 0, 0))]),
          checkpoint(now, 2, 44, 'Discount codes apply to the subtotal', [repo('shop', '1c90d7e2f3a4', 4, change([2, 5, 0], 179, 31))]),
          checkpoint(now, 1, 52, 'Before anything: the cart as it was', [repo('shop', '1c90d7e2f3a4', 0, change([3, 6, 0], 251, 37)), repo('ui-kit', '1c90d7e2f3a4', 0, change([0, 1, 0], 14, 2))]),
        ],
      },
    },
    {
      jobs: [],
      detail: {
        ...guest,
        last_call_at: new Date(guestEnd - MINUTE).toISOString(),
        ports: [],
        git: { branch: 'feat/guest-checkout', ahead: 0, dirty: 0, pushed: [{ branch: 'feat/guest-checkout', commit: 'f0c3a91e7b2d5e8c1a9b6d2e4f70c3a8b5d1e962', at: new Date(guestEnd - 3 * MINUTE).toISOString() }] },
        calls: [
          call(guestEnd, 40, 'environment_close', 'closed, the work is pushed'),
          call(guestEnd, 180, 'lane_exec', 'git push origin feat/guest-checkout'),
          call(guestEnd, 260, 'browser_snapshot', 'localhost:3000/order/HS-10482, 31 refs'),
        ],
        commands: [command(guestEnd, 'exec-r9', 'git push origin feat/guest-checkout', 'passed', 3, 0)],
        viewer: null,
        closed_reason: 'agent',
        unsaved: null,
        proofs: [
          videoProof('r9cl', 'pf_r9_video', 'A guest pays and gets the confirmation, end to end', new Date(guestStart + 36 * MINUTE).toISOString(), 'http://localhost:3000/checkout', 42.6),
          proof('r9cl', 'pf_r9_confirm', 'The order confirmation for a guest', new Date(guestStart + 33 * MINUTE).toISOString(), 'http://localhost:3000/order/HS-10482'),
          proof('r9cl', 'pf_r9_postcode', 'The postcode error, in words a person reads', new Date(guestStart + 23 * MINUTE).toISOString(), 'http://localhost:3000/checkout'),
        ],
        bringup: done('r9cl'),
        prepared: {
          used: true,
          from: { commit: '3f9a1c0e72b4', built_at: at(now, -2 * 24 * HOUR - 4 * HOUR), age: '2 days' },
          checkout: { ok: true, seconds: 0.7, commit: 'a03b7e19c4d2' },
          refresh: { command: 'pnpm install --frozen-lockfile --offline', ok: true, exit_code: 0, seconds: 1.6 },
          setup_s: { build: 188, here: 1.6, saved: 186.4 },
          warm: "this machine came from the node's warm pool",
        },
        app: { command: 'pnpm dev', port: 3000, listening: true, seconds: 1.4 },
        network: { policy: 'limited', allowed: { preset: 'package registries and code hosts', hosts: [], secret_hosts: ['api.stripe.com'] }, blocked: [{ host: 'js.stripe.com', count: 2, last: new Date(guestEnd - 30 * MINUTE).toISOString() }], secrets: ['STRIPE_TEST_SECRET'] },
        replay: { frames: 30, since: guest.created_at, bytes: 30 * 51_000, kept_until: new Date(guestEnd + 14 * 24 * HOUR).toISOString() },
        checkpoints: [],
      },
    },
  ];
}

/** The dark mode save points at its environment, which closed when it was saved. */
export function linkSaves(saves: EnvSave[], envs: MockEnv[]) {
  const dark = envs.find((item) => item.detail.id === 'd1dm')?.detail;
  const save = saves.find((entry) => entry.id === 'sv_d1dm_3');
  if (dark && save) {
    save.env_id = 'd1dm';
    save.provider = 'hetzner';
    save.server_type = dark.machine.server_type;
    save.price_hourly_eur = dark.price_hourly_eur;
    save.kind = 'save';
  }
}
