/**
 * The Mono token set has one source, web/tokens/build-tokens.py, which writes web/tokens/tokens.json
 * and src/styles/tokens.css. These tests keep the two outputs in step with each other and with the
 * script, and run the script's contrast and colour-blind checks.
 */
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const WEB = join(__dirname, '..', '..');
const json = JSON.parse(readFileSync(join(WEB, 'tokens', 'tokens.json'), 'utf8')) as Record<string, Tree>;
const css = readFileSync(join(__dirname, 'tokens.css'), 'utf8');

interface Tree {
  [key: string]: Tree | unknown;
}
interface Token {
  $type: string;
  $value: unknown;
  $extensions?: { agentos?: { cssVar?: string } };
}

/** The declarations of one block of tokens.css, as name to value. */
function block(source: string): Map<string, string> {
  const out = new Map<string, string>();
  for (const match of source.matchAll(/(--[a-z0-9-]+):\s*([^;]+);/g)) out.set(match[1]!, match[2]!.trim());
  return out;
}

const lightSource = css.slice(css.indexOf(':root {'), css.indexOf('@media (prefers-color-scheme: dark)'));
const systemDarkSource = css.slice(css.indexOf('@media (prefers-color-scheme: dark)'), css.indexOf(":root[data-theme='dark']"));
const pinnedDarkSource = css.slice(css.indexOf(":root[data-theme='dark']"), css.indexOf('@media (prefers-reduced-motion'));
const light = block(lightSource);
const systemDark = block(systemDarkSource);
const pinnedDark = block(pinnedDarkSource);

function leaves(tree: Tree, path: string[] = []): Array<[string[], Token]> {
  return Object.entries(tree).flatMap(([key, value]): Array<[string[], Token]> => {
    if (key.startsWith('$') || typeof value !== 'object' || value === null) return [];
    if ('$value' in value) return [[[...path, key], value as Token]];
    return leaves(value as Tree, [...path, key]);
  });
}

/** What tokens.css should say for a colour token. */
function cssColour(value: unknown): string {
  if (typeof value === 'string') {
    // A reference: {color.neutral.4} is var(--neutral-4), {color.surface.card} is var(--surface-card).
    const parts = value.slice(1, -1).split('.');
    return `var(--${(parts[0] === 'color' ? parts.slice(1) : parts).join('-')})`;
  }
  const colour = value as { components: number[]; alpha?: number; hex: string };
  if (colour.alpha === undefined) return colour.hex;
  const [r, g, b] = colour.components.map((c) => Math.round(c * 255));
  return `rgb(${r} ${g} ${b} / ${colour.alpha})`;
}

const px = (value: unknown) => `${(value as { value: number }).value}px`;

describe('the Mono tokens: tokens.json and tokens.css agree', () => {
  it('has the dark values twice, once for the system and once for a pinned theme, and they match', () => {
    expect(systemDark.size).toBeGreaterThan(80);
    expect(Object.fromEntries(systemDark)).toEqual(Object.fromEntries(pinnedDark));
  });

  it('writes every colour of both themes with the value tokens.json holds', () => {
    for (const [theme, declared] of [['light', light], ['dark', systemDark]] as const) {
      const colours = leaves(json[theme] as Tree).filter(([, token]) => token.$type === 'color');
      expect(colours.length).toBeGreaterThan(80);
      for (const [path, token] of colours) {
        const name = token.$extensions?.agentos?.cssVar ?? `--${path.slice(1).join('-')}`;
        expect([theme, name, declared.get(name)]).toEqual([theme, name, cssColour(token.$value)]);
      }
    }
  });

  it('writes the colours both themes share, the space, radius, size and type tokens', () => {
    for (const [path, token] of leaves(json.base as Tree)) {
      const [group] = path;
      if (token.$type === 'color') {
        const name = token.$extensions?.agentos?.cssVar ?? '';
        expect([name, light.get(name)]).toEqual([name, cssColour(token.$value)]);
      } else if (token.$type === 'dimension' && ['space', 'radius', 'control-height', 'row-height'].includes(group!)) {
        const name = `--${path.join('-')}`;
        expect([name, light.get(name)]).toEqual([name, px(token.$value)]);
      } else if (token.$type === 'typography') {
        const role = `--type-${path[1]}`;
        const value = token.$value as Record<string, unknown>;
        const size = String(value.fontSize).slice(1, -1).split('.')[1];
        const lineHeight = String(value.lineHeight).slice(1, -1).split('.')[1];
        expect([role, light.get(`${role}-font-size`)]).toEqual([role, `var(--font-${size})`]);
        expect([role, light.get(`${role}-line-height`)]).toEqual([role, `var(--line-height-${lineHeight})`]);
        expect([role, light.get(`${role}-tracking`)]).toEqual([role, px(value.letterSpacing)]);
      } else if (token.$type === 'duration') {
        const name = `--${path.join('-')}`;
        expect([name, light.get(name)]).toEqual([name, `${(token.$value as { value: number }).value}ms`]);
      }
    }
  });

  it('names the faces the app registers from @fontsource-variable', () => {
    expect(light.get('--font-family-sans')).toMatch(/^'Geist Variable',/);
    expect(light.get('--font-family-mono')).toMatch(/^'Geist Mono Variable',/);
  });

  it('is what build-tokens.py writes, and passes its contrast and colour-blind checks', () => {
    const run = spawnSync('python3', [join(WEB, 'tokens', 'build-tokens.py'), '--check'], { encoding: 'utf8' });
    expect(run.error).toBeUndefined();
    const failures = run.stdout.split('\n').filter((line) => line.startsWith('FAIL'));
    expect(failures).toEqual([]);
    expect(run.stdout).toMatch(/pairs \d+ fails 0/);
    expect(run.status).toBe(0);
  });
});
