/**
 * Adding an AI service. The hosted panel holds no secret, ever: no field, no type, no handler. It asks
 * who should think and which computer keeps them, then hands over to that computer, the way Email does.
 * The real command waits behind Technical details, unless a computer without a screen needs it. Concept
 * stage: answered by the mock only.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { ArrowRight, ArrowUpRight, ChevronDown, ChevronLeft, Cpu, KeyRound, LogIn } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { useBrains, useHandover, useHandoverFilled, useNodes, useProviders, useStartHandover } from '@/api/hooks';
import type { FleetNode } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { CodeBlock } from '@/components/ui/misc';
import { Spinner } from '@/components/ui/spinner';
import { StateGlyph } from '@/components/ui/state-badge';
import { EmptyState, InlineError, QueryView, Skeleton } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { computerName } from '@/lib/computers';
import { useTechnical } from '@/lib/technical';
import { SettingsHeader } from '@/features/settings/page';
import { KeyRoute } from './key-route';
import { KEY_CHANNEL } from './key-form';
import { addCommand, SERVICES, serviceInfo } from './words';
import type { ServiceInfo } from './words';

const HOW = {
  key: { icon: KeyRound, word: 'Uses a key', thing: 'key' },
  'sign-in': { icon: LogIn, word: 'You sign in', thing: 'sign-in' },
  local: { icon: Cpu, word: 'Nothing to paste', thing: 'model' },
} as const;

/** A computer with no screen of its own. The cloud says so on a real one; here the name does. */
const hasScreen = (id: string) => /mini|mac|imac/.test(id);

function ServiceChoice({ org, chosen, added, computer }: { org: string; chosen?: string; added: Set<string>; computer?: string }) {
  return (
    <Card>
      <ul className="divide-y divide-line">
        {SERVICES.map((service) => {
          const Icon = HOW[service.how].icon;
          const here = added.has(service.id);
          return (
            <li key={service.id}>
              <Link
                to="/$org/settings/brains/add"
                params={{ org }}
                search={{ service: service.id, computer }}
                className={cn('flex items-start gap-3 px-4 py-4 transition-colors duration-(--duration-fast) ease-standard hover:bg-fill-1 sm:px-6', chosen === service.id && 'bg-fill-1')}
              >
                <span className="grid h-5 shrink-0 place-items-center text-faint"><Icon aria-hidden="true" className="size-4" /></span>
                <span className="min-w-0 flex-1">
                  <span className="block text-14 font-medium">{service.name}</span>
                  <span className="block text-13 text-pretty text-muted">{service.sentence}</span>
                  <span className="block text-13 text-faint">{here ? 'Already on a computer. Add it to another.' : HOW[service.how].word}</span>
                </span>
                <span className="grid h-5 shrink-0 place-items-center text-faint"><ArrowRight aria-hidden="true" className="size-4" /></span>
              </Link>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

/** Each computer in words. One that is offline stays listed, and says why it cannot be chosen. */
function ComputerChoice({ computers, value, onChange }: { computers: FleetNode[]; value: string; onChange: (id: string) => void }) {
  return (
    <div role="radiogroup" aria-label="Which computer keeps it" className="grid gap-2">
      {computers.map((computer) => {
        const away = computer.state === 'down';
        const selected = computer.id === value;
        return (
          <button
            key={computer.id}
            type="button"
            role="radio"
            aria-checked={selected}
            aria-disabled={away || undefined}
            onClick={() => !away && onChange(computer.id)}
            className={cn(
              'flex min-h-12 items-center gap-3 rounded-lg border px-4 py-2 text-left transition-colors duration-(--duration-fast) ease-standard',
              selected ? 'border-fg' : 'border-line hover:border-line-strong',
              away && 'cursor-not-allowed',
            )}
          >
            <span className={cn('grid size-4 shrink-0 place-items-center rounded-full border', selected ? 'border-fg' : 'border-control')}>{selected ? <span className="size-2 rounded-full bg-fg" /> : null}</span>
            <span className="min-w-0 flex-1">
              <span className="block text-14 font-medium">{computerName(computer.id)}</span>
              <span className="block text-13 text-muted">{away ? 'Offline, so it cannot take this now' : hasScreen(computer.id) ? 'Has a screen' : 'No screen, so it uses its terminal'}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

function Disclosure({ label, open: initial, children }: { label: string; open: boolean; children: React.ReactNode }) {
  const [open, setOpen] = useState(initial);
  const shown = open || initial;
  return (
    <div>
      <button type="button" aria-expanded={shown} onClick={() => setOpen(!open)} className="inline-flex items-center gap-1 text-13 text-muted hover:text-fg">
        {label}
        <ChevronDown aria-hidden="true" className={cn('size-3 transition-transform duration-(--duration-fast) ease-standard', shown && 'rotate-180')} />
      </button>
      {shown ? <div className="mt-2 grid gap-2 text-13 text-pretty text-muted">{children}</div> : null}
    </div>
  );
}

function HandOver({ org, service, computers, computer, onComputer }: { org: string; service: ServiceInfo; computers: FleetNode[]; computer: string; onComputer: (id: string) => void }) {
  const start = useStartHandover(org);
  const [technical] = useTechnical();
  const [id, setId] = useState<string>();
  const handover = useHandover(org, id);
  const { mutate: filled } = useHandoverFilled(org);
  const stage = handover.data?.stage ?? null;
  const name = computerName(computer);
  // On a computer this page already is its screen, and the computer makes the form for this trip.
  const here = useProviders().data?.code === true;
  const screen = here || hasScreen(computer);
  const command = addCommand(service);
  const thing = HOW[service.how].thing;

  // The form on the computer says it was used. In real use the computer tells the cloud.
  useEffect(() => {
    if (!id) return;
    const channel = new BroadcastChannel(KEY_CHANNEL);
    channel.onmessage = () => filled(id);
    return () => channel.close();
  }, [filled, id]);

  const begin = () => {
    // Opened now, in the click, because a tab opened after the answer comes back is blocked. The form's
    // own address comes from the computer, once the handover has started: there is no address before that.
    const tab = screen ? window.open('', '_blank') : null;
    start.mutate(
      { service: service.id, computer },
      {
        onSuccess: (made) => {
          setId(made.id);
          if (tab && made.form_url) tab.location.href = made.form_url;
          else tab?.close();
        },
        onError: () => tab?.close(),
      },
    );
  };

  const picture = (
    <KeyRoute
      computer={name.replace(' (home)', '')}
      service={service.name.replace('Your ', '')}
      thing={thing}
      keyAt={stage === 'done' ? 'kept' : stage === 'checking' || stage === 'failed' ? 'computer' : 'you'}
      checking={stage === 'checking'}
      refused={stage === 'failed'}
    />
  );

  if (stage === 'done') {
    return (
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_380px] lg:items-start">
        <div>
          <StateGlyph state="good" size={24} />
          <h3 className="mt-2 text-20 font-semibold text-balance">{service.name} is ready on {name}</h3>
          <p className="mt-1 text-14 text-pretty text-muted">It answered a small test question. The {thing} stays on {name}, and agentos Cloud only heard that it worked.</p>
          <div className="mt-4 flex flex-wrap gap-2">
            <Button asChild variant="primary"><Link to="/$org/settings/workers" params={{ org }}>Go to Workers</Link></Button>
            <Button asChild><Link to="/$org/settings/brains" params={{ org }}>See AI models</Link></Button>
          </div>
        </div>
        {picture}
      </div>
    );
  }
  if (stage === 'failed') {
    return (
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_380px] lg:items-start">
        <div>
          <StateGlyph state="stuck" size={24} />
          <h3 className="mt-2 text-20 font-semibold text-balance">{service.name} did not accept that {thing}</h3>
          <p className="mt-1 text-14 text-pretty text-muted">Nothing was saved, and the {thing} that was tried is gone from {name}. Copy a new one from your {service.name} account and try again.</p>
          <Button variant="primary" className="mt-4" onClick={() => { setId(undefined); }}>Try again</Button>
        </div>
        {picture}
      </div>
    );
  }
  if (stage === 'waiting' || stage === 'checking') {
    return (
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_380px] lg:items-start">
        <div>
          <p className="flex items-center gap-2 text-13 text-muted"><Spinner className="size-3" />{stage === 'checking' ? `Asking ${service.name} a small test question` : `Waiting for you on ${name}`}</p>
          <h3 className="mt-2 text-20 font-semibold text-balance">{stage === 'checking' ? `${name} has it, and is checking` : `Paste the ${thing} on ${name}`}</h3>
          <p className="mt-1 text-14 text-pretty text-muted">{stage === 'checking' ? 'Nothing is saved until the answer comes back.' : 'This page moves on by itself. You can keep it open, or come back to it.'}</p>
          {stage === 'waiting' && screen && handover.data?.form_url ? (
            <Button asChild className="mt-4">
              <a href={handover.data.form_url} target="_blank" rel="noreferrer">Open the form again<ArrowUpRight aria-hidden="true" className="size-4" /></a>
            </Button>
          ) : null}
        </div>
        {picture}
      </div>
    );
  }

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_380px] lg:items-start">
      <div className="grid gap-4">
        <div>
          <h3 className="text-16 font-semibold">Which computer keeps it?</h3>
          <p className="mt-1 text-14 text-pretty text-muted">
            {service.how === 'key' ? `Your ${service.name} key goes straight to this computer, on a small page it makes for this one job. It never passes through agentos Cloud.` : service.how === 'sign-in' ? 'You sign in on this computer, so the sign-in never passes through agentos Cloud.' : 'It runs on this computer, so there is nothing to paste and nothing leaves it.'}
          </p>
        </div>
        <ComputerChoice computers={computers} value={computer} onChange={onComputer} />
        {service.keysAt ? (
          <a href={service.keysAt} target="_blank" rel="noreferrer" className="link inline-flex items-center gap-1 justify-self-start text-13">
            Get a key from {service.name}
            <ArrowUpRight aria-hidden="true" className="size-3" />
          </a>
        ) : service.how === 'key' ? (
          <p className="text-13 text-faint">Have your key from your {service.name} account ready.</p>
        ) : null}
        {screen ? (
          <div className="flex flex-wrap items-center gap-3">
            <Button variant="primary" size="lg" loading={start.isPending} onClick={begin}>
              Open the form on {computerName(computer)}
              <ArrowUpRight aria-hidden="true" className="size-4" />
            </Button>
          </div>
        ) : (
          <div className="grid gap-2">
            <p className="text-14 text-pretty">{name} has no screen, so its terminal is the way in. It asks for the {thing} with the letters hidden.</p>
            <CodeBlock code={command} label="One line for its terminal" />
            <Button className="justify-self-start" onClick={begin}>I ran it</Button>
          </div>
        )}
        {start.isError && here ? <InlineError>{computerName(computer)} is offline, so nothing was started. Choose another computer, or wait until it is back.</InlineError> : null}
        {screen ? (
          <>
            <Disclosure label={`Not sitting at ${name} right now?`} open={false}>
              The form only opens on {name} itself, so the {thing} never crosses the internet. Open this same page there and you carry on from this step.
            </Disclosure>
            <Disclosure label="Technical details" open={technical}>
              <CodeBlock code={command} label="The same thing, in its terminal" />
            </Disclosure>
          </>
        ) : null}
      </div>
      {picture}
    </div>
  );
}

export function AddService() {
  const { org } = useParams({ from: '/$org/settings/brains/add' });
  const search = useSearch({ from: '/$org/settings/brains/add' });
  const navigate = useNavigate({ from: '/$org/settings/brains/add' });
  const nodes = useNodes(org);
  const brains = useBrains(org);
  const service = serviceInfo(search.service);
  const added = useMemo(() => new Set((brains.data ?? []).map((brain) => brain.name)), [brains.data]);
  return (
    <>
      <Link to="/$org/settings/brains" params={{ org }} className="mb-4 inline-flex items-center gap-1 text-13 text-faint hover:text-fg">
        <ChevronLeft aria-hidden="true" className="size-4" />
        AI models
      </Link>
      <SettingsHeader title="Add an AI service" description={service ? `${service.name}. ${service.sentence}` : 'Who should think for you? It lives on one of your computers, and agentos never holds a key.'} />
      <QueryView card query={nodes} skeleton={<div className="grid gap-4"><Skeleton className="h-64 rounded-xl" /></div>}>
        {(computers) => {
          if (!computers.length) {
            return (
              <Card>
                <EmptyState icon={<KeyRound />} title="An AI service needs a computer" description="It lives on a computer of yours, never in agentos Cloud. Add a computer first." action={<Button asChild variant="primary"><Link to="/$org/nodes" params={{ org }} search={{ add: true }}>Add a computer</Link></Button>} />
              </Card>
            );
          }
          if (!service) return <ServiceChoice org={org} added={added} computer={search.computer} />;
          const fallback = computers.find((computer) => computer.state !== 'down') ?? computers[0];
          const computer = computers.find((item) => item.id === search.computer) ?? fallback;
          return (
            <>
              {computer ? (
                <HandOver
                  key={`${service.id}/${computer.id}`}
                  org={org}
                  service={service}
                  computers={computers}
                  computer={computer.id}
                  onComputer={(id) => void navigate({ search: (previous) => ({ ...previous, computer: id }), replace: true, resetScroll: false })}
                />
              ) : null}
              <p className="mt-8 border-t border-line pt-4">
                <Link to="/$org/settings/brains/add" params={{ org }} search={{ computer: search.computer }} className="inline-flex items-center gap-1 text-13 text-muted hover:text-fg">
                  <ChevronLeft aria-hidden="true" className="size-3" />
                  Choose a different service
                </Link>
              </p>
            </>
          );
        }}
      </QueryView>
    </>
  );
}
