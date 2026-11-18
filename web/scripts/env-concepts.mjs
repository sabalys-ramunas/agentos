#!/usr/bin/env node
/**
 * Screenshots of the environment screens on the mock, light and dark, at 1440 and 390, into OUT_DIR
 * (default screenshots/env-concepts). BASE_URL is the dev server. `--only a,b` narrows by name.
 *
 *   BASE_URL=http://localhost:5341 node scripts/env-concepts.mjs
 */
/* global window, document -- used inside page.evaluate, which runs in the browser */
import { mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const out = process.env.OUT_DIR ?? join(root, 'screenshots', 'env-concepts');
const base = process.env.BASE_URL ?? 'http://localhost:5341';
const O = 'northwind-group';
const args = process.argv.slice(2);
const option = (name) => {
  const index = args.indexOf(`--${name}`);
  return index >= 0 ? args[index + 1] : undefined;
};
const only = option('only')?.split(',');
const widths = option('width') ? [Number(option('width'))] : [1440, 390];
const themes = option('theme') ? [option('theme')] : ['light', 'dark'];

/** name, path, optional action, whether to take the full page. */
const shots = [
  ['01-list', `/${O}/environments`, null],
  ['02-open-environment', `/${O}/environments/e7k2`, null],
  ['02b-how-it-opened', `/${O}/environments/e7k2`, async (page) => {
    await page.getByRole('button', { name: /Opened in/ }).click();
    await page.getByRole('region', { name: 'The app' }).scrollIntoViewIfNeeded();
  }, false],
  ['03-app-failed', `/${O}/environments/h5ap`, null],
  ['04-network-limited', `/${O}/environments/n3lm`, async (page) => {
    await page.getByRole('heading', { name: 'Network' }).scrollIntoViewIfNeeded();
  }],
  ['05-checkpoints', `/${O}/environments/c5kp`, null],
  ['06-checkpoint-diff', `/${O}/environments/c5kp`, async (page) => {
    await page.getByRole('button', { name: /Show what changed since checkpoint 3/ }).click();
    await page.getByRole('dialog').waitFor();
    await page.waitForTimeout(300);
  }, false],
  ['07-replay', `/${O}/environments/r9cl`, async (page) => {
    await page.getByRole('slider', { name: 'Replay position' }).waitFor();
    await page.getByRole('slider', { name: 'Replay position' }).focus();
    await page.keyboard.press('Home');
    for (let index = 0; index < 15; index += 1) await page.keyboard.press('ArrowRight');
    await page.evaluate(() => document.activeElement?.blur());
    await page.waitForTimeout(300);
  }],
  ['08-replay-person', `/${O}/environments/r9cl`, async (page) => {
    await page.getByRole('slider', { name: 'Replay position' }).focus();
    await page.keyboard.press('Home');
    for (let index = 0; index < 18; index += 1) await page.keyboard.press('ArrowRight');
    await page.evaluate(() => document.querySelector('[aria-label="What happened"]')?.parentElement?.parentElement?.scrollIntoView());
    await page.waitForTimeout(300);
  }, false],
  ['09-prepared-machines', `/${O}/environments/prepared`, null],
  ['10-saved-resume', `/${O}/environments`, async (page) => {
    await page.getByRole('heading', { name: 'Saved' }).scrollIntoViewIfNeeded();
  }],
  ['11-saved-page', `/${O}/environments/d1dm`, null],
  ['12-waking', `/${O}/environments/d1dm`, async (page) => {
    await page.getByRole('button', { name: 'Resume', exact: true }).first().click();
    await page.getByRole('dialog').getByRole('button', { name: 'Resume', exact: true }).click();
    await page.getByText(/Waking/).first().waitFor();
    // two reads in: the machine is rented and booting
    await page.waitForTimeout(13_000);
  }, false],
  ['12b-resume-dialog', `/${O}/environments`, async (page) => {
    await page.getByRole('button', { name: 'Resume Checkout redesign' }).click();
    await page.getByRole('dialog').waitFor();
    await page.waitForTimeout(300);
  }, false],
  ['13-starting-live', `/${O}/environments/q3mn`, null],
  ['14-video-proof', `/${O}/environments/r9cl`, async (page) => {
    await page.getByRole('button', { name: /Play the video/ }).click();
    await page.getByRole('dialog').waitFor();
    await page.waitForTimeout(300);
  }, false],
  ['15-resume-refused', `/${O}/environments?mock=still,environment-full`, async (page) => {
    await page.getByRole('button', { name: 'Resume Checkout redesign' }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Resume', exact: true }).click();
    await page.getByRole('alert').waitFor();
    await page.waitForTimeout(300);
  }, false],
];

const selected = only ? shots.filter(([name]) => only.some((term) => name.includes(term))) : shots;
mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const failures = [];

async function shoot(width, theme) {
  const height = width > 500 ? 900 : 844;
  const context = await browser.newContext({ viewport: { width, height }, colorScheme: theme, deviceScaleFactor: width > 500 ? 1 : 2, reducedMotion: 'reduce' });
  const page = await context.newPage();
  page.on('pageerror', (error) => failures.push(`pageerror: ${error.message}`));
  for (const [name, path, action, full = true] of selected) {
    await page.goto(`${base}/sign-in?mock=still`, { waitUntil: 'domcontentloaded' });
    await page.evaluate(() => {
      sessionStorage.setItem('agentos.mock.scenario', 'still');
      localStorage.removeItem('agentos.mock.signed-out');
    });
    await page.goto(`${base}${path}`, { waitUntil: 'load' }).catch(() => {});
    await page.waitForTimeout(900);
    try {
      if (action) await action(page);
      await page.waitForTimeout(250);
      if (full) await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: join(out, `${name}.${width}.${theme}.png`), fullPage: full });
    } catch (error) {
      failures.push(`${name} at ${width} ${theme}: ${String(error.message).split('\n')[0]}`);
    }
  }
  await context.close();
}

for (const width of widths) for (const theme of themes) await shoot(width, theme);
await browser.close();
const unique = [...new Set(failures)];
if (unique.length) {
  console.error(unique.join('\n'));
  process.exitCode = 1;
}
console.log(`wrote ${selected.length * widths.length * themes.length} shots to ${out}`);
