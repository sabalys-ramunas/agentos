/**
 * Small readers for search params. A screen's params are read with these, so a hand-typed or old
 * address never crashes a page: anything unexpected is simply left out.
 */
export type Search = Record<string, unknown>;

/** A non-empty string, or undefined. */
export const text = (value: unknown, max = 200): string | undefined => (typeof value === 'string' && value ? value.slice(0, max) : undefined);

/** One of the given words, or undefined. */
export function oneOf<const T extends string>(value: unknown, options: readonly T[]): T | undefined {
  return typeof value === 'string' && (options as readonly string[]).includes(value) ? (value as T) : undefined;
}

/** A whole number at or above zero (the router may hand over a number or a string), or undefined. */
export function count(value: unknown): number | undefined {
  const number = typeof value === 'number' ? value : typeof value === 'string' && value.trim() ? Number(value) : NaN;
  return Number.isInteger(number) && number >= 0 ? number : undefined;
}

/** true for `true` or `'true'` or `'1'`, false for `false` or `'false'` or `'0'`, else undefined. */
export function flag(value: unknown): boolean | undefined {
  if (value === true || value === 'true' || value === '1' || value === 1) return true;
  if (value === false || value === 'false' || value === '0' || value === 0) return false;
  return undefined;
}

/** Drops the undefined keys, so a link with no params has a clean address. */
export function clean<T extends object>(search: T): T {
  return Object.fromEntries(Object.entries(search).filter(([, value]) => value !== undefined)) as T;
}
