/**
 * The proofs of an environment: the screenshots its agent kept to show its work, newest first. Each is a
 * thumbnail with what it shows, when it was taken and the page it is of; a click opens it large. They are
 * kept on the node, so they stay after the machine is gone.
 */
import { Images } from 'lucide-react';
import { useState } from 'react';
import type { EnvDetail, EnvProof } from '@/api/types';
import { Card, CardHeader } from '@/components/ui/card';
import { Dialog } from '@/components/ui/dialog';
import { dateTime, plural, timeAgo } from '@/lib/format';

/** "localhost:3000/offer": the page without its scheme, as a person reads an address. */
export function pageWords(url: string | null): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    const path = parsed.pathname === '/' ? '' : parsed.pathname;
    return `${parsed.host}${path}`;
  } catch {
    return url;
  }
}

function Thumb({ proof, now, onOpen }: { proof: EnvProof; now: number; onOpen: () => void }) {
  const page = pageWords(proof.page_url);
  return (
    <li className="min-w-0">
      <button
        type="button"
        onClick={onOpen}
        aria-label={`Open the proof: ${proof.title}`}
        className="group grid w-full min-w-0 gap-2 rounded-lg text-left outline-offset-2"
      >
        {/* One frame for every thumbnail, the top of the page in it; the large view keeps the real shape. */}
        <span className="block aspect-[16/10] overflow-hidden rounded-lg border border-line bg-inset transition-colors duration-(--duration-fast) group-hover:border-line-strong">
          <img src={proof.src} alt="" loading="lazy" width={proof.width} height={proof.height} className="size-full object-cover object-top" />
        </span>
        <span className="grid min-w-0 gap-px">
          <span className="truncate text-14 font-medium text-fg" title={proof.title}>
            {proof.title}
          </span>
          <span className="truncate text-secondary text-muted">
            <time dateTime={proof.taken_at} title={dateTime(proof.taken_at)}>
              {timeAgo(proof.taken_at, now)}
            </time>
            {page ? <span title={proof.page_url ?? undefined}> · {page}</span> : null}
          </span>
        </span>
      </button>
    </li>
  );
}

export function ProofsCard({ env, now }: { env: EnvDetail; now: number }) {
  const proofs = env.proofs ?? [];
  const [openId, setOpenId] = useState<string | null>(null);
  const open = proofs.find((proof) => proof.id === openId) ?? null;
  const page = open ? pageWords(open.page_url) : null;
  return (
    <Card className="overflow-hidden">
      <CardHeader title="Proofs" meta={proofs.length ? plural(proofs.length, 'screenshot') : undefined} />
      {proofs.length ? (
        <ul aria-label="Proofs, newest first" className="grid grid-cols-2 gap-4 p-5 sm:grid-cols-3">
          {proofs.map((proof) => (
            <Thumb key={proof.id} proof={proof} now={now} onOpen={() => setOpenId(proof.id)} />
          ))}
        </ul>
      ) : (
        <p className="flex items-center gap-2 px-5 py-4 text-14 text-muted">
          <Images aria-hidden="true" className="size-4 shrink-0 text-faint" />
          No proofs yet. When the agent keeps a screenshot to show its work, it appears here, and stays after the machine is gone.
        </p>
      )}
      <Dialog
        open={open !== null}
        onOpenChange={(next) => {
          if (!next) setOpenId(null);
        }}
        title={open?.title ?? 'Proof'}
        description={open ? `${dateTime(open.taken_at)}${page ? ` · ${page}` : ''}` : undefined}
        className="sm:max-w-[min(1200px,calc(100vw-32px))]"
      >
        {open ? (
          <div className="overflow-hidden rounded-lg border border-line bg-inset">
            <img src={open.src} alt={open.title} width={open.width} height={open.height} className="h-auto w-full" />
          </div>
        ) : null}
      </Dialog>
    </Card>
  );
}
