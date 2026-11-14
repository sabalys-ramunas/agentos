/**
 * A read-only output block (web/DESIGN.md, Terminals): dark in both themes, because output is the
 * one thing on a page that looks like a screen. ANSI colours are read from the text and drawn as
 * spans of text, never as HTML. Follow mode keeps the newest line in view until the reader scrolls
 * up; a line can be highlighted (a failure, the line a finding points at).
 */
import { ArrowDownToLine, WrapText } from 'lucide-react';
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';

export interface Segment {
  text: string;
  color?: 'red' | 'green' | 'yellow' | 'blue' | 'magenta' | 'cyan' | 'dim';
  bold?: boolean;
}

const SGR_COLOR: Record<number, Segment['color']> = {
  30: 'dim', 31: 'red', 32: 'green', 33: 'yellow', 34: 'blue', 35: 'magenta', 36: 'cyan', 37: undefined,
  90: 'dim', 91: 'red', 92: 'green', 93: 'yellow', 94: 'blue', 95: 'magenta', 96: 'cyan', 97: undefined,
};

// eslint-disable-next-line no-control-regex
const ESCAPE = /\u001b\[([0-9;]*)([A-Za-z])/g;

/** Splits one line of terminal output into coloured segments. Escapes other than colour are dropped. */
export function parseAnsi(line: string): Segment[] {
  const out: Segment[] = [];
  let color: Segment['color'];
  let bold = false;
  let dim = false;
  let last = 0;
  const push = (text: string) => {
    if (!text) return;
    const seg: Segment = { text };
    const shown = color ?? (dim ? 'dim' : undefined);
    if (shown) seg.color = shown;
    if (bold) seg.bold = true;
    out.push(seg);
  };
  for (const match of line.matchAll(ESCAPE)) {
    push(line.slice(last, match.index));
    last = (match.index ?? 0) + match[0].length;
    if (match[2] !== 'm') continue;
    const codes = (match[1] || '0').split(';').map(Number);
    for (const code of codes) {
      if (code === 0) {
        color = undefined;
        bold = false;
        dim = false;
      } else if (code === 1) bold = true;
      else if (code === 2) dim = true;
      else if (code === 22) {
        bold = false;
        dim = false;
      } else if (code === 39) color = undefined;
      else if (code in SGR_COLOR) color = SGR_COLOR[code];
    }
  }
  push(line.slice(last));
  return out;
}

const COLOR: Record<NonNullable<Segment['color']>, string> = {
  red: 'text-ansi-red',
  green: 'text-ansi-green',
  yellow: 'text-ansi-yellow',
  blue: 'text-ansi-blue',
  magenta: 'text-ansi-magenta',
  cyan: 'text-ansi-cyan',
  dim: 'text-terminal-dim',
};

export type LineMark = 'bad' | 'ok' | 'warn';

export interface TerminalProps {
  /** Lines of output, as the process wrote them (ANSI colour codes are fine). */
  lines: string[];
  /** What the region is, for screen readers: "Output of pnpm vitest run". */
  label: string;
  /** The command, shown as the first line after a $. */
  command?: string;
  /** Line numbers (1-based, counting from `firstLine`) to mark. */
  highlight?: Record<number, LineMark>;
  /** The number of the first line, when the block shows a window of a longer log. */
  firstLine?: number;
  numbered?: boolean;
  /** Start in follow mode: the newest line stays in view. */
  follow?: boolean;
  /** Show the bar under the output with follow and wrap. */
  controls?: boolean;
  /** Extra controls for the bar (a find box, a copy button). */
  tools?: ReactNode;
  /** The height class of the scroll box. */
  heightClass?: string;
  className?: string;
}

export function Terminal({
  lines,
  label,
  command,
  highlight,
  firstLine = 1,
  numbered = true,
  follow: followAtStart = false,
  controls = true,
  tools,
  heightClass = 'max-h-90',
  className,
}: TerminalProps) {
  const box = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(followAtStart);
  const [wrap, setWrap] = useState(false);
  const parsed = useMemo(() => lines.map(parseAnsi), [lines]);

  useLayoutEffect(() => {
    const el = box.current;
    if (follow && el) el.scrollTop = el.scrollHeight;
  }, [follow, parsed]);

  // Scrolling up leaves follow mode; reaching the end again takes it back.
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const onScroll = () => {
      const atEnd = el.scrollHeight - el.scrollTop - el.clientHeight < 8;
      setFollow((current) => (current === atEnd ? current : atEnd));
    };
    el.addEventListener('scroll', onScroll, { passive: true });
    return () => el.removeEventListener('scroll', onScroll);
  }, []);

  return (
    <div className={cn('overflow-hidden rounded-lg border border-line bg-terminal', className)}>
      {/* A log that scrolls needs to be reachable from the keyboard. */}
      <div
        ref={box}
        role="log"
        aria-label={label}
        aria-live="off"
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
        tabIndex={0}
        data-wrap={wrap ? '' : undefined}
        data-numbers={numbered ? undefined : 'off'}
        className={cn('ui-terminal overflow-auto py-3 focus-inset', heightClass)}
      >
        {command ? (
          <div data-line="">
            {numbered ? <span data-line-no="" /> : null}
            <span data-line-text="">
              <span className="text-terminal-dim">$ </span>
              {command}
            </span>
          </div>
        ) : null}
        {parsed.map((segments, index) => {
          const no = firstLine + index;
          return (
            <div key={no} data-line="" data-highlight={highlight?.[no]}>
              {numbered ? <span data-line-no="">{no}</span> : null}
              <span data-line-text="" title={wrap ? undefined : lines[index]?.replace(ESCAPE, '')}>
                {segments.length === 0 ? ' ' : null}
                {segments.map((seg, i) => (
                  <span key={i} className={cn(seg.color && COLOR[seg.color], seg.bold && 'font-semibold')}>
                    {seg.text}
                  </span>
                ))}
              </span>
            </div>
          );
        })}
      </div>
      {controls ? (
        <div className="flex flex-wrap items-center gap-2 border-t border-line bg-inset px-3 py-1 text-caption text-muted">
          <span className="font-mono tabular">{lines.length} lines</span>
          <div className="ml-auto flex items-center gap-1">
            {tools}
            <button
              type="button"
              aria-pressed={wrap}
              onClick={() => setWrap((value) => !value)}
              className="inline-flex h-7 items-center gap-1 rounded-md px-2 hover:bg-hover hover:text-fg aria-pressed:text-fg"
            >
              <WrapText aria-hidden="true" className="size-3.5" />
              Wrap
            </button>
            <button
              type="button"
              aria-pressed={follow}
              onClick={() => setFollow((value) => !value)}
              className="inline-flex h-7 items-center gap-1 rounded-md px-2 hover:bg-hover hover:text-fg aria-pressed:text-fg"
            >
              <ArrowDownToLine aria-hidden="true" className="size-3.5" />
              Follow
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
