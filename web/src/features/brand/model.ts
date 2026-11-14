/**
 * A profile's brand as data: tokens resolved through their aliases, sorted into colour, type, spacing,
 * corners and the rest, colours paired light and dark when their names say so, what a waiting draft
 * changes, and the tokens a Button can honestly be drawn from. No React here, and no colour of our own:
 * every value comes from the profile.
 */
import type { BrandDraft, BrandGuideline, BrandProfile, BrandToken } from '@/api/types';
import { paintable } from '@/features/checks/model';

export { paintable };

const ALIAS = /^\{([^}]+)\}$/;

/** A token's value with aliases followed (`{color.paper}` to its colour), at most 8 steps. */
export function resolver(tokens: BrandToken[]): (value: string) => string {
  const byName = new Map(tokens.map((token) => [token.name, token.value]));
  return (value: string) => {
    let current = value;
    for (let step = 0; step < 8; step++) {
      const match = ALIAS.exec(current.trim());
      const next = match?.[1] ? byName.get(match[1]) : undefined;
      if (next === undefined) return current;
      current = next;
    }
    return current;
  };
}

export type Group = 'colour' | 'type' | 'space' | 'radius' | 'other';

export function groupOf(token: BrandToken): Group {
  const name = token.name.toLowerCase();
  const type = (token.type ?? '').toLowerCase();
  if (type === 'color' || /^colou?rs?\./.test(name)) return 'colour';
  if (/radius|rounded|corner/.test(name)) return 'radius';
  if (/^(font|type|typography|text|line-?height|letter)/.test(name) || /^font|typography|lineheight|letterspacing/.test(type)) return 'type';
  if (/^(space|spacing|gap|size\.space)/.test(name)) return 'space';
  return 'other';
}

export function grouped(tokens: BrandToken[]): Record<Group, BrandToken[]> {
  const out: Record<Group, BrandToken[]> = { colour: [], type: [], space: [], radius: [], other: [] };
  for (const token of tokens) out[groupOf(token)].push(token);
  return out;
}

export interface ColourPair {
  /** The token name with its light or dark part taken out. */
  name: string;
  light: BrandToken | null;
  dark: BrandToken | null;
}

const MODE = /(^|[.\-_])(light|dark)(?=$|[.\-_])/i;

/** Colours whose names carry `light` or `dark` paired by the rest of the name; the others alone. */
export function pairColours(tokens: BrandToken[]): { pairs: ColourPair[]; single: BrandToken[] } {
  const pairs = new Map<string, ColourPair>();
  const single: BrandToken[] = [];
  for (const token of tokens) {
    const match = MODE.exec(token.name);
    if (!match) {
      single.push(token);
      continue;
    }
    const base = token.name.replace(MODE, '').replace(/^[.\-_]+|[.\-_]+$/g, '').replace(/([.\-_]){2,}/g, '$1');
    const pair = pairs.get(base) ?? { name: base, light: null, dark: null };
    if (match[2]?.toLowerCase() === 'dark') pair.dark = token;
    else pair.light = token;
    pairs.set(base, pair);
  }
  return { pairs: [...pairs.values()], single };
}

/** A dimension token in px for drawing a bar or a corner, or null when it is not a plain length. */
export function pixels(value: string): number | null {
  const match = /^(-?\d+(?:\.\d+)?)(px|rem)?$/.exec(value.trim());
  if (!match) return null;
  const number = Number(match[1]);
  return match[2] === 'rem' ? number * 16 : number;
}

export interface TokenChange {
  name: string;
  before: string | null;
  after: string | null;
  colour: boolean;
}

/** What the draft changes in the tokens, from its own tokens beside today's. Null when the node sent none. */
export function tokenChanges(profile: BrandProfile, draft: BrandDraft): TokenChange[] | null {
  if (!draft.tokens) return null;
  const now = profile.brand?.tokens ?? [];
  const resolveNow = resolver(now);
  const resolveNext = resolver(draft.tokens);
  const before = new Map(now.map((token) => [token.name, token]));
  const after = new Map(draft.tokens.map((token) => [token.name, token]));
  const out: TokenChange[] = [];
  for (const [name, token] of after) {
    const old = before.get(name);
    if (old && old.value === token.value) continue;
    out.push({ name, before: old ? resolveNow(old.value) : null, after: resolveNext(token.value), colour: groupOf(token) === 'colour' });
  }
  for (const [name, token] of before) {
    if (!after.has(name)) out.push({ name, before: resolveNow(token.value), after: null, colour: groupOf(token) === 'colour' });
  }
  return out;
}

export interface GuidelineChange {
  id: string;
  kind: 'added' | 'removed' | 'changed';
  before: BrandGuideline | null;
  after: BrandGuideline | null;
}

export function guidelineChanges(profile: BrandProfile, draft: BrandDraft): GuidelineChange[] | null {
  if (!draft.guidelines) return null;
  const before = new Map(profile.guidelines.map((item) => [item.id, item]));
  const after = new Map(draft.guidelines.map((item) => [item.id, item]));
  const out: GuidelineChange[] = [];
  for (const [id, item] of after) {
    const old = before.get(id);
    if (!old) out.push({ id, kind: 'added', before: null, after: item });
    else if (JSON.stringify(old) !== JSON.stringify(item)) out.push({ id, kind: 'changed', before: old, after: item });
  }
  for (const [id, item] of before) if (!after.has(id)) out.push({ id, kind: 'removed', before: item, after: null });
  return out;
}

/** How a guideline is checked, in words: "contrast.text at 4.5", or advice. */
export function guidelineRule(item: BrandGuideline): string {
  if (item.advisory || !item.check) return 'Advice for agents and reviewers. Not measured.';
  return `Measured with ${item.check}${item.threshold !== null ? ` at ${item.threshold}` : ''} on every check.`;
}

export interface DrawnButton {
  background: string;
  color: string;
  radius: string | null;
  paddingX: string | null;
  paddingY: string | null;
  font: string | null;
  size: string | null;
  /** The tokens it is drawn from, by name, so the label says exactly where each part came from. */
  from: string[];
}

function pick(tokens: BrandToken[], patterns: RegExp[]): BrandToken | undefined {
  for (const pattern of patterns) {
    const found = tokens.find((token) => pattern.test(token.name));
    if (found) return found;
  }
  return undefined;
}

/**
 * A Button drawn from the profile's own tokens, only when the profile has both a fill and a text colour
 * for it. Every part comes from a named token; a part with no token is left to the browser's default.
 */
export function drawnButton(profile: BrandProfile): DrawnButton | null {
  const tokens = profile.brand?.tokens ?? [];
  const resolve = resolver(tokens);
  const { colour, radius, space, type } = grouped(tokens);
  const fill = pick(colour, [/button.*(bg|background|fill)/i, /\.(accent|primary|brand|action)$/i, /(accent|primary|brand|action)/i]);
  const ink = pick(colour, [/button.*(text|fg|label)/i, /on-?(accent|primary|brand|action)/i, /text-on/i]);
  const background = fill ? paintable(resolve(fill.value)) : null;
  const color = ink ? paintable(resolve(ink.value)) : null;
  if (!fill || !ink || !background || !color) return null;
  const corner = pick(radius, [/button/i, /\.(md|m|medium|default|base)$/i, /./]);
  const spaces = space.filter((token) => pixels(resolve(token.value)) !== null).sort((a, b) => (pixels(resolve(a.value)) ?? 0) - (pixels(resolve(b.value)) ?? 0));
  const padY = spaces.find((token) => (pixels(resolve(token.value)) ?? 0) >= 6);
  const padX = spaces.find((token) => (pixels(resolve(token.value)) ?? 0) >= 12);
  const family = pick(type, [/button.*family/i, /font\.(body|sans|base|ui)$/i, /family|^font\.[a-z]+$/i]);
  const fontSize = pick(type, [/button.*size/i, /size\.(body|base|md)$/i]);
  const familyValue = family ? resolve(family.value) : (profile.brand?.fonts[0]?.family ?? null);
  const safeFamily = familyValue && /^[\w\s,"'-]+$/.test(familyValue) ? familyValue : null;
  const sizeValue = fontSize && pixels(resolve(fontSize.value)) !== null ? resolve(fontSize.value) : null;
  const radiusValue = corner && pixels(resolve(corner.value)) !== null ? resolve(corner.value) : null;
  const from = [fill.name, ink.name, radiusValue ? corner?.name : null, padY?.name, padX?.name, family?.name ?? (safeFamily ? 'brand.fonts' : null), sizeValue ? fontSize?.name : null].filter(
    (name): name is string => !!name,
  );
  return {
    background,
    color,
    radius: radiusValue,
    paddingY: padY ? resolve(padY.value) : null,
    paddingX: padX ? resolve(padX.value) : null,
    font: safeFamily,
    size: sizeValue,
    from: [...new Set(from)],
  };
}

/** The address part of a profile: its ref (`harbor/shop`); `harbor~shop` is read the same. */
export function refOf(param: string): string {
  return param.includes('/') ? param : param.replace('~', '/');
}
