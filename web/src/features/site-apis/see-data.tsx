/**
 * The door for a person: the whole result as a table to read, copy into a spreadsheet, download, and
 * keep up to date. No code and no words a person would need to look up.
 */
import { ArrowDown, ArrowUp, Check, Clipboard, Download, RefreshCw, Search } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useSiteAction } from '@/api/hooks';
import type { SiteApiDetail, SiteEndpoint, SiteField } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/field';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/menu';
import { useCopy } from '@/components/ui/misc';
import { Table, Td, Th, Tr } from '@/components/ui/table';
import { cn } from '@/lib/cn';
import { plural, slugify } from '@/lib/format';
import { toCsv, toTsv } from './words';

const FIRST = 10;

const KEEP: { value: SiteApiDetail['keep_updated']; label: string; note: string }[] = [
  { value: 'off', label: 'Only when asked', note: 'It reads when something calls it.' },
  { value: 'hourly', label: 'Every hour', note: 'Uses one read an hour of this account\'s pace.' },
  { value: 'morning', label: 'Every morning', note: 'One read at 07:00, before the day starts.' },
];

const numeric = (text: string) => Number(text.replace(/[^0-9.-]/g, ''));

function compare(field: SiteField, a: string, b: string) {
  if (field.kind === 'money' || field.kind === 'count') return numeric(a) - numeric(b);
  return a.localeCompare(b);
}

function KeepUpdated({ org, api }: { org: string; api: SiteApiDetail }) {
  const action = useSiteAction(org, api.key);
  const [open, setOpen] = useState(false);
  const chosen = action.isPending && action.variables.kind === 'keep-updated' ? action.variables.every : api.keep_updated;
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button size="sm" icon={<RefreshCw aria-hidden="true" className="size-4" />}>
          {chosen === 'off' ? 'Keep updated' : chosen === 'hourly' ? 'Updated hourly' : 'Updated each morning'}
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-72 p-1">
        <p className="px-2 pt-1 pb-2 text-caption text-faint">How often should this table read the site?</p>
        {KEEP.map((option) => (
          <button
            key={option.value}
            type="button"
            onClick={() => {
              action.mutate({ kind: 'keep-updated', every: option.value });
              setOpen(false);
            }}
            className="grid w-full grid-cols-[16px_minmax(0,1fr)] gap-x-2 rounded-md px-2 py-2 text-left transition-colors duration-(--duration-fast) ease-standard hover:bg-hover active:bg-pressed"
          >
            <span className="pt-px">{chosen === option.value ? <Check aria-hidden="true" className="size-4" /> : null}</span>
            <span className="text-secondary font-medium">{option.label}</span>
            <span className="col-start-2 text-caption text-faint">{option.note}</span>
          </button>
        ))}
      </PopoverContent>
    </Popover>
  );
}

export function SeeData({ org, api, endpoint }: { org: string; api: SiteApiDetail; endpoint: SiteEndpoint }) {
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState<{ key: string; up: boolean } | null>(null);
  const [all, setAll] = useState(false);
  const { copied, copy } = useCopy();
  const { fields, rows } = endpoint;

  const shown = useMemo(() => {
    const words = query.trim().toLowerCase();
    const matched = words ? rows.filter((row) => Object.values(row).some((value) => value.toLowerCase().includes(words))) : rows;
    const field = sort ? fields.find((item) => item.key === sort.key) : null;
    if (!sort || !field) return matched;
    return [...matched].sort((a, b) => compare(field, a[sort.key] ?? '', b[sort.key] ?? '') * (sort.up ? 1 : -1));
  }, [fields, query, rows, sort]);
  const visible = all ? shown : shown.slice(0, FIRST);

  const download = () => {
    const url = URL.createObjectURL(new Blob([toCsv(fields, shown)], { type: 'text/csv' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `${slugify(api.title)}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 p-4">
        <div className="relative min-w-48 flex-1">
          <Search aria-hidden="true" className="pointer-events-none absolute top-2 left-2 size-4 text-faint" />
          <Input aria-label="Find in this table" placeholder={`Find in ${plural(rows.length, 'row')}`} value={query} onChange={(event) => setQuery(event.target.value)} className="pl-8" />
        </div>
        <Button size="sm" icon={copied ? <Check aria-hidden="true" className="size-4 text-ok" /> : <Clipboard aria-hidden="true" className="size-4" />} onClick={() => copy(toTsv(fields, shown))}>
          {copied ? 'Copied' : 'Copy for a sheet'}
        </Button>
        <Button size="sm" icon={<Download aria-hidden="true" className="size-4" />} onClick={download}>
          Download CSV
        </Button>
        <KeepUpdated org={org} api={api} />
      </div>
      {shown.length ? (
        <div className="scrollbar-none overflow-x-auto border-t border-line">
          <Table aria-label={endpoint.title}>
            <thead>
              <tr>
                {fields.map((field, index) => {
                  const on = sort?.key === field.key;
                  return (
                    <Th key={field.key} aria-sort={on ? (sort.up ? 'ascending' : 'descending') : 'none'} className={cn(index === 0 && 'first:pl-4', 'last:pr-4')}>
                      <button
                        type="button"
                        onClick={() => setSort(on && !sort.up ? null : { key: field.key, up: !on })}
                        className="-mx-1 flex items-center gap-1 rounded-sm px-1 transition-colors duration-(--duration-fast) ease-standard hover:text-fg"
                      >
                        {field.label}
                        {on ? sort.up ? <ArrowUp aria-hidden="true" className="size-3" /> : <ArrowDown aria-hidden="true" className="size-3" /> : <span aria-hidden="true" className="size-3" />}
                      </button>
                    </Th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {visible.map((row, rowIndex) => (
                <Tr key={rowIndex} className="hover:bg-hover">
                  {fields.map((field, index) => (
                    <Td key={field.key} className={cn('first:pl-4 last:pr-4', index === 1 && 'w-full max-w-0 truncate', field.kind === 'money' || field.kind === 'count' ? 'tabular' : undefined, index === 0 && 'font-medium')}>
                      {row[field.key]}
                    </Td>
                  ))}
                </Tr>
              ))}
            </tbody>
          </Table>
        </div>
      ) : (
        <p className="border-t border-line p-8 text-center text-body text-muted">Nothing in this table says "{query}".</p>
      )}
      {shown.length > FIRST ? (
        <div className="flex items-center justify-between border-t border-line px-4 py-2">
          <p className="tabular text-secondary text-faint">
            {visible.length} of {shown.length}
          </p>
          <Button size="sm" variant="ghost" onClick={() => setAll((value) => !value)}>
            {all ? 'Show fewer' : `Show all ${shown.length}`}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
