#!/usr/bin/env node
/**
 * Opens every page of the app on a real node, in a real browser, and says what went wrong: a page
 * that showed an error, an API call the node refused, a console error, a page wider than the screen.
 *
 *   node scripts/dev-node.mjs --port 7451 &
 *   AGENTOS_API_TARGET=http://127.0.0.1:7451 VITE_API_MODE=live pnpm dev --port 5311 &
 *   node scripts/walk-node.mjs --port 7451 --app http://localhost:5311 [--only accounts,vault] [--shots dir]
 */
import { mkdirSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';

const web = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const arg = (name, fallback) => {
  const at = process.argv.indexOf(`--${name}`);
  return at > 0 ? process.argv[at + 1] : fallback;
};
const port = arg('port', '7451');
const app = arg('app', 'http://localhost:5311');
const only = arg('only', '');
const shots = arg('shots', `${web}/screenshots/node`);
const widths = (arg('widths', '1440')).split(',').map(Number);
const { cookie } = JSON.parse(readFileSync(`${web}/.dev-node-${port}.json`, 'utf8'));
const [name, value] = cookie.split(/=(.*)/s);

const PAGES = [
  ['home', '/local'], ['needs-you', '/local/needs-you'], ['accounts', '/local/accounts'],
  ['account', '/local/accounts/biz-a/social'], ['logins', '/local/logins'],
  ['engagement', '/local/accounts/engagement'], ['engagement-today', '/local/accounts/biz-a/social/engagement'],
  ['engagement-plan', '/local/accounts/biz-a/social/engagement?tab=plan'],
  ['engagement-draft', '/local/accounts/biz-b/social/engagement?tab=plan'],
  ['engagement-connected', '/local/accounts/biz-b/forum/engagement?tab=connected'],
  ['routines', '/local/routines'], ['routine-new', '/local/routines/new'],
  ['email', '/local/email'], ['site-apis', '/local/site-apis'], ['history', '/local/history'],
  ['computers', '/local/computers'], ['computer', '/local/computers/dev'],
  ['computer-logs', '/local/computers/dev?tab=logs'], ['computer-installed', '/local/computers/dev?tab=installed'],
  ['computer-resources', '/local/computers/dev?tab=resources'], ['computer-schedules', '/local/computers/dev?tab=schedules'],
  ['computer-workers', '/local/computers/dev?tab=workers'], ['computer-connection', '/local/computers/dev?tab=connection'],
  ['computer-browsers', '/local/computers/dev?tab=browsers'],
  ['computer-health', '/local/computers/dev?tab=health'], ['computer-backups', '/local/computers/dev?tab=backups'],
  ['computer-environments', '/local/computers/dev?tab=environments'], ['computer-settings', '/local/computers/dev?tab=settings'],
  ['settings', '/local/settings'], ['members', '/local/settings/members'], ['invites', '/local/settings/invites'],
  ['workspaces', '/local/settings/workspaces'], ['access', '/local/settings/access'], ['grants', '/local/settings/grants'],
  ['notifications', '/local/settings/notifications'], ['webhooks', '/local/settings/webhooks'],
  ['retention', '/local/settings/retention'], ['memory', '/local/settings/memory'], ['billing', '/local/settings/billing'], ['audit', '/local/settings/audit'],
  ['danger', '/local/settings/danger'], ['yes', '/local/settings/yes'], ['ai-models', '/local/settings/brains'],
  ['ai-add', '/local/settings/brains/add'], ['workers', '/local/settings/workers'], ['vault', '/local/settings/vault'],
  ['account-me', '/local/settings/account'], ['passkeys', '/local/settings/passkeys'], ['devices', '/local/settings/devices'],
  ['standing', '/local/settings/standing'],
  ['proof', '/local/activity/proof'], ['proof-account', '/local/activity/proof?lane=biz-a/social'],
  ['business', '/local/workspaces/biz-a'],
];

const FAILURE_WORDS = ['Could not load', 'This page does not exist', 'Not switched on yet', 'Something went wrong', 'Not served'];

const browser = await chromium.launch({ channel: 'chrome', headless: true });
let bad = 0;
for (const width of widths) {
  const context = await browser.newContext({ viewport: { width, height: width < 500 ? 812 : 900 } });
  await context.addCookies([{ name, value, url: app, httpOnly: true, sameSite: 'Strict' }]);
  mkdirSync(shots, { recursive: true });
  const terms = only ? only.split(',') : [];
  for (const [label, path] of PAGES.filter(([key]) => !terms.length || terms.some((term) => key.includes(term)))) {
    const page = await context.newPage();
    const problems = [];
    page.on('pageerror', (error) => problems.push(`crash: ${error.message.slice(0, 140)}`));
    page.on('console', (message) => {
      if (message.type() === 'error' && !/Failed to load resource/.test(message.text())) problems.push(`console: ${message.text().slice(0, 140)}`);
    });
    page.on('response', (response) => {
      const url = new URL(response.url());
      if (url.pathname.startsWith('/api/') && response.status() >= 400) problems.push(`${response.status()} ${response.request().method()} ${url.pathname}`);
    });
    await page.goto(`${app}${path}`, { waitUntil: 'domcontentloaded' });
    await page.waitForLoadState('networkidle', { timeout: 6000 }).catch(() => undefined);
    await page.waitForTimeout(300);
    // What a person reads as a failure: the app's own error and not-found cards, which carry an alert or a
    // heading. Page text is not read, because a node's own data (a tool's description, say) may quote those words.
    for (const card of await page.locator('[role="alert"], h1, h2, h3').allInnerTexts()) {
      const hit = FAILURE_WORDS.find((words) => card.includes(words));
      if (hit) problems.push(`page says: ${card.trim().replace(/\s+/g, ' ').slice(0, 100)}`);
    }
    const wide = await page.evaluate(() => globalThis.document.documentElement.scrollWidth - globalThis.document.documentElement.clientWidth);
    if (wide > 1) problems.push(`wider than the screen by ${wide}px`);
    await page.screenshot({ path: `${shots}/${label}-${width}.png`, fullPage: true });
    const unique = [...new Set(problems)];
    if (unique.length) bad += 1;
    console.log(`${unique.length ? 'BAD ' : 'ok  '} ${label} (${width})`);
    for (const item of unique.slice(0, 8)) console.log(`       ${item}`);
    await page.close();
  }
  await context.close();
}
await browser.close();
console.log(`\n${bad} pages with problems`);
process.exit(bad ? 1 : 0);
