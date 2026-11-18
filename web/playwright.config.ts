import { defineConfig } from '@playwright/test';

// Several agents share a machine, so each can run the suite on its own port: E2E_PORT=5392.
// Never 5310 by default: the local Docker stack listens there, and this suite must not reach it.
const port = Number(process.env.E2E_PORT ?? 5392);

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  fullyParallel: false,
  reporter: [['list']],
  // The catalog's baselines (e2e/catalog.spec.ts): one set for every machine, so no platform in the name,
  // and a machine without them yet writes them instead of failing.
  snapshotPathTemplate: '{testDir}/__screenshots__/{arg}{ext}',
  updateSnapshots: 'missing',
  use: {
    baseURL: `http://localhost:${port}`,
    channel: 'chrome',
    viewport: { width: 1440, height: 900 },
    serviceWorkers: 'allow',
  },
  webServer: {
    command: `pnpm dev --port ${port}`,
    url: `http://localhost:${port}`,
    reuseExistingServer: Boolean(process.env.E2E_REUSE),
    timeout: 60_000,
  },
});
