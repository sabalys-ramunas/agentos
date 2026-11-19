/**
 * The page-to-data view, the heart of a Site API. Above, the page as the computer saw it. Below, what
 * it became: a table for a person, the JSON for a developer. A ribbon of wires runs between them, one
 * hue per field, and pointing at a value on either side lights its partner and draws the line between
 * them. Stacked rather than side by side so that a column on the page falls straight into the same
 * column of data, and both keep room for real values. Every state of an API shows here too: a broken
 * field is a broken wire, and a resting one has quiet wires.
 */
import { RefreshCw } from 'lucide-react';
import { RadioGroup } from 'radix-ui';
import type { ReactNode, RefObject } from 'react';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { SiteApiHealth, SiteField, SitePage, SitePageColumn, SiteRow } from '@/api/types';
import { Button } from '@/components/ui/button';
import { CopyButton } from '@/components/ui/misc';
import { cn } from '@/lib/cn';
import { plural, timeAgo } from '@/lib/format';
import { columnWeight, hueOf, hueStyle, PageSheet } from './page-sheet';
import type { Lit } from './page-sheet';
import './site-apis.css';

export type SplitView = 'table' | 'json';

const BAND = 72;
const LANE = 8;

interface Point {
  x: number;
  y: number;
}

interface Geometry {
  width: number;
  height: number;
  /** The bottom of the sheet and the top of the data. */
  from: number;
  to: number;
  top: Record<string, Point>;
  bottom: Record<string, Point>;
  outs: (Point & { label: string })[];
}

/** Fields stacked on a card share an x, so the wires leave side by side instead of on top of each other. */
function spread(points: [string, Point][]): [string, Point][] {
  const sorted = [...points].sort((a, b) => a[1].x - b[1].x);
  let last = -Infinity;
  return sorted.map(([key, point]) => {
    const x = Math.max(point.x, last + LANE + 4);
    last = x;
    return [key, { ...point, x }];
  });
}

function useGeometry(stage: RefObject<HTMLDivElement | null>, ...deps: unknown[]) {
  const [geometry, setGeometry] = useState<Geometry | null>(null);
  const measure = useCallback(() => {
    const element = stage.current;
    if (!element) return;
    const box = element.getBoundingClientRect();
    const sheet = element.querySelector('[data-sheet]')?.getBoundingClientRect();
    const data = element.querySelector('[data-panel]')?.getBoundingClientRect();
    if (!sheet || !data) return;
    const read = (selector: string, name: string, edge: 'center' | 'left' = 'center') =>
      [...element.querySelectorAll<HTMLElement>(selector)].map((item): [string, Point] => {
        const rect = item.getBoundingClientRect();
        return [item.dataset[name] ?? '', { x: (edge === 'left' ? rect.left : rect.left + rect.width / 2) - box.left, y: rect.top + rect.height / 2 - box.top }];
      });
    setGeometry({
      width: box.width,
      height: box.height,
      from: sheet.bottom - box.top,
      to: data.top - box.top,
      top: Object.fromEntries(spread(read('[data-top]', 'top'))),
      bottom: Object.fromEntries(read('[data-bottom]', 'bottom', element.querySelector('[data-json]') ? 'left' : 'center')),
      outs: read('[data-out-column]', 'outColumn').map(([label, point]) => ({ ...point, label })),
    });
  }, [stage]);
  useLayoutEffect(() => {
    measure();
    const element = stage.current;
    if (!element) return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    void document.fonts?.ready.then(measure);
    return () => observer.disconnect();
    // The anchors move with whatever the caller says moved them.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [measure, ...deps]);
  return geometry;
}

const ribbon = (x: number, from: number, X: number, to: number) => {
  const middle = (from + to) / 2;
  return `C${x} ${middle} ${X} ${middle} ${X} ${to}`;
};

/** A wire to a key in the JSON: down its own lane at the side, then in along the key's line. */
function toKey(from: Point, lane: number, to: number, key: Point) {
  const radius = 6;
  return `M${from.x} ${from.y} ${ribbon(from.x, from.y, lane, to)} V${key.y - radius} Q${lane} ${key.y} ${lane + radius} ${key.y} H${key.x - 4}`;
}

interface WireProps {
  geometry: Geometry;
  fields: SiteField[];
  view: SplitView;
  lit: Lit | null;
  /** Someone is pointing, so the other wires step back. */
  dim: boolean;
  litPoints: { from: Point; to: Point; entry: number } | null;
  broken?: string | null;
  /** The new column on the page, and the field it might be. */
  candidate?: { label: string; field: string } | null;
  quiet: boolean;
  reading: boolean;
  gutter: number;
}

function Wires({ geometry, fields, view, lit, dim, litPoints, broken, candidate, quiet, reading, gutter }: WireProps) {
  const { from, to } = geometry;
  const path = (field: SiteField, index: number) => {
    const top = geometry.top[field.key];
    const bottom = geometry.bottom[field.key];
    if (!top || !bottom) return null;
    const start = { x: top.x, y: from };
    return view === 'json' ? toKey(start, gutter + 12 + index * LANE, to, bottom) : `M${start.x} ${from} ${ribbon(start.x, from, bottom.x, to)}`;
  };
  return (
    <svg aria-hidden="true" width={geometry.width} height={geometry.height} className="split-wires pointer-events-none absolute top-0 left-0 z-10" data-any-lit={dim ? '' : undefined} data-quiet={quiet ? '' : undefined}>
      {geometry.outs.map((out) => {
        const target = candidate?.label === out.label ? geometry.bottom[candidate.field] : null;
        if (target) return <path key={out.label} d={`M${out.x} ${from} ${ribbon(out.x, from, target.x, to)}`} className="split-candidate" />;
        return (
          <g key={out.label}>
            <line x1={out.x} x2={out.x} y1={from} y2={from + 12} className="split-stub" />
            <line x1={out.x - 3} x2={out.x + 3} y1={from + 12} y2={from + 12} className="split-stub" />
          </g>
        );
      })}
      {fields.map((field, index) => {
        const line = path(field, index);
        if (!line) return null;
        if (field.key === broken) {
          const x = geometry.top[field.key]?.x ?? 0;
          const middle = from + (to - from) * 0.5;
          return (
            <g key={field.key}>
              <path d={`M${x} ${from} V${middle - 8}`} className="split-broken" />
              <path d={`M${x - 4} ${middle - 12} l8 8 m0 -8 l-8 8`} stroke="var(--stuck-dot)" strokeWidth={1.5} fill="none" strokeLinecap="round" />
            </g>
          );
        }
        return (
          <g key={field.key} style={hueStyle(index)}>
            <path d={line} className="split-wire" />
            {reading ? <path d={line} pathLength={1} className="split-current" style={{ animationDelay: `${index * 70}ms` }} /> : null}
          </g>
        );
      })}
      {lit && litPoints && lit.field !== broken ? (
        <g style={hueStyle(fields.findIndex((field) => field.key === lit.field))}>
          <path
            d={
              view === 'json'
                ? `M${litPoints.from.x} ${litPoints.from.y} V${from} ${ribbon(litPoints.from.x, from, gutter + 12 + fields.findIndex((field) => field.key === lit.field) * LANE, to)} V${litPoints.entry} H${litPoints.to.x - 6} V${litPoints.to.y} H${litPoints.to.x}`
                : `M${litPoints.from.x} ${litPoints.from.y} V${from} ${ribbon(litPoints.from.x, from, litPoints.to.x, to)} V${litPoints.to.y}`
            }
            className="split-lit"
          />
          {[litPoints.from, litPoints.to].map((point, index) => (
            <circle key={index} cx={point.x} cy={point.y} r={3.5} fill="var(--hue)" stroke="var(--background)" strokeWidth={2} />
          ))}
        </g>
      ) : null}
    </svg>
  );
}

function ViewSwitch({ value, onChange }: { value: SplitView; onChange: (view: SplitView) => void }) {
  const options: [SplitView, string][] = [
    ['table', 'Table'],
    ['json', 'JSON'],
  ];
  return (
    <RadioGroup.Root aria-label="Show the data as" value={value} onValueChange={(next) => onChange(next as SplitView)} className="flex h-7 rounded-md bg-hover p-px">
      {options.map(([key, label]) => (
        <RadioGroup.Item
          key={key}
          value={key}
          className="rounded-sm px-2 text-caption font-medium text-muted transition-colors duration-(--duration-fast) ease-standard hover:text-fg data-[state=checked]:bg-card data-[state=checked]:text-fg data-[state=checked]:shadow-[0_0_0_1px_var(--border)]"
        >
          {label}
        </RadioGroup.Item>
      ))}
    </RadioGroup.Root>
  );
}

interface PanelProps {
  fields: SiteField[];
  rows: SiteRow[];
  lit: Lit | null;
  onFocusField: (field: string | null) => void;
  newRows?: number[];
  held?: string | null;
  faded?: boolean;
}

function DataTable({ fields, rows, lit, onFocusField, newRows, held, faded }: PanelProps) {
  const template = fields.map((field) => columnWeight({ field: field.key, label: field.label })).join(' ');
  return (
    <div role="table" aria-label="What it read" className={cn('p-3', faded && 'opacity-55')}>
      <div role="row" className="grid border-b border-line pb-1" style={{ gridTemplateColumns: template }}>
        {fields.map((field, index) => (
          <button
            key={field.key}
            role="columnheader"
            type="button"
            data-bottom={field.key}
            onFocus={() => onFocusField(field.key)}
            onBlur={() => onFocusField(null)}
            data-cell={`${field.key}|-1`}
            className="focus-inset flex h-7 min-w-0 items-center gap-2 rounded-sm px-2 text-left text-caption font-medium text-muted"
          >
            <span aria-hidden="true" className="size-2 shrink-0 rounded-full" style={{ backgroundColor: hueOf(index) }} />
            <span className="truncate">{field.label}</span>
          </button>
        ))}
      </div>
      {rows.map((row, rowIndex) => (
        <div key={rowIndex} role="row" className="grid border-b border-line last:border-b-0" style={{ gridTemplateColumns: template }}>
          {fields.map((field, index) => {
            const isHeld = held === field.key;
            return (
              <div
                key={field.key}
                role="cell"
                data-cell={`${field.key}|${rowIndex}`}
                data-lit={lit?.field === field.key && lit.row === rowIndex ? '' : undefined}
                data-row-lit={lit?.row === rowIndex && lit.field !== field.key ? '' : undefined}
                style={hueStyle(index)}
                title={row[field.key]}
                className={cn('split-cell flex h-9 min-w-0 items-center gap-2 px-2 text-body', field.kind === 'money' || field.kind === 'count' ? 'tabular' : undefined, isHeld && 'text-faint')}
              >
                {isHeld ? (
                  <span className="truncate text-secondary">Held back</span>
                ) : (
                  <>
                    <span className="truncate">{row[field.key]}</span>
                    {index === 0 && newRows?.includes(rowIndex) ? <span className="shrink-0 rounded-sm border border-line px-1 text-micro text-muted font-normal">New</span> : null}
                  </>
                )}
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}

/** Real JSON, set three records across so a page of it still fits. The copy button gives the whole of it. */
function DataJson({ fields, rows, lit, onFocusField, gutter, name, total }: PanelProps & { gutter: number; name: string; total: number }) {
  return (
    <div data-json="" className="mono grid gap-2 py-3 pr-3 text-caption leading-5" style={{ paddingLeft: gutter + 12 + fields.length * LANE + 8 }}>
      <p className="text-faint">
        {'{ '}
        <span className="text-muted">"{name}"</span>
        {': [ '}
        <span className="font-sans">{plural(total, 'record')}, {rows.length} shown</span>
      </p>
      <div className="grid grid-cols-3 gap-x-4 gap-y-2">
        {rows.map((row, rowIndex) => (
          <div key={rowIndex} data-record="" className="min-w-0">
            <p className="text-faint">{'{'}</p>
            {fields.map((field, index) => (
              <p
                key={field.key}
                data-cell={`${field.key}|${rowIndex}`}
                data-lit={lit?.field === field.key && lit.row === rowIndex ? '' : undefined}
                data-row-lit={lit?.row === rowIndex && lit.field !== field.key ? '' : undefined}
                style={hueStyle(index)}
                className="split-cell truncate pl-2"
              >
                <button
                  type="button"
                  data-bottom={rowIndex === 0 ? field.key : undefined}
                  onFocus={() => onFocusField(field.key)}
                  onBlur={() => onFocusField(null)}
                  tabIndex={rowIndex === 0 ? 0 : -1}
                  className="focus-inset rounded-sm text-muted"
                >
                  "{field.key}"
                </button>
                <span className="text-faint">: </span>
                <span title={row[field.key]}>"{row[field.key]}"</span>
                <span className="text-faint">{index < fields.length - 1 ? ',' : ''}</span>
              </p>
            ))}
            <p className="text-faint">{'}'}{rowIndex < rows.length - 1 ? ',' : ''}</p>
          </div>
        ))}
      </div>
      <p className="text-faint">{'] }'}</p>
    </div>
  );
}

export interface SplitProps {
  page: SitePage;
  fields: SiteField[];
  rows: SiteRow[];
  total: number;
  name: string;
  health?: SiteApiHealth;
  newRows?: number[];
  readAt?: string | null;
  /** Lays a note over the page. */
  veil?: ReactNode;
  /** The page as it is now, when it changed. */
  columns?: SitePageColumn[];
  broken?: string | null;
  /** A field whose values are not being passed on. */
  held?: string | null;
  arrived?: string | null;
  note?: ReactNode;
  /** What the lower panel holds, when it is not today's data. */
  dataLabel?: string;
  reading?: boolean;
  onRead?: () => void;
  readBusy?: boolean;
  action?: ReactNode;
  /** The field lit when nothing is being pointed at, so the picture explains itself. */
  rest?: Lit | null;
  limit?: number;
}

export function Split({ page, fields, rows, total, name, health = 'good', newRows, readAt, veil, columns, broken, held, arrived, note, dataLabel = 'What it becomes', reading = false, onRead, readBusy, action, rest, limit = 6 }: SplitProps) {
  const stage = useRef<HTMLDivElement>(null);
  const [view, setView] = useState<SplitView>('table');
  const [pointed, setPointed] = useState<Lit | null>(null);
  const quiet = health === 'resting' || health === 'paused' || health === 'sign_in' || health === 'site_check';
  const empty = health === 'empty';
  const lit = quiet ? null : (pointed ?? rest ?? null);
  const shown = useMemo(() => rows.slice(0, limit), [rows, limit]);
  const geometry = useGeometry(stage, view, fields.length, columns, shown.length, health);
  const [litPoints, setLitPoints] = useState<{ from: Point; to: Point; entry: number } | null>(null);
  const gutter = 8;

  useEffect(() => {
    const element = stage.current;
    if (!element || !lit) {
      setLitPoints(null);
      return;
    }
    const box = element.getBoundingClientRect();
    // The right edge of a cell, so the line runs down the column and never through a value.
    const edge = (node: Element | null | undefined): Point | null => {
      if (!node) return null;
      const rect = node.getBoundingClientRect();
      return { x: rect.right - 10 - box.left, y: rect.top + rect.height / 2 - box.top };
    };
    const cells = element.querySelectorAll(`[data-cell="${CSS.escape(lit.field)}|${Math.max(0, lit.row)}"]`);
    const sheetCell = [...cells].find((cell) => cell.closest('[data-sheet]'));
    const dataCell = [...cells].find((cell) => cell.closest('[data-panel]'));
    const from = edge(sheetCell);
    const to = edge(dataCell);
    if (from && to) {
      const left = dataCell?.getBoundingClientRect().left ?? 0;
      // In the JSON the line comes in above the record, so it never runs across another one's text.
      const record = dataCell?.closest('[data-record]')?.getBoundingClientRect().top ?? 0;
      setLitPoints({ from, to: view === 'json' ? { x: left - box.left + 2, y: to.y } : to, entry: record - box.top - 3 });
    } else setLitPoints(null);
  }, [lit?.field, lit?.row, view, geometry, lit]);

  const point = (target: EventTarget) => {
    const cell = (target as HTMLElement).closest<HTMLElement>('[data-cell]');
    if (!cell?.dataset.cell) return;
    const [field = '', row = '0'] = cell.dataset.cell.split('|');
    setPointed((now) => (now?.field === field && now.row === Math.max(0, Number(row)) ? now : { field, row: Math.max(0, Number(row)) }));
  };

  const left = page.columns.filter((column) => !column.field).length;
  const json = useMemo(() => JSON.stringify({ [name]: rows }, null, 2), [name, rows]);

  return (
    <section aria-label="The page and the data it becomes" className="board-surface registered rounded-(--radius-card) border border-board-line">
      <div className="scrollbar-none overflow-x-auto rounded-(--radius-card)">
        <div
          ref={stage}
          className="relative min-w-[760px] p-4 md:p-5"
          onPointerOver={(event) => point(event.target)}
          onPointerLeave={() => setPointed(null)}
        >
          <div className="mb-2 flex h-7 items-center justify-between gap-4">
            <p className="silk">The page, as your computer saw it</p>
            <p className="tabular text-caption text-faint">{note ?? (readAt ? `Read ${timeAgo(readAt).toLowerCase()}` : 'Not read yet')}</p>
          </div>
          <PageSheet page={page} rows={rows} fields={fields} lit={lit} limit={limit} veil={veil} columns={columns} gone={broken} arrived={arrived} reading={reading} empty={empty} />
          <div className="relative grid place-items-center" style={{ height: BAND }}>
            <p className="tabular relative z-20 rounded-full border border-board-line bg-board px-2 py-px text-caption text-muted">
              {empty ? `${plural(0, 'row')} found` : `${plural(total, 'row')}, ${plural(fields.length, 'column')} kept${left ? `, ${left} left out` : ''}`}
            </p>
          </div>
          <div data-panel="" className="overflow-hidden rounded-lg border border-board-line bg-card">
            <div className="flex h-11 items-center gap-2 border-b border-line px-3">
              <p className="silk mr-auto" style={view === 'json' ? { paddingLeft: gutter + fields.length * LANE + 8 } : undefined}>
                {dataLabel}
              </p>
              {action}
              {onRead ? (
                <Button size="sm" variant="ghost" loading={readBusy} icon={<RefreshCw aria-hidden="true" className="size-4" />} onClick={onRead}>
                  Read it now
                </Button>
              ) : null}
              <ViewSwitch value={view} onChange={setView} />
              <CopyButton text={json} label="Copy this JSON" />
            </div>
            {view === 'table' ? (
              <DataTable fields={fields} rows={shown} lit={lit} onFocusField={(field) => setPointed(field ? { field, row: 0 } : null)} newRows={newRows} held={held ?? broken} faded={quiet || empty} />
            ) : (
              <DataJson fields={fields} rows={shown} lit={lit} onFocusField={(field) => setPointed(field ? { field, row: 0 } : null)} gutter={gutter} name={name} total={total} />
            )}
            {total > shown.length ? <p className="border-t border-line px-5 py-2 text-caption text-faint">and {total - shown.length} more below, under See the data</p> : null}
          </div>
          {geometry ? <Wires geometry={geometry} fields={fields} view={view} lit={lit} dim={pointed !== null} litPoints={litPoints} broken={broken} candidate={arrived && held ? { label: arrived, field: held } : null} quiet={quiet} reading={reading} gutter={stageGutter(stage.current, gutter)} /> : null}
        </div>
      </div>
    </section>
  );
}

/** Where the JSON's side lanes start, in stage coordinates. */
function stageGutter(stage: HTMLDivElement | null, gutter: number) {
  const panel = stage?.querySelector('[data-panel]')?.getBoundingClientRect();
  const box = stage?.getBoundingClientRect();
  return panel && box ? panel.left - box.left + gutter : gutter;
}
