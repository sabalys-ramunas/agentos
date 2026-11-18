/**
 * The proofs of an environment: the screenshots and videos its agent kept to show its work, newest first.
 * Each is a thumbnail with what it shows, when it was taken and the page it is of; a click opens it large.
 * A video shows its GIF preview (or a still frame of it) with its length, and opens a player that seeks.
 * They are kept on the node, so they stay after the machine is gone.
 */
import { Images, Play } from 'lucide-react';
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

/** "0:42", "1:05:09": a video's length as a player shows it. */
export function clipLength(seconds: number | null | undefined): string | null {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return null;
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = String(total % 60).padStart(2, '0');
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${secs}` : `${minutes}:${secs}`;
}

const isVideo = (proof: EnvProof) => proof.kind === 'video';

/** The picture on a card: a screenshot, a video's GIF, or (no GIF) the video's own first frame. */
function ThumbPicture({ proof }: { proof: EnvProof }) {
  if (!isVideo(proof)) return <img src={proof.src} alt="" loading="lazy" width={proof.width} height={proof.height} className="size-full object-cover object-top" />;
  const length = clipLength(proof.duration_s);
  return (
    <>
      {proof.gif_src ? (
        <img src={proof.gif_src} alt="" loading="lazy" width={proof.gif?.width ?? proof.width} height={proof.gif?.height ?? proof.height} className="size-full object-cover object-top" />
      ) : (
        <video src={`${proof.src}#t=0.1`} preload="metadata" muted playsInline aria-hidden="true" className="size-full object-cover object-top" />
      )}
      <span aria-hidden="true" className="absolute inset-0 flex items-center justify-center">
        <span className="flex size-10 items-center justify-center rounded-full bg-inverse/80 text-on-inverse transition-transform duration-(--duration-fast) group-hover:scale-105">
          <Play className="size-4 translate-x-px" />
        </span>
      </span>
      {length ? <span className="absolute right-2 bottom-2 rounded-sm bg-inverse/80 px-1 font-mono text-code-caption leading-4 text-on-inverse tabular">{length}</span> : null}
    </>
  );
}

function Thumb({ proof, now, onOpen }: { proof: EnvProof; now: number; onOpen: () => void }) {
  const page = pageWords(proof.page_url);
  const video = isVideo(proof);
  return (
    <li className="min-w-0">
      <button
        type="button"
        onClick={onOpen}
        aria-label={`${video ? 'Play the video' : 'Open the proof'}: ${proof.title}${video && clipLength(proof.duration_s) ? `, ${clipLength(proof.duration_s)}` : ''}`}
        className="group grid w-full min-w-0 gap-2 rounded-lg text-left outline-offset-2"
      >
        {/* One frame for every thumbnail, the top of the page in it; the large view keeps the real shape. */}
        <span className="relative block aspect-[16/10] overflow-hidden rounded-lg border border-line bg-inset transition-colors duration-(--duration-fast) group-hover:border-line-strong">
          <ThumbPicture proof={proof} />
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

/** "2 screenshots and 1 video". */
function proofsMeta(proofs: EnvProof[]): string {
  const videos = proofs.filter(isVideo).length;
  const shots = proofs.length - videos;
  return [shots ? plural(shots, 'screenshot') : null, videos ? plural(videos, 'video') : null].filter(Boolean).join(' and ');
}

export function ProofsCard({ env, now }: { env: EnvDetail; now: number }) {
  const proofs = env.proofs ?? [];
  const [openId, setOpenId] = useState<string | null>(null);
  const open = proofs.find((proof) => proof.id === openId) ?? null;
  const page = open ? pageWords(open.page_url) : null;
  return (
    <Card className="overflow-hidden">
      <CardHeader title="Proofs" meta={proofs.length ? proofsMeta(proofs) : undefined} />
      {proofs.length ? (
        <ul aria-label="Proofs, newest first" className="grid grid-cols-2 gap-4 p-5 sm:grid-cols-3">
          {proofs.map((proof) => (
            <Thumb key={proof.id} proof={proof} now={now} onOpen={() => setOpenId(proof.id)} />
          ))}
        </ul>
      ) : (
        <p className="flex items-center gap-2 px-5 py-4 text-14 text-muted">
          <Images aria-hidden="true" className="size-4 shrink-0 text-faint" />
          No proofs yet. When the agent keeps a screenshot or a video to show its work, it appears here, and stays after the machine is gone.
        </p>
      )}
      <Dialog
        open={open !== null}
        onOpenChange={(next) => {
          if (!next) setOpenId(null);
        }}
        title={open?.title ?? 'Proof'}
        description={open ? `${dateTime(open.taken_at)}${page ? ` · ${page}` : ''}${open && isVideo(open) && clipLength(open.duration_s) ? ` · ${clipLength(open.duration_s)}` : ''}` : undefined}
        className="sm:max-w-[min(1200px,calc(100vw-32px))]"
      >
        {open ? (
          <div className="overflow-hidden rounded-lg border border-line bg-inset">
            {isVideo(open) ? (
              // the node serves the MP4 with range requests, so the player seeks without loading all of it
              <video
                key={open.id}
                src={open.src}
                poster={open.gif_src}
                controls
                autoPlay
                playsInline
                preload="metadata"
                width={open.width}
                height={open.height}
                aria-label={`Video: ${open.title}`}
                className="block h-auto max-h-[75dvh] w-full bg-terminal"
              >
                <a href={open.src}>Download the video</a>
              </video>
            ) : (
              <img src={open.src} alt={open.title} width={open.width} height={open.height} className="h-auto w-full" />
            )}
          </div>
        ) : null}
      </Dialog>
    </Card>
  );
}
