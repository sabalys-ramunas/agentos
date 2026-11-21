/**
 * What a check's data means in plain words: screens from their keys, findings numbered in the order the
 * node groups them, the checks named for a person, measured and expected values as short lines, and the
 * text an agent can act on. No React here, so it is tested on its own.
 */
import type { FindingSeverity, StudioFinding, StudioFindingGroup, StudioRun, StudioRunRow, StudioScreen } from '@/api/types';
import type { StatusLabel, Tone } from '@/lib/labels';
import { plural } from '@/lib/format';

export interface NumberedFinding extends StudioFinding {
  /** Its number in the check, from 1, in the order the node groups them (worst first within a component). */
  n: number;
  /** The component group it sits in, as the node names it. */
  group: string;
}

/** Every finding of a run, numbered from 1 in the node's order. */
export function numbered(groups: StudioFindingGroup[]): NumberedFinding[] {
  let n = 0;
  return groups.flatMap((group) => group.findings.map((finding) => ({ ...finding, n: ++n, group: group.component })));
}

export const SEVERITY_WORD: Record<FindingSeverity, string> = { blocker: 'Blocker', major: 'Major', minor: 'Minor', info: 'Note' };
export const SEVERITY_TONE: Record<FindingSeverity, Tone> = { blocker: 'bad', major: 'warn', minor: 'idle', info: 'neutral' };
const SEVERITY_RANK: Record<FindingSeverity, number> = { blocker: 0, major: 1, minor: 2, info: 3 };

/** Findings at `severity` or worse. */
export function atLeast(findings: NumberedFinding[], severity: FindingSeverity | undefined): NumberedFinding[] {
  if (!severity) return findings;
  return findings.filter((finding) => SEVERITY_RANK[finding.severity] <= SEVERITY_RANK[severity]);
}

/** "360 light" for `default@360-light`, with the state when it is not the default one. */
export function screenWords(screen: Pick<StudioScreen, 'width' | 'theme' | 'state'>): string {
  const base = `${screen.width} ${screen.theme}`;
  return screen.state && screen.state !== 'default' ? `${base}, ${screen.state}` : base;
}

const KEY = /^(?<state>[^@]+)@(?<width>\d+)-(?<theme>light|dark)$/;

/** A screen key read as a screen, for keys the run's screen list does not hold. */
export function screenOfKey(key: string): Pick<StudioScreen, 'width' | 'theme' | 'state'> | null {
  const match = KEY.exec(key);
  if (!match?.groups) return null;
  return { width: Number(match.groups.width), theme: match.groups.theme as 'light' | 'dark', state: match.groups.state ?? 'default' };
}

export function keyWords(key: string): string {
  const screen = screenOfKey(key);
  return screen ? screenWords(screen) : key;
}

/** Where the finding was seen, by screen key: from seen_at and from the screens it has a place on. */
export function screensOf(finding: StudioFinding): string[] {
  return [...new Set([...finding.seen_at.map((seen) => seen.screen), ...finding.crop_screens, ...Object.keys(finding.rects ?? {})])];
}

export function onScreen(finding: StudioFinding, key: string): boolean {
  return screensOf(finding).includes(key);
}

/** The worst severity on each screen, for the tone of its count. */
export function worstByScreen(findings: StudioFinding[]): Map<string, FindingSeverity> {
  const out = new Map<string, FindingSeverity>();
  for (const finding of findings) {
    for (const key of screensOf(finding)) {
      const was = out.get(key);
      if (!was || SEVERITY_RANK[finding.severity] < SEVERITY_RANK[was]) out.set(key, finding.severity);
    }
  }
  return out;
}

/**
 * The node's verdict in the page's words: a count of blockers is plural, and "info" reads "to know".
 * "Not clean: 7 findings (2 blocker, 1 info)" becomes "Not clean: 7 findings (2 blockers, 1 to know)".
 */
export function verdictWords(verdict: string): string {
  return verdict.replace(/\b(\d+) blocker\b/g, (_, n: string) => (n === '1' ? '1 blocker' : `${n} blockers`)).replace(/\b(\d+) info\b/g, '$1 to know');
}

/** How many findings show on each screen. */
export function countsByScreen(findings: StudioFinding[]): Map<string, number> {
  const out = new Map<string, number>();
  for (const finding of findings) for (const key of screensOf(finding)) out.set(key, (out.get(key) ?? 0) + 1);
  return out;
}

/** The checks studio runs, named for a person. Unknown ones keep their id. */
const CHECK_WORDS: Record<string, string> = {
  'contrast.text': 'Text is hard to read',
  'contrast.ui': 'A control is hard to see',
  'target.size': 'Too small to tap',
  'text.truncated': 'Text is cut off',
  'text.overflow': 'Text runs outside its box',
  'layout.overflow': 'The page is wider than the screen',
  'layout.overlap': 'Two things overlap',
  'names.missing': 'A control has no name',
  'health.images': 'An image is heavier than it needs to be',
  'health.console': 'The page logged an error',
  'health.network': 'A request failed',
  'theme.leak': 'Part of the page ignores the theme',
  'token.color': 'A colour is off the design system',
  'token.space': 'A spacing is off the design system',
  'token.radius': 'A corner is off the design system',
  'token.font': 'A type value is off the design system',
  'token.shadow': 'A shadow is off the design system',
  'token.border': 'A border is off the design system',
  'token.hardcoded': 'A value only looks like a token',
  'component.hand_rolled': 'A hand-rolled control',
};

export function checkWords(check: string): string {
  return CHECK_WORDS[check] ?? check;
}

/** The finding's title: what is wrong, and where when the page names it. */
export function findingTitle(finding: StudioFinding): string {
  const what = checkWords(finding.check);
  const name = finding.where.name?.trim();
  return name ? `${what}: "${name}"` : what;
}

/** How it is known, in words. */
export function confidenceWords(finding: StudioFinding): string {
  if (finding.confidence === 'measured') return 'Measured on the page. The same every run.';
  if (finding.confidence === 'vision') return 'Seen in the picture by a model. Worth a look before acting on it.';
  return finding.why_heuristic ? `A rule of thumb: ${finding.why_heuristic}.` : 'A rule of thumb, not a measurement.';
}

const SKIP = new Set(['selectors', 'candidates', 'method', 'category', 'match', 'css_var', 'properties']);

function valueText(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : String(Math.round(value * 1000) / 1000);
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  if (Array.isArray(value)) return value.map(valueText).filter(Boolean).join(', ');
  if (typeof value === 'object') return Object.entries(value as Record<string, unknown>).map(([key, inner]) => `${key} ${valueText(inner)}`).join(', ');
  return String(value);
}

/** A measured or expected record as one short line: "ratio 2.1, color of the text". Lists of selectors are left out. */
export function recordLine(record: Record<string, unknown>): string {
  return Object.entries(record)
    .filter(([key, value]) => !SKIP.has(key) && value !== null && value !== undefined && value !== '')
    .map(([key, value]) => `${key.replace(/_/g, ' ')} ${valueText(value)}`)
    .join(', ');
}

/** The selectors where else it was seen on the page (token findings carry a list). */
export function otherSelectors(finding: StudioFinding): string[] {
  const list = finding.measured.selectors;
  return Array.isArray(list) ? list.filter((item): item is string => typeof item === 'string' && item !== finding.where.selector) : [];
}

/** The text an agent can act on: the finding id, the check, where, measured, expected and the fix. */
export function forAgent(run: Pick<StudioRun, 'id' | 'url'>, finding: NumberedFinding): string {
  const where = [finding.where.selector, finding.where.role, finding.where.name ? `"${finding.where.name}"` : null].filter(Boolean).join(' ');
  const lines = [
    `Studio finding ${finding.id} in check ${run.id} of ${run.url}`,
    `Check: ${finding.check} (${finding.severity}, ${finding.confidence})`,
    `Where: ${where}${finding.component ? ` in ${finding.component}` : ''}, on ${screensOf(finding).map(keyWords).join(', ') || 'the whole page'}`,
  ];
  const measured = recordLine(finding.measured);
  const expected = recordLine(finding.expected);
  if (measured) lines.push(`Measured: ${measured}`);
  if (expected) lines.push(`Expected: ${expected}`);
  if (finding.fix) lines.push(`Fix: ${finding.fix}`);
  lines.push(`Read it with finding_get id=${finding.id} run=${run.id}, then check again with ui_check since=${run.id}.`);
  return lines.join('\n');
}

/** The check of the same page again, reading only what changed. */
export function checkAgain(run: Pick<StudioRunRow, 'id' | 'url' | 'profile'>): string {
  return `ui_check url=${run.url}${run.profile ? ` profile=${run.profile}` : ''} since=${run.id}`;
}

/** The path and query of a page, for a table cell: the host is the same on most rows. */
export function pagePath(url: string): { path: string; host: string } {
  try {
    const parsed = new URL(url);
    return { path: `${parsed.pathname}${parsed.search}` || '/', host: parsed.host };
  } catch {
    return { path: url, host: '' };
  }
}

/** The run's state as a status: what a person needs to know first. */
export function runStatus(run: Pick<StudioRunRow, 'counts' | 'screens' | 'screens_failed'>): StatusLabel {
  const { counts } = run;
  if (run.screens > 0 && run.screens_failed >= run.screens) return { label: 'Did not run', tone: 'bad' };
  if (counts.blocker > 0) return { label: plural(counts.blocker, 'blocker'), tone: 'bad' };
  const open = counts.total - counts.info;
  if (open > 0) return { label: plural(open, 'finding'), tone: 'warn' };
  if (run.screens_failed > 0) return { label: 'Clean where it ran', tone: 'warn' };
  return { label: 'Clean', tone: 'ok' };
}

/** A share of 0 to 1 as a whole percent. */
export function percent(share: number): string {
  return `${Math.round(share * 100)}%`;
}

/** The design system categories, named for a person. */
export const CATEGORY_WORDS: Record<string, string> = {
  color: 'Colour',
  space: 'Spacing',
  radius: 'Corners',
  type: 'Type',
  font_size: 'Font size',
  font_family: 'Font family',
  font_weight: 'Weight',
  line_height: 'Line height',
  shadow: 'Shadow',
  border_width: 'Border',
};

export interface LookAlike {
  finding: NumberedFinding | StudioFinding;
  value: string;
  token: string | null;
  tokenValue: string | null;
  distance: string | null;
  count: number;
  category: string | null;
}

/** Values that only look like a token: hard-coded copies and near misses, with the nearest token and how far. */
export function lookAlikes<T extends StudioFinding>(findings: T[]): LookAlike[] {
  return findings
    .filter((finding) => finding.check === 'token.hardcoded' || (finding.check.startsWith('token.') && finding.measured.match === 'near'))
    .map((finding) => {
      const { measured, expected } = finding;
      const distance =
        typeof measured.delta_eok === 'number'
          ? `ΔE ${Math.round(measured.delta_eok * 1000) / 1000}`
          : typeof measured.delta_px === 'number'
            ? `${measured.delta_px} px off`
            : typeof measured.delta === 'number'
              ? `${measured.delta} off`
              : null;
      return {
        finding,
        value: valueText(measured.value) || finding.where.selector,
        token: typeof expected.token === 'string' ? expected.token : null,
        tokenValue: typeof expected.value === 'string' ? expected.value : null,
        distance,
        count: typeof measured.count === 'number' ? measured.count : finding.seen_at.reduce((sum, seen) => sum + seen.count, 0),
        category: typeof measured.category === 'string' ? measured.category : null,
      };
    });
}

/** A colour from data that the browser can paint, or null: never a made-up one. */
export function paintable(value: string | null | undefined): string | null {
  if (!value) return null;
  const text = value.trim();
  if (!/^(#[0-9a-f]{3,8}|rgba?\([^)]*\)|hsla?\([^)]*\)|oklch\([^)]*\)|oklab\([^)]*\))$/i.test(text)) return null;
  if (typeof CSS !== 'undefined' && typeof CSS.supports === 'function' && !CSS.supports('color', text)) return null;
  return text;
}
