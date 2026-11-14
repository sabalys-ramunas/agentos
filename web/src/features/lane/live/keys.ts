/** X11 keysyms for what a person types on a phone into a live view (RFC 6143, section 7.5.4). */
export const KEYSYM = {
  Enter: 0xff0d,
  Backspace: 0xff08,
  Tab: 0xff09,
  Escape: 0xff1b,
} as const;

/** Latin-1 maps to itself; anything else is the Unicode keysym range. */
export function keysymFor(char: string): number {
  if (char === '\n' || char === '\r') return KEYSYM.Enter;
  if (char === '\t') return KEYSYM.Tab;
  const code = char.codePointAt(0) ?? 0x3f;
  return code < 0x100 ? code : 0x01000000 + code;
}

/** What the terminal's key row sends. Bytes, as a PTY reads them. */
export const TERMINAL_KEYS = [
  { label: 'Esc', bytes: '\x1b' },
  { label: 'Tab', bytes: '\t' },
  { label: 'Ctrl C', bytes: '\x03' },
  { label: 'Up', bytes: '\x1b[A' },
  { label: 'Down', bytes: '\x1b[B' },
] as const;

/** A zoom level kept within what reads well: fit, up to four times. */
export const clampZoom = (value: number) => Math.min(4, Math.max(1, Math.round(value * 100) / 100));

/** The largest size with the picture's shape that fits the box. */
export function fit(width: number, height: number, boxWidth: number, boxHeight: number) {
  if (width <= 0 || height <= 0 || boxWidth <= 0 || boxHeight <= 0) return { width: 0, height: 0 };
  const scale = Math.min(boxWidth / width, boxHeight / height);
  return { width: Math.floor(width * scale), height: Math.floor(height * scale) };
}

/** Scroll that keeps the point under a pinch or a tap still while the zoom changes. */
export function anchorScroll(scroll: number, point: number, from: number, to: number) {
  return Math.max(0, (scroll + point) * (to / from) - point);
}
