/**
 * The page as the computer saw it, drawn as a plain sheet: its address, its menu, and what it found
 * outlined. This is a wireframe on purpose. It holds the page's text as text and never its look, so
 * it cannot pass for the real site, and it never shows a login. Columns left out stay visible under
 * a hatch, because seeing what was skipped is half of trusting what was kept.
 */
import { Lock } from 'lucide-react';
import type { CSSProperties, ReactNode } from 'react';
import type { SiteField, SitePage, SitePageColumn, SiteRow } from '@/api/types';
import { cn } from '@/lib/cn';

export interface Lit {
  field: string;
  row: number;
}

export const hueOf = (index: number) => `var(--field-${(index % 6) + 1})`;
export const hueStyle = (index: number) => ({ '--hue': hueOf(index) }) as CSSProperties;

const WIDE = new Set(['coffee', 'customer', 'product', 'to', 'client', 'property', 'guest', 'tour', 'review', 'route', 'room', 'status']);

/** Long text gets the room. Everything else fits its value. */
export function columnWeight(column: { field: string | null; label: string }) {
  if (column.field && WIDE.has(column.field)) return 'minmax(0, 1.9fr)';
  return 'minmax(0, 1fr)';
}

const cellText = (column: SitePageColumn, row: SiteRow, index: number) => (column.field ? (row[column.field] ?? '') : (column.values?.[index] ?? ''));

interface SheetProps {
  page: SitePage;
  rows: SiteRow[];
  fields: SiteField[];
  lit: Lit | null;
  /** Fewer rows than this are not drawn: the sheet is a cut of the page, not all of it. */
  limit?: number;
  /** Replaces the columns, to show how the page looks now. */
  columns?: SitePageColumn[];
  /** A column that has gone from the page, outlined in red. */
  gone?: string | null;
  /** A column that has appeared on the page, outlined in amber. */
  arrived?: string | null;
  /** Dims the page and lays a note over it. */
  veil?: ReactNode;
  /** Teach it: the regions a person can point at. */
  picked?: string | null;
  onPick?: (id: string) => void;
  /** Teach it: pressing a column keeps it or leaves it out. */
  onColumn?: (label: string) => void;
  showRegions?: boolean;
  /** Teach it: what the computer calls each region. */
  regionWords?: Record<string, string>;
  reading?: boolean;
  empty?: boolean;
}

function Chrome({ page }: { page: SitePage }) {
  return (
    <div className="flex h-8 items-center gap-2 border-b border-line bg-subtle px-3">
      <Lock aria-hidden="true" className="size-3 shrink-0 text-faint" />
      <span className="mono min-w-0 truncate text-12 text-muted">{page.url.replace('https://', '')}</span>
    </div>
  );
}

function Masthead({ page, region }: { page: SitePage; region?: (id: string, body: ReactNode, className?: string) => ReactNode }) {
  const nav = (
    <div className="flex min-w-0 gap-4 overflow-hidden">
      {page.nav.map((item, index) => (
        <span key={item} className={cn('text-12 whitespace-nowrap', index === 1 ? 'font-medium text-muted' : 'text-faint')}>
          {item}
        </span>
      ))}
    </div>
  );
  return (
    <div className="flex h-10 items-center gap-6 border-b border-line px-4">
      <span className="text-13 font-semibold whitespace-nowrap">{page.wordmark}</span>
      {region ? region('nav', nav, 'min-w-0') : nav}
      <span aria-hidden="true" className="ml-auto h-4 w-14 shrink-0 rounded-sm bg-fill-2" />
    </div>
  );
}

function Side({ page }: { page: SitePage }) {
  return (
    <ul className="grid content-start gap-2 py-1">
      {page.side.map((note) => (
        <li key={note} className="border-l border-line pl-2 text-11 leading-4 text-faint">
          {note}
        </li>
      ))}
    </ul>
  );
}

function TableBody({ page, rows, fields, lit, limit, columns, gone, arrived, onColumn, empty }: SheetProps & { limit: number }) {
  const shown = columns ?? page.columns;
  const count = empty ? 0 : Math.min(limit, rows.length);
  const template = shown.map(columnWeight).join(' ');
  return (
    <div className="grid gap-1" style={{ gridTemplateColumns: template }}>
      {shown.map((column) => {
        const index = fields.findIndex((item) => item.key === column.field);
        const kept = index >= 0;
        const isGone = gone !== null && gone !== undefined && column.field === gone;
        const isNew = arrived === column.label;
        const columnLit = kept && lit?.field === column.field;
        const inner = (
          <>
            <div
              data-top={kept ? column.field : undefined}
              data-out-column={kept ? undefined : column.label}
              className={cn('flex h-7 items-center truncate px-2 text-11 font-medium', kept ? 'text-muted' : 'text-faint')}
            >
              {column.label}
            </div>
            {Array.from({ length: count }, (_, row) => {
              const value = rows[row] ? cellText(column, rows[row], row) : '';
              return (
                <div
                  key={row}
                  data-cell={kept ? `${column.field}|${row}` : undefined}
                  data-lit={columnLit && lit?.row === row ? '' : undefined}
                  data-row-lit={!columnLit && lit?.row === row ? '' : undefined}
                  title={value}
                  style={kept ? hueStyle(index) : undefined}
                  className={cn('split-cell flex h-7 items-center truncate px-2 text-12', kept ? 'text-fg' : 'text-faint')}
                >
                  <span className="truncate">{value}</span>
                </div>
              );
            })}
            {empty ? <div aria-hidden="true" className="h-20" /> : null}
          </>
        );
        const props = {
          'data-kept': kept && !isGone ? '' : undefined,
          'data-out': !kept && !isNew ? '' : undefined,
          'data-gone': isGone ? '' : undefined,
          'data-new': isNew ? '' : undefined,
          'data-lit': columnLit ? '' : undefined,
          style: kept ? hueStyle(index) : undefined,
        };
        return onColumn ? (
          <button
            key={column.label}
            type="button"
            aria-pressed={kept}
            aria-label={`${column.label}: ${kept ? 'kept. Press to leave it out' : 'left out. Press to keep it'}`}
            onClick={() => onColumn(column.label)}
            className="split-column teach-column focus-inset min-w-0 text-left"
            {...props}
          >
            {inner}
          </button>
        ) : (
          <div key={column.label} className="split-column min-w-0" {...props}>
            {inner}
          </div>
        );
      })}
    </div>
  );
}

function Spot({ field, row, index, lit, className, children }: { field: string; row: number; index: number; lit: Lit | null; className?: string; children: ReactNode }) {
  return (
    <span
      data-cell={`${field}|${row}`}
      data-top={row === 0 ? field : undefined}
      data-lit={lit?.field === field && (lit.row === row || row === 0) ? '' : undefined}
      style={hueStyle(index)}
      className={cn('split-spot inline-block min-w-0 truncate px-1', className)}
    >
      {children}
    </span>
  );
}

function Cards({ rows, fields, lit, limit }: { rows: SiteRow[]; fields: SiteField[]; lit: Lit | null; limit: number }) {
  const order = fields.map((item, index) => ({ item, index }));
  return (
    <div className="grid grid-cols-4 gap-2">
      {rows.slice(0, limit).map((row, rowIndex) => (
        <div key={rowIndex} className="grid gap-2 rounded-md border border-line p-2">
          <div aria-hidden="true" className="h-10 rounded-sm bg-fill-1" />
          {order.map(({ item, index }) => (
            <Spot key={item.key} field={item.key} row={rowIndex} index={index} lit={lit} className={item.key === 'product' ? 'text-12 font-medium' : 'text-11 text-muted'}>
              {item.key === 'product' ? row[item.key] : `${item.label}: ${row[item.key]}`}
            </Spot>
          ))}
        </div>
      ))}
    </div>
  );
}

function Thread({ rows, fields, lit, limit }: { rows: SiteRow[]; fields: SiteField[]; lit: Lit | null; limit: number }) {
  const position = (key: string) => fields.findIndex((item) => item.key === key);
  const [first, second, third, fourth] = fields;
  return (
    <ul className="grid gap-2">
      {rows.slice(0, limit).map((row, rowIndex) => (
        <li key={rowIndex} className="grid grid-cols-[24px_minmax(0,1fr)] gap-2 border-b border-line pb-2 last:border-b-0">
          <span aria-hidden="true" className="size-6 rounded-full bg-fill-2" />
          <div className="grid min-w-0 gap-1">
            <div className="flex min-w-0 items-center gap-4 text-11 text-muted">
              {first ? <Spot field={first.key} row={rowIndex} index={position(first.key)} lit={lit} className="font-medium text-fg">{row[first.key]}</Spot> : null}
              {second ? <Spot field={second.key} row={rowIndex} index={position(second.key)} lit={lit}>{row[second.key]}</Spot> : null}
              {fourth ? <Spot field={fourth.key} row={rowIndex} index={position(fourth.key)} lit={lit} className="text-faint">{row[fourth.key]}</Spot> : null}
            </div>
            {third ? <Spot field={third.key} row={rowIndex} index={position(third.key)} lit={lit} className="text-12 text-fg">{row[third.key]}</Spot> : null}
          </div>
        </li>
      ))}
    </ul>
  );
}

export function PageSheet(props: SheetProps) {
  const { page, rows, fields, lit, limit = 6, veil, picked, onPick, showRegions, regionWords, reading, empty } = props;
  const pickable = (id: string, body: ReactNode, className?: string) =>
    showRegions ? (
      <button type="button" data-picked={picked === id ? '' : undefined} onClick={() => onPick?.(id)} className={cn('teach-region focus-inset block text-left', className)}>
        {regionWords?.[id] ? <span className="teach-tag rounded-sm bg-blue px-1 text-12 font-medium whitespace-nowrap text-on-primary">{regionWords[id]}</span> : null}
        {body}
      </button>
    ) : (
      body
    );
  const hasSide = page.side.length > 0 && page.layout === 'table';
  const main = (
    <div className="min-w-0">
      <div className="mb-2 flex items-baseline gap-2 px-1">
        <p className="text-14 font-semibold">{page.heading}</p>
        <p className="mono text-12 text-faint">{page.count_words}</p>
        <span aria-hidden="true" className="ml-auto flex gap-2">
          <span className="h-5 w-16 rounded-sm border border-line" />
          <span className="h-5 w-10 rounded-sm bg-fill-2" />
        </span>
      </div>
      {page.layout === 'cards' ? (
        <Cards rows={rows} fields={fields} lit={lit} limit={Math.min(limit, 4)} />
      ) : page.layout === 'thread' ? (
        <Thread rows={rows} fields={fields} lit={lit} limit={Math.min(limit, 3)} />
      ) : (
        <TableBody {...props} limit={limit} />
      )}
    </div>
  );
  return (
    <div className={cn('relative rounded-lg border border-board-line bg-bg', !showRegions && 'overflow-hidden')} data-sheet="">
      <Chrome page={page} />
      <Masthead page={page} region={showRegions ? pickable : undefined} />
      <div className={cn('relative grid gap-4 p-3', hasSide && 'grid-cols-[132px_minmax(0,1fr)]', veil && 'opacity-40 grayscale')} aria-hidden={veil ? true : undefined}>
        {hasSide ? pickable('side', <Side page={page} />, 'self-start') : null}
        {pickable('main', main, 'min-w-0')}
      </div>
      {veil ? <div className="absolute inset-x-0 top-[72px] bottom-0 grid place-items-center p-4">{veil}</div> : null}
      {reading ? <div aria-hidden="true" className="split-scan" /> : null}
      {empty ? (
        <p className="absolute inset-x-0 bottom-8 text-center">
          <span className="rounded-sm bg-bg px-2 py-1 text-13 text-muted">The page shows no rows here.</span>
        </p>
      ) : null}
    </div>
  );
}
