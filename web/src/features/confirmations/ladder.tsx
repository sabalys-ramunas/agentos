/**
 * How strong a yes each thing needs, as a grid: where it applies down the side (everywhere, then each
 * business), what it is across the top, and in each cell a four-step ladder. More filled steps, a
 * stronger yes. Each step is a radio, so a click or the arrow keys move it, and only the steps a row
 * may take are offered: what is under the floor is drawn, never met.
 */
import { RadioGroup } from 'radix-ui';
import { Globe, Lock } from 'lucide-react';
import type { ConfirmRow } from '@/api/types';
import { BusinessMark } from '@/components/brand/identity';
import { cn } from '@/lib/cn';
import type { Rung, Rungs } from '@/lib/confirm';
import { RUNG_WORDS, RUNGS, rungIndex, rungLine, ROWS } from '@/lib/confirm';

/** The column heads: what each row is, in two or three words. The full title is the cell's name. */
const SHORT: Record<ConfirmRow, string> = {
  commit: 'Posts and replies',
  marked: 'Ask me first',
  after_untrusted: 'After outside words',
  pay: 'Money',
  irreversible: "Can't be undone",
};

/** The fill of a step for each strength: grey for on its own, then violet getting deeper. */
const FILL: Record<Rung, string> = {
  auto: 'bg-control',
  // The accent roles carry their own dark values, so the ladder reads on the night navy too.
  chat: 'bg-accent-half',
  signed_in: 'bg-accent',
  present: 'bg-accent-text',
};

/** A ladder drawn, for the legend and for a cell nobody can change. */
export function LadderMark({ rung, className }: { rung: Rung; className?: string }) {
  const filled = rungIndex(rung) + 1;
  return (
    <span aria-hidden="true" className={cn('inline-flex items-center gap-1', className)}>
      {RUNGS.map((step, index) => (
        <span key={step} className={cn('size-3 rounded-sm', index < filled ? FILL[rung] : 'border border-line-strong bg-card')} />
      ))}
    </span>
  );
}

/** The four strengths, each with what it takes: the ladder's legend. */
export function LadderLegend({ chat }: { chat: boolean }) {
  return (
    <ul aria-label="The four strengths" className="grid grid-cols-2 gap-x-6 gap-y-3 md:grid-cols-4">
      {RUNGS.map((rung) => (
        <li key={rung} className="grid gap-1">
          <span className="flex items-center gap-2 text-secondary text-fg">
            <LadderMark rung={rung} />
            {RUNG_WORDS[rung].word}
          </span>
          <span className="text-caption text-faint">{rungLine(rung, chat)}</span>
        </li>
      ))}
    </ul>
  );
}

interface CellProps {
  /** "Money", or "Money, Meridian Travel": the cell's name for a screen reader. */
  label: string;
  rung: Rung;
  /** The lowest step this cell may take. */
  floor: Rung;
  /** Points at a step before choosing it, so the day above can show what it would mean. */
  onPeek?: (rung: Rung | null) => void;
  onPick?: (rung: Rung) => void;
}

function Cell({ label, rung, floor, onPeek, onPick }: CellProps) {
  const filled = rungIndex(rung) + 1;
  const lowest = rungIndex(floor);
  if (!onPick) {
    return (
      <span title={`${label}: ${RUNG_WORDS[rung].word}`} className="inline-flex h-8 items-center">
        <LadderMark rung={rung} />
        <span className="sr-only">{RUNG_WORDS[rung].word}</span>
      </span>
    );
  }
  return (
    <RadioGroup.Root
      aria-label={`How strong a yes: ${label}`}
      value={rung}
      onValueChange={(next) => onPick(next as Rung)}
      onPointerLeave={() => onPeek?.(null)}
      onBlur={() => onPeek?.(null)}
      orientation="horizontal"
      className="inline-flex h-8 items-center"
    >
      {RUNGS.map((step, index) => {
        const square = cn('block size-3 rounded-sm transition-colors duration-(--duration-fast) ease-standard', index < filled ? FILL[rung] : 'border border-line-strong bg-card');
        // Under the floor: part of every ladder in this row, drawn but not a choice.
        if (index < lowest) {
          return (
            <span key={step} aria-hidden="true" className="flex h-8 w-4 items-center justify-center" title={`It can go no lower than ${RUNG_WORDS[floor].word.toLowerCase()}`}>
              <span className={square} />
            </span>
          );
        }
        return (
          <RadioGroup.Item
            key={step}
            value={step}
            aria-label={RUNG_WORDS[step].word}
            title={RUNG_WORDS[step].word}
            onPointerEnter={(event) => event.pointerType === 'mouse' && onPeek?.(step)}
            onFocus={() => onPeek?.(step)}
            className="group/step flex h-8 w-4 items-center justify-center rounded-sm focus-visible:outline-offset-0"
          >
            <span className={cn(square, 'group-hover/step:ring-2 group-hover/step:ring-accent-line')} />
          </RadioGroup.Item>
        );
      })}
    </RadioGroup.Root>
  );
}

export interface GridScope {
  id: string | null;
  name: string;
  color?: string | null;
  rungs: Rungs;
  floors: Record<ConfirmRow, Rung>;
  /** "Asks for more than everywhere else". */
  raised?: boolean;
}

interface GridProps {
  scopes: GridScope[];
  /** Who may change which row. Without it, the row is drawn and cannot be changed. */
  may: (row: ConfirmRow) => boolean;
  onPeek: (scope: string | null, row: ConfirmRow, rung: Rung | null) => void;
  onPick: (scope: string | null, row: ConfirmRow, rung: Rung) => void;
}

export function YesGrid({ scopes, may, onPeek, onPick }: GridProps) {
  return (
    <div className="min-w-0 overflow-x-auto">
      <table className="w-full min-w-[640px] border-collapse text-left">
        <caption className="sr-only">How strong a yes each thing needs, everywhere and in each business</caption>
        <thead>
          <tr className="border-b border-line">
            <th scope="col" className="sticky left-0 z-1 h-12 bg-card pr-3 pl-5 text-table-header text-faint">
              Where
            </th>
            <th scope="col" className="h-12 px-1 text-center text-table-header text-faint">
              <span className="inline-flex items-center gap-1" title="Looking and drafting: nothing leaves, so there is nothing to ask.">
                <Lock aria-hidden className="size-3" />
                Looking
              </span>
            </th>
            {ROWS.map((row) => (
              <th key={row.key} scope="col" className="h-12 px-1 text-center text-table-header text-faint" title={row.title}>
                {SHORT[row.key]}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {scopes.map((scope) => (
            <tr key={scope.id ?? 'all'} className="border-b border-line last:border-b-0">
              <th scope="row" className="sticky left-0 z-1 h-16 min-w-44 bg-card pr-3 pl-5 text-left font-normal">
                <span className="flex min-w-0 items-center gap-3">
                  {scope.id ? (
                    <BusinessMark name={scope.name} color={scope.color} size={24} />
                  ) : (
                    <span aria-hidden className="inline-flex size-6 shrink-0 items-center justify-center rounded-md bg-inset text-muted">
                      <Globe className="size-4" />
                    </span>
                  )}
                  <span className="grid min-w-0">
                    <span className="truncate text-label text-fg" title={scope.name}>
                      {scope.name}
                    </span>
                    {scope.raised ? <span className="truncate text-caption text-faint">Asks for more</span> : null}
                  </span>
                </span>
              </th>
              <td className="px-1 text-center">
                <Cell label={`Looking and drafting, ${scope.name}`} rung="auto" floor="auto" />
              </td>
              {ROWS.map((row) => {
                const label = scope.id ? `${row.title}, ${scope.name}` : row.title;
                return (
                  <td key={row.key} className="px-1 text-center">
                    <Cell
                      label={label}
                      rung={scope.rungs[row.key]}
                      floor={scope.floors[row.key]}
                      onPeek={may(row.key) ? (rung) => onPeek(scope.id, row.key, rung) : undefined}
                      onPick={may(row.key) ? (rung) => onPick(scope.id, row.key, rung) : undefined}
                    />
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Each row's words, for the list under the grid: what it covers and why it can go no lower. */
export function RowNotes({ seen }: { seen: (row: ConfirmRow) => string }) {
  return (
    <dl className="grid gap-3 sm:grid-cols-2">
      {ROWS.map((row) => (
        <div key={row.key} className="min-w-0">
          <dt className="text-label text-fg">{row.title}</dt>
          <dd className="mt-1 text-secondary text-muted">{row.because ?? row.covers}</dd>
          <dd className="mt-1 text-caption text-faint">{seen(row.key)}</dd>
        </div>
      ))}
    </dl>
  );
}
