/**
 * Adding an AI service (spec-16, settings-ai-add.png). The hosted panel holds no secret, ever: no field, no type,
 * no handler. On one page it asks who should think (radio cards) and which computer keeps them (a select), then
 * hands over to that computer, the way Email does.
 * The real command waits behind Technical details, unless a computer without a screen needs it. Concept
 * stage: answered by the mock only.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { ArrowUpRight, BrainCircuit, ChevronDown, ChevronLeft, Cpu, ExternalLink, KeyRound, LogIn, Waypoints, Zap } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { useBrains, useHandover, useHandoverFilled, useNodes, useProviders, useStartHandover } from '@/api/hooks';
import type { FleetNode } from '@/api/types';
import { usePageCrumb } from '@/components/shell/crumb';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Choice, ChoiceGroup } from '@/components/ui/choice';
import { Field } from '@/components/ui/field';
import { Select } from '@/components/ui/select';
import { AppTile } from '@/components/ui/tile';
import type { TileTone } from '@/components/ui/tile';
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

const LOOK: Record<string, { tone: TileTone; icon: LucideIcon }> = {
  typesafe: { tone: 'blue', icon: Zap },
  openrouter: { tone: 'amber', icon: Waypoints },
  'claude-max': { tone: 'violet', icon: BrainCircuit },
  'ollama-local': { tone: 'teal', icon: Cpu },
};

/** Who should think: one card per service, as radios, with a line when it is already on a computer. */
function ServiceChoice({ value, onChange, added }: { value: string; onChange: (id: string) => void; added: Set<string> }) {
  return (
    <ChoiceGroup<string> label="Who should think for you" value={value} onValueChange={onChange} columns={2}>
      {SERVICES.map((service) => {
        const look = LOOK[service.id] ?? { tone: 'ink' as const, icon: HOW[service.how].icon };
        return (
          <Choice
            key={service.id}
            value={service.id}
            icon={<AppTile tone={look.tone} icon={look.icon} />}
            title={service.name}
            badge={added.has(service.id) ? <Badge tone="ok" title="Already on a computer. You can add it to another.">On a computer</Badge> : null}
            description={<span className="text-pretty">{service.sentence}</span>}
          />
        );
      })}
    </ChoiceGroup>
  );
}

/** Which computer keeps it, as a select. One that is offline stays listed, and says so. */
function ComputerChoice({ computers, value, onChange, hint }: { computers: FleetNode[]; value: string; onChange: (id: string) => void; hint: string }) {
  return (
    <Field label="On which computer" hint={hint} className="max-w-md">
      <Select
        value={value}
        onValueChange={onChange}
        options={computers.map((computer) => ({ value: computer.id, label: computerName(computer.id), hint: computer.state === 'down' ? 'Offline' : undefined, disabled: computer.state === 'down' }))}
      />
    </Field>
  );
}

function Disclosure({ label, open: initial, children }: { label: string; open: boolean; children: React.ReactNode }) {
  const [open, setOpen] = useState(initial);
  const shown = open || initial;
  return (
    <div>
      <button type="button" aria-expanded={shown} onClick={() => setOpen(!open)} className="inline-flex items-center gap-1 text-secondary text-muted hover:text-fg">
        {label}
        <ChevronDown aria-hidden="true" className={cn('size-3 transition-transform duration-(--duration-fast) ease-standard', shown && 'rotate-180')} />
      </button>
      {shown ? <div className="mt-2 grid gap-2 text-secondary text-pretty text-muted">{children}</div> : null}
    </div>
  );
}

interface HandOverProps {
  org: string;
  service: ServiceInfo;
  computers: FleetNode[];
  computer: string;
  added: Set<string>;
  onService: (id: string) => void;
  onComputer: (id: string) => void;
}

/**
 * One page (settings-ai-add.png): who should think as radio cards and which computer as a select, then Continue on
 * the computer, which opens the form it serves. A computer with no screen shows the one line for its terminal
 * instead. Once handed over, the route picture says where the key is, through waiting, checking, ready or refused.
 */
function HandOver({ org, service, computers, computer, added, onService, onComputer }: HandOverProps) {
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
  const away = computers.find((item) => item.id === computer)?.state === 'down';

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
  const again = (
    <Button variant="ghost" icon={<ChevronLeft className="size-4" />} onClick={() => setId(undefined)}>
      Choose a different service
    </Button>
  );

  if (stage === 'done') {
    return (
      <Stage picture={picture}>
        <StateGlyph state="good" size={24} />
        <h3 className="mt-3 text-heading text-balance">{service.name} is ready on {name}</h3>
        <p className="mt-1 text-body text-pretty text-muted">It answered a small test question. The {thing} stays on {name}, and openajar Cloud only heard that it worked.</p>
        <div className="mt-5 flex flex-wrap gap-2">
          <Button asChild variant="primary"><Link to="/$org/settings/workers" params={{ org }}>Go to Workers</Link></Button>
          <Button asChild><Link to="/$org/settings/brains" params={{ org }}>See AI models</Link></Button>
        </div>
      </Stage>
    );
  }
  if (stage === 'failed') {
    return (
      <Stage picture={picture}>
        <StateGlyph state="stuck" size={24} />
        <h3 className="mt-3 text-heading text-balance">{service.name} did not accept that {thing}</h3>
        <p className="mt-1 text-body text-pretty text-muted">Nothing was saved, and the {thing} that was tried is gone from {name}. Copy a new one from your {service.name} account and try again.</p>
        <div className="mt-5 flex flex-wrap gap-2">
          <Button variant="primary" onClick={() => setId(undefined)}>Try again</Button>
        </div>
      </Stage>
    );
  }
  if (stage === 'waiting' || stage === 'checking') {
    return (
      <Stage picture={picture}>
        <p className="flex items-center gap-2 text-secondary text-muted"><Spinner className="size-3" />{stage === 'checking' ? `Asking ${service.name} a small test question` : `Waiting for you on ${name}`}</p>
        <h3 className="mt-2 text-heading text-balance">{stage === 'checking' ? `${name} has it, and is checking` : `Paste the ${thing} on ${name}`}</h3>
        <p className="mt-1 text-body text-pretty text-muted">{stage === 'checking' ? 'Nothing is saved until the answer comes back.' : 'This page moves on by itself. You can keep it open, or come back to it.'}</p>
        <div className="mt-5 flex flex-wrap gap-2">
          {stage === 'waiting' && screen && handover.data?.form_url ? (
            <Button asChild>
              <a href={handover.data.form_url} target="_blank" rel="noreferrer">Open the form again<ArrowUpRight aria-hidden="true" className="size-4" /></a>
            </Button>
          ) : null}
          {stage === 'waiting' ? again : null}
        </div>
      </Stage>
    );
  }

  const how = service.how === 'key' ? `The ${thing} is pasted on that computer, never here.` : service.how === 'sign-in' ? 'You sign in on that computer, never here.' : 'It runs on that computer, so there is nothing to paste.';
  const hint = away ? `${name} is offline, so it cannot take this now. Choose another.` : screen ? how : `${how} It has no screen, so its terminal is the way in.`;
  return (
    <div className="grid gap-8">
      <ServiceChoice value={service.id} onChange={onService} added={added} />
      <div className="grid gap-4">
        <ComputerChoice computers={computers} value={computer} onChange={onComputer} hint={hint} />
        {service.keysAt ? (
          <a href={service.keysAt} target="_blank" rel="noreferrer" className="link inline-flex items-center gap-1 justify-self-start text-secondary">
            Get a key from {service.name}
            <ArrowUpRight aria-hidden="true" className="size-3" />
          </a>
        ) : null}
        {!screen && !away ? (
          <div className="grid max-w-2xl gap-2">
            <p className="text-body text-pretty">{name} asks for the {thing} with the letters hidden. Run this one line in its terminal:</p>
            <CodeBlock code={command} label="One line for its terminal" />
          </div>
        ) : null}
        {start.isError ? <InlineError>{name} did not answer, so nothing was started. Choose another computer, or wait until it is back.</InlineError> : null}
      </div>
      <div className="grid gap-4 border-t border-line pt-6">
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Button asChild variant="ghost">
            <Link to="/$org/settings/brains" params={{ org }}>Cancel</Link>
          </Button>
          {screen ? (
            <Button variant="primary" loading={start.isPending} disabled={away} onClick={begin} icon={<ExternalLink className="size-4" />}>
              Continue on the computer
            </Button>
          ) : (
            <Button variant="primary" loading={start.isPending} disabled={away} onClick={begin}>
              I ran it
            </Button>
          )}
        </div>
        {screen ? (
          <div className="grid gap-2">
            <Disclosure label={`Not sitting at ${name} right now?`} open={false}>
              The form only opens on {name} itself, so the {thing} never crosses the internet. Open this same page there and you carry on from this step.
            </Disclosure>
            <Disclosure label="Technical details" open={technical}>
              <CodeBlock code={command} label="The same thing, in its terminal" />
            </Disclosure>
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** A step after the hand-over: what is happening on the left, the route picture on the right. */
function Stage({ picture, children }: { picture: ReactNode; children: ReactNode }) {
  return (
    <Card className="grid gap-6 p-6 lg:grid-cols-[minmax(0,1fr)_340px] lg:items-center">
      <div>{children}</div>
      {picture}
    </Card>
  );
}

export function AddService() {
  const { org } = useParams({ from: '/$org/settings/brains/add' });
  const search = useSearch({ from: '/$org/settings/brains/add' });
  const navigate = useNavigate({ from: '/$org/settings/brains/add' });
  const nodes = useNodes(org);
  const brains = useBrains(org);
  usePageCrumb('Add an AI service', { mono: false });
  // The first service is chosen to begin with, as drawn; the address keeps the choice.
  const service = serviceInfo(search.service) ?? SERVICES[0]!;
  const added = useMemo(() => new Set((brains.data ?? []).map((brain) => brain.name)), [brains.data]);
  const set = (next: { service?: string; computer?: string }) => void navigate({ search: (previous) => ({ ...previous, ...next }), replace: true, resetScroll: false });
  return (
    <>
      <SettingsHeader title="Add an AI service" description="Who should think for you? It lives on one of your computers, and openajar never holds a key." />
      <QueryView card query={nodes} skeleton={<div className="grid gap-4"><Skeleton className="h-64 rounded-xl" /><Skeleton className="h-10 w-96 max-w-full" /></div>}>
        {(computers) => {
          if (!computers.length) {
            return (
              <Card>
                <EmptyState icon={<KeyRound />} title="An AI service needs a computer" description="It lives on a computer of yours, never in openajar Cloud. Add a computer first." action={<Button asChild variant="primary"><Link to="/$org/nodes" params={{ org }} search={{ add: true }}>Add a computer</Link></Button>} />
              </Card>
            );
          }
          const fallback = computers.find((computer) => computer.state !== 'down') ?? computers[0];
          const computer = computers.find((item) => item.id === search.computer) ?? fallback;
          return computer ? (
            <HandOver
              key={`${service.id}/${computer.id}`}
              org={org}
              service={service}
              computers={computers}
              computer={computer.id}
              added={added}
              onService={(id) => set({ service: id })}
              onComputer={(id) => set({ computer: id })}
            />
          ) : null;
        }}
      </QueryView>
    </>
  );
}
