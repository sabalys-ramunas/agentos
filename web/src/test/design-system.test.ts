/**
 * The design system, held in the source (web/DESIGN-SYSTEM.md, "The checks"). Every screen uses tokens
 * and components; this test says where one does not and what to write instead.
 *
 * Findings that were in the code when the rules came in (2026-11-18) are counted per file in
 * design-allowlist.json, and the count may only go down: a file over its count fails, a new file has no
 * count, and a file under its count fails too until the list is lowered (`pnpm design:shrink` does that,
 * and never raises one). `exceptions` are the few reviewed places a rule does not apply, each with why.
 */
import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { GROUPS } from '@/catalog/registry';
import { checkCss, checkSource, declaredIn, type Finding } from './design/rules';

const WEB = join(__dirname, '..', '..');
const ALLOWLIST = join(__dirname, 'design-allowlist.json');

interface Allowlist {
  $comment: string;
  exceptions: Record<string, Record<string, string>>;
  counts: Record<string, Record<string, number>>;
}

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}

const all = walk(join(WEB, 'src')).map((path) => relative(WEB, path));
// Tests and the mock are data, not screens.
const sources = all.filter((file) => /\.(tsx?|css)$/.test(file) && !/\.test\.|^src\/test\/|^src\/mocks\//.test(file));
const read = (file: string) => readFileSync(join(WEB, file), 'utf8');
const allowlist = JSON.parse(readFileSync(ALLOWLIST, 'utf8')) as Allowlist;

function findings(): Finding[] {
  const defined = new Set<string>();
  for (const file of sources) for (const name of declaredIn(read(file))) defined.add(name);
  return sources.flatMap((file) => {
    if (file.endsWith('.css')) return file.startsWith('src/features/') ? checkCss(file, read(file)) : [];
    return checkSource(file, read(file), defined);
  });
}

const found = findings().filter((finding) => !allowlist.exceptions[finding.rule]?.[finding.file]);

function counted(rule: string): Map<string, Finding[]> {
  const out = new Map<string, Finding[]>();
  for (const finding of found.filter((f) => f.rule === rule)) out.set(finding.file, [...(out.get(finding.file) ?? []), finding]);
  return out;
}

const RULES = ['raw-colour', 'arbitrary-value', 'not-a-token', 'unknown-token', 'inline-style', 'hand-rolled', 'vocabulary', 'legacy-token'] as const;

if (process.env.DESIGN_SHRINK) {
  // Lowers each count to what is there now, and drops what is gone. It never raises a count.
  for (const rule of RULES) {
    const now = counted(rule);
    const next: Record<string, number> = {};
    for (const [file, allowed] of Object.entries(allowlist.counts[rule] ?? {})) {
      const left = Math.min(allowed, now.get(file)?.length ?? 0);
      if (left) next[file] = left;
    }
    allowlist.counts[rule] = next;
  }
  writeFileSync(ALLOWLIST, `${JSON.stringify(allowlist, null, 2)}\n`);
}

const say = (f: Finding) => `  ${f.file}:${f.line}  ${f.found}\n      fix: ${f.fix}`;

describe('the design system (web/DESIGN-SYSTEM.md)', () => {
  it.each(RULES)('%s: no new findings, and the allowlist only shrinks', (rule) => {
    const allowed = allowlist.counts[rule] ?? {};
    const problems: string[] = [];
    for (const [file, list] of counted(rule)) {
      const limit = allowed[file] ?? 0;
      if (list.length > limit) {
        problems.push(`${file} has ${list.length} (${limit} allowed). Fix the new one:\n${list.map(say).join('\n')}`);
      }
    }
    for (const [file, limit] of Object.entries(allowed)) {
      const now = counted(rule).get(file)?.length ?? 0;
      if (now < limit) problems.push(`${file} is down to ${now} from ${limit}: good. Lower it in src/test/design-allowlist.json (pnpm design:shrink).`);
    }
    expect(problems.join('\n\n')).toBe('');
  });

  it('keeps every exception to a file that exists, with a reason', () => {
    for (const [rule, files] of Object.entries(allowlist.exceptions)) {
      for (const [file, why] of Object.entries(files)) {
        expect([rule, file, existsSync(join(WEB, file))]).toEqual([rule, file, true]);
        expect(why.length).toBeGreaterThan(20);
      }
    }
  });

  it('has a catalog entry and a group index line for every file under components/', () => {
    const components = all.filter((file) => file.startsWith('src/components/') && /\.tsx?$/.test(file) && !file.includes('.test.'));
    const listed = new Set(GROUPS.flatMap((group) => group.entries.map((entry) => entry.file)));
    const fix = (file: string) => `${file}: add it to src/catalog/registry.tsx (a demo, or shownBy) and a line to its group's index in web/design-system/`;
    expect(components.filter((file) => !listed.has(file)).map(fix)).toEqual([]);
    for (const group of GROUPS) {
      const index = readFileSync(join(WEB, 'design-system', `${group.id}.md`), 'utf8');
      for (const entry of group.entries) {
        expect([entry.file, existsSync(join(WEB, entry.file))]).toEqual([entry.file, true]);
        expect([entry.file, Boolean(entry.demo) !== Boolean(entry.shownBy)]).toEqual([entry.file, true]);
        // The index names the file (without src/), so a reader finds it from there.
        expect([`design-system/${group.id}.md names ${entry.file}`, index.includes(entry.file.replace(/^src\//, ''))]).toEqual([`design-system/${group.id}.md names ${entry.file}`, true]);
      }
    }
  });
});
