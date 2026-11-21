/**
 * Closing an environment, in up to three steps. Close asks first. The node refuses with 409 `unsaved_work`
 * when files are changed and not committed or commits are not pushed; then the page lists them and offers
 * Discard and close, which asks once more, because the work goes with the machine.
 */
import { FileDiff, GitCommitHorizontal } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { useCloseEnvironment } from '@/api/hooks';
import { ApiError } from '@/api/http';
import type { EnvDetail, EnvUnsaved } from '@/api/types';
import { Button } from '@/components/ui/button';
import { ConfirmDialog, Dialog } from '@/components/ui/dialog';
import { cn } from '@/lib/cn';
import { plural } from '@/lib/format';
import { euro } from './words';

type Step = 'confirm' | 'unsaved' | 'discard' | null;

const strings = (value: unknown): string[] | null => (Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : null);

/**
 * The unsaved work the node named in its refusal. It may sit beside the error, inside it, or under
 * `unsaved`; when the refusal names none, the environment's own `unsaved` is used.
 */
export function unsavedFrom(error: unknown, fallback: EnvUnsaved | null): EnvUnsaved {
  const body = error instanceof ApiError ? error.body : {};
  const nested = (body.unsaved && typeof body.unsaved === 'object' ? body.unsaved : body.details && typeof body.details === 'object' ? body.details : {}) as Record<string, unknown>;
  const dirty = strings(body.dirty_files) ?? strings(nested.dirty_files);
  const unpushed = strings(body.unpushed) ?? strings(nested.unpushed);
  const stashes = strings(body.stashes) ?? strings(nested.stashes);
  if (dirty || unpushed || stashes) return { dirty_files: dirty ?? [], unpushed: unpushed ?? [], stashes: stashes ?? [] };
  return fallback ?? { dirty_files: [], unpushed: [] };
}

/** "3 changed files and 2 commits that are not pushed". */
export function unsavedWords(unsaved: EnvUnsaved): string {
  const parts = [];
  if (unsaved.dirty_files.length) parts.push(plural(unsaved.dirty_files.length, 'changed file'));
  if (unsaved.unpushed.length) parts.push(`${plural(unsaved.unpushed.length, 'commit')} not pushed`);
  if (unsaved.stashes?.length) parts.push(plural(unsaved.stashes.length, 'stash', 'stashes'));
  return parts.length ? parts.join(' and ') : 'work the node could not list';
}

const LIST_MAX = 12;

/** The files and commits that would be lost, each a mono line, the rest counted. */
export function UnsavedList({ unsaved }: { unsaved: EnvUnsaved }) {
  return (
    <div className="grid gap-4">
      {unsaved.dirty_files.length ? (
        <section aria-label="Changed files" className="grid gap-2">
          <h3 className="flex items-center gap-2 text-secondary font-medium text-fg">
            <FileDiff aria-hidden="true" className="size-4 text-faint" />
            {plural(unsaved.dirty_files.length, 'changed file')}, not committed
          </h3>
          <ul className="grid gap-1 rounded-lg border border-line bg-inset px-3 py-2">
            {unsaved.dirty_files.slice(0, LIST_MAX).map((file) => (
              <li key={file} title={file} className="truncate font-mono text-code-caption text-muted">
                {file}
              </li>
            ))}
            {unsaved.dirty_files.length > LIST_MAX ? <li className="text-caption text-faint">and {unsaved.dirty_files.length - LIST_MAX} more</li> : null}
          </ul>
        </section>
      ) : null}
      {unsaved.unpushed.length ? (
        <section aria-label="Commits not pushed" className="grid gap-2">
          <h3 className="flex items-center gap-2 text-secondary font-medium text-fg">
            <GitCommitHorizontal aria-hidden="true" className="size-4 text-faint" />
            {plural(unsaved.unpushed.length, 'commit')}, not pushed
          </h3>
          <ul className="grid gap-1 rounded-lg border border-line bg-inset px-3 py-2">
            {unsaved.unpushed.slice(0, LIST_MAX).map((commit) => (
              <li key={commit} className="font-mono text-code-caption text-muted [overflow-wrap:anywhere]">
                {commit}
              </li>
            ))}
            {unsaved.unpushed.length > LIST_MAX ? <li className="text-caption text-faint">and {unsaved.unpushed.length - LIST_MAX} more</li> : null}
          </ul>
        </section>
      ) : null}
      {unsaved.stashes?.length ? (
        <section aria-label="Stashes" className="grid gap-2">
          <h3 className="flex items-center gap-2 text-secondary font-medium text-fg">
            <GitCommitHorizontal aria-hidden="true" className="size-4 text-faint" />
            {plural(unsaved.stashes.length, 'stash', 'stashes')}, on no branch
          </h3>
          <ul className="grid gap-1 rounded-lg border border-line bg-inset px-3 py-2">
            {unsaved.stashes.slice(0, LIST_MAX).map((stash) => (
              <li key={stash} className="font-mono text-code-caption text-muted [overflow-wrap:anywhere]">
                {stash}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

/** The Close button and the dialogs behind it. `glass` on the night band; `className` to size it (a phone's full width). */
export function CloseEnvironment({ org, env, glass = false, className }: { org: string; env: EnvDetail; glass?: boolean; className?: string }) {
  const close = useCloseEnvironment(org);
  const [step, setStep] = useState<Step>(null);
  const [unsaved, setUnsaved] = useState<EnvUnsaved | null>(null);

  const run = (discard: boolean) =>
    close.mutate(
      { id: env.id, discard },
      {
        onSuccess: (closed) => {
          setStep(null);
          toast.success(`${env.title} is closing. It cost ${euro(closed.cost_eur)} so far, and the machine is deleted in a moment.`);
        },
        onError: (error) => {
          if (error instanceof ApiError && error.code === 'unsaved_work' && !discard) {
            setUnsaved(unsavedFrom(error, env.unsaved));
            setStep('unsaved');
            return;
          }
          setStep(null);
          toast.error(error instanceof ApiError ? error.message : 'The environment could not be closed. Nothing was changed.');
        },
      },
    );

  const lost = unsaved ? unsavedWords(unsaved) : '';

  return (
    <>
      <Button variant={glass ? 'glass' : 'danger'} className={cn(glass && 'text-bad', className)} onClick={() => setStep('confirm')}>
        Close
      </Button>
      <ConfirmDialog
        open={step === 'confirm'}
        onOpenChange={(open) => !open && setStep(null)}
        title="Close this environment?"
        description={
          env.state === 'gone'
            ? 'Its machine already stopped, so there is nothing left to save. Closing ends it here, and its saves stay.'
            : 'The machine and its browser are deleted, and the cost stops. openajar checks first that nothing is left unpushed.'
        }
        confirmLabel="Close it"
        danger
        loading={close.isPending}
        onConfirm={() => run(false)}
      />
      <Dialog
        open={step === 'unsaved'}
        onOpenChange={(open) => !open && setStep(null)}
        title="This environment has unsaved work"
        description={`It has ${lost}. Closing deletes the machine and the work with it. Ask the agent to commit and push it, or discard it.`}
        footer={
          <>
            <Button onClick={() => setStep(null)}>Keep it open</Button>
            <Button variant="danger" onClick={() => setStep('discard')}>
              Discard and close
            </Button>
          </>
        }
      >
        {unsaved ? <UnsavedList unsaved={unsaved} /> : null}
      </Dialog>
      <ConfirmDialog
        open={step === 'discard'}
        onOpenChange={(open) => !open && setStep(null)}
        title="Discard the work and close?"
        description={`The ${lost} ${unsaved && unsaved.dirty_files.length + unsaved.unpushed.length + (unsaved.stashes?.length ?? 0) === 1 ? 'is' : 'are'} deleted with the machine. This cannot be undone.`}
        confirmLabel="Discard and close"
        danger
        loading={close.isPending}
        onConfirm={() => run(true)}
      />
    </>
  );
}
