/**
 * One Site API. The page leads with the job: a plain sentence, then the page and what it became. What
 * is wrong, if anything, sits above that in plain words with one next step, and shows on the picture
 * as well: a broken field is a broken wire. Then the endpoints, the two doors (one for a person, one
 * for a developer), the week of calls, what it runs on, and its keys.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { ArrowLeft, Code2, Hand, Lock, Moon, Pause, Play, Table2 } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { useSiteAction, useSiteApi, useWorkspaces } from '@/api/hooks';
import { isNotServed } from '@/api/http';
import type { SiteApiDetail as Api, SiteEndpoint, SitePageColumn } from '@/api/types';
import type { SiteDoor } from '@/app/routes/org';
import { BusinessMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { StateBadge, StateGlyph } from '@/components/ui/state-badge';
import { ErrorState, NotLive, Skeleton } from '@/components/ui/states';
import { useNow } from '@/lib/use-now';
import { cn } from '@/lib/cn';
import { clock, compact, plural, timeAgo, timeUntil } from '@/lib/format';
import { useTechnical } from '@/lib/technical';
import { Keys } from './keys';
import { SeeData } from './see-data';
import { Split } from './split';
import { Requests, RunsOn } from './usage';
import { UseCode } from './use-code';
import { EFFECT_WORD, endpointOf, freshWords, HEALTH_WORD, markOf, problemWords, speed, STATE_OF } from './words';

type Look = 'taught' | 'now';

function Veil({ icon: Icon, title, note }: { icon: LucideIcon; title: string; note: string }) {
  return (
    <div className="max-w-sm rounded-lg border border-line bg-raised p-4 text-center">
      <Icon aria-hidden="true" className="mx-auto size-4 text-muted" />
      <p className="mt-2 text-14 font-medium">{title}</p>
      <p className="mt-1 text-13 text-pretty text-muted">{note}</p>
    </div>
  );
}

function veilOf(api: Api) {
  switch (api.health) {
    case 'sign_in':
      return <Veil icon={Lock} title="Stopped at the sign-in page" note="The page is not shown, and nothing is typed into it. It waits here for you." />;
    case 'site_check':
      return <Veil icon={Hand} title="Stopped at the site's own check" note="It will not try to answer, and it will not try again. It waits here for you." />;
    case 'resting':
      return <Veil icon={Moon} title={`Resting until ${clock(api.problem?.resumes_at ?? api.read_at ?? '')}`} note="Reading sooner could cost you the account, so it waits." />;
    case 'paused':
      return <Veil icon={Pause} title="Not reading" note="This Site API is paused, so nothing goes near the site." />;
    default:
      return null;
  }
}

/** What happened and the one next step. Everything else on the page stays where it is. */
function Trouble({ org, api, endpoint, look, onShow }: { org: string; api: Api; endpoint: SiteEndpoint; look: Look; onShow: () => void }) {
  const action = useSiteAction(org, api.key);
  const [technical] = useTechnical();
  const [told, setTold] = useState(false);
  const now = useNow(api.health === 'resting');
  const field = endpoint.fields.find((item) => item.key === api.problem?.field);
  const words = problemWords(api, field);
  if (!words) return null;
  const [ws = '', name = ''] = api.lane.split('/');
  const state = STATE_OF[api.health];
  const looked = look === 'now';

  const fixed = (message: string) => () => toast(message);
  let step;
  switch (api.health) {
    case 'changed':
      step = looked ? (
        <Button variant="primary" loading={action.isPending} onClick={() => action.mutate({ kind: 'repair' }, { onSuccess: fixed(`${api.title} is answering again`) })}>
          Use "{api.problem?.now_label}" for {field?.label}
        </Button>
      ) : (
        <Button variant="primary" onClick={onShow}>
          Show me
        </Button>
      );
      break;
    case 'sign_in':
    case 'site_check':
      step = (
        <Button variant="primary" asChild>
          <Link to="/$org/lanes/$ws/$name" params={{ org, ws, name }}>
            {api.health === 'sign_in' ? 'Sign in again' : 'Open the account'}
          </Link>
        </Button>
      );
      break;
    case 'empty':
      step = looked ? (
        <Button variant="primary" loading={action.isPending} onClick={() => action.mutate({ kind: 'accept-empty' }, { onSuccess: fixed('Good. It will take an empty list as true for now') })}>
          It really is empty
        </Button>
      ) : (
        <Button variant="primary" onClick={onShow}>
          Show me
        </Button>
      );
      break;
    case 'resting':
      step = (
        <Button aria-pressed={told} onClick={() => setTold((value) => !value)}>
          {told ? 'You will be told' : 'Tell me when it is back'}
        </Button>
      );
      break;
    case 'paused':
      step = (
        <Button variant="primary" icon={<Play aria-hidden="true" className="size-4" />} onClick={() => action.mutate({ kind: 'resume' })}>
          Resume
        </Button>
      );
      break;
    default:
      step = null;
  }

  return (
    <section
      aria-label="What needs attention"
      className={cn('mb-6 grid items-center gap-4 rounded-(--radius-card) border bg-bg p-4 md:grid-cols-[auto_minmax(0,1fr)_auto]', state === 'stuck' ? 'border-(--stuck-line)' : state === 'waiting' ? 'border-(--wait-line)' : 'border-line')}
    >
      <StateGlyph state={state} size={24} className="max-md:hidden" />
      <div className="min-w-0">
        <h2 className="text-16 font-semibold text-pretty">{words.title}</h2>
        <p className="mt-1 text-14 text-pretty text-muted">{looked && api.health === 'changed' ? `The new column is outlined above the wires. If it says the same thing as "${field?.label}", use it and the wire joins up again.` : looked && api.health === 'empty' ? 'The page is below, as the computer saw it. If this is right, say so. If rows should be there, teach it the page again.' : words.body}</p>
        <p className="mt-1 text-13 text-faint">
          {words.callers}
          {api.health === 'resting' && api.problem?.resumes_at ? ` ${timeUntil(api.problem.resumes_at, now)} to go.` : ''}
          {technical ? <span className="mono"> {words.code}</span> : null}
        </p>
      </div>
      <div className="flex gap-2">
        {looked && api.health === 'empty' ? (
          <Button asChild>
            <Link to="/$org/site-apis/new" params={{ org }} search={{ lane: api.lane, step: 'found' }}>
              Teach it again
            </Link>
          </Button>
        ) : null}
        {step}
      </div>
    </section>
  );
}

function Header({ org, api, business, color }: { org: string; api: Api; business: string | undefined; color: string | undefined }) {
  const action = useSiteAction(org, api.key);
  const proposed = api.stage === 'proposed';
  return (
    <header className="mb-6">
      <Link to="/$org/site-apis" params={{ org }} className="group mb-4 inline-flex items-center gap-1 text-13 text-muted transition-colors duration-(--duration-fast) ease-standard hover:text-fg">
        <ArrowLeft aria-hidden="true" className="size-4 transition-transform duration-(--duration-fast) ease-standard group-hover:-translate-x-px" />
        Site APIs
      </Link>
      <div className="flex items-start gap-4 max-sm:flex-col">
        <SiteMark site={markOf(api.site)} size={40} tile label={false} className="bg-bg max-sm:hidden" />
        <div className="min-w-0 flex-1">
          <h1 className="text-24 font-semibold text-balance sm:text-32">{api.title}</h1>
          <p className="mt-1 max-w-2xl text-16 text-pretty text-muted">{api.words}</p>
          <p className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-13 text-faint">
            <span className="inline-flex items-center gap-2">
              {business && color ? <BusinessMark name={business} color={color} size={16} /> : null}
              {business}
            </span>
            <span className="mono">{api.site.domain}</span>
            <span className="tabular">
              {proposed ? 'Tried once, not on yet' : `${compact(api.calls_7d)} calls this week`}
            </span>
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-3">
          <StateBadge state={proposed ? 'waiting' : STATE_OF[api.health]} label={proposed ? 'Waiting for you' : HEALTH_WORD[api.health]} />
          {api.stage === 'active' && api.health === 'good' ? (
            <Button icon={<Pause aria-hidden="true" className="size-4" />} onClick={() => action.mutate({ kind: 'pause' })}>
              Pause
            </Button>
          ) : null}
        </div>
      </div>
    </header>
  );
}

function ProposalStrip({ org, api, endpoint }: { org: string; api: Api; endpoint: SiteEndpoint }) {
  const action = useSiteAction(org, api.key);
  const navigate = useNavigate();
  const { proposal } = api;
  if (!proposal) return null;
  return (
    <section aria-label="Proposed by an agent" className="mb-6 rounded-(--radius-card) border border-(--wait-line) bg-bg">
      <div className="grid gap-4 p-4 md:grid-cols-2">
        <div>
          <h2 className="text-16 font-semibold">{proposal.by} proposed this, and only a person can turn it on</h2>
          <p className="mt-1 text-14 text-pretty text-muted">
            {proposal.tried_rows === undefined ? null : `It read ${plural(proposal.tried_rows, 'row')} once to try it. `}Below is exactly what your computer recorded, in the computer's words, not the agent's.
          </p>
        </div>
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 text-13">
          <dt className="text-faint">Site</dt>
          <dd className="mono">{api.site.domain}</dd>
          <dt className="text-faint">Steps</dt>
          <dd>
            <ol className="grid gap-px">
              {proposal.steps.map((step, index) => (
                <li key={step}>
                  {index + 1}. {step}
                </li>
              ))}
            </ol>
          </dd>
          <dt className="text-faint">Fields</dt>
          <dd>{endpoint.fields.map((field) => field.label).join(', ')}</dd>
          <dt className="text-faint">It may</dt>
          <dd>{EFFECT_WORD[endpoint.effect].note}</dd>
        </dl>
      </div>
      <div className="flex items-center justify-end gap-2 border-t border-line p-3">
        <Button
          onClick={() =>
            action.mutate(
              { kind: 'decline' },
              {
                onSuccess: () => {
                  toast('Declined. Nothing was turned on');
                  void navigate({ to: '/$org/site-apis', params: { org } });
                },
              },
            )
          }
        >
          Don't turn on
        </Button>
        <Button variant="primary" loading={action.isPending} onClick={() => action.mutate({ kind: 'activate' }, { onSuccess: () => toast(`${api.title} is on`) })}>
          Turn it on
        </Button>
      </div>
    </section>
  );
}

function Endpoints({ api, selected }: { api: Api; selected: SiteEndpoint }) {
  return (
    <section aria-labelledby="site-endpoints" className="mt-8">
      <h2 id="site-endpoints" className="mb-3 text-16 font-semibold">
        {plural(api.endpoints.length, 'endpoint')}
      </h2>
      <ul className="divide-y divide-line overflow-hidden rounded-(--radius-card) border border-line">
        {api.endpoints.map((endpoint) => {
          const on = endpoint.key === selected.key;
          return (
            <li key={endpoint.key}>
              <Link
                from="/$org/site-apis/$api"
                search={(search) => ({ ...search, endpoint: endpoint.key })}
                replace
                resetScroll={false}
                aria-current={on ? 'true' : undefined}
                className={cn(
                  'relative grid gap-x-6 gap-y-1 px-4 py-3 transition-colors duration-(--duration-fast) ease-standard hover:bg-fill-1 md:grid-cols-[minmax(0,1.4fr)_auto_auto_auto_auto] md:items-center',
                  on && 'bg-fill-1 before:absolute before:inset-y-0 before:left-0 before:w-0.5 before:bg-fg',
                )}
              >
                <span className="min-w-0">
                  <span className="block truncate text-14 font-medium">{endpoint.title}</span>
                  <span className="mono block truncate text-12 text-faint">
                    GET /{api.key}/{endpoint.key}
                  </span>
                </span>
                <span className="text-13 text-muted">{EFFECT_WORD[endpoint.effect].word}</span>
                <span className="text-13 text-muted">{freshWords(endpoint.cache_s)}</span>
                <span className="tabular text-13 text-muted">{endpoint.p50_ms === null ? '–' : speed(endpoint.p50_ms)}</span>
                <span className="tabular text-13 text-muted md:w-24 md:text-right">{compact(endpoint.calls_7d)} calls</span>
              </Link>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

const DOORS: { key: SiteDoor; icon: LucideIcon; title: string; note: string }[] = [
  { key: 'data', icon: Table2, title: 'See the data', note: 'Read it here, copy it into a sheet, or keep it up to date. No code.' },
  { key: 'code', icon: Code2, title: 'Use it in code', note: 'A URL, a key and an OpenAPI document, with a console to try it.' },
];

function Doors({ org, api, endpoint, door }: { org: string; api: Api; endpoint: SiteEndpoint; door: SiteDoor }) {
  return (
    <section aria-label="Two ways in" className="mt-8">
      <div role="tablist" aria-label="Two ways in" className="grid md:grid-cols-2">
        {DOORS.map((item, index) => {
          const on = item.key === door;
          return (
            <Link
              key={item.key}
              role="tab"
              aria-selected={on}
              from="/$org/site-apis/$api"
              search={(search) => ({ ...search, door: item.key })}
              replace
              resetScroll={false}
              className={cn(
                'door relative flex items-start gap-3 border border-line p-4',
                index === 0 ? 'rounded-tl-(--radius-card) md:border-r-0' : 'rounded-tr-(--radius-card)',
                on ? 'z-10 border-b-transparent bg-bg' : 'bg-subtle hover:bg-fill-1',
              )}
            >
              <span className={cn('grid size-8 shrink-0 place-items-center rounded-lg', on ? 'bg-fg text-bg' : 'border border-line bg-bg text-muted')}>
                <item.icon aria-hidden="true" className="size-4" />
              </span>
              <span className="min-w-0">
                <span className="block text-16 font-semibold">{item.title}</span>
                <span className="block text-13 text-muted">{item.note}</span>
              </span>
            </Link>
          );
        })}
      </div>
      <div className="-mt-px overflow-hidden rounded-b-(--radius-card) border border-line bg-bg">
        {door === 'data' ? <SeeData key={endpoint.key} org={org} api={api} endpoint={endpoint} /> : <UseCode key={endpoint.key} org={org} api={api} endpoint={endpoint} />}
      </div>
    </section>
  );
}

function Plumbing({ api, endpoint }: { api: Api; endpoint: SiteEndpoint }) {
  const [technical] = useTechnical();
  if (!technical) return null;
  return (
    <details className="mt-8 rounded-(--radius-card) border border-line">
      <summary className="cursor-pointer px-4 py-3 text-13 font-medium">Technical details</summary>
      <dl className="mono grid grid-cols-[auto_minmax(0,1fr)] gap-x-6 gap-y-1 border-t border-line p-4 text-12">
        <dt className="text-faint">api</dt>
        <dd>{api.key}</dd>
        <dt className="text-faint">version</dt>
        <dd>{api.version}</dd>
        <dt className="text-faint">surface</dt>
        <dd>{api.lane}</dd>
        <dt className="text-faint">flow</dt>
        <dd>connector.{api.key}.{endpoint.key}</dd>
        <dt className="text-faint">cache_s</dt>
        <dd>{endpoint.cache_s}</dd>
        <dt className="text-faint">pace</dt>
        <dd>{endpoint.pace_per_hour} per hour</dd>
        <dt className="text-faint">expect</dt>
        <dd>{endpoint.expect_min === null ? 'nothing' : `min_items ${endpoint.expect_min}`}</dd>
      </dl>
    </details>
  );
}

/** This computer keeps the answers a caller gets, not a copy of the page, so there is no picture to draw yet. */
function PageNotKept({ endpoint, onRead, reading }: { endpoint: SiteEndpoint; onRead: (() => void) | undefined; reading: boolean }) {
  return (
    <section aria-label="The page" className="mt-6 grid gap-3 rounded-lg border border-line p-4">
      <h2 className="text-16 font-semibold">No picture of the page yet</h2>
      <p className="text-13 text-pretty text-muted">
        This computer keeps the data a read returns, not a copy of the page, so there is nothing to draw here. The fields
        it reads are {endpoint.fields.length ? endpoint.fields.map((field) => field.label).join(', ') : 'not declared'}.
      </p>
      {onRead ? (
        <div>
          <Button variant="secondary" loading={reading} onClick={onRead}>
            Read now
          </Button>
        </div>
      ) : null}
    </section>
  );
}

function Loaded({ org, api, door, endpointKey }: { org: string; api: Api; door: SiteDoor; endpointKey: string | undefined }) {
  const workspaces = useWorkspaces(org).data;
  const business = workspaces?.find((item) => item.id === api.workspace);
  const read = useSiteAction(org, api.key);
  const [look, setLook] = useState<Look>('taught');
  const [reading, setReading] = useState(false);
  const endpoint = endpointOf(api, endpointKey);
  const moved = api.problem?.field;
  const changed = api.health === 'changed';
  const proposed = api.stage === 'proposed';

  const page = endpoint.page;
  const columns: SitePageColumn[] | undefined =
    changed && look === 'now' && page
      ? page.columns.map((column) => (column.field === moved ? { label: api.problem?.now_label ?? '', field: null, values: api.problem?.now_values } : column))
      : undefined;

  // The picture is right under the words, so nothing scrolls: the next step stays in view.
  const show = () => setLook('now');

  const readNow = () => {
    setReading(true);
    read.mutate(
      { kind: 'read' },
      {
        onSettled: () => setReading(false),
        onSuccess: () => toast('Read just now'),
      },
    );
  };

  return (
    <div className="pb-16">
      <Header org={org} api={api} business={business?.name} color={business?.color} />
      {proposed ? <ProposalStrip org={org} api={api} endpoint={endpoint} /> : <Trouble org={org} api={api} endpoint={endpoint} look={look} onShow={show} />}
      {page ? (
        <Split
          key={endpoint.key}
          page={page}
          fields={endpoint.fields}
          rows={endpoint.rows}
          total={endpoint.total ?? endpoint.rows.length}
          name={endpoint.key.split('_').at(-1) ?? 'rows'}
          health={api.health}
          newRows={endpoint.new_rows}
          readAt={page.seen_at}
          veil={veilOf(api)}
          columns={columns}
          broken={changed && look === 'taught' ? moved : null}
          held={changed ? moved : null}
          arrived={columns ? api.problem?.now_label : null}
          reading={reading}
          readBusy={reading}
          onRead={api.health === 'good' && !proposed ? readNow : undefined}
          dataLabel={proposed ? 'What it would become' : api.health === 'good' ? undefined : `Last good read, ${timeAgo(api.read_at).toLowerCase()}`}
          rest={changed || api.health === 'empty' ? null : page.layout === 'table' ? { field: endpoint.fields[3]?.key ?? endpoint.fields[0]?.key ?? '', row: 1 } : { field: endpoint.fields[0]?.key ?? '', row: 0 }}
          note={changed ? (look === 'now' ? `The page now, read ${timeAgo(page.seen_at).toLowerCase()}` : 'The page as you taught it, with what no longer reads') : undefined}
        />
      ) : (
        <PageNotKept endpoint={endpoint} onRead={api.health === 'good' && !proposed ? readNow : undefined} reading={reading} />
      )}
      {proposed ? null : <Endpoints api={api} selected={endpoint} />}
      <Doors org={org} api={api} endpoint={endpoint} door={door} />
      <div className="mt-8 grid gap-8 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        {proposed ? <div /> : <Requests org={org} api={api} />}
        <RunsOn org={org} api={api} business={business?.name} />
      </div>
      {proposed ? null : (
        <div className="mt-8">
          <Keys org={org} api={api} />
        </div>
      )}
      <Plumbing api={api} endpoint={endpoint} />
    </div>
  );
}

export function SiteApiDetail() {
  const { org, api } = useParams({ from: '/$org/site-apis/$api' });
  const { door = 'data', endpoint } = useSearch({ from: '/$org/site-apis/$api' });
  const query = useSiteApi(org, api);
  if (isNotServed(query.error)) return <Card><NotLive /></Card>;
  if (query.error) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  if (!query.data) {
    return (
      <div className="grid gap-6">
        <Skeleton className="h-28 max-w-2xl rounded-lg" />
        <Skeleton className="h-[640px] rounded-(--radius-card)" />
      </div>
    );
  }
  return <Loaded org={org} api={query.data} door={door} endpointKey={endpoint} />;
}
