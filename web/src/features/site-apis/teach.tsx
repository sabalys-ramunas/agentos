/**
 * Teach it: making a Site API should feel like pointing at a page. On the left the page, with what
 * the computer found outlined. On the right what it says about it and the one thing to decide. The
 * sentence on top is the whole Site API so far, and each phrase goes back to its step. Nothing here
 * needs a technical word, and a developer can flip the panel to the definition at any point. Only
 * a person turns one on. In the concept it runs on the mock, so turning on opens the demo's own.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { ArrowLeft, ArrowRight, Globe, SearchX } from 'lucide-react';
import { RadioGroup } from 'radix-ui';
import type { ReactNode } from 'react';
import { useEffect, useState } from 'react';
import { useSiteDiscovery, useWorkspaces } from '@/api/hooks';
import { isNotServed } from '@/api/http';
import type { Lane, SiteDiscovery } from '@/api/types';
import type { TeachStep } from '@/app/routes/org';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Field, Input } from '@/components/ui/field';
import { ErrorState, NotLive, Skeleton } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { plural } from '@/lib/format';
import { draftColumns, draftFields, draftRows, firstDraft, keyOf } from './draft';
import type { Draft } from './draft';
import { PageSheet } from './page-sheet';
import type { Lit } from './page-sheet';
import { Split } from './split';
import './site-apis.css';
import { AccountStage } from './teach-account';
import { ActivatePanel, ColumnsPanel, Definition, DonePanel, EmptySheet, FoundPanel, Panel, TryPanel } from './teach-panels';

const ORDER: TeachStep[] = ['account', 'page', 'found', 'columns', 'try', 'activate'];
export const FRESHNESS = [
  { value: 60, label: 'Within a minute', note: 'Reads often. Best for things that change by the minute.' },
  { value: 300, label: 'Within 5 minutes', note: 'A good start for most lists.' },
  { value: 3600, label: 'Within the hour', note: 'Gentle on the account. Best for slow lists.' },
];
export const SURE = { sure: 'Sure', likely: 'Likely', unsure: 'Not sure' } as const;

/** A value that keeps moving, so the panel shows the real page and not one frozen example. */
export function useTick(ms: number) {
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
        on ? 'bg-pressed text-fg decoration-fg' : done ? 'text-fg decoration-line-strong hover:bg-hover hover:decoration-fg' : 'cursor-default text-faint decoration-dotted decoration-line-strong',
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
    <p className="min-w-0 text-lede text-pretty text-muted max-md:order-last max-md:w-full">
      On {phrase('account', found ? `${lane?.account?.user_id ?? 'your account'} at ${found.site.name}` : (lane?.account?.user_id ?? 'which account'))}, read {phrase('found', found && at >= 2 ? `the table of ${found.total}` : 'which part of the page')}, keep {phrase('columns', draft && at >= 3 ? plural(draft.kept.length, 'column') : 'which columns')}, and call it {phrase('activate', draft && at >= 4 ? draft.title : 'a name')}.
    </p>
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
        <div className="mx-auto grid max-w-md place-items-center rounded-lg border border-board-line bg-card p-8 text-center">
          <SearchX aria-hidden="true" className="size-4 text-muted" />
          <p className="mt-2 text-label">Nothing repeats on this page</p>
          <p className="mt-1 text-secondary text-muted">A Site API needs a list, a table or a set of cards. Try the page that shows them.</p>
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
          <p className="text-body text-pretty text-muted">It reads the site the way you would, signed in as this account, and it only ever uses this one. Pick the one that can see what you want.</p>
          <p className="mt-4 text-secondary text-pretty text-faint">Already signed in, so nothing is typed and no password is needed here.</p>
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
                        <span className="block truncate text-secondary font-medium">{item.words}</span>
                        <span className="mono block truncate text-caption text-faint">{item.url.replace('https://', '')}</span>
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
              <p className="text-body text-pretty text-muted">Nothing on this page repeats, so there is no table to make. Try another page, or another account.</p>
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
        <Link to="/$org/site-apis" params={{ org }} aria-label="Back to Site APIs" className="grid size-8 shrink-0 place-items-center rounded-md text-muted transition-colors duration-(--duration-fast) ease-standard hover:bg-hover hover:text-fg">
          <ArrowLeft aria-hidden="true" className="size-4" />
        </Link>
        <Sentence step={step} go={(next) => go(next)} lane={lane} found={found} draft={draft} />
        <RadioGroup.Root aria-label="Show this as" value={mode} onValueChange={(next) => setMode(next as 'steps' | 'json')} className="ml-auto flex h-7 shrink-0 rounded-md bg-hover p-px">
          {[
            ['steps', 'Steps'],
            ['json', 'JSON definition'],
          ].map(([key = '', label]) => (
            <RadioGroup.Item
              key={key}
              value={key}
              disabled={key === 'json' && !canJson}
              className="rounded-sm px-2 text-caption font-medium whitespace-nowrap text-muted transition-colors duration-(--duration-fast) ease-standard hover:text-fg disabled:text-disabled data-[state=checked]:bg-card data-[state=checked]:text-fg data-[state=checked]:shadow-[0_0_0_1px_var(--border)]"
            >
              {label}
            </RadioGroup.Item>
          ))}
        </RadioGroup.Root>
      </div>
      <div className="grid min-h-0 flex-1 lg:grid-cols-[minmax(0,1fr)_400px]">
        <div className="board-surface min-h-0 overflow-auto p-6">{stage()}</div>
        <div className="min-h-0 border-line bg-card max-lg:border-t lg:border-l">{mode === 'json' && canJson && found && draft ? <Definition found={found} draft={draft} /> : panel()}</div>
      </div>
    </div>
  );
}
