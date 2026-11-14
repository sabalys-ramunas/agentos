/**
 * The failed tests of a run, read from its own output: the lines pytest, vitest, jest, swift test and
 * go test print for a failure. Each failure says where in the output it was read, so a page can jump
 * there. A format that is not recognised gives nothing: no failure is ever guessed.
 */
import type { TestRunner } from '@/api/types';

export type Stream = 'stdout' | 'stderr';

export interface Failure {
  /** The test, as the runner names it: `cartTotal › rounds per line`, `test_refund`, `TestRefund/zero`. */
  name: string;
  file?: string;
  line?: number;
  /** The first line of what the runner said went wrong. */
  message?: string;
  /** Where it was read: the stream and the line (0-based) a page jumps to. */
  at: { stream: Stream; line: number };
}

// eslint-disable-next-line no-control-regex
const ANSI = /\u001b\[[0-9;?]*[A-Za-z]/g;

export const plain = (line: string) => line.replace(ANSI, '').replace(/\r$/, '');

/** `src/a.test.ts:88:31` or `tests/test_a.py:12` anywhere in a line. */
const FILE_AT = /([\w./@+-]+\.(?:py|tsx?|jsx?|mjs|cjs|mts|cts|vue|svelte|go|swift)):(\d+)(?::\d+)?/;

function fileAt(text: string): { file: string; line: number } | undefined {
  const match = FILE_AT.exec(text);
  return match ? { file: match[1]!, line: Number(match[2]) } : undefined;
}

const cut = (text: string, max = 300) => {
  const value = text.trim();
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
};

function pytest(lines: string[], stream: Stream): Failure[] {
  const out: Failure[] = [];
  // Where each failure's section starts, and the `path:line: Error` line inside it.
  const sections = new Map<string, { at: number; file?: string; line?: number; message?: string }>();
  let open: string | undefined;
  for (let i = 0; i < lines.length; i += 1) {
    const text = lines[i]!;
    const head = /^_{3,} (.+?) _{3,}$/.exec(text);
    if (head) {
      open = head[1]!.trim();
      sections.set(open, { at: i });
      continue;
    }
    if (/^={3,}/.test(text)) open = undefined;
    if (!open) continue;
    const section = sections.get(open)!;
    const where = /^([\w./@+-]+\.py):(\d+): (\w[\w.]*)$/.exec(text);
    if (where && !section.file) {
      section.file = where[1];
      section.line = Number(where[2]);
    }
    if (/^E {2,}\S/.test(text) && !section.message) section.message = cut(text.replace(/^E\s+/, ''));
  }
  for (let i = 0; i < lines.length; i += 1) {
    const match = /^(FAILED|ERROR) (\S+?)(?: - (.*))?$/.exec(lines[i]!);
    if (!match) continue;
    const id = match[2]!;
    const [path, ...parts] = id.split('::');
    const name = parts.length ? parts.join('::') : id;
    const short = parts.at(-1) ?? id;
    const section = sections.get(short) ?? sections.get(parts.join('.')) ?? sections.get(name);
    out.push({
      name,
      file: path && path.includes('.py') ? path : section?.file,
      line: section?.line,
      message: match[3] ? cut(match[3]) : section?.message,
      at: { stream, line: section?.at ?? i },
    });
  }
  if (out.length) return out;
  // `-q` with `-rN`: no short summary, only the sections.
  for (const [name, section] of sections) {
    if (/^(test|Test)/.test(name.split('.').at(-1) ?? '')) out.push({ name, file: section.file, line: section.line, message: section.message, at: { stream, line: section.at } });
  }
  return out;
}

function vitest(lines: string[], stream: Stream): Failure[] {
  const out: Failure[] = [];
  // The detail blocks: ` FAIL  src/a.test.ts > suite > name`, the error, then ` ❯ src/a.test.ts:88:31`.
  for (let i = 0; i < lines.length; i += 1) {
    const match = /^\s*FAIL\s+(\S+?\.\w+)\s+>\s+(.+)$/.exec(lines[i]!);
    if (!match) continue;
    const failure: Failure = { name: match[2]!.trim().replace(/\s+>\s+/g, ' › '), file: match[1], at: { stream, line: i } };
    for (let j = i + 1; j < Math.min(lines.length, i + 40); j += 1) {
      const text = lines[j]!;
      if (/^\s*FAIL\s/.test(text) || /^⎯{3,}/.test(text.trim())) break;
      if (!failure.message && /\S/.test(text) && !/^\s*❯/.test(text)) failure.message = cut(text);
      const where = /^\s*❯\s+(.*)$/.exec(text);
      const at = where ? fileAt(where[1]!) : undefined;
      if (at && at.file.endsWith(failure.file!.replace(/^.*\//, ''))) {
        failure.line = at.line;
        break;
      }
    }
    out.push(failure);
  }
  if (out.length) return out;
  // The default reporter's tree: ` ❯ src/a.test.ts (12 tests | 2 failed)`, then `   × name 12ms`.
  let file: string | undefined;
  for (let i = 0; i < lines.length; i += 1) {
    const text = lines[i]!;
    const group = /^\s*[❯✓×]\s+(\S+?\.\w+)\s+\(\d+ tests?/.exec(text);
    if (group) {
      file = group[1];
      continue;
    }
    const failed = /^\s+[×✗]\s+(.+?)(?:\s+\d+(?:\.\d+)?m?s)?$/.exec(text);
    if (failed && file) out.push({ name: failed[1]!.trim().replace(/\s+>\s+/g, ' › '), file, at: { stream, line: i } });
  }
  return out;
}

function jest(lines: string[], stream: Stream): Failure[] {
  const out: Failure[] = [];
  let file: string | undefined;
  for (let i = 0; i < lines.length; i += 1) {
    const text = lines[i]!;
    const suite = /^\s*FAIL\s+(\S+)/.exec(text);
    if (suite) {
      file = suite[1];
      continue;
    }
    const match = /^\s*● (.+)$/.exec(text);
    if (!match || /^Test suite failed to run/.test(match[1]!)) continue;
    const failure: Failure = { name: match[1]!.trim(), file, at: { stream, line: i } };
    for (let j = i + 1; j < Math.min(lines.length, i + 60); j += 1) {
      const next = lines[j]!;
      if (/^\s*● /.test(next) || /^\s*(FAIL|PASS)\s/.test(next)) break;
      if (!failure.message && next.trim()) failure.message = cut(next);
      const at = /\bat .*\((.+)\)$/.exec(next) ?? /\bat (\S+:\d+:\d+)$/.exec(next);
      const where = at ? fileAt(at[1]!) : undefined;
      if (where && !where.file.includes('node_modules')) {
        failure.file = file ?? where.file;
        failure.line = where.line;
        break;
      }
    }
    out.push(failure);
  }
  return out;
}

function swift(lines: string[], stream: Stream): Failure[] {
  const out: Failure[] = [];
  const seen = new Set<string>();
  for (let i = 0; i < lines.length; i += 1) {
    const text = lines[i]!;
    // XCTest: `/path/CartTests.swift:38: error: -[Kit.CartTests testRounding] : XCTAssertEqual failed: ...`
    const xc = /^(.+?\.swift):(\d+): error: (?:-\[)?([\w.]+[ .]\w+)\]? : (.*)$/.exec(text);
    if (xc) {
      const name = xc[3]!.replace(/^.*?\.(\w+) (\w+)$/, '$1.$2').replace(' ', '.');
      if (seen.has(name)) continue;
      seen.add(name);
      out.push({ name, file: xc[1]!.replace(/^.*\/(Tests|Sources)\//, '$1/'), line: Number(xc[2]), message: cut(xc[4]!), at: { stream, line: i } });
      continue;
    }
    // Swift Testing: `✘ Test roundsPerLine() recorded an issue at CartTests.swift:38:5: Expectation failed: ...`
    const st = /^\s*[✘✗x]\s+Test (.+?) recorded an issue at ([\w./+-]+\.swift):(\d+)(?::\d+)?: (.*)$/.exec(text);
    if (st) {
      const name = st[1]!.replace(/^"(.*)"$/, '$1');
      if (seen.has(name)) continue;
      seen.add(name);
      out.push({ name, file: st[2], line: Number(st[3]), message: cut(st[4]!), at: { stream, line: i } });
      continue;
    }
    // XCTest on Linux names the failure without a location: `Test Case 'CartTests.testRounding' failed`.
    const linux = /^Test Case '(?:-\[)?([\w. ]+?)\]?' failed/.exec(text);
    if (linux) {
      const name = linux[1]!.replace(/^.*?\.(\w+) (\w+)$/, '$1.$2').replace(' ', '.');
      if (seen.has(name)) continue;
      seen.add(name);
      out.push({ name, at: { stream, line: i } });
    }
  }
  return out;
}

function go(lines: string[], stream: Stream): Failure[] {
  const out: Failure[] = [];
  for (let i = 0; i < lines.length; i += 1) {
    const match = /^(\s*)--- FAIL: (\S+) \(/.exec(lines[i]!);
    if (!match) continue;
    const failure: Failure = { name: match[2]!, at: { stream, line: i } };
    const indent = match[1]!.length;
    for (let j = i + 1; j < Math.min(lines.length, i + 20); j += 1) {
      const text = lines[j]!;
      if (/^\s*--- (FAIL|PASS|SKIP):/.test(text) || /^(FAIL|ok|PASS)\b/.test(text)) break;
      const where = /^\s+([\w./-]+\.go):(\d+): (.*)$/.exec(text);
      if (where && text.length - text.trimStart().length > indent) {
        failure.file = where[1];
        failure.line = Number(where[2]);
        failure.message = cut(where[3]!);
        break;
      }
    }
    out.push(failure);
  }
  // A parent fails because a subtest did: keep only the leaves, which say where.
  return out.filter((failure) => !out.some((other) => other !== failure && other.name.startsWith(`${failure.name}/`)));
}

const PARSERS: Record<TestRunner, (lines: string[], stream: Stream) => Failure[]> = { pytest, vitest, jest, swift, go };

/** Splits output into lines the way the page numbers them. */
export const splitLines = (text: string): string[] => {
  if (!text) return [];
  const lines = text.split('\n');
  if (lines.at(-1) === '') lines.pop();
  return lines;
};

/**
 * The failures the runner printed, in the order it printed them, from both streams. Empty when the
 * lines are not in a form this reads.
 */
export function parseFailures(runner: TestRunner, stdout: string[], stderr: string[]): Failure[] {
  const parse = PARSERS[runner];
  if (!parse) return [];
  const found = [...parse(stdout.map(plain), 'stdout'), ...parse(stderr.map(plain), 'stderr')];
  const seen = new Set<string>();
  return found.filter((failure) => {
    const key = `${failure.file ?? ''}|${failure.name}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/** A line that reads as a failure in any runner's words: marked red in the output. */
const FAILED_LINE =
  /^\s*(?:[×✗✕✘]\s|❯ .*\bfailed\b|FAIL\b|FAILED\b|ERROR\b|--- FAIL:|E {2,}\S|● |panic:|Traceback \(most recent call last\)|\S+\.swift:\d+: error:)|^(?:\w+Error|AssertionError)\b|^\s*\w+Error: /;

export function isFailedLine(line: string): boolean {
  return FAILED_LINE.test(plain(line));
}
