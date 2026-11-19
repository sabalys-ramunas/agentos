/**
 * Connect a mailbox, for someone who has never heard of a mail server. A person types an address and
 * watches it get found, in plain words. Each stage is in the URL, so Back works and every state has an
 * address. The panel holds no password field: at the point a password is needed it hands over to the
 * person's own computer, and the route picture beside the steps shows why that is the short way.
 * Concept stage: answered by the mock only.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { ArrowLeft } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useMailDiscover, useProviders } from '@/api/hooks';
import { Card } from '@/components/ui/card';
import { NotLive } from '@/components/ui/states';
import { apiMode } from '@/config';
import { RoutePicture } from './route-picture';
import type { KeyAt } from './route-picture';
import './email.css';
import { endingWords } from './words';
import type { Check, ConnectStage, Ending } from './words';
import { ChecksStep, DoneStep, FailedStep, HandOverStep, WaitingStep } from './connect-hand-over';
import type { Go, OpenedForm, TypedServers } from './connect-steps';
import type { MailComputer, MailDiscovery } from '@/api/types';
import { AddressStep, Disclosure, Finding, FoundStep, PasswordStep, ServerNames, Step } from './connect-steps';

const ENDINGS: readonly string[] = ['wrong-password', 'two-step-off', 'imap-blocked', 'certificate'];
const asEnding = (value: string | undefined) => (value && ENDINGS.includes(value) ? (value as Ending) : null);

/** Concept only: walk to any state without having to fail for real. */
const WALK: { word: string; search: { address: string; stage?: ConnectStage; ending?: string } }[] = [
  { word: 'Gmail, from the start', search: { address: 'astercoffee.orders@gmail.example', stage: 'found' } },
  { word: 'Wrong password', search: { address: 'astercoffee.orders@gmail.example', stage: 'failed', ending: 'wrong-password' } },
  { word: '2-Step Verification off', search: { address: 'astercoffee.orders@gmail.example', stage: 'failed', ending: 'two-step-off' } },
  { word: 'Blocked by an admin', search: { address: 'sales@harbor-supply.example', stage: 'failed', ending: 'imap-blocked' } },
  { word: 'Servers not found', search: { address: 'hello@lindqvist-yachts.example', stage: 'found' } },
  { word: 'Certificate', search: { address: 'post@old-mill-post.example', stage: 'failed', ending: 'certificate' } },
  { word: 'Needs a person to look', search: { address: 'ola@nordlys-post.example', stage: 'found' } },
  { word: 'iCloud, later', search: { address: 'mia@icloud.example', stage: 'found' } },
];

function ConceptWalk({ org }: { org: string }) {
  return (
    <aside aria-label="Concept shortcuts" className="mt-12 border-t border-line pt-4">
      <p className="silk mb-2">Concept only: see another outcome</p>
      <div className="flex flex-wrap gap-1">
        {WALK.map((item) => (
          <Link key={item.word} to="/$org/email/connect" params={{ org }} search={item.search} className="rounded-md px-2 py-1 text-caption text-muted transition-colors duration-(--duration-fast) ease-standard hover:bg-hover hover:text-fg">
            {item.word}
          </Link>
        ))}
      </div>
    </aside>
  );
}

const CAPTION: Partial<Record<ConnectStage, (computer: string, provider: string) => string>> = {
  found: (computer) => `${computer} is looking this up. Only the name after the @ leaves your computer.`,
  password: (computer, provider) => `You make the password at ${provider}. Next it goes to ${computer}, and from there to ${provider}.`,
  'hand-over': (computer, provider) => `It goes from you to ${computer}, then to ${provider}. That is the whole trip.`,
  waiting: (computer) => `Your password goes from you to ${computer} only when you press Connect on the form there.`,
  checks: (computer, provider) => `${computer} signs in to ${provider} twice: once to read, once to send.`,
  done: (computer) => `The password stays on ${computer}. agentos Cloud knows the mailbox is healthy, and that is all it knows.`,
};

const ORDER: ConnectStage[] = ['found', 'password', 'hand-over', 'waiting', 'checks', 'done'];

/** The two checks, one after the other. A failed check is its own stage, reached from the concept's list. */
function useChecks(stage: ConnectStage, go: Go) {
  const [read, setRead] = useState<Check>('waiting');
  const [send, setSend] = useState<Check>('waiting');
  useEffect(() => {
    if (stage !== 'checks') return;
    setRead('running');
    setSend('waiting');
    const first = setTimeout(() => {
      setRead('yes');
      setSend('running');
    }, 1_100);
    const second = setTimeout(() => setSend('yes'), 2_100);
    const last = setTimeout(() => go('done', { replace: true }), 2_900);
    return () => [first, second, last].forEach(clearTimeout);
  }, [stage, go]);
  return { read, send };
}

interface Situation {
  stage: ConnectStage;
  ending: Ending | null;
  found: MailDiscovery | undefined;
  failure: ReturnType<typeof endingWords> | null;
  checks: { read: Check; send: Check };
  computer: string;
  provider: string | null;
}

/** Where the key is in the route picture, how its two checks stand, and the sentence under it. */
function pictureOf({ stage, ending, found, failure, checks, computer, provider }: Situation) {
  const keyAt = keyAtOf(stage, ending);
  const read = stage === 'done' ? 'yes' : stage === 'checks' ? checks.read : (failure?.read ?? 'waiting');
  const send = stage === 'done' ? 'yes' : stage === 'checks' ? checks.send : (failure?.send ?? 'waiting');
  const looking = stage === 'found' && !(found && found.result !== 'found');
  return { keyAt, read, send, caption: failure?.caption ?? captionOf(stage, found, computer, provider), looking };
}

function keyAtOf(stage: ConnectStage, ending: Ending | null): KeyAt {
  if (stage === 'done') return 'kept';
  if (stage === 'checks' || (stage === 'failed' && ending !== 'two-step-off')) return 'computer';
  return stage === 'hand-over' || stage === 'waiting' || stage === 'password' ? 'you' : 'none';
}

function captionOf(stage: ConnectStage, found: MailDiscovery | undefined, computer: string, provider: string | null): string {
  if (found?.result === 'later') return 'Nothing was sent anywhere, and nothing was saved.';
  if (stage === 'found' && found && found.result !== 'found') return 'Your password has not gone anywhere. It waits until the names are right.';
  return CAPTION[stage]?.(computer, provider ?? 'your mail') ?? 'Your password will go from you to your own computer, and from there to your mail. Never through agentos Cloud.';
}

/** A mailbox is connected from the computer that holds its password, so the cloud says so instead of a form. */
export function Connect() {
  const onComputer = useProviders().data?.code === true;
  if (!onComputer) return <Card className="p-4 sm:p-6"><NotLive /></Card>;
  return <ConnectHere />;
}

function ConnectHere() {
  const { org } = useParams({ from: '/$org/email/connect' });
  const search = useSearch({ from: '/$org/email/connect' });
  const navigate = useNavigate({ from: '/$org/email/connect' });
  const discover = useMailDiscover(org);
  const address = search.address ?? '';
  const stage: ConnectStage = address ? (search.stage ?? 'found') : 'address';
  const ending = asEnding(search.ending);
  const found = discover.data?.address === address ? discover.data : undefined;
  const asked = useRef('');
  // The server names a person typed, for the node's form to show and check; the form the panel opened, while it waits.
  const [typed, setTyped] = useState<TypedServers | null>(null);
  const [form, setForm] = useState<OpenedForm | null>(null);
  useEffect(() => {
    setTyped(null);
    setForm(null);
  }, [address]);

  const go = useMemo<Go>(
    () => (next, extra) => void navigate({ search: (old) => ({ ...old, stage: next, ending: extra?.ending, computer: extra?.computer ?? old.computer }), replace: extra?.replace }),
    [navigate],
  );

  useEffect(() => {
    if (!address || asked.current === address) return;
    asked.current = address;
    discover.mutate(address);
  }, [address, discover]);

  // A good answer moves on by itself, after a moment to read "Found Gmail".
  useEffect(() => {
    if (stage !== 'found' || found?.result !== 'found') return;
    const timer = setTimeout(() => go(found.password === 'app_password' ? 'password' : 'hand-over', { replace: true }), 2_100);
    return () => clearTimeout(timer);
  }, [stage, found, go]);

  const checks = useChecks(stage, go);
  const provider = found?.provider ?? null;
  const computer = found?.computers.find((item) => item.id === search.computer) ?? found?.computers[0] ?? { id: 'home-mini', word: '', has_screen: true };
  const failure = stage === 'failed' && ending && found ? endingWords(ending, provider ?? 'Your mail company', found.domain, computer.id) : null;
  const picture = pictureOf({ stage, ending, found, failure, checks, computer: computer.id, provider });

  return (
    <div className="pb-16">
      <Link to="/$org/email" params={{ org }} className="mb-6 inline-flex items-center gap-1 text-secondary text-muted hover:text-fg">
        <ArrowLeft className="size-3" /> Email
      </Link>
      <div className="grid gap-10 lg:grid-cols-[minmax(0,1fr)_26rem] lg:gap-16">
        <div className="min-w-0">
          {stage === 'address' ? (
            <AddressStep initial={address} onFind={(value) => void navigate({ search: { address: value, stage: 'found' } })} />
          ) : stage === 'done' ? (
            <DoneStep org={org} address={address} provider={provider ?? 'your mail company'} computer={computer.id} />
          ) : (
            <>
              <h1 className="mb-6 text-page-title-compact break-all">Connecting {address}</h1>
              <Spine
                org={org}
                address={address}
                stage={stage}
                ending={ending}
                found={found}
                discoverFailed={discover.isError}
                computer={computer}
                checks={checks}
                typed={typed}
                form={form}
                onTyped={setTyped}
                onOpened={setForm}
                go={go}
                onAgain={() => void navigate({ search: {} })}
              />
            </>
          )}
          {apiMode === 'mock' ? <ConceptWalk org={org} /> : null}
        </div>
        <aside aria-label="Where your password goes" className="lg:sticky lg:top-6 lg:self-start">
          <div className="picture-stage registered rounded-(--radius-card) border border-line p-4">
            <p className="silk mb-3">Where your password goes</p>
            <RoutePicture computer={computer.id} provider={provider} site={found?.site ?? null} looking={picture.looking} keyAt={picture.keyAt} read={picture.read} send={picture.send} />
          </div>
          <p aria-live="polite" className="mt-3 text-body text-pretty text-muted">{picture.caption}</p>
        </aside>
      </div>
    </div>
  );
}

interface SpineProps {
  org: string;
  address: string;
  stage: ConnectStage;
  ending: Ending | null;
  found: MailDiscovery | undefined;
  discoverFailed: boolean;
  computer: MailComputer;
  checks: { read: Check; send: Check };
  typed: TypedServers | null;
  form: OpenedForm | null;
  onTyped: (typed: TypedServers | null) => void;
  onOpened: (form: OpenedForm | null) => void;
  go: Go;
  onAgain: () => void;
}

/** The steps down the spine, each folded, open or still to come. */
function Spine(props: SpineProps) {
  const { address, stage, ending, found, discoverFailed, computer, go } = props;
  const provider = found?.provider ?? null;
  const needsAppPassword = found?.password === 'app_password';
  // A failure belongs to the step it happened in: 2-Step Verification comes up while making the password.
  const position = ORDER.indexOf(stage === 'failed' ? (ending === 'two-step-off' ? 'password' : 'checks') : stage);
  const stateOf = (from: ConnectStage, to = from): 'done' | 'now' | 'later' => (position > ORDER.indexOf(to) ? 'done' : position >= ORDER.indexOf(from) ? 'now' : 'later');
  const later = found?.result === 'later';
  let number = 1;
  return (
    <ol className="spine">
      <Step
        number={number++}
        title={found?.result === 'found' && stage !== 'found' ? `Found ${provider}` : 'Finding your mail'}
        state={stateOf('found')}
        summary={found?.incoming ? <Disclosure label="Show the server names"><ServerNames found={found} /></Disclosure> : undefined}
      >
        <Finding domain={address.split('@')[1] ?? ''} found={found} failed={discoverFailed} />
        {found ? (
          <FoundStep
            found={found}
            onHandOver={(names) => {
              props.onTyped(names ?? null);
              go('hand-over');
            }}
            onAgain={props.onAgain}
          />
        ) : null}
      </Step>
      {later ? null : needsAppPassword || !found ? (
        <Step number={number++} title={`${provider ?? 'Your mail'} needs an app password`} state={stateOf('password')}>
          {stage === 'failed' && ending === 'two-step-off' && found ? <FailedStep ending={ending} found={found} computer={computer.id} go={go} /> : <PasswordStep provider={provider ?? 'Gmail'} go={go} />}
        </Step>
      ) : null}
      {later ? null : <LastSteps {...props} first={number} stateOf={stateOf} />}
    </ol>
  );
}

/** Handing the password to the computer, then the two checks. */
function LastSteps({ org, stage, ending, found, computer, checks, typed, form, onOpened, go, first, stateOf }: SpineProps & { first: number; stateOf: (from: ConnectStage, to?: ConnectStage) => 'done' | 'now' | 'later' }) {
  const provider = found?.provider ?? null;
  return (
    <>
      <Step number={first} title="Give it to your computer" state={stateOf('hand-over', 'waiting')} summary={`on ${computer.id}`}>
        {found && stage === 'hand-over' ? <HandOverStep org={org} found={found} computer={computer} typed={typed} go={go} onOpened={onOpened} /> : null}
        {found && stage === 'waiting' ? <WaitingStep org={org} found={found} computer={computer} typed={typed} form={form} go={go} onOpened={onOpened} /> : null}
      </Step>
      <Step number={first + 1} title="Two quick checks" state={stateOf('checks')}>
        {stage === 'failed' && ending && found ? <FailedStep ending={ending} found={found} computer={computer.id} go={go} /> : <ChecksStep provider={provider ?? 'your mail company'} read={checks.read} send={checks.send} />}
      </Step>
    </>
  );
}
