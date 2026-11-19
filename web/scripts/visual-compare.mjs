#!/usr/bin/env node
/**
 * Proves a change drew nothing different: photographs the catalog and the main screens on the mock, then
 * compares two sets pixel by pixel. Made for codemods (scripts/codemod-legacy-tokens.mjs), where the
 * class names change and the page must not.
 *
 *   pnpm dev --port 5343                                   (the mock; any free port)
 *   node scripts/visual-compare.mjs shoot --out /tmp/before [--base http://localhost:5343] [--no-tracking]
 *   ... make the change ...
 *   node scripts/visual-compare.mjs shoot --out /tmp/after
 *   node scripts/visual-compare.mjs diff /tmp/before /tmp/after [--out /tmp/diff]
 *
 * Every page is drawn at one fixed moment, in UTC and en-GB, with motion off, and the parts that move on
 * their own (the live view, times, pictures, skeletons, a chart of the mock's real times) are masked, so two runs of the same code match exactly.
 * `--no-tracking` sets letter-spacing to 0 everywhere: a change that only moves letters by a fraction of a
 * pixel then compares equal, and every other difference still shows. `diff` writes a picture of each
 * changed page with the changed pixels in red, and exits 1 when any pixel differs.
 * Uses system Chrome and Playwright only: the comparison runs on a canvas in the browser.
 */
/* global document, window, Image, sessionStorage, localStorage -- used inside page.evaluate */
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { chromium } from '@playwright/test';

const O = 'northwind-group';
const args = process.argv.slice(2);
const option = (name, fallback) => {
  const index = args.indexOf(`--${name}`);
  return index >= 0 ? args[index + 1] : fallback;
};

/** name, path, what to wait for (a role and a name, or text). */
const PAGES = [
  ['catalog', `/${O}/design`, { role: 'table', name: 'Commands' }],
  ['home', `/${O}`, null],
  ['environments', `/${O}/environments`, null],
  ['environment-open', `/${O}/environments/e7k2`, null],
  ['environment-closed', `/${O}/environments/d1dm`, null],
  ['prepared', `/${O}/environments/prepared`, null],
  ['settings', `/${O}/settings`, null],
];
const WIDTHS = [1440, 390];
const THEMES = ['light', 'dark'];

async function open(page, base, path, wait) {
  await page.goto(`${base}/sign-in`, { waitUntil: 'domcontentloaded' });
  await page.evaluate(() => {
    sessionStorage.setItem('agentos.mock.scenario', 'still');
    localStorage.removeItem('agentos.mock.signed-out');
    for (const key of Object.keys(localStorage)) if (key.startsWith('agentos.technical.')) localStorage.removeItem(key);
  });
  await page.goto(`${base}${path}`, { waitUntil: 'load' });
  if (wait) await page.getByRole(wait.role, { name: wait.name }).first().waitFor({ timeout: 15_000 });
  await page.waitForTimeout(1200);
  await page.evaluate(() => document.fonts.ready);
}

async function shoot() {
  const out = option('out');
  if (!out) throw new Error('shoot needs --out <folder>');
  const base = option('base', 'http://localhost:5343');
  const flat = args.includes('--no-tracking');
  mkdirSync(out, { recursive: true });
  const browser = await chromium.launch({ channel: 'chrome' });
  for (const theme of THEMES) {
    for (const width of WIDTHS) {
      const context = await browser.newContext({
        viewport: { width, height: 900 },
        colorScheme: theme,
        reducedMotion: 'reduce',
        timezoneId: 'UTC',
        locale: 'en-GB',
      });
      const page = await context.newPage();
      // One fixed moment, so "4m ago" reads the same on every run.
      await page.clock.setFixedTime(new Date('2026-11-18T14:00:00Z'));
      for (const [name, path, wait] of PAGES) {
        await open(page, base, path, wait);
        if (flat) await page.addStyleTag({ content: '*, *::before, *::after { letter-spacing: 0 !important; }' });
        const mask = [
          page.locator('[data-catalog-entry="src/features/environments/env-cards.tsx"]'),
          page.locator('[aria-label^="Live view of"], canvas, video, time, img, .skeleton'),
          // The mock answers from a service worker, which the page's fixed clock does not reach.
          page.locator('figure[aria-label="Seconds to ready of each recent open"]'),
        ];
        await page.screenshot({ path: join(out, `${name}-${theme}-${width}.png`), fullPage: true, animations: 'disabled', mask, caret: 'hide' });
        process.stdout.write('.');
      }
      await context.close();
    }
  }
  await browser.close();
  process.stdout.write(`\n${out}\n`);
}

/** Runs in the browser: the number of differing pixels, and a picture of them. */
async function compareInPage(page, a, b) {
  return page.evaluate(
    async ([left, right]) => {
      const load = (src) =>
        new Promise((resolve, reject) => {
          const image = new Image();
          image.onload = () => resolve(image);
          image.onerror = reject;
          image.src = src;
        });
      const [one, two] = await Promise.all([load(left), load(right)]);
      if (one.width !== two.width || one.height !== two.height) {
        return { differ: -1, size: `${one.width}x${one.height} against ${two.width}x${two.height}` };
      }
      const pixels = (image) => {
        const canvas = document.createElement('canvas');
        canvas.width = image.width;
        canvas.height = image.height;
        const context = canvas.getContext('2d');
        context.drawImage(image, 0, 0);
        return context.getImageData(0, 0, image.width, image.height);
      };
      const [p, q] = [pixels(one), pixels(two)];
      let differ = 0;
      const canvas = document.createElement('canvas');
      canvas.width = one.width;
      canvas.height = one.height;
      const context = canvas.getContext('2d');
      context.globalAlpha = 0.25;
      context.drawImage(two, 0, 0);
      const picture = context.getImageData(0, 0, one.width, one.height);
      for (let index = 0; index < p.data.length; index += 4) {
        if (p.data[index] !== q.data[index] || p.data[index + 1] !== q.data[index + 1] || p.data[index + 2] !== q.data[index + 2] || p.data[index + 3] !== q.data[index + 3]) {
          differ += 1;
          picture.data.set([255, 0, 0, 255], index);
        }
      }
      if (!differ) return { differ };
      context.putImageData(picture, 0, 0);
      return { differ, picture: canvas.toDataURL('image/png') };
    },
    [a, b],
  );
}

async function diff() {
  const [left, right] = args.slice(1, 3);
  if (!left || !right) throw new Error('diff needs two folders');
  const out = option('out');
  if (out) mkdirSync(out, { recursive: true });
  const browser = await chromium.launch({ channel: 'chrome' });
  const page = await browser.newPage();
  const url = (path) => `data:image/png;base64,${readFileSync(path).toString('base64')}`;
  let changed = 0;
  for (const name of readdirSync(left).filter((file) => file.endsWith('.png')).sort()) {
    if (!existsSync(join(right, name))) {
      console.log(`${name}: missing in ${right}`);
      changed += 1;
      continue;
    }
    const result = await compareInPage(page, url(join(left, name)), url(join(right, name)));
    if (result.differ === 0) {
      console.log(`${name}: same`);
      continue;
    }
    changed += 1;
    console.log(`${name}: ${result.differ < 0 ? `size ${result.size}` : `${result.differ} pixels differ`}`);
    if (out && result.picture) writeFileSync(join(out, name), Buffer.from(result.picture.split(',')[1], 'base64'));
  }
  await browser.close();
  console.log(changed ? `${changed} pictures differ` : 'every picture is the same');
  process.exit(changed ? 1 : 0);
}

const command = args[0];
if (command === 'shoot') await shoot();
else if (command === 'diff') await diff();
else {
  console.error('usage: visual-compare.mjs shoot --out <folder> | diff <before> <after> [--out <folder>]');
  process.exit(2);
}
