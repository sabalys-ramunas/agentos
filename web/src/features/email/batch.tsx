/**
 * The sheet: fifty people in one look. An agent proposes, a person disposes, and a person says yes to
 * the batch, never to each address. Everyone starts in. Pressing a person leaves them out under the
 * hatch, dragging across leaves out several, and what stands out is what is worth reading twice,
 * because an address on a page is text a stranger wrote. Pressing Take sends the sheet to the list.
 * Concept stage: answered by the mock only.
 */
import { SegmentedControl } from '@/components/ui/tabs';
import { Link, useNavigate, useParams } from '@tanstack/react-router';
import { ArrowLeft, Lock, Undo2 } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useDecideBatch, useLeadBatch, useWorkspaces } from '@/api/hooks';
import { isNotServed } from '@/api/http';
import type { Lead, LeadBatch } from '@/api/types';
import { BusinessMark, PersonMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/dialog';
import { StateGlyph } from '@/components/ui/state-badge';
import { EmptyState, ErrorState, NotLive, Skeleton } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { plural, safeUrl } from '@/lib/format';
import { FromComputer } from './shared';
import { addressParts, agoWords, countryName, LOOK_WORDS, SUPPRESSED_WORD } from './words';

type Filter = 'all' | 'linkedin' | 'directory' | 'look';

const matches = (lead: Lead, filter: Filter) => filter === 'all' || (filter === 'look' ? Boolean(lead.look) : lead.source === filter);

function Tile({ lead, out, dim, tabbable, onDown, onEnter, onClick, onPoint, onKey }: {
  lead: Lead;
  out: boolean;
  dim: boolean;
  tabbable: boolean;
  onDown: (event: React.PointerEvent) => void;
  onEnter: (event: React.PointerEvent) => void;
  onClick: (event: React.MouseEvent) => void;
  onPoint: () => void;
  onKey: (event: React.KeyboardEvent) => void;
}) {
  const { local, domain } = addressParts(lead.address);
  return (
    <button
      type="button"
      data-lead={lead.id}
      data-out={out ? '' : undefined}
      data-dim={dim ? '' : undefined}
      data-look={lead.look && !out ? '' : undefined}
      aria-pressed={!out}
      aria-label={`${lead.name}, ${lead.company}, ${lead.address}${lead.look ? `, ${LOOK_WORDS[lead.look].short}` : ''}`}
      tabIndex={tabbable ? 0 : -1}
      onPointerDown={onDown}
      onPointerEnter={(event) => {
        onPoint();
        onEnter(event);
      }}
      onFocus={onPoint}
      onClick={onClick}
      onKeyDown={onKey}
      className="sheet-tile focus-inset block h-12 px-3 py-1"
    >
      <span className="flex min-w-0 items-baseline gap-2 text-secondary">
        <span className="sheet-name min-w-0 truncate font-medium">{lead.name}</span>
        <span className="sheet-sub min-w-0 flex-1 truncate text-caption text-faint">{lead.company}</span>
        {lead.look && !out ? <StateGlyph state="waiting" size={14} className="self-center" /> : null}
      </span>
      <span className="sheet-sub mono flex min-w-0 text-caption text-faint">
        <span className="min-w-0 truncate">{local}</span>
        <span className="shrink-0 text-muted">{domain}</span>
      </span>
    </button>
  );
}

/** The person being pointed at, in one fixed place, so reading never moves anything. */
function Reading({ lead }: { lead: Lead | null }) {
  if (!lead) return <p className="text-secondary text-faint">Point at someone to read where they came from.</p>;
  return (
    <div className="flex min-w-0 items-center gap-3">
      <PersonMark id={lead.id} name={lead.name} size={32} />
      <div className="min-w-0 flex-1">
        <p className="truncate text-secondary">
          <span className="font-medium">{lead.name}</span> <span className="text-muted">· {[lead.role, lead.company, lead.country ? countryName(lead.country) : null].filter(Boolean).join(', ')}</span>
        </p>
        <p className="truncate text-caption text-faint">
          {lead.look ? <span className="text-fg">{LOOK_WORDS[lead.look].long}</span> : <>Found on <span className="mono">{(lead.source_url ?? lead.source_word).replace('https://', '')}</span>, {agoWords(lead.found_at)}. {lead.why}</>}
        </p>
      </div>
    </div>
  );
}

/** All fifty, small, on the desk. Each dot is a tile, so leaving someone out shows here too. */
function DeskDots({ batch, order, out, point }: { batch: LeadBatch; order: Lead[]; out: ReadonlySet<string>; point: string | null }) {
  return (
    <span aria-label={`${batch.leads.length - out.size} of ${batch.leads.length} kept`} role="img" className="grid w-max grid-cols-[repeat(25,6px)] gap-1">
      {order.map((lead) => <span key={lead.id} className="desk-dot" data-look={lead.look ? '' : undefined} data-out={out.has(lead.id) ? '' : undefined} data-point={point === lead.id ? '' : undefined} />)}
      {(batch.suppressed ?? []).map((lead) => <span key={lead.id} className="desk-dot" data-never="" />)}
    </span>
  );
}

function Filters({ value, onChange, batch }: { value: Filter; onChange: (filter: Filter) => void; batch: LeadBatch }) {
  const items: { key: Filter; word: string; count: number }[] = [
    { key: 'all', word: 'Everyone', count: batch.leads.length },
    ...batch.sources.map((source) => ({ key: source.key as Filter, word: source.word, count: batch.leads.filter((lead) => lead.source === source.key).length })),
    { key: 'look', word: 'Worth a look', count: batch.leads.filter((lead) => lead.look).length },
  ];
  return (
    <div role="group" aria-label="Show" className="flex flex-wrap items-center gap-1">
      {items.map((item) => (
        <button key={item.key} type="button" aria-pressed={value === item.key} onClick={() => onChange(item.key)} className={cn('flex h-7 items-center gap-2 rounded-md px-2 text-secondary transition-colors duration-(--duration-fast) ease-standard', value === item.key ? 'bg-pressed text-fg' : 'text-muted hover:bg-hover')}>
          {item.key === 'look' ? <StateGlyph state="waiting" size={14} /> : null}
          {item.word}
          <span className="tabular text-caption text-faint">{item.count}</span>
        </button>
      ))}
    </div>
  );
}

function Rows({ leads, out, onToggle }: { leads: Lead[]; out: ReadonlySet<string>; onToggle: (id: string) => void }) {
  return (
    <div className="overflow-x-auto rounded-(--radius-card) border border-line">
      <table className="w-full text-secondary">
        <thead className="text-left text-caption text-faint">
          <tr className="border-b border-line">
            <th className="w-10 px-3 py-2 font-normal"><span className="sr-only">Keep</span></th>
            <th className="px-3 py-2 font-normal">Name</th>
            <th className="px-3 py-2 font-normal">Company</th>
            <th className="px-3 py-2 font-normal">Address</th>
            <th className="px-3 py-2 font-normal">Where it came from</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {leads.map((lead) => {
            const url = safeUrl(lead.source_url);
            return (
              <tr key={lead.id} className={cn(out.has(lead.id) && 'left-out text-faint')}>
                <td className="px-3 py-2"><input type="checkbox" aria-label={`Keep ${lead.name}`} checked={!out.has(lead.id)} onChange={() => onToggle(lead.id)} className="accent-(--foreground)" /></td>
                <td className="px-3 py-2 whitespace-nowrap">{lead.name}</td>
                <td className="px-3 py-2 whitespace-nowrap text-muted">{lead.company}</td>
                <td className="mono px-3 py-2 text-caption whitespace-nowrap">{lead.address}</td>
                <td className="px-3 py-2 whitespace-nowrap">{url ? <a href={url} target="_blank" rel="noreferrer" className="link">{lead.source_word}</a> : lead.source_word}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Sheet({ org, batch }: { org: string; batch: LeadBatch }) {
  const navigate = useNavigate();
  const decide = useDecideBatch(org, batch.id);
  const workspace = useWorkspaces(org).data?.find((item) => item.id === batch.workspace);
  const [out, setOut] = useState<ReadonlySet<string>>(new Set());
  const [undo, setUndo] = useState<ReadonlySet<string>[]>([]);
  const [filter, setFilter] = useState<Filter>('all');
  const [view, setView] = useState<'sheet' | 'list'>('sheet');
  const [point, setPoint] = useState<string | null>(null);
  const [cursor, setCursor] = useState(0);
  const [dropping, setDropping] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const paint = useRef<boolean | null>(null);
  const mouse = useRef(false);
  const take = useRef<HTMLDivElement>(null);
  const sheet = useRef<HTMLDivElement>(null);

  // What is worth a look comes first in its group, so the eye lands there.
  const groups = useMemo(
    () => batch.sources.map((source) => ({ ...source, leads: batch.leads.filter((lead) => lead.source === source.key).sort((a, b) => Number(Boolean(b.look)) - Number(Boolean(a.look))) })),
    [batch],
  );
  const order = useMemo(() => groups.flatMap((group) => group.leads), [groups]);
  const kept = batch.leads.length - out.size;
  const pointed = batch.leads.find((lead) => lead.id === point) ?? null;

  useEffect(() => {
    const stop = () => {
      paint.current = null;
    };
    window.addEventListener('pointerup', stop);
    return () => window.removeEventListener('pointerup', stop);
  }, []);

  const change = (next: ReadonlySet<string>) => {
    setUndo((steps) => [...steps.slice(-20), out]);
    setOut(next);
  };
  const toggle = (id: string) => {
    const next = new Set(out);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    change(next);
  };
  const setMany = (ids: string[], drop: boolean) => {
    const next = new Set(out);
    for (const id of ids) {
      if (drop) next.add(id);
      else next.delete(id);
    }
    change(next);
  };

  const onKey = (event: React.KeyboardEvent, index: number) => {
    const columns = sheet.current ? getComputedStyle(sheet.current.querySelector('[data-grid]') as Element).gridTemplateColumns.split(' ').length : 4;
    const by = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: columns, ArrowUp: -columns }[event.key];
    if (!by) return;
    event.preventDefault();
    const next = Math.min(order.length - 1, Math.max(0, index + by));
    setCursor(next);
    sheet.current?.querySelector<HTMLElement>(`[data-lead="${order[next]?.id}"]`)?.focus();
  };

  /** The kept tiles fall into the button, so taking them feels like what it is. */
  const land = async () => {
    const target = take.current?.getBoundingClientRect();
    const tiles = [...(sheet.current?.querySelectorAll<HTMLElement>('[data-lead]') ?? [])];
    const calm = matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (!target || calm || !tiles[0]?.animate) return;
    setLeaving(true);
    await Promise.all(
      tiles.map((tile, index) => {
        const from = tile.getBoundingClientRect();
        const x = target.left + target.width / 2 - (from.left + from.width / 2);
        const y = target.top + target.height / 2 - (from.top + from.height / 2);
        const gone = tile.hasAttribute('data-out');
        return tile.animate(gone ? [{ opacity: 1 }, { opacity: 0 }] : [{ transform: 'none', opacity: 1 }, { transform: `translate(${x}px, ${y}px) scale(0.08)`, opacity: 0.2 }], { duration: 460, delay: index * 7, easing: 'cubic-bezier(0.16, 1, 0.3, 1)', fill: 'forwards' }).finished;
      }),
    );
  };

  const onTake = async () => {
    const keep = batch.leads.filter((lead) => !out.has(lead.id)).map((lead) => lead.id);
    const decided = decide.mutateAsync(keep);
    await land();
    const { list } = await decided;
    if (list) void navigate({ to: '/$org/email/lists/$list', params: { org, list } });
  };

  const looks = batch.leads.filter((lead) => lead.look).length;
  return (
    <div data-bleed="" className="flex h-[calc(100dvh-var(--topbar-height))] min-h-0 flex-col max-lg:h-auto">
      <div className="shrink-0 px-4 pt-4 sm:px-6 lg:px-10">
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-1">
        <p className="flex min-w-0 items-center gap-2 text-secondary text-muted">
          <Link to="/$org/email/leads" params={{ org }} className="inline-flex items-center gap-1 hover:text-fg">
            <ArrowLeft className="size-3" /> Leads
          </Link>
          <span aria-hidden="true" className="text-faint">/</span>
          {workspace ? <BusinessMark name={workspace.name} color={workspace.color} size={16} /> : null}
          <span className="truncate">{workspace?.name} · {batch.list_name}</span>
        </p>
        <FromComputer computers={['hetzner-1']} />
      </div>
      <h1 className="mt-3 text-headline text-balance">{batch.by} found {batch.found} people. <span className="text-faint">Who should stay?</span></h1>
      <p className="mt-1 text-body text-pretty text-muted">Everyone starts in. Press someone to leave them out, or drag across several. {plural(looks, 'is', 'are')} worth reading twice.</p>

      <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
        <Filters value={filter} onChange={setFilter} batch={batch} />
        <SegmentedControl
          label="How to show them"
          value={view}
          onValueChange={setView}
          options={[
            { value: 'sheet', label: 'Sheet' },
            { value: 'list', label: 'List' },
          ]}
        />
      </div>
      </div>

      <div ref={sheet} className="scrollbar-none mt-2 grid min-h-0 flex-1 content-start gap-3 overflow-y-auto px-4 py-1 sm:px-6 lg:px-10">
        {view === 'list' ? (
          <Rows leads={order.filter((lead) => matches(lead, filter))} out={out} onToggle={toggle} />
        ) : (
          groups.map((group) => {
            const ids = group.leads.map((lead) => lead.id);
            const allOut = ids.every((id) => out.has(id));
            return (
              <section key={group.key} aria-label={group.word}>
                <div className="mb-1 flex items-center gap-2 text-secondary">
                  {group.key === 'linkedin' ? <SiteMark site="linkedin" size={14} label={false} /> : <SiteMark site="baltic-marine-directory.example" size={14} label={false} />}
                  <span className="font-medium">{group.word}</span>
                  <span className="min-w-0 truncate text-faint">{plural(group.leads.length, 'person', 'people')} {group.note}{group.key === 'directory' && batch.suppressed ? `, and ${batch.suppressed.length} who are on the do-not-write list, so they cannot be added` : ''}</span>
                  <button type="button" className="ml-auto shrink-0 text-caption text-muted hover:text-fg" onClick={() => setMany(ids, !allOut)}>
                    {allOut ? 'Bring them all back' : `Leave out all ${ids.length}`}
                  </button>
                </div>
                <div data-grid className="grid grid-cols-1 gap-px p-px sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-5">
                  {group.leads.map((lead) => {
                    const index = order.indexOf(lead);
                    return (
                      <Tile
                        key={lead.id}
                        lead={lead}
                        out={out.has(lead.id)}
                        dim={!matches(lead, filter)}
                        tabbable={index === cursor}
                        onPoint={() => setPoint(lead.id)}
                        onKey={(event) => onKey(event, index)}
                        onDown={(event) => {
                          mouse.current = event.pointerType === 'mouse';
                          if (!mouse.current) return;
                          paint.current = !out.has(lead.id);
                          setCursor(index);
                          toggle(lead.id);
                        }}
                        onEnter={(event) => {
                          if (paint.current === null || event.buttons !== 1) return;
                          if (out.has(lead.id) !== paint.current) toggle(lead.id);
                        }}
                        onClick={(event) => {
                          if (event.detail === 0 || !mouse.current) toggle(lead.id);
                        }}
                      />
                    );
                  })}
                  {group.key === 'directory' ? (batch.suppressed ?? []).map((lead) => (
                    <div key={lead.id} data-never="" className="sheet-tile flex h-12 items-center gap-2 px-3" aria-label={`${lead.name}: ${SUPPRESSED_WORD[lead.why]}. Not in this batch.`}>
                      <Lock aria-hidden="true" className="size-3 shrink-0 text-faint" />
                      <span className="min-w-0">
                        <span className="block truncate text-secondary text-muted">{lead.name}</span>
                        <span className="block truncate text-caption text-faint">{SUPPRESSED_WORD[lead.why]}</span>
                      </span>
                    </div>
                  )) : null}
                </div>
              </section>
            );
          })
        )}
      </div>

      {/* The desk: what is being read, and the one decision. It never moves. */}
      <div className="sticky bottom-0 z-10 shrink-0 border-t border-line bg-card px-4 py-3 sm:px-6 lg:px-10">
        <div className="grid items-center gap-x-6 gap-y-3 lg:grid-cols-[minmax(0,1fr)_auto]">
          <div className="min-h-8 min-w-0"><Reading lead={pointed} /></div>
          <div className="flex flex-wrap items-center gap-2">
            <div ref={take} className="mr-2 hidden xl:block"><DeskDots batch={batch} order={order} out={out} point={point} /></div>
            {undo.length ? (
              <Button variant="ghost" size="md" icon={<Undo2 aria-hidden="true" className="size-3" />} onClick={() => {
                setOut(undo.at(-1) ?? new Set());
                setUndo(undo.slice(0, -1));
              }}>
                Undo
              </Button>
            ) : null}
            <Button onClick={() => setDropping(true)}>Drop them all</Button>
            <Button variant="ghost" onClick={() => change(out.size === batch.leads.length ? new Set() : new Set(batch.leads.map((lead) => lead.id)))}>{out.size === batch.leads.length ? 'Start with everyone' : 'Start with nobody'}</Button>
            <div>
              <Button variant="primary" size="lg" loading={decide.isPending || leaving} aria-disabled={kept === 0} onClick={() => kept > 0 && void onTake()}>
                {out.size === 0 ? `Take all ${kept}` : kept ? `Take these ${kept}` : 'Nobody kept'}
              </Button>
            </div>
          </div>
        </div>
      </div>

      <ConfirmDialog
        open={dropping}
        onOpenChange={setDropping}
        title={`Drop all ${batch.leads.length} people?`}
        description="None of them goes on a list, and nobody is written to."
        confirmLabel="Drop them all"
        onConfirm={() => {
          decide.mutate([], { onSuccess: () => void navigate({ to: '/$org/email/leads', params: { org } }) });
        }}
      />
    </div>
  );
}

export function Batch() {
  const { org, batch: id } = useParams({ from: '/$org/email/leads/$batch' });
  const batch = useLeadBatch(org, id);
  if (isNotServed(batch.error)) return <Card><NotLive /></Card>;
  if (batch.error) return <ErrorState error={batch.error} onRetry={() => void batch.refetch()} />;
  if (!batch.data) return <div className="grid gap-4"><Skeleton className="h-24 max-w-3xl rounded-lg" /><Skeleton className="h-96 rounded-lg" /></div>;
  if (batch.data.status !== 'waiting') {
    return <Card><EmptyState icon={<Lock />} title="This batch has been decided" description="A batch gets one look, and it has had it." action={<Link to="/$org/email/leads" params={{ org }} className="link text-body">Back to leads</Link>} /></Card>;
  }
  return <Sheet org={org} batch={batch.data} />;
}
