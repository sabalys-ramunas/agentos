/**
 * Teach it: making a Site API should feel like pointing at a page. On the left the page, with what
 * the computer found outlined. On the right what it says about it and the one thing to decide. The
 * sentence on top is the whole Site API so far, and each phrase goes back to its step. Nothing here
 * needs a technical word, and a developer can flip the panel to the definition at any point. Only
 * a person turns one on. In the concept it runs on the mock, so turning on opens the demo's own.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { ArrowLeft, ArrowRight, Check, Globe, RefreshCw, SearchX } from 'lucide-react';
import { RadioGroup } from 'radix-ui';
import type { ReactNode } from 'react';
import { useEffect, useMemo, useState } from 'react';
import { useSiteDiscovery, useWorkspaces } from '@/api/hooks';
import { isNotServed } from '@/api/http';
import type { Lane, SiteDiscovery, SitePage, Workspace } from '@/api/types';
import type { TeachStep } from '@/app/router';
import { BusinessMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Field, Input } from '@/components/ui/field';
import { CodeBlock, CopyButton } from '@/components/ui/misc';
import { ErrorState, NotLive, Skeleton } from '@/components/ui/states';
import { Switch } from '@/components/ui/toggle';
import { cn } from '@/lib/cn';
import { plural, slugify } from '@/lib/format';
import { definitionOf, draftColumns, draftFields, draftRows, firstDraft, keyOf } from './draft';
import type { Draft } from './draft';
import { hueStyle, PageSheet } from './page-sheet';
import type { Lit } from './page-sheet';
import { Split } from './split';
import { BASE, speed } from './words';
import './site-apis.css';

const ORDER: TeachStep[] = ['account', 'page', 'found', 'columns', 'try', 'activate'];
const FRESHNESS = [
  { value: 60, label: 'Within a minute', note: 'Reads often. Best for things that change by the minute.' },
  { value: 300, label: 'Within 5 minutes', note: 'A good start for most lists.' },
  { value: 3600, label: 'Within the hour', note: 'Gentle on the account. Best for slow lists.' },
];
const SURE = { sure: 'Sure', likely: 'Likely', unsure: 'Not sure' } as const;

/** A value that keeps moving, so the panel shows the real page and not one frozen example. */
function useTick(ms: number) {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setTick((value) => value + 1), ms);
    return () => clearInterval(timer);
  }, [ms]);
  return tick;
}

function Phrase({ on, done, onClick, children }: { on: boolean; done: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!done && !on}
      aria-current={on ? 'step' : undefined}
      className={cn(
        'rounded-sm px-1 font-medium underline decoration-1 underline-offset-4 transition-colors duration-(--duration-fast) ease-standard',
        on ? 'bg-fill-2 text-fg decoration-fg' : done ? 'text-fg decoration-line-strong hover:bg-fill-1 hover:decoration-fg' : 'cursor-default text-faint decoration-dotted decoration-line-strong',
      )}
    >
      {children}
    </button>
  );
}

function Sentence({ step, go, lane, found, draft }: { step: TeachStep; go: (step: TeachStep) => void; lane: Lane | undefined; found: SiteDiscovery | null | undefined; draft: Draft | null }) {
  const at = step === 'done' ? ORDER.length : ORDER.indexOf(step);
  const reached = (name: TeachStep) => ORDER.indexOf(name) < at;
  const phrase = (name: TeachStep, text: string) => (
    <Phrase on={step === name} done={reached(name)} onClick={() => go(name)}>
      {text}
    </Phrase>
  );
  return (
    <p className="min-w-0 text-16 text-pretty text-muted max-md:order-last max-md:w-full">
      On {phrase('account', found ? `${lane?.account?.user_id ?? 'your account'} at ${found.site.name}` : (lane?.account?.user_id ?? 'which account'))}, read {phrase('found', found && at >= 2 ? `the table of ${found.total}` : 'which part of the page')}, keep {phrase('columns', draft && at >= 3 ? plural(draft.kept.length, 'column') : 'which columns')}, and call it {phrase('activate', draft && at >= 4 ? draft.title : 'a name')}.
    </p>
  );
}

function Panel({ count, title, children, foot }: { count?: string; title: string; children?: ReactNode; foot?: ReactNode }) {
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

function AccountStage({ org, workspaces, onPick }: { org: string; workspaces: Workspace[]; onPick: (lane: Lane) => void }) {
  const likely = workspaces.flatMap((workspace) => workspace.lanes.filter((lane) => lane.kind === 'browser' && lane.readiness === 'ready' && lane.account?.site?.endsWith('.example')).map((lane) => ({ lane, workspace })));
  const browsers = workspaces.flatMap((workspace) => workspace.lanes.filter((lane) => lane.kind === 'browser'));
  const anyReady = browsers.some((lane) => lane.readiness === 'ready');
  return (
    <div className="mx-auto grid max-w-4xl gap-6">
      {anyReady ? null : (
        <p className="text-14 text-pretty text-muted">
          {browsers.length ? 'None of your accounts is ready yet. Sign in to one on its own page, and it will show here.' : 'You have no accounts to read a page with yet.'}
        </p>
      )}
      {likely.length ? (
        <section aria-label="Most likely">
          <h2 className="silk mb-2">Most likely</h2>
          {likely.map(({ lane, workspace }) => (
            <button
              key={lane.id}
              type="button"
              onClick={() => onPick(lane)}
              className="group flex w-full items-center gap-3 rounded-(--radius-card) border border-line bg-bg p-4 text-left transition-[border-color,background-color,transform] duration-(--duration-fast) ease-standard hover:border-line-strong hover:bg-fill-1 active:translate-y-px"
            >
              <SiteMark site={lane.id} size={40} tile label={false} className="bg-bg" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-16 font-semibold">{lane.account?.user_id}</span>
                <span className="block truncate text-13 text-muted">
                  {workspace.name}, signed in at <span className="mono">{lane.account?.site}</span>
                </span>
              </span>
              <ArrowRight aria-hidden="true" className="size-4 shrink-0 text-faint transition-transform duration-(--duration-fast) ease-standard group-hover:translate-x-1" />
            </button>
          ))}
        </section>
      ) : null}
      {workspaces.map((workspace) => {
        const lanes = workspace.lanes.filter((lane) => lane.kind === 'browser');
        if (!lanes.length) return null;
        return (
          <section key={workspace.id} aria-label={workspace.name}>
            <h2 className="mb-2 flex items-center gap-2 text-13 font-medium">
              <BusinessMark name={workspace.name} color={workspace.color} size={16} />
              {workspace.name}
            </h2>
            <ul className="grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-4">
              {lanes.map((lane) => {
                const ready = lane.readiness === 'ready';
                const [ws = '', name = lane.name] = lane.id.split('/');
                const inner = (
                  <>
                    <SiteMark site={lane.id} size={24} tile label={false} className="bg-bg" />
                    <span className="min-w-0">
                      <span className="block truncate text-13 font-medium">{lane.account?.user_id ?? lane.name}</span>
                      <span className="block truncate text-12 text-faint">{ready ? (lane.account?.site ?? lane.name) : 'Not signed in yet'}</span>
                    </span>
                  </>
                );
                const rowClass = 'flex w-full items-center gap-2 rounded-lg border border-line bg-bg p-2 text-left transition-[border-color,background-color,transform] duration-(--duration-fast) ease-standard hover:border-line-strong hover:bg-fill-1 active:translate-y-px';
                return (
                  <li key={lane.id}>
                    {ready ? (
                      <button type="button" onClick={() => onPick(lane)} className={rowClass}>
                        {inner}
                      </button>
                    ) : (
                      // Not ready is not a dead end: the account's own page has the sign-in.
                      <Link to="/$org/lanes/$ws/$name" params={{ org, ws, name }} className={rowClass}>
                        {inner}
                      </Link>
                    )}
                  </li>
                );
              })}
            </ul>
          </section>
        );
      })}
    </div>
  );
}

function EmptySheet({ url, looking }: { url: string; looking: boolean }) {
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

function FoundPanel({ found, region, onRegion, onNext }: { found: SiteDiscovery; region: string; onRegion: (id: string) => void; onNext: () => void }) {
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

function ColumnsPanel({ found, draft, setDraft, onLit, onNext }: { found: SiteDiscovery; draft: Draft; setDraft: (next: Draft) => void; onLit: (lit: Lit | null) => void; onNext: () => void }) {
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

function TryPanel({ reading, total, onAgain, onNext }: { reading: boolean; total: number; onAgain: () => void; onNext: () => void }) {
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

function ActivatePanel({ found, draft, setDraft, onActivate }: { found: SiteDiscovery; draft: Draft; setDraft: (next: Draft) => void; onActivate: () => void }) {
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

function DonePanel({ org, found, draft }: { org: string; found: SiteDiscovery; draft: Draft }) {
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

function Definition({ found, draft }: { found: SiteDiscovery; draft: Draft }) {
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

export function Teach() {
  const { org } = useParams({ from: '/$org/site-apis/new' });
  const { step = 'account', lane: laneId } = useSearch({ from: '/$org/site-apis/new' });
  const navigate = useNavigate({ from: '/$org/site-apis/new' });
  const workspaces = useWorkspaces(org);
  const discovery = useSiteDiscovery(org, laneId ?? null);
  const found = discovery.data;
  const lane = workspaces.data?.flatMap((workspace) => workspace.lanes).find((item) => item.id === laneId);

  const [draft, setDraft] = useState<Draft | null>(null);
  const [url, setUrl] = useState('');
  const [looking, setLooking] = useState(false);
  const [region, setRegion] = useState('main');
  const [lit, setLit] = useState<Lit | null>(null);
  const [mode, setMode] = useState<'steps' | 'json'>('steps');
  const [reading, setReading] = useState(step === 'try');

  useEffect(() => {
    if (found && !draft) setDraft(firstDraft(found));
  }, [draft, found]);

  useEffect(() => {
    if (step !== 'try') return;
    setReading(true);
    const timer = setTimeout(() => setReading(false), 1900);
    return () => clearTimeout(timer);
  }, [step]);

  const go = (next: TeachStep, nextLane = laneId) => void navigate({ search: { step: next, lane: nextLane } });
  const pageUrl = url || (found?.suggestions[0]?.url ?? '');

  const look = () => {
    setLooking(true);
    setTimeout(() => {
      setLooking(false);
      go('found');
    }, 1500);
  };

  const rows = found && draft ? draftRows(found, draft) : [];
  const fields = found && draft ? draftFields(found, draft) : [];
  const columns = found && draft ? draftColumns(found, draft) : undefined;
  const sheet = found && draft ? { ...found.page, columns: columns ?? [] } : null;

  if (isNotServed(workspaces.error)) return <Card><NotLive /></Card>;

  const stage = () => {
    if (step === 'account') return workspaces.data ? <AccountStage org={org} workspaces={workspaces.data} onPick={(item) => go('page', item.id)} /> : <Skeleton className="mx-auto h-96 max-w-4xl rounded-lg" />;
    if (step === 'page') return <EmptySheet url={pageUrl} looking={looking} />;
    if (!found || !sheet) {
      if (discovery.isPending) return <EmptySheet url="" looking />;
      // Not served here is not a page with nothing on it, and a failed look is not one either: only a read that worked says so.
      if (isNotServed(discovery.error)) return <Card><NotLive /></Card>;
      if (discovery.error) return <ErrorState error={discovery.error} onRetry={() => void discovery.refetch()} />;
      return (
        <div className="mx-auto grid max-w-md place-items-center rounded-lg border border-board-line bg-bg p-8 text-center">
          <SearchX aria-hidden="true" className="size-4 text-muted" />
          <p className="mt-2 text-14 font-medium">Nothing repeats on this page</p>
          <p className="mt-1 text-13 text-muted">A Site API needs a list, a table or a set of cards. Try the page that shows them.</p>
        </div>
      );
    }
    if (step === 'found') {
      return (
        <div className="scrollbar-none mx-auto max-w-5xl overflow-x-auto pt-4 pl-1 md:overflow-visible">
          <div className="min-w-[720px]">
          <PageSheet page={sheet} rows={rows} fields={fields} lit={null} columns={columns} showRegions regionWords={Object.fromEntries(found.regions.map((item) => [item.id, item.words]))} picked={region} onPick={setRegion} limit={18} />
          </div>
        </div>
      );
    }
    if (step === 'columns') {
      return (
        <div className="scrollbar-none mx-auto max-w-5xl overflow-x-auto md:overflow-visible">
          <div className="min-w-[720px]">
          <PageSheet
            page={sheet}
            rows={rows}
            fields={fields}
            lit={lit}
            columns={columns}
            limit={18}
            onColumn={(label) => {
              if (!draft) return;
              const on = !draft.kept.includes(label);
              const order = found.page.columns.map((column) => column.label);
              setDraft({ ...draft, kept: order.filter((name) => (name === label ? on : draft.kept.includes(name))) });
            }}
          />
          </div>
        </div>
      );
    }
    return (
      <div className="mx-auto max-w-5xl">
        <Split page={sheet} fields={fields} rows={rows} total={found.total} name={keyOf(draft?.title ?? 'rows') || 'rows'} reading={reading} readAt={new Date().toISOString()} rest={{ field: fields[3]?.key ?? fields[0]?.key ?? '', row: 1 }} limit={5} />
      </div>
    );
  };

  const panel = () => {
    if (step === 'account') {
      return (
        <Panel count="Step 1 of 6" title="Which account should it read as?">
          <p className="text-14 text-pretty text-muted">It reads the site the way you would, signed in as this account, and it only ever uses this one. Pick the one that can see what you want.</p>
          <p className="mt-4 text-13 text-pretty text-faint">Already signed in, so nothing is typed and no password is needed here.</p>
        </Panel>
      );
    }
    if (step === 'page') {
      return (
        <Panel
          count="Step 2 of 6"
          title="Which page has what you want?"
          foot={
            <Button variant="primary" loading={looking} disabled={!pageUrl} icon={<ArrowRight aria-hidden="true" className="size-4" />} onClick={look}>
              {looking ? 'Looking at the page' : 'Look at this page'}
            </Button>
          }
        >
          <div className="grid gap-4">
            <Field label="Its address" hint="Copy it from your browser, or pick one below.">
              <Input mono value={pageUrl} onChange={(event) => setUrl(event.target.value)} placeholder="https://" />
            </Field>
            {found ? (
              <ul className="grid gap-1">
                {found.suggestions.map((item) => (
                  <li key={item.url}>
                    <button
                      type="button"
                      onClick={() => setUrl(item.url)}
                      className={cn('flex w-full items-center gap-2 rounded-md border px-2 py-2 text-left transition-colors duration-(--duration-fast) ease-standard', pageUrl === item.url ? 'border-fg' : 'border-line hover:border-line-strong')}
                    >
                      <Globe aria-hidden="true" className="size-4 shrink-0 text-faint" />
                      <span className="min-w-0">
                        <span className="block truncate text-13 font-medium">{item.words}</span>
                        <span className="mono block truncate text-12 text-faint">{item.url.replace('https://', '')}</span>
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        </Panel>
      );
    }
    if (!found || !draft) {
      return (
        <Panel title={discovery.isPending ? 'Looking at the page' : 'I could not find a list here'}>
          {discovery.isPending ? null : (
            <>
              <p className="text-14 text-pretty text-muted">Nothing on this page repeats, so there is no table to make. Try another page, or another account.</p>
              <Button className="mt-4" onClick={() => go('account')}>
                Choose another account
              </Button>
            </>
          )}
        </Panel>
      );
    }
    switch (step) {
      case 'found':
        return <FoundPanel found={found} region={region} onRegion={setRegion} onNext={() => go('columns')} />;
      case 'columns':
        return <ColumnsPanel found={found} draft={draft} setDraft={setDraft} onLit={setLit} onNext={() => go('try')} />;
      case 'try':
        return (
          <TryPanel
            reading={reading}
            total={found.total}
            onAgain={() => {
              setReading(true);
              setTimeout(() => setReading(false), 1900);
            }}
            onNext={() => go('activate')}
          />
        );
      case 'activate':
        return <ActivatePanel found={found} draft={draft} setDraft={setDraft} onActivate={() => go('done')} />;
      case 'done':
        return <DonePanel org={org} found={found} draft={draft} />;
    }
  };

  const canJson = Boolean(found && draft) && step !== 'account' && step !== 'page';

  return (
    <div data-bleed="" className="flex h-[calc(100dvh-var(--topbar-height))] min-h-0 flex-col max-lg:h-auto">
      <div className="flex min-h-14 flex-wrap items-center gap-x-4 gap-y-2 border-b border-line px-4 py-2 md:flex-nowrap">
        <Link to="/$org/site-apis" params={{ org }} aria-label="Back to Site APIs" className="grid size-8 shrink-0 place-items-center rounded-md text-muted transition-colors duration-(--duration-fast) ease-standard hover:bg-fill-1 hover:text-fg">
          <ArrowLeft aria-hidden="true" className="size-4" />
        </Link>
        <Sentence step={step} go={(next) => go(next)} lane={lane} found={found} draft={draft} />
        <RadioGroup.Root aria-label="Show this as" value={mode} onValueChange={(next) => setMode(next as 'steps' | 'json')} className="ml-auto flex h-7 shrink-0 rounded-md bg-fill-1 p-px">
          {[
            ['steps', 'Steps'],
            ['json', 'JSON definition'],
          ].map(([key = '', label]) => (
            <RadioGroup.Item
              key={key}
              value={key}
              disabled={key === 'json' && !canJson}
              className="rounded-sm px-2 text-12 font-medium whitespace-nowrap text-muted transition-colors duration-(--duration-fast) ease-standard hover:text-fg disabled:text-disabled data-[state=checked]:bg-bg data-[state=checked]:text-fg data-[state=checked]:shadow-[0_0_0_1px_var(--border)]"
            >
              {label}
            </RadioGroup.Item>
          ))}
        </RadioGroup.Root>
      </div>
      <div className="grid min-h-0 flex-1 lg:grid-cols-[minmax(0,1fr)_400px]">
        <div className="board-surface min-h-0 overflow-auto p-6">{stage()}</div>
        <div className="min-h-0 border-line bg-bg max-lg:border-t lg:border-l">{mode === 'json' && canJson && found && draft ? <Definition found={found} draft={draft} /> : panel()}</div>
      </div>
    </div>
  );
}
