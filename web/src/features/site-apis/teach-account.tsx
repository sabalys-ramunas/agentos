/** Teach's first stage: pick the account whose site to read. */
import { Link } from '@tanstack/react-router';
import { ArrowRight } from 'lucide-react';
import type { Lane, Workspace } from '@/api/types';
import { BusinessMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';

export function AccountStage({ org, workspaces, onPick }: { org: string; workspaces: Workspace[]; onPick: (lane: Lane) => void }) {
  const likely = workspaces.flatMap((workspace) => workspace.lanes.filter((lane) => lane.kind === 'browser' && lane.readiness === 'ready' && lane.account?.site?.endsWith('.example')).map((lane) => ({ lane, workspace })));
  const browsers = workspaces.flatMap((workspace) => workspace.lanes.filter((lane) => lane.kind === 'browser'));
  const anyReady = browsers.some((lane) => lane.readiness === 'ready');
  return (
    <div className="mx-auto grid max-w-4xl gap-6">
      {anyReady ? null : (
        <p className="text-14 text-pretty text-muted">
          {browsers.length ? 'None of your accounts is ready yet. Sign in to one on its own page, and it will show here.' : 'You have no accounts to read a page with yet.'}
        </p>
      )}
      {likely.length ? (
        <section aria-label="Most likely">
          <h2 className="silk mb-2">Most likely</h2>
          {likely.map(({ lane, workspace }) => (
            <button
              key={lane.id}
              type="button"
              onClick={() => onPick(lane)}
              className="group flex w-full items-center gap-3 rounded-(--radius-card) border border-line bg-bg p-4 text-left transition-[border-color,background-color,transform] duration-(--duration-fast) ease-standard hover:border-line-strong hover:bg-fill-1 active:translate-y-px"
            >
              <SiteMark site={lane.id} size={40} tile label={false} className="bg-bg" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-16 font-semibold">{lane.account?.user_id}</span>
                <span className="block truncate text-13 text-muted">
                  {workspace.name}, signed in at <span className="mono">{lane.account?.site}</span>
                </span>
              </span>
              <ArrowRight aria-hidden="true" className="size-4 shrink-0 text-faint transition-transform duration-(--duration-fast) ease-standard group-hover:translate-x-1" />
            </button>
          ))}
        </section>
      ) : null}
      {workspaces.map((workspace) => {
        const lanes = workspace.lanes.filter((lane) => lane.kind === 'browser');
        if (!lanes.length) return null;
        return (
          <section key={workspace.id} aria-label={workspace.name}>
            <h2 className="mb-2 flex items-center gap-2 text-13 font-medium">
              <BusinessMark name={workspace.name} color={workspace.color} size={16} />
              {workspace.name}
            </h2>
            <ul className="grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-4">
              {lanes.map((lane) => {
                const ready = lane.readiness === 'ready';
                const [ws = '', name = lane.name] = lane.id.split('/');
                const inner = (
                  <>
                    <SiteMark site={lane.id} size={24} tile label={false} className="bg-bg" />
                    <span className="min-w-0">
                      <span className="block truncate text-13 font-medium">{lane.account?.user_id ?? lane.name}</span>
                      <span className="block truncate text-12 text-faint">{ready ? (lane.account?.site ?? lane.name) : 'Not signed in yet'}</span>
                    </span>
                  </>
                );
                const rowClass = 'flex w-full items-center gap-2 rounded-lg border border-line bg-bg p-2 text-left transition-[border-color,background-color,transform] duration-(--duration-fast) ease-standard hover:border-line-strong hover:bg-fill-1 active:translate-y-px';
                return (
                  <li key={lane.id}>
                    {ready ? (
                      <button type="button" onClick={() => onPick(lane)} className={rowClass}>
                        {inner}
                      </button>
                    ) : (
                      // Not ready is not a dead end: the account's own page has the sign-in.
                      <Link to="/$org/lanes/$ws/$name" params={{ org, ws, name }} className={rowClass}>
                        {inner}
                      </Link>
                    )}
                  </li>
                );
              })}
            </ul>
          </section>
        );
      })}
    </div>
  );
}
