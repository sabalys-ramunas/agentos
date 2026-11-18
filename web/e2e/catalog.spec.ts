/**
 * The design system's catalog (/$org/design, web/DESIGN-SYSTEM.md), photographed group by group in both
 * themes at 1440 and 390, so a change to a component shows as a picture that changed. The baselines are
 * in e2e/__screenshots__; after a change you meant, `pnpm test:e2e catalog --update-snapshots` takes new
 * ones, and the diff in the pull request is the review.
 */
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const GROUPS = ['foundations', 'layout', 'data', 'feedback', 'actions', 'overlays', 'instrument', 'composites'];

async function open(page: Page) {
  // One fixed moment, so "4m ago" and "closes at 16:21" read the same on every run and every machine.
  await page.clock.setFixedTime(new Date('2026-11-18T14:00:00Z'));
  await page.goto('/sign-in');
  await page.evaluate(() => {
    sessionStorage.setItem('agentos.mock.scenario', 'still');
    localStorage.removeItem('agentos.mock.signed-out');
  });
  await page.goto('/northwind-group/design');
  await expect(page.getByRole('heading', { level: 1, name: 'The design system' })).toBeVisible();
  // The composites read the mock; wait until they have drawn.
  await expect(page.getByRole('table', { name: 'Commands' })).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
}

for (const scheme of ['light', 'dark'] as const) {
  for (const width of [1440, 390]) {
    test.describe(`the catalog, ${scheme}, ${width} wide`, () => {
      test.use({ colorScheme: scheme, viewport: { width, height: 900 }, timezoneId: 'UTC', locale: 'en-GB' });

      test('every group looks as it did, and nothing is wider than the screen', async ({ page }) => {
        await open(page);
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
        // The live view draws a clock and moving pictures: it is shown, but not compared.
        const live = page.locator('[data-catalog-entry="src/features/environments/env-cards.tsx"]').first();
        for (const group of GROUPS) {
          const section = page.locator(`section#${group}`);
          await section.scrollIntoViewIfNeeded();
          await expect(section).toHaveScreenshot(`${group}-${scheme}-${width}.png`, {
            animations: 'disabled',
            mask: [live, page.locator('time'), page.locator('img')],
            maxDiffPixelRatio: 0.02,
          });
        }
      });
    });
  }
}
