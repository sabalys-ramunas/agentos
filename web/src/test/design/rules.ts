/**
 * The design rules: what makes a screen on the system, checked on the source. Each finding names the
 * file and line, what was found, and the fix. design-system.test.ts runs them over src/ and holds the
 * findings that were there when the rules came in to a list that may only shrink (design-allowlist.json).
 *
 * Why in the source and not only on a rendered page: most UI here is written by coding agents, and an
 * agent reads a failing test with a fix in it far more reliably than a screenshot. Studio (ui_check,
 * system_check) still measures the rendered page; these rules stop the cause before it renders.
 */
import { checkClass, varsIn } from './classes';
import type { ClassRule } from './classes';
import { parse } from './source';
import type { Parsed } from './source';

export type Rule = ClassRule | 'inline-style' | 'hand-rolled' | 'unknown-token' | 'vocabulary';

export interface Finding {
  rule: Rule;
  file: string;
  line: number;
  found: string;
  fix: string;
}

/** Screens: held to every rule. Components may use arbitrary values and inline styles; they are the system. */
export const isScreen = (file: string) => /^src\/(features|app|catalog)\//.test(file);

const COLOUR = /(?<![\w&])#(?:[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?)\b|\b(?:rgba?|hsla?|oklch|oklab|color-mix)\(/;
/** A short hex is only a colour on its own or in an arbitrary value: `#add` may be an anchor. */
const SHORT_HEX = /^#[0-9a-fA-F]{3,4}$|\[#[0-9a-fA-F]{3,4}\]/;
const NAMED = /^(white|black|red|green|blue|gray|grey|yellow|orange|purple|pink|silver|navy|teal|maroon)$/i;
const COLOUR_ATTRS = new Set(['fill', 'stroke', 'color', 'stopColor', 'floodColor', 'lightingColor']);
const RAW_COLOUR_FIX = 'Colour is a token: a class (text-muted, bg-card, border-line, bg-ok-dot ...) or var(--token). Add a new one to tokens/build-tokens.py, never inline';

/** Style keys whose literal value is a design decision. Geometry from data (left, width, height) is not. */
const STYLE_DESIGN_KEYS =
  /^(color|background(Color|Image)?|border(Color|Width|Radius|Top|Bottom|Left|Right|Style)?|boxShadow|outline(Color|Offset)?|fill|stroke|font(Size|Weight|Family)|lineHeight|letterSpacing|padding\w*|margin\w*|gap|rowGap|columnGap|zIndex|transition\w*|animation\w*|opacity)$/;
const ONLY_VARS = /^(?:\s*var\(--[a-z0-9-]+\)\s*,?)+$/;

/** Words the vocabulary (src/lib/vocabulary.ts) replaces, with the everyday word to write. */
const VOCABULARY: Array<[RegExp, string]> = [
  [/\blanes?\b/i, 'account (THINGS.lane)'],
  [/\bnodes?\b/i, 'machine (THINGS.node)'],
  [/\bflows?\b/i, 'routine (THINGS.flow)'],
  [/\bworkflows?\b/i, 'routine (a routine and a workflow are one thing)'],
  [/\bprincipals?\b/i, 'who did it (THINGS.principal, principalName)'],
  [/\bbrains?\b/i, 'AI model (THINGS.brain)'],
  [/\bplugins?\b/i, 'worker (THINGS.plugin)'],
  [/\bvault\b/i, 'Saved logins (THINGS.vault)'],
  [/\baudit log\b/i, 'Who changed what (THINGS.audit)'],
  [/\bdanger zone\b/i, 'Delete this organization (THINGS.dangerZone)'],
  [/\bstanding approvals?\b/i, 'Always allowed (THINGS.standing)'],
  [/\bcontent hash\b/i, 'Content ID (THINGS.contentHash)'],
  [/\bComputers\b/, 'Machines (PLACES.machines)'],
  [/\bInspect\b/, 'Technical details (THINGS.inspect)'],
];

const HAND_ROLLED: Array<{ test: (tokens: Set<string>) => boolean; found: string; fix: string }> = [
  {
    test: (t) => t.has('rounded-xl') && t.has('border') && (t.has('bg-card') || t.has('bg-bg')),
    found: 'a card drawn by hand (rounded-xl border bg-card)',
    fix: 'Use <Card> with <CardHeader> (components/ui/card.tsx): it has the radius, hairline, lift and head',
  },
  {
    test: (t) => t.has('rounded-full') && [...t].some((c) => /^(size-[123]|size-2\.5|h-2)$/.test(c)) && [...t].some((c) => /^bg-(ok|warn|bad|idle|live)(-dot)?$|^bg-(green|amber|red)$/.test(c)),
    found: 'a status dot drawn by hand',
    fix: 'Use <StatusDot tone="ok" /> or <Status status={...} /> (components/ui/status.tsx): a shape and a word, never colour alone',
  },
  { test: (t) => t.has('animate-spin'), found: 'a spinner drawn by hand', fix: 'Use <Spinner /> (components/ui/spinner.tsx), or `loading` on a Button' },
  { test: (t) => t.has('text-stat'), found: 'a stat number drawn by hand', fix: 'Use <StatTile> in a <StatStrip> (components/ui/stat-tile.tsx)' },
  {
    test: (t) => t.has('text-page-title') || t.has('md:text-page-title'),
    found: 'a page title drawn by hand',
    fix: 'Use <PageHeader title description actions /> (components/ui/misc.tsx)',
  },
];

/** Components kept for the screens that still use them, each with the one to use instead. */
const DEPRECATED: Record<string, string> = {
  '@/components/charts/small#Segmented': 'SegmentedControl (components/ui/tabs.tsx): the Mono segmented control. Segmented is the older one',
  '@/components/lane-icon#WorkspaceMark': 'BusinessMark (components/brand/identity.tsx). WorkspaceMark is its old name',
};

function importFindings(file: string, parsed: Parsed, out: Finding[]) {
  for (const { module, name, line } of parsed.imports) {
    const instead = DEPRECATED[`${module}#${name}`];
    if (instead) out.push({ rule: 'legacy-token', file, line, found: `import { ${name} } from '${module}'`, fix: `Use ${instead}` });
  }
}

function stringFindings(file: string, parsed: Parsed, out: Finding[]) {
  const screen = isScreen(file);
  for (const { text, line } of parsed.strings) {
    const tokens = text.split(/\s+/).filter(Boolean);
    for (const token of tokens) {
      if (COLOUR.test(token) || SHORT_HEX.test(token)) {
        out.push({ rule: 'raw-colour', file, line, found: token, fix: RAW_COLOUR_FIX });
        continue;
      }
      const finding = checkClass(token);
      if (!finding || (!screen && finding.rule === 'arbitrary-value')) continue;
      out.push({ rule: finding.rule, file, line, found: token, fix: finding.fix });
    }
    if (screen && tokens.length) {
      const set = new Set(tokens);
      for (const pattern of HAND_ROLLED) if (pattern.test(set)) out.push({ rule: 'hand-rolled', file, line, found: pattern.found, fix: pattern.fix });
    }
  }
}

function jsxFindings(file: string, parsed: Parsed, out: Finding[]) {
  const screen = isScreen(file);
  for (const el of parsed.elements) {
    for (const [name, value] of el.attrs) {
      if (COLOUR_ATTRS.has(name) && value && NAMED.test(value)) out.push({ rule: 'raw-colour', file, line: el.line, found: `${name}="${value}"`, fix: RAW_COLOUR_FIX });
    }
    if (!screen) continue;
    if (el.tag === 'table') out.push({ rule: 'hand-rolled', file, line: el.line, found: 'a <table> drawn by hand', fix: 'Use <Table>, <Th>, <Tr>, <Td> in <TableScroll> (components/ui/table.tsx): the head, row heights, sort and the phone list come with it' });
    const cls = el.attrs.get('className') ?? '';
    if (el.attrs.get('role') === 'alert' && /\btext-(red|bad)\b/.test(cls)) {
      out.push({ rule: 'hand-rolled', file, line: el.line, found: 'an inline error drawn by hand', fix: 'Use <InlineError> (components/ui/states.tsx), or `error` on a <Field>' });
    }
  }
  if (!screen) return;
  for (const line of parsed.clipboard) {
    out.push({ rule: 'hand-rolled', file, line, found: 'navigator.clipboard.writeText', fix: 'Use <CopyButton text>, <CodeBlock code> or useCopy() (components/ui/misc.tsx): they say Copied and clean up' });
  }
  for (const style of parsed.styles) {
    if (!STYLE_DESIGN_KEYS.test(style.key) || style.literal === null || ONLY_VARS.test(style.literal)) continue;
    out.push({ rule: 'inline-style', file, line: style.line, found: `style={{ ${style.key}: ${JSON.stringify(style.literal)} }}`, fix: 'A design value in style is invisible to the tokens. Use a class (a token), or var(--token) when the value must be computed' });
  }
}

function wordFindings(file: string, parsed: Parsed, out: Finding[]) {
  for (const { text, line } of parsed.words) {
    for (const [pattern, word] of VOCABULARY) {
      const match = pattern.exec(text);
      if (match) out.push({ rule: 'vocabulary', file, line, found: `"${match[0]}" in "${text.slice(0, 60)}"`, fix: `Say ${word}. Technical terms wait behind Technical details (useTechnical)` });
    }
  }
}

function tokenFindings(file: string, parsed: Parsed, defined: Set<string>, out: Finding[]) {
  for (const { text, line } of parsed.strings) {
    for (const name of new Set(text.split(/\s+/).flatMap(varsIn))) {
      if (!defined.has(name) && !/^--(tw|radix)-/.test(name)) {
        out.push({ rule: 'unknown-token', file, line, found: name, fix: `${name} is not a token, so it reads as nothing. See src/styles/tokens.css and theme.css for the names` });
      }
    }
  }
}

/** Every finding in one TypeScript source file. `file` is relative to web/ (src/features/...). */
export function checkSource(file: string, text: string, defined: Set<string>): Finding[] {
  const parsed = parse(file, text);
  const out: Finding[] = [];
  stringFindings(file, parsed, out);
  importFindings(file, parsed, out);
  jsxFindings(file, parsed, out);
  wordFindings(file, parsed, out);
  tokenFindings(file, parsed, defined, out);
  return out;
}

/** Raw colours in a screen's own stylesheet (features/<it>/<it>.css). Comments are skipped. */
export function checkCss(file: string, text: string): Finding[] {
  const out: Finding[] = [];
  text
    .replace(/\/\*[\s\S]*?\*\//g, (comment) => comment.replace(/[^\n]/g, ' '))
    .split('\n')
    .forEach((line, index) => {
      const value = line.includes(':') ? line.slice(line.indexOf(':') + 1) : '';
      const colour = /#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|oklch|color-mix)\(|:\s*(white|black)\b/.exec(value);
      if (colour) out.push({ rule: 'raw-colour', file, line: index + 1, found: colour[0].trim(), fix: RAW_COLOUR_FIX });
    });
  return out;
}

/** The custom properties a stylesheet declares, or a script sets (`'--hue': ...`, `setProperty('--x'`). */
export function declaredIn(text: string): string[] {
  return [...text.matchAll(/(--[a-z0-9-]+)['"]?\s*[:,]/g)].map((match) => match[1]!);
}
