import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

const SRC = join(__dirname, '..');

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? files(path) : [path];
  });
}

const sources = files(SRC).filter((path) => /\.(ts|tsx|css)$/.test(path));

describe('source rules from DESIGN.md', () => {
  it('has no em dash anywhere, including comments', () => {
    const dash = String.fromCharCode(0x2014);
    expect(sources.filter((path) => readFileSync(path, 'utf8').includes(dash))).toEqual([]);
  });

  it('stays on the 4 px grid: no half-step spacing utilities', () => {
    const offGrid = /\b(?:gap|p|px|py|pt|pb|pl|pr|m|mx|my|mt|mb|ml|mr|space-[xy])-\d+\.5\b/;
    const screens = sources.filter((path) => path.endsWith('.tsx'));
    expect(screens.filter((path) => offGrid.test(readFileSync(path, 'utf8')))).toEqual([]);
  });

  it('keeps every colour in tokens.css: no hex in a screen', () => {
    const features = sources.filter((path) => path.includes('/features/') && !/\.test\.tsx?$/.test(path));
    // The one exception is the QR code, which must stay dark on light in both themes.
    const offenders = features.filter((path) => !path.endsWith('onboarding.tsx') && /#[0-9a-fA-F]{6}\b/.test(readFileSync(path, 'utf8')));
    expect(offenders).toEqual([]);
  });

  it('keeps the primitives on tokens too: no hex in components/ui, except the specimen\'s drawn page', () => {
    // The drawn product page is another brand's picture (data, marked data-studio="ignore").
    const ui = sources.filter((path) => path.includes('/components/ui/') && !path.endsWith('/specimen/shot.ts') && !/\.test\.tsx?$/.test(path));
    expect(ui.filter((path) => /#[0-9a-fA-F]{6}\b/.test(readFileSync(path, 'utf8')))).toEqual([]);
  });

  it('writes the Mono token set as plain values per theme, never light-dark(), so studio can read it', () => {
    // web/DESIGN.md, Tokens: tokens.css is generated with a prefers-color-scheme block and a data-theme
    // block. Only the app-only tokens in aliases.css may use light-dark().
    const tokens = sources.find((path) => path.endsWith('/styles/tokens.css'));
    expect(tokens).toBeDefined();
    expect(readFileSync(tokens!, 'utf8')).not.toMatch(/light-dark\(/);
  });

  it('never writes HTML into the page: node and page text render as text only', () => {
    const app = sources.filter((path) => /\.tsx?$/.test(path) && !/\.test\.tsx?$/.test(path));
    expect(app.filter((path) => /dangerouslySetInnerHTML|\.innerHTML\s*=|insertAdjacentHTML/.test(readFileSync(path, 'utf8')))).toEqual([]);
  });

  it('never lets a screen import the mock backend', () => {
    const screens = sources.filter((path) => /\/(features|components|app)\//.test(path) && !path.includes('.test.'));
    expect(screens.filter((path) => /from '@\/mocks|from '\.\.?\/.*mocks/.test(readFileSync(path, 'utf8')))).toEqual([]);
  });

  it('has no password field, and holds no password, passphrase or key in state, anywhere in the app', () => {
    // The node serves every form that takes a secret (docs/WEB-NODE.md). The app never types one.
    const app = sources.filter((path) => /\.tsx?$/.test(path) && !/\.test\.tsx?$/.test(path));
    expect(app.filter((path) => /type=["']password["']/.test(readFileSync(path, 'utf8')))).toEqual([]);
    // A quoted value is data (the vault kind 'password' is a name), so only the code around it is read.
    const held = /useState[^\n]*(password|passphrase|secret|api_?key|totp)/i;
    const unquoted = (line: string) => line.replace(/'[^']*'|"[^"]*"|`[^`]*`/g, "''");
    expect(app.filter((path) => readFileSync(path, 'utf8').split('\n').some((line) => held.test(unquoted(line))))).toEqual([]);
  });
});
