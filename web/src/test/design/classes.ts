/**
 * The design rules for one class name (`bg-card`, `md:p-[7px]`, `text-red-500`): is it a token, and if
 * not, what to write instead. The token keys come from tokens/theme-keys.json, which build-tokens.py
 * writes from the same table as the Tailwind theme, so a class this file accepts is one that exists.
 */
import keys from '../../../tokens/theme-keys.json';

export type ClassRule = 'raw-colour' | 'arbitrary-value' | 'not-a-token' | 'legacy-token';

export interface ClassFinding {
  rule: ClassRule;
  fix: string;
}

const COLORS = new Set(keys.colors);
const LEGACY_COLORS: Record<string, string> = keys.legacyColors;
const LEGACY_TEXT: Record<string, string> = keys.legacyText;
const ROLES = new Set(keys.text);
const RADII = new Set(keys.radius);
const SHADOWS = new Set(keys.shadow);
const LAYERS = new Map(Object.entries(keys.z).map(([name, value]) => [value, name]));

const PALETTE =
  'red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose|slate|gray|grey|zinc|neutral|stone|white|black';
const COLOUR_UTILITY = 'bg|text|border(?:-[xytrblse])?|fill|stroke|ring|ring-offset|outline|divide|decoration|accent|caret|from|via|to|shadow|placeholder';
const PALETTE_CLASS = new RegExp(`^(${COLOUR_UTILITY})-((?:${PALETTE})(?:-\\d{2,3})?)(?:/\\d+)?$`);
/** Any colour utility followed by a word: `bg-card`, `text-muted`, `border-ok-line`. */
const COLOUR_KEY = new RegExp(`^(${COLOUR_UTILITY})-([a-z][a-z0-9-]*?)(?:/\\d+)?$`);

/** Utilities whose value is a design decision: colour, type, space, radius, depth, layer, motion. */
const DESIGN_UTILITY =
  /^(bg|text|font|leading|tracking|p[xytrblse]?|m[xytrblse]?|gap(?:-[xy])?|space-[xy]|rounded(?:-[a-z]+)?|shadow|inset-shadow|drop-shadow|border(?:-[xytrblse])?|outline(?:-offset)?|ring(?:-offset)?|z|duration|delay|ease|opacity|fill|stroke|accent|caret|decoration|divide(?:-[xy])?|blur|from|via|to)$/;
/** Arbitrary CSS properties (`[font-size:13px]`) that are design values. */
const DESIGN_PROPERTY =
  /^(color|background(?:-color)?|font(?:-size|-weight|-family)?|line-height|letter-spacing|padding.*|margin.*|gap|border.*|box-shadow|outline.*|z-index|transition.*|animation.*|opacity|fill|stroke)$/;
/** An arbitrary value that only names a token: `bg-[var(--wait-soft)]`, `border-[color:var(--x)]`. */
const ONLY_A_TOKEN = /^(?:[a-z-]+:)?var\(--[a-z0-9-]+\)$/;

const TAILWIND_SIZES = /^text-(xs|sm|base|lg|[2-9]?xl)$/;
const TAILWIND_RADII = /^rounded(?:-[trblse]{1,2})?-(xs|2xl|3xl|4xl)$/;
const TAILWIND_SHADOWS = /^shadow-(2xs|xs|sm|md|lg|xl|2xl|inner)$/;

/** The utility itself, with variants (`md:`, `hover:`, `data-[x]:`) and `!` taken off. */
export function baseOf(token: string): string {
  let depth = 0;
  let start = 0;
  for (let index = 0; index < token.length; index += 1) {
    const char = token[index];
    if (char === '[' || char === '(') depth += 1;
    else if (char === ']' || char === ')') depth -= 1;
    else if (char === ':' && depth === 0) start = index + 1;
  }
  return token.slice(start).replace(/^!|!$/g, '').replace(/^-/, '');
}

function colourFinding(base: string): ClassFinding | null {
  const palette = PALETTE_CLASS.exec(base);
  if (palette && !COLORS.has(palette[2]!) && !(palette[2]! in LEGACY_COLORS)) {
    return {
      rule: 'raw-colour',
      fix: `\`${base}\` is Tailwind's palette, which is cleared here. Use a token: text-fg, text-muted, text-bad, bg-card, bg-inset, border-line (web/design-system/foundations.md)`,
    };
  }
  const key = COLOUR_KEY.exec(base);
  if (key && key[2]! in LEGACY_COLORS) {
    return { rule: 'legacy-token', fix: `\`${base}\` is a pre-Mono name: write \`${key[1]}-${LEGACY_COLORS[key[2]!]}\`` };
  }
  return null;
}

function arbitraryFinding(base: string): ClassFinding | null {
  const property = /^\[([a-z-]+):(.+)\]$/.exec(base);
  if (property) {
    return DESIGN_PROPERTY.test(property[1]!) && !ONLY_A_TOKEN.test(property[2]!)
      ? { rule: 'arbitrary-value', fix: `\`${base}\` writes a design value by hand. Use a token class, or a component that has it` }
      : null;
  }
  const arbitrary = /^([a-z][a-z0-9-]*?)-\[(.+)\]$/.exec(base);
  if (!arbitrary || !DESIGN_UTILITY.test(arbitrary[1]!) || ONLY_A_TOKEN.test(arbitrary[2]!)) return null;
  const [, utility, value] = arbitrary;
  const hint = /^#|rgb|hsl|color-mix|oklch/.test(value!)
    ? 'a colour token (bg-card, text-muted, border-line ...)'
    : utility === 'text' || utility === 'leading' || utility === 'tracking' || utility === 'font'
      ? 'a type role (text-body, text-secondary, text-caption, text-code ...)'
      : utility!.startsWith('rounded')
        ? 'rounded-sm (4), rounded-md (6), rounded-lg (8), rounded-xl (10) or rounded-full'
        : utility === 'shadow'
          ? 'shadow-lift, shadow-popover or shadow-dialog; a ring or an edge is a border, never a shadow'
          : utility === 'z'
            ? `a layer: ${[...LAYERS].map(([n, name]) => `z-${n} (${name})`).join(', ')}`
            : 'a step on the 4 px grid (p-4, gap-3 ...) or a token';
  return { rule: 'arbitrary-value', fix: `\`${base}\` is an arbitrary value. Use ${hint}` };
}

function scaleFinding(base: string): ClassFinding | null {
  if (TAILWIND_SIZES.test(base)) return { rule: 'not-a-token', fix: `\`${base}\` does not exist here (Tailwind's sizes are cleared). Use a type role: text-body, text-secondary, text-caption, text-section-title` };
  if (TAILWIND_RADII.test(base)) return { rule: 'not-a-token', fix: `\`${base}\` does not exist here. Use rounded-sm, rounded-md, rounded-lg, rounded-xl or rounded-full` };
  if (TAILWIND_SHADOWS.test(base)) return { rule: 'not-a-token', fix: `\`${base}\` does not exist here. Use shadow-lift (cards), shadow-popover or shadow-dialog` };
  const z = /^z-(\d+)$/.exec(base);
  if (z && !LAYERS.has(Number(z[1]))) return { rule: 'not-a-token', fix: `\`${base}\` is not a layer. Use ${[...LAYERS].map(([n, name]) => `z-${n} (${name})`).join(', ')}` };
  const duration = /^(duration|delay)-(\d+)$/.exec(base);
  if (duration) return { rule: 'not-a-token', fix: `\`${base}\` is not a duration token. Use ${duration[1]}-(--duration-fast) (100), -base (150), -slow (200) or -settle (280)` };
  const size = /^text-(\d+)$/.exec(base);
  if (size && size[1]! in LEGACY_TEXT) return { rule: 'legacy-token', fix: `\`${base}\` is the old size scale: use a type role (${LEGACY_TEXT[size[1]!]})` };
  const role = /^text-([a-z-]+)$/.exec(base);
  if (role && /title|display|lede|caption|eyebrow|micro|stat|number|code|label|body|secondary|header/.test(role[1]!) && !ROLES.has(role[1]!) && !COLORS.has(role[1]!)) {
    return { rule: 'not-a-token', fix: `\`${base}\` is not a type role. The roles: ${[...ROLES].map((r) => `text-${r}`).join(', ')}` };
  }
  const radius = /^rounded(?:-[trblse]{1,2})?-([a-z0-9]+)$/.exec(base);
  if (radius && !RADII.has(radius[1]!) && !/^[trblse]{1,2}$/.test(radius[1]!)) return { rule: 'not-a-token', fix: `\`${base}\` is not a radius. Use rounded-sm, -md, -lg, -xl or -full` };
  const shadow = /^shadow-([a-z]+)$/.exec(base);
  if (shadow && !SHADOWS.has(shadow[1]!) && !COLORS.has(shadow[1]!)) return { rule: 'not-a-token', fix: `\`${base}\` is not a shadow. Use shadow-lift, shadow-popover or shadow-dialog` };
  return null;
}

/** What is wrong with one class name, or null when it is on the system. */
export function checkClass(token: string): ClassFinding | null {
  if (!/^[!-]?[a-z0-9[@*]/.test(token) || token.length > 160) return null;
  const base = baseOf(token);
  return colourFinding(base) ?? arbitraryFinding(base) ?? scaleFinding(base);
}

/** The var(--x) and utility-(--x) names a class refers to, to check that each is a real token. */
export function varsIn(token: string): string[] {
  // A name cut by a template (`var(--status-${tone}-dot)`) ends in a dash: it is built, not named.
  return [...token.matchAll(/(?:var\(|\()(--[a-z0-9-]+)/g)].map((match) => match[1]!).filter((name) => !name.endsWith('-'));
}
