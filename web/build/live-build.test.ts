/**
 * Builds the live app for real, into a folder of its own, and reads what it wrote. The concept drawings
 * of a computer's forms (fake key and password boxes, `/computer-form`, `/computer-form/ai-key`) are mock
 * mode only: no live build may hold them, and no mock code either. The route list is in src/app/router.tsx.
 */
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readdirSync, readFileSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { CONCEPT_TEXT, findMockCode } from './guard';

const root = join(__dirname, '..');

function build(): string {
  const out = mkdtempSync(join(tmpdir(), 'agentos-live-'));
  // NODE_ENV as a real build has it: under the test runner it is `test`, which builds in dev mode.
  const env = { ...process.env, VITE_API_MODE: 'live', NODE_ENV: 'production' };
  execFileSync(process.execPath, [join(root, 'node_modules/vite/bin/vite.js'), 'build', '--outDir', out, '--emptyOutDir', '--logLevel', 'error'], { cwd: root, env, stdio: 'pipe' });
  return out;
}

function text(dir: string): string {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return [text(path)];
    return /\.(js|mjs|html|css)$/.test(path) ? [readFileSync(path, 'utf8')] : [];
  }).join('\n');
}

describe('the live build', () => {
  it('holds no concept drawing of a computer form, no route to one, no mock code and no catalog', () => {
    const out = build();
    try {
      const all = text(out);
      for (const words of CONCEPT_TEXT) expect(all, words).not.toContain(words);
      expect(all).not.toContain('/computer-form/ai-key');
      expect(all).not.toContain('"/computer-form"');
      expect(findMockCode(out)).toEqual([]);
      // The design system's catalog is dev only (src/app/routes/catalog.tsx): none of it ships.
      expect(all).not.toContain('agentos-design-catalog');
    } finally {
      rmSync(out, { recursive: true, force: true });
    }
  }, 180_000);
});
