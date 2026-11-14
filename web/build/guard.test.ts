import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { assertCleanBuild, assertLiveMode, CONCEPT_TEXT, findMockCode, MOCK_MARKER, MockBuildError, mockGuard } from './guard';

describe('the build guard (security review 16)', () => {
  it.each([{ VITE_API_MODE: 'mock' }, {}, { VITE_API_MODE: 'development' }])('refuses to build with %j', (env) => {
    expect(() => assertLiveMode(env)).toThrow(MockBuildError);
  });

  it('builds against the real API', () => {
    expect(() => assertLiveMode({ VITE_API_MODE: 'live' })).not.toThrow();
  });

  it('finds mock code that reached a bundle, and passes a clean one', () => {
    const dist = mkdtempSync(join(tmpdir(), 'agentos-guard-'));
    mkdirSync(join(dist, 'assets'));
    writeFileSync(join(dist, 'index.html'), '<script src="/assets/app.js"></script>');
    writeFileSync(join(dist, 'assets', 'app.js'), 'console.log("panel")');
    expect(findMockCode(dist)).toEqual([]);
    writeFileSync(join(dist, 'assets', 'mock.js'), `const m="${MOCK_MARKER}"`);
    writeFileSync(join(dist, 'assets', 'msw.js'), 'register("/mockServiceWorker.js")');
    writeFileSync(join(dist, 'mockServiceWorker.js'), '');
    expect(findMockCode(dist).map((path) => path.slice(dist.length))).toEqual(
      expect.arrayContaining(['/assets/mock.js', '/assets/msw.js', '/mockServiceWorker.js']),
    );
  });

  it('finds a concept drawing of a computer form (fake key or password box) in a build', () => {
    const dist = mkdtempSync(join(tmpdir(), 'agentos-concept-'));
    writeFileSync(join(dist, 'app.js'), 'console.log("panel")');
    expect(findMockCode(dist)).toEqual([]);
    writeFileSync(join(dist, 'key.js'), `const label = "${CONCEPT_TEXT[0]}"`);
    expect(findMockCode(dist).map((path) => path.slice(dist.length))).toEqual(['/key.js']);
  });

  it('checks the folder a build writes to, whatever it is called, and removes the mock workers from it', () => {
    const out = mkdtempSync(join(tmpdir(), 'agentos-outdir-'));
    writeFileSync(join(out, 'app.js'), 'console.log("panel")');
    writeFileSync(join(out, 'mockServiceWorker.js'), '');
    expect(() => assertCleanBuild(out)).not.toThrow();
    writeFileSync(join(out, 'pages.js'), `const page = "${CONCEPT_TEXT[1]}"`);
    expect(() => assertCleanBuild(out)).toThrow(MockBuildError);
  });

  it('the plugin checks the output folder it was configured with, not only dist', () => {
    const root = mkdtempSync(join(tmpdir(), 'agentos-root-'));
    const out = join(root, 'custom-build');
    mkdirSync(out);
    writeFileSync(join(out, 'app.js'), `const page = "${CONCEPT_TEXT[0]}"`);
    const guard = mockGuard('live', root) as unknown as { configResolved: (config: { build: { outDir: string } }) => void; closeBundle: () => void };
    guard.configResolved({ build: { outDir: out } });
    expect(() => guard.closeBundle()).toThrow(MockBuildError);
  });
});
