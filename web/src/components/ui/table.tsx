/**
 * The Mono table (web/DESIGN.md, Tables). No grey band: the head is the table-header role in the
 * tertiary ink over one hairline, sticky in its scroll box. Rows are 52 px (40 compact, 60 relaxed).
 * Figures sit right in Geist's tabular figures at the body size; money sits right in Geist Mono. Hover is a fill; selection a tint with a
 * 2 px violet edge. Under 768 px a table with `stack` becomes a list. The rules live in primitives.css.
 */
import { ArrowDown, ArrowUpDown } from 'lucide-react';
import type { HTMLAttributes, KeyboardEvent, ReactNode, TdHTMLAttributes, ThHTMLAttributes } from 'react';
import { cn } from '@/lib/cn';
import { Skeleton } from './states';

export type Density = 'compact' | 'default' | 'relaxed';

export interface TableProps extends HTMLAttributes<HTMLTableElement> {
  density?: Density;
  /** Under 768 px, draw the rows as a list instead of a table that scrolls sideways. */
  stack?: boolean;
}

export function Table({ className, density = 'default', stack = false, ...props }: TableProps) {
  return <table data-density={density} data-stack={stack ? '' : undefined} className={cn('ui-table', className)} {...props} />;
}

/** The scroll box a wide table sits in, so the page never scrolls sideways. */
export function TableScroll({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('min-w-0 overflow-x-auto', className)} {...props} />;
}

export type SortDirection = 'asc' | 'desc';

export interface ThProps extends Omit<ThHTMLAttributes<HTMLTableCellElement>, 'align'> {
  /** The direction this column is sorted in, or undefined when another column leads. */
  sort?: SortDirection;
  /** Makes the head a button. Called with the direction the column should take next. */
  onSort?: (next: SortDirection) => void;
  /** `end` for anything made of digits: the head sits over its numbers. */
  align?: 'start' | 'end';
  /** A unit, shown after the label in the tertiary ink: "Memory" + "MB". */
  unit?: string;
  /** In the phone list form: `hide` folds the head away with its column's cells, so a screen reader never
   * hears a column ("Age") that has no value under it. */
  phone?: 'hide';
}

export function Th({ className, sort, onSort, align = 'start', unit, phone, children, ...props }: ThProps) {
  const ariaSort = sort === 'asc' ? 'ascending' : sort === 'desc' ? 'descending' : undefined;
  const label = (
    <>
      {children}
      {unit ? <span className="font-normal text-faint"> {unit}</span> : null}
    </>
  );
  return (
    <th
      scope="col"
      data-phone={phone}
      aria-sort={ariaSort}
      className={cn(align === 'end' && 'text-right', className)}
      {...props}
    >
      {onSort ? (
        <button
          type="button"
          onClick={() => onSort(sort === 'desc' ? 'asc' : 'desc')}
          className={cn(
            'group/sort -mx-2 inline-flex h-7 items-center gap-1 rounded-md px-2 text-inherit',
            'transition-colors duration-(--duration-fast) ease-standard hover:bg-hover hover:text-fg',
            // The arrow sits on the label's far side, so the label never moves.
            align === 'end' && 'flex-row-reverse',
          )}
        >
          <span>{label}</span>
          {sort ? (
            <ArrowDown aria-hidden="true" className={cn('size-3.5 transition-transform duration-(--duration-fast)', sort === 'asc' && 'rotate-180')} />
          ) : (
            <ArrowUpDown
              aria-hidden="true"
              className="size-3.5 opacity-0 transition-opacity duration-(--duration-fast) group-hover/sort:opacity-100 group-focus-visible/sort:opacity-100"
            />
          )}
        </button>
      ) : (
        label
      )}
    </th>
  );
}

export interface TdProps extends TdHTMLAttributes<HTMLTableCellElement> {
  /** Figures (counts, durations, percentages): right aligned, Geist at the body size, tabular figures. */
  numeric?: boolean;
  /** Money: right aligned, Geist Mono at 13, tabular, so the cents line up down the column. */
  money?: boolean;
  /** In the phone list form: `hide` folds the cell away. */
  phone?: 'hide';
  /** In the phone list form, a short label shown before the value ("Memory"). */
  label?: string;
  /** The cell that takes the spare width; the phone list gives it a line of its own. */
  main?: boolean;
}

export function Td({ className, numeric, money, phone, label, main, ...props }: TdProps) {
  return (
    <td
      data-numeric={numeric || money ? '' : undefined}
      data-phone={phone}
      data-label={label}
      data-main={main ? '' : undefined}
      className={cn(
        'whitespace-nowrap',
        numeric && 'text-right text-fg tabular',
        money && 'text-right font-mono text-number text-fg tabular',
        main && mainCell,
        className,
      )}
      {...props}
    />
  );
}

/** The cell that takes the spare width and truncates. Every other column fits its content. */
export const mainCell = 'w-full max-w-0 md:min-w-40 lg:min-w-55';

/** A missing value: an en dash in the tertiary ink. A zero is a zero. */
export function NoValue() {
  return (
    <span className="text-faint" aria-label="None">
      –
    </span>
  );
}

export interface TrProps extends HTMLAttributes<HTMLTableRowElement> {
  selected?: boolean;
  /** Opens or picks something: the row takes a hover fill, a pointer and Enter. */
  onActivate?: () => void;
}

export function Tr({ className, selected, onActivate, onKeyDown, ...props }: TrProps) {
  return (
    <tr
      aria-selected={selected === undefined ? undefined : selected}
      data-interactive={onActivate ? '' : undefined}
      tabIndex={onActivate ? 0 : undefined}
      onClick={onActivate ? () => onActivate() : undefined}
      onKeyDown={(event: KeyboardEvent<HTMLTableRowElement>) => {
        onKeyDown?.(event);
        if (onActivate && event.key === 'Enter' && event.target === event.currentTarget) onActivate();
      }}
      className={className}
      {...props}
    />
  );
}

/** The two lines of a main cell: a title that truncates with its full text on hover, and a second line. */
export function CellTitle({ title, second, mono }: { title: ReactNode; second?: ReactNode; mono?: boolean }) {
  const text = typeof title === 'string' ? title : undefined;
  const secondText = typeof second === 'string' ? second : undefined;
  return (
    <div className="grid min-w-0">
      <span title={text} className={cn('truncate font-medium text-fg', mono && 'font-mono text-code')}>
        {title}
      </span>
      {second ? (
        <span data-second-line="" title={secondText} className="truncate text-secondary text-faint">
          {second}
        </span>
      ) : null}
    </div>
  );
}

/** Row actions: fade in on hover and focus, always there on a touch screen. */
export function RowActions({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div data-row-actions="" className={cn('inline-flex justify-end gap-px', className)} {...props} />;
}

/** A skeleton shaped like the table: the real head and `rows` rows at the real height. */
export function TableSkeleton({ columns, rows = 7, density = 'default' }: { columns: string[]; rows?: number; density?: Density }) {
  return (
    <TableScroll>
    <Table density={density} aria-hidden="true">
      <thead>
        <tr>
          {columns.map((column, index) => (
            <Th key={index}>{column}</Th>
          ))}
        </tr>
      </thead>
      <tbody>
        {Array.from({ length: rows }, (_, row) => (
          <tr key={row}>
            {columns.map((_, index) => (
              <td key={index}>
                <Skeleton className={cn('h-3', index === 0 ? 'w-24 max-w-full md:w-48' : 'w-10 md:w-16')} />
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </Table>
    </TableScroll>
  );
}

/** The empty table: the head stays, and the body says what would be here and the next step. */
export function TableEmpty({ colSpan, children }: { colSpan: number; children: ReactNode }) {
  return (
    <tr>
      <td colSpan={colSpan} className="h-auto! border-b-0 p-0! whitespace-normal">
        {children}
      </td>
    </tr>
  );
}
