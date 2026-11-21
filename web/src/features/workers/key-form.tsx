/**
 * The page a computer serves for one job: taking a key for an AI service, straight from the person,
 * with no cloud in between. Drawn here so the owner can judge it. It stands outside the panel, and the
 * box is a drawing with no field behind it: the hosted panel must never own an input a key could enter.
 */
import { useSearch } from '@tanstack/react-router';
import { ArrowRight, Monitor } from 'lucide-react';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Logo } from '@/components/ui/misc';
import { StateGlyph } from '@/components/ui/state-badge';
import { cn } from '@/lib/cn';
import { computerName } from '@/lib/computers';
import { serviceInfo } from './words';

export const KEY_CHANNEL = 'agentos.workers.key';

function DrawnBox({ filled }: { filled: boolean }) {
  return (
    <div role="img" aria-label={filled ? 'Drawing of a key box with hidden letters in it' : 'Drawing of an empty key box'} className={cn('flex h-12 items-center gap-1 rounded-lg border border-line-strong bg-card px-4')}>
      {filled ? Array.from({ length: 22 }, (_, dot) => <span key={dot} className="size-2 rounded-full bg-fg" />) : <span className="text-body text-disabled">Paste the key here</span>}
    </div>
  );
}

export function KeyForm() {
  const search = useSearch({ from: '/computer-form/ai-key' });
  const service = serviceInfo(search.service)?.name ?? 'TypeSafe';
  const id = search.computer ?? 'home-mini';
  const [filled, setFilled] = useState(search.state === 'filled');
  const [done, setDone] = useState(search.state === 'done');
  const connect = () => {
    const channel = new BroadcastChannel(KEY_CHANNEL);
    channel.postMessage({ kind: 'filled' });
    channel.close();
    setDone(true);
  };
  return (
    <div className="board-surface flex min-h-dvh flex-col">
      <p className="border-b border-board-line bg-card px-4 py-2 text-center text-secondary text-pretty text-muted">
        <strong className="font-medium text-fg">Concept drawing.</strong> In real use this page comes from {id}, never from openajar Cloud. The box here is only a picture, and takes no typing.
      </p>
      <div className="flex flex-1 flex-col items-center justify-center px-4 py-10">
        <div className="w-full max-w-md">
          <div className="flex h-8 items-center gap-2 rounded-t-lg border border-b-0 border-board-line bg-card px-3">
            <Monitor aria-hidden="true" className="size-3 shrink-0 text-faint" />
            <span className="mono min-w-0 flex-1 truncate text-caption text-muted">127.0.0.1:7421/brains/add</span>
            <span className="shrink-0 text-caption text-faint">This computer only</span>
          </div>
          <main className="registered rounded-b-lg border border-board-line bg-card p-6">
            <p className="flex items-center gap-2">
              <Logo className="size-4" />
              <span className="silk">Served by {id}</span>
            </p>
            {done ? (
              <>
                <p className="mt-4"><StateGlyph state="good" size={24} /></p>
                <h1 className="mt-2 text-heading text-balance">All done. You can close this tab.</h1>
                <p className="mt-2 text-body text-pretty text-muted">The key is in this computer's vault. The panel already has the news.</p>
              </>
            ) : (
              <>
                <h1 className="mt-4 text-heading text-balance">Paste your {service} key</h1>
                <p className="mt-1 text-body text-muted">It stays on {computerName(id)}.</p>
                <div className="mt-4"><DrawnBox filled={filled} /></div>
                <Button variant="primary" size="lg" className="mt-4 w-full" aria-disabled={!filled} onClick={() => filled && connect()}>
                  Keep it on this computer
                </Button>
                <p aria-label={`Your key goes from you to ${id}, then to ${service}`} className="mt-4 flex items-center gap-2 border-t border-line pt-3 text-caption text-muted">
                  <span>You</span>
                  <ArrowRight aria-hidden="true" className="size-3 text-faint" />
                  <span className="mono">{id}</span>
                  <ArrowRight aria-hidden="true" className="size-3 text-faint" />
                  <span>{service}</span>
                </p>
                <p className="mt-2 text-caption text-faint">Works once. Closes in ten minutes.</p>
              </>
            )}
          </main>
          {!done && !filled ? (
            <div className="mt-3 flex items-center justify-center gap-2 text-caption text-faint">
              <span>Concept:</span>
              <button type="button" className="link" onClick={() => setFilled(true)}>pretend to paste a key</button>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
