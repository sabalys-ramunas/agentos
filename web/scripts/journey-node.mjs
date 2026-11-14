#!/usr/bin/env node
/**
 * What a person really does on a computer, in a real browser, against the real build the node
 * serves. Each step clicks what a person clicks and checks what the node then says.
 *
 *   pnpm build
 *   AGENTOS_PANEL_DIR=$PWD/dist node scripts/dev-node.mjs --port 7451 &
 *   node scripts/journey-node.mjs --port 7451
 *
 * It changes the dev node's data, so start a fresh dev node for each run.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium, expect } from '@playwright/test';

const web = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const at = process.argv.indexOf('--port');
const port = at > 0 ? process.argv[at + 1] : '7451';
const app = `http://localhost:${port}`;
const { cookie } = JSON.parse(readFileSync(`${web}/.dev-node-${port}.json`, 'utf8'));
const [name, value] = cookie.split(/=(.*)/s);

const browser = await chromium.launch({ channel: 'chrome', headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.addCookies([{ name, value, url: app, httpOnly: true, sameSite: 'Strict' }]);
const page = await context.newPage();
const crashes = [];
page.on('pageerror', (error) => crashes.push(error.message.slice(0, 160)));

let failed = 0;
async function step(label, run) {
  try {
    await run();
    console.log(`ok    ${label}`);
  } catch (error) {
    failed += 1;
    console.log(`FAIL  ${label}\n      ${String(error.message).split('\n').slice(0, 3).join(' ').slice(0, 220)}`);
    await page.screenshot({ path: `${web}/screenshots/journey-${failed}.png` }).catch(() => undefined);
  }
}
const rail = () => page.getByRole('navigation', { name: 'Main' }).first().getByRole('link', { name: /Needs you/ });
const needs = async (count) => expect(rail()).toContainText(String(count), { timeout: 8000 });

// A fresh seed has four things for a person: a photo to say yes to, an agent's question, an account that needs
// a hand (biz-b's Social, a robot check), and an engagement plan to approve (biz-b's Instagram plan, which the
// seed leaves unapproved). Needs you lists each one, and the count is their sum (web features/needs-you/count.ts).
await step('Home says what needs a person', async () => {
  await page.goto(`${app}/local`);
  await expect(page.locator('h1').first()).toContainText(/need you/i);
  await needs(4);
});

await step('Post a photo that waits for a yes', async () => {
  await page.goto(`${app}/local/needs-you`);
  await page.getByRole('button', { name: /Post it/ }).first().click();
  await needs(3);
});

await step('Answer an agent\'s question', async () => {
  await page.getByText(/Two cover images/).first().click();
  // A tap right after a decision is ignored on purpose, so a double tap never lands on the next item.
  await page.waitForTimeout(1500);
  await page.getByRole('button', { name: /^bright/i }).first().click();
  await needs(2);
});

await step('Take over an account, and hand it back', async () => {
  await page.goto(`${app}/local/accounts/biz-a/social`);
  await page.getByRole('button', { name: /Take over/ }).first().click();
  await expect(page.getByRole('button', { name: /Hand back/ }).first()).toBeVisible({ timeout: 8000 });
  await page.getByRole('button', { name: /Hand back/ }).first().click();
  await expect(page.getByRole('button', { name: /Take over/ }).first()).toBeVisible({ timeout: 8000 });
});

await step('Freeze an account, and unfreeze it', async () => {
  await page.goto(`${app}/local/accounts/biz-a/shop`);
  await page.getByRole('button', { name: /^Freeze/ }).first().click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: /Freeze/ }).last().click();
  await expect(page.getByText(/Frozen/).first()).toBeVisible({ timeout: 8000 });
  await page.getByRole('button', { name: /Unfreeze/ }).first().click();
  await page.getByRole('dialog').getByRole('button', { name: /Unfreeze/ }).last().click();
  await expect(page.getByRole('button', { name: /^Freeze/ }).first()).toBeVisible({ timeout: 8000 });
});

await step('Add an AI service through the computer\'s own form', async () => {
  await page.goto(`${app}/local/settings/brains/add?service=typesafe`);
  const [form] = await Promise.all([context.waitForEvent('page'), page.getByRole('button', { name: /Open the form/ }).click()]);
  await form.waitForLoadState();
  await expect(form.getByRole('heading', { name: /Add TypeSafe/ })).toBeVisible({ timeout: 8000 });
  await form.locator('input[type=password]').fill('not-a-real-key-for-this-test');
  await form.getByRole('button', { name: /Keep it on/ }).click();
  await expect(form.getByText(/All done/).first()).toBeVisible({ timeout: 8000 });
  await form.close();
  await expect(page.getByText(/is ready on/).first()).toBeVisible({ timeout: 8000 });
});

await step('Turn the worker on', async () => {
  await page.goto(`${app}/local/settings/workers`);
  await page.getByRole('switch', { name: /Browser worker on/ }).first().click();
  await page.getByRole('dialog').getByRole('button', { name: /Turn it on/ }).click();
  await expect(page.getByText(/next time agentos starts/).first()).toBeVisible({ timeout: 8000 });
});

await step('Nothing crashed', async () => expect(crashes).toEqual([]));

await browser.close();
console.log(failed ? `\n${failed} steps failed` : '\nEvery step worked');
process.exit(failed ? 1 : 0);
