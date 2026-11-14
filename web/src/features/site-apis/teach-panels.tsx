/** Teach's later stages: what was found on the page, its columns, a try, turning it on, and done. */
import { Link } from '@tanstack/react-router';
import { ArrowRight, Check, RefreshCw } from 'lucide-react';
import { RadioGroup } from 'radix-ui';
import type { ReactNode } from 'react';
import { useMemo } from 'react';
import type { SiteDiscovery, SitePage } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Field, Input } from '@/components/ui/field';
import { CodeBlock, CopyButton } from '@/components/ui/misc';
import { Switch } from '@/components/ui/toggle';
import { cn } from '@/lib/cn';
import { plural, slugify } from '@/lib/format';
import { definitionOf, draftColumns, keyOf } from './draft';
import type { Draft } from './draft';
import { hueStyle } from './page-sheet';
import type { Lit } from './page-sheet';
import { BASE, speed } from './words';
import { FRESHNESS, SURE, useTick } from './teach';

export function Panel({ count, title, children, foot }: { count?: string; title: string; children?: ReactNode; foot?: ReactNode }) {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        {count ? <p className="silk mb-2">{count}</p> : null}
        <h1 className="text-24 font-semibold text-balance">{title}</h1>
        <div className="mt-4">{children}</div>
      </div>
      {foot ? <div className="flex items-center justify-end gap-2 border-t border-line p-4">{foot}</div> : null}
    </div>
  );
}

export function EmptySheet({ url, looking }: { url: string; looking: boolean }) {
  const skeleton: SitePage = { url, wordmark: '', nav: [], heading: '', count_words: '', layout: 'table', columns: [], side: [], seen_at: '' };
  return (
    <div className="relative mx-auto max-w-5xl overflow-hidden rounded-lg border border-board-line bg-bg">
      <div className="flex h-8 items-center border-b border-line bg-subtle px-3">
        <span className="mono truncate text-12 text-muted">{skeleton.url.replace('https://', '') || 'Choose a page'}</span>
      </div>
      <div aria-hidden="true" className="grid gap-4 p-6">
        <span className="h-3 w-40 rounded-sm bg-fill-2" />
        <div className="grid grid-cols-5 gap-2">
          {Array.from({ length: 30 }, (_, index) => (
            <span key={index} className="h-3 rounded-sm bg-fill-1" />
          ))}
        </div>
        <span className="h-3 w-2/3 rounded-sm bg-fill-1" />
        <span className="h-3 w-1/2 rounded-sm bg-fill-1" />
      </div>
      {looking ? <div aria-hidden="true" className="split-scan" /> : null}
    </div>
  );
}

function RegionSample({ found, region }: { found: SiteDiscovery; region: string }) {
  const tick = useTick(2400);
  if (region === 'side' || region === 'nav') {
    const items = region === 'side' ? found.page.side : found.page.nav;
    return (
      <ul className="grid gap-1 text-13 text-muted">
        {items.slice(0, 4).map((item) => (
          <li key={item} className="truncate">{item}</li>
        ))}
      </ul>
    );
  }
  const rows = found.rows;
  const row = rows[tick % rows.length];
  const shown = found.page.columns.filter((column) => column.field).slice(0, 4);
  return (
    <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 text-13">
      {shown.map((column) => (
        <div key={column.label} className="contents">
          <dt className="text-faint">{column.label}</dt>
          <dd className="tabular truncate">{column.field ? row?.[column.field] : ''}</dd>
        </div>
      ))}
    </dl>
  );
}

export function FoundPanel({ found, region, onRegion, onNext }: { found: SiteDiscovery; region: string; onRegion: (id: string) => void; onNext: () => void }) {
  const main = found.regions[0];
  return (
    <Panel
      count="Step 3 of 6"
      title={`I found ${main?.words ?? 'a list'}`}
      foot={
        region === 'main' ? (
          <Button variant="primary" icon={<ArrowRight aria-hidden="true" className="size-4" />} onClick={onNext}>
            Use this table
          </Button>
        ) : (
          <Button variant="primary" onClick={() => onRegion('main')}>
            Go back to the table
          </Button>
        )
      }
    >
      <p className="text-14 text-pretty text-muted">Point at something on the page to see what it holds. Everything I can read is outlined.</p>
      <RadioGroup.Root aria-label="What to read" value={region} onValueChange={onRegion} className="mt-4 grid gap-2">
        {found.regions.map((item) => (
          <RadioGroup.Item
            key={item.id}
            value={item.id}
            className={cn('rounded-lg border p-3 text-left transition-colors duration-(--duration-fast) ease-standard', region === item.id ? 'border-fg' : 'border-line hover:border-line-strong')}
          >
            <span className="flex items-center justify-between gap-2">
              <span className="text-14 font-medium">{item.words.charAt(0).toUpperCase() + item.words.slice(1)}</span>
              <span className="text-12 text-faint">{SURE[item.sure]}</span>
            </span>
            {region === item.id ? (
              <span className="mt-2 block border-t border-line pt-2">
                <RegionSample found={found} region={item.id} />
              </span>
            ) : null}
          </RadioGroup.Item>
        ))}
      </RadioGroup.Root>
    </Panel>
  );
}

export function ColumnsPanel({ found, draft, setDraft, onLit, onNext }: { found: SiteDiscovery; draft: Draft; setDraft: (next: Draft) => void; onLit: (lit: Lit | null) => void; onNext: () => void }) {
  const tick = useTick(2000);
  const columns = draftColumns(found, draft);
  let keptIndex = -1;
  const toggle = (label: string, on: boolean) => {
    const order = found.page.columns.map((column) => column.label);
    setDraft({ ...draft, kept: order.filter((name) => (name === label ? on : draft.kept.includes(name))) });
  };
  return (
    <Panel
      count="Step 4 of 6"
      title="Keep what you need, and name it your way"
      foot={
        <Button variant="primary" disabled={draft.kept.length === 0} icon={<ArrowRight aria-hidden="true" className="size-4" />} onClick={onNext}>
          Try it
        </Button>
      }
    >
      <p className="text-14 text-pretty text-muted">Press a column on the page, or use the switches. The names are yours, and they are what your tools will see.</p>
      <ul className="mt-4 grid gap-1">
        {columns.map((column) => {
          const on = draft.kept.includes(column.label);
          if (on) keptIndex += 1;
          const values = column.values ?? [];
          const key = keyOf(column.label);
          return (
            <li
              key={column.label}
              onPointerEnter={() => onLit(on ? { field: key, row: tick % Math.min(6, values.length) } : null)}
              onPointerLeave={() => onLit(null)}
              style={on ? hueStyle(keptIndex) : undefined}
              className="grid grid-cols-[auto_minmax(0,1fr)_minmax(0,1fr)] items-center gap-2 rounded-md px-1 py-1 transition-colors duration-(--duration-fast) ease-standard hover:bg-fill-1"
            >
              <Switch aria-label={`Keep ${column.label}`} checked={on} onCheckedChange={(next) => toggle(column.label, next)} />
              {on ? (
                <span className="relative min-w-0">
                  <span aria-hidden="true" className="absolute top-3 left-2 size-2 rounded-full bg-(--hue)" />
                  <Input
                    aria-label={`Name for ${column.label}`}
                    value={draft.names[column.label] ?? column.label}
                    onChange={(event) => setDraft({ ...draft, names: { ...draft.names, [column.label]: event.target.value } })}
                    className="h-8 pl-6"
                  />
                </span>
              ) : (
                <span className="truncate px-2 text-13 text-faint">{column.label}</span>
              )}
              <span className={cn('tabular truncate text-13', on ? 'text-muted' : 'text-faint')}>{values[tick % Math.max(1, values.length)]}</span>
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}

export function TryPanel({ reading, total, onAgain, onNext }: { reading: boolean; total: number; onAgain: () => void; onNext: () => void }) {
  const checks = ['Every row has a value in each column', 'It reads the same as the page', 'Nothing private came with it, only these columns'];
  return (
    <Panel
      count="Step 5 of 6"
      title={reading ? 'Reading the page now' : `It worked: ${plural(total, 'row')} in ${speed(1840)}`}
      foot={
        <>
          <Button icon={<RefreshCw aria-hidden="true" className="size-4" />} disabled={reading} onClick={onAgain}>
            Try again
          </Button>
          <Button variant="primary" disabled={reading} icon={<ArrowRight aria-hidden="true" className="size-4" />} onClick={onNext}>
            Looks right
          </Button>
        </>
      }
    >
      <p className="text-14 text-pretty text-muted">{reading ? 'This is one real read, from your own account. It only looks.' : 'That was one real read from your own account. It counted as one read of the account\'s pace.'}</p>
      <ul className="mt-4 grid gap-2">
        {checks.map((check, index) => (
          <li key={check} className={cn('flex items-start gap-2 text-14 transition-opacity duration-(--duration-base) ease-standard', reading && 'opacity-40')} style={{ transitionDelay: `${index * 80}ms` }}>
            <Check aria-hidden="true" className="mt-px size-4 shrink-0 text-green" />
            {check}
          </li>
        ))}
      </ul>
    </Panel>
  );
}

export function ActivatePanel({ found, draft, setDraft, onActivate }: { found: SiteDiscovery; draft: Draft; setDraft: (next: Draft) => void; onActivate: () => void }) {
  const noun = draft.title.trim().toLowerCase() || 'rows';
  return (
    <Panel
      count="Step 6 of 6"
      title="Name it and turn it on"
      foot={
        <>
          <p className="mr-auto text-12 text-faint">Only a person can do this.</p>
          <Button variant="primary" disabled={!draft.title.trim()} onClick={onActivate}>
            Turn it on
          </Button>
        </>
      }
    >
      <div className="grid gap-4">
        <Field label="What should it be called?">
          <Input value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} />
        </Field>
        <fieldset>
          <legend className="mb-2 text-13 font-medium">How fresh does it need to be?</legend>
          <RadioGroup.Root aria-label="How fresh" value={String(draft.cache_s)} onValueChange={(value) => setDraft({ ...draft, cache_s: Number(value) })} className="grid gap-1">
            {FRESHNESS.map((option) => (
              <RadioGroup.Item
                key={option.value}
                value={String(option.value)}
                className={cn('rounded-lg border px-3 py-2 text-left transition-colors duration-(--duration-fast) ease-standard', draft.cache_s === option.value ? 'border-fg' : 'border-line hover:border-line-strong')}
              >
                <span className="block text-13 font-medium">{option.label}</span>
                <span className="block text-12 text-faint">{option.note}</span>
              </RadioGroup.Item>
            ))}
          </RadioGroup.Root>
        </fieldset>
        <Field label="Tell me if it finds fewer than" hint={`So a page that changed never looks like having no ${noun}.`}>
          <Input type="number" min={0} value={String(draft.expect_min)} onChange={(event) => setDraft({ ...draft, expect_min: Number(event.target.value) })} className="w-24" />
        </Field>
        <p className="text-13 text-pretty text-muted">
          It runs on one account, at {found.site.name}, at up to 60 reads an hour. A key to call it comes after this.
        </p>
      </div>
    </Panel>
  );
}

export function DonePanel({ org, found, draft }: { org: string; found: SiteDiscovery; draft: Draft }) {
  const url = `${BASE}/${slugify(found.site.name)}/${keyOf(draft.title)}`;
  return (
    <Panel
      title={`${draft.title} is on`}
      foot={
        <>
          <Button asChild>
            <Link to="/$org/site-apis/$api" params={{ org, api: found.api }} search={{ door: 'code' }}>
              Use it in code
            </Link>
          </Button>
          <Button variant="primary" asChild>
            <Link to="/$org/site-apis/$api" params={{ org, api: found.api }}>
              See the data
            </Link>
          </Button>
        </>
      }
    >
      <p className="text-14 text-pretty text-muted">Your computer will keep this table up to date from {found.site.name}. If the page ever changes, it will stop and tell you rather than guess.</p>
      <div className="mt-4 grid gap-2">
        <p className="text-13 font-medium">Its address</p>
        <CodeBlock code={url} label="Copy the address" />
        <p className="text-13 text-faint">It needs a key, which you can make on its page.</p>
      </div>
    </Panel>
  );
}

export function Definition({ found, draft }: { found: SiteDiscovery; draft: Draft }) {
  const text = useMemo(() => JSON.stringify(definitionOf(found, draft), null, 2), [found, draft]);
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-11 items-center justify-between border-b border-line px-4">
        <p className="text-13 font-medium">Definition</p>
        <CopyButton text={text} label="Copy the definition" />
      </div>
      <pre className="mono min-h-0 flex-1 overflow-auto bg-subtle p-4 text-12 leading-5">{text}</pre>
    </div>
  );
}
