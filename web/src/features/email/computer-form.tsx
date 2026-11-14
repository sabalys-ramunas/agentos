/**
 * The page a computer serves for one job: taking a password, straight from the person, with no cloud
 * in between. Drawn here so the owner can judge it. It stands outside the panel, and the box is a
 * drawing with no field behind it: the hosted panel must never own an input a password could enter.
 */
import { useSearch } from '@tanstack/react-router';
import { ArrowRight, Monitor } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Logo } from '@/components/ui/misc';
import { Spinner } from '@/components/ui/spinner';
import { StateGlyph } from '@/components/ui/state-badge';
import { cn } from '@/lib/cn';
import './email.css';

const CHANNEL = 'agentos.email.connect';
type Filled = 'none' | 'app' | 'normal';
type Phase = 'ready' | 'checking' | 'done' | 'expired';

/** Dots in four groups of four: the shape of an app password, so a wrong one looks wrong. */
function DrawnBox({ filled }: { filled: Filled }) {
  const groups = [0, 1, 2, 3];
  return (
    <div role="img" aria-label={filled === 'none' ? 'Drawing of an empty password box' : 'Drawing of a password box with hidden letters in it'} className={cn('flex h-12 items-center gap-3 rounded-lg border px-3', filled === 'normal' ? 'border-(--wait-line)' : filled === 'app' ? 'border-fg' : 'border-line-strong')}>
      {filled === 'none' ? <span className="text-14 text-disabled">Paste the 16 letters here</span> : null}
      {filled === 'app' ? groups.map((group) => (
        <span key={group} className="flex gap-1">
          {groups.map((dot) => <span key={dot} className="spine-rise size-2 rounded-full bg-fg" style={{ animationDelay: `${(group * 4 + dot) * 18}ms` }} />)}
        </span>
      )) : null}
      {filled === 'normal' ? <span className="flex gap-1">{Array.from({ length: 9 }, (_, dot) => <span key={dot} className="size-2 rounded-full bg-fg" />)}</span> : null}
    </div>
  );
}

function Clock({ running }: { running: boolean }) {
  const [left, setLeft] = useState(600);
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setLeft((value) => Math.max(0, value - 1)), 1_000);
    return () => clearInterval(timer);
  }, [running]);
  return <span className="tabular">{Math.floor(left / 60)}:{String(left % 60).padStart(2, '0')}</span>;
}

export function ComputerForm() {
  const search = useSearch({ from: '/computer-form' });
  const address = search.address ?? 'astercoffee.orders@gmail.example';
  const computer = search.computer ?? 'home-mini';
  const [filled, setFilled] = useState<Filled>(search.state === 'filled' ? 'app' : search.state === 'normal' ? 'normal' : 'none');
  const [phase, setPhase] = useState<Phase>(search.state === 'expired' ? 'expired' : search.state === 'done' ? 'done' : 'ready');

  useEffect(() => {
    if (phase !== 'checking') return;
    const timer = setTimeout(() => setPhase('done'), 2_000);
    return () => clearTimeout(timer);
  }, [phase]);

  const connect = () => {
    const channel = new BroadcastChannel(CHANNEL);
    // The panel's mock listens, and adds the mailbox the way the node's own form would.
    channel.postMessage({ kind: 'filled', address, mailbox: search.mailbox, computer });
    channel.close();
    setPhase('checking');
  };

  return (
    <div className="board-surface flex min-h-dvh flex-col">
      <p className="border-b border-board-line bg-bg px-4 py-2 text-center text-13 text-pretty text-muted">
        <strong className="font-medium text-fg">Concept drawing.</strong> In real use this page comes from {computer}, never from agentos Cloud. The box here is only a picture, and takes no typing.
      </p>
      <div className="flex flex-1 flex-col items-center justify-center px-4 py-10">
        <div className="w-full max-w-md">
          <div className="flex h-8 items-center gap-2 rounded-t-lg border border-b-0 border-board-line bg-bg px-3">
            <Monitor aria-hidden="true" className="size-3 shrink-0 text-faint" />
            <span className="mono min-w-0 flex-1 truncate text-12 text-muted">127.0.0.1:7421/mail/connect</span>
            <span className="shrink-0 text-12 text-faint">This computer only</span>
          </div>
          <main className="registered rounded-b-lg border border-board-line bg-bg p-6">
            <p className="flex items-center gap-2">
              <Logo className="size-4" />
              <span className="silk">Served by {computer}</span>
            </p>
            {phase === 'expired' ? (
              <>
                <h1 className="mt-4 text-20 font-semibold text-balance">This form has closed</h1>
                <p className="mt-2 text-14 text-pretty text-muted">It only works for ten minutes, and only once, so a forgotten tab cannot be used later. Go back to the panel and start again. Nothing was saved.</p>
              </>
            ) : phase === 'done' ? (
              <>
                <p className="mt-4"><StateGlyph state="good" size={24} /></p>
                <h1 className="mt-2 text-20 font-semibold text-balance">All done. You can close this tab.</h1>
                <p className="mt-2 text-14 text-pretty text-muted">The password is in this computer's vault. The panel already has the news.</p>
              </>
            ) : (
              <>
                <h1 className="mt-4 text-20 font-semibold text-balance">Paste your app password</h1>
                <p className="mt-1 text-14 break-all text-muted">{address}</p>
                <div className="mt-4"><DrawnBox filled={filled} /></div>
                <p className="mt-2 min-h-10 text-13 text-pretty text-muted">
                  {filled === 'normal' ? 'That looks like a normal password. Gmail wants the 16 letters from Google\'s page, so nothing was sent.' : 'Spaces are fine, they get taken out. From here it goes to Gmail to sign in, then into this computer\'s vault, and nowhere else.'}
                </p>
                <Button variant="primary" size="lg" className="mt-2 w-full" loading={phase === 'checking'} aria-disabled={filled !== 'app'} onClick={() => filled === 'app' && phase === 'ready' && connect()}>
                  {phase === 'checking' ? 'Checking with Gmail' : 'Connect'}
                </Button>
                <p aria-label={`Your password goes from you to ${computer}, then to Gmail`} className="mt-4 flex items-center gap-2 border-t border-line pt-3 text-12 text-muted">
                  <span>You</span>
                  <ArrowRight aria-hidden="true" className="size-3 text-faint" />
                  <span className="mono">{computer}</span>
                  <ArrowRight aria-hidden="true" className="size-3 text-faint" />
                  <span>Gmail</span>
                </p>
                <p className="mt-2 flex items-center justify-between gap-2 text-12 text-faint">
                  <span>{phase === 'checking' ? <span className="inline-flex items-center gap-2"><Spinner className="size-3" />Signing in to read and to send</span> : 'Works once'}</span>
                  <span>Closes in <Clock running={phase === 'ready'} /></span>
                </p>
              </>
            )}
          </main>
          {phase === 'ready' ? (
            <div className="mt-3 flex flex-wrap items-center justify-center gap-2 text-12 text-faint">
              <span>Concept:</span>
              <button type="button" className="link" onClick={() => setFilled('app')}>pretend to paste the 16 letters</button>
              <button type="button" className="link" onClick={() => setFilled('normal')}>pretend to paste a normal password</button>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
