/**
 * Keeps the mock backend out of every build (security review 16). The mock answers any sign-in
 * and stubs `navigator.credentials`, so a bundle holding it would let anyone in. `vite build`
 * refuses to start in mock mode, whatever `--mode` says, and checks what it wrote before it ends.
 */
import { readdirSync, readFileSync, rmSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { loadEnv } from 'vite';
import type { Plugin } from 'vite';

/** Written into the mock's start-up code, so a bundle that holds it can be recognised. */
export const MOCK_MARKER = 'agentos-mock-backend';

/** Only the mock uses these. Neither may be in a build. */
const MOCK_FILES = ['mockServiceWorker.js', 'sw-mock.js'];

/**
 * Words only the concept drawings of a computer's own forms (`/computer-form`, `/computer-form/ai-key`)
 * have: fake key and password boxes, for judging the design in mock mode. No live build may hold them.
 */
export const CONCEPT_TEXT = ['pretend to paste a key', 'pretend to paste the 16 letters'] as const;

export class MockBuildError extends Error {
  override name = 'MockBuildError';
}

export function assertLiveMode(env: Record<string, string | undefined>): void {
  if (env.VITE_API_MODE !== 'live') {
    throw new MockBuildError(
      `Refusing to build with VITE_API_MODE=${env.VITE_API_MODE ?? '(unset)'}. A build must use the real API: set VITE_API_MODE=live.`,
    );
  }
}

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? files(path) : [path];
  });
}

/** Paths under `dir` that hold mock code. Empty means the build is clean. */
export function findMockCode(dir: string): string[] {
  return files(dir).filter((path) => {
    if (MOCK_FILES.some((name) => path.endsWith(name))) return true;
    if (!/\.(js|mjs|html)$/.test(path)) return false;
    const text = readFileSync(path, 'utf8');
    return text.includes(MOCK_MARKER) || text.includes('mockServiceWorker') || CONCEPT_TEXT.some((words) => text.includes(words));
  });
}

/**
 * Checks the folder a build wrote to, whatever it is called (`--outDir` included). The mock's workers
 * arrive with public/, which is copied whole, so they are removed here first. Throws when mock code is left.
 */
export function assertCleanBuild(dir: string): void {
  for (const name of MOCK_FILES) rmSync(join(dir, name), { force: true });
  const found = findMockCode(dir);
  if (found.length) throw new MockBuildError(`Mock code reached the build: ${found.join(', ')}`);
}

export function mockGuard(mode: string, root: string): Plugin {
  // The folder the build writes to: `build.outDir` (dist by default, or whatever --outDir names).
  let outDir = join(root, 'dist');
  return {
    name: 'agentos-mock-guard',
    apply: 'build',
    configResolved(config) {
      assertLiveMode({ ...loadEnv(mode, root, 'VITE_') });
      outDir = resolve(root, config.build.outDir);
    },
    closeBundle() {
      assertCleanBuild(outDir);
    },
  };
}
