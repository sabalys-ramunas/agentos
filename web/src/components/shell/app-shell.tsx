import { Link, Outlet } from '@tanstack/react-router';
import { useEffect, useState } from 'react';
import { useSession } from '@/api/hooks';
import { useLiveEvents } from '@/api/live';
import { usePushSync } from '@/api/push';
import { Button } from '../ui/button';
import { Logo } from '../ui/misc';
import { EmptyState } from '../ui/states';
import { CommandPalette } from './command-palette';
import { FreezeDialogs } from './freeze-control';
import { Rail, TabBar } from './rail';
import { Shortcuts } from './shortcuts';
import { TopBar } from './top-bar';
import { useBandHeight } from './page-band';

/** The org was deleted, or this person was removed from it. They need to know, and a way on. */
function OrgGone() {
  const orgs = useSession().data?.orgs ?? [];
  const next = orgs[0];
  return (
    <main className="flex min-h-dvh flex-col items-center justify-center px-4">
      <Logo className="mb-2 size-8" />
      <EmptyState
        className="py-8"
        title="You no longer have access to this org"
        description="It may have been deleted, or your access was removed. Nothing else about your account has changed."
        action={
          <Button variant="primary" asChild>
            {next ? (
              <Link to="/$org" params={{ org: next.slug }}>
                Go to {next.name}
              </Link>
            ) : (
              <Link to="/onboarding">Create an org</Link>
            )}
          </Button>
        }
      />
    </main>
  );
}

/** Long enough for a move to a new address, or a delete, to finish without a flash. */
const GONE_DELAY_MS = 200;

function useGone(org: string) {
  const session = useSession();
  const missing = session.data !== undefined && !session.data.orgs.some((item) => item.slug === org);
  const [gone, setGone] = useState(false);
  useEffect(() => {
    if (!missing) {
      setGone(false);
      return;
    }
    const timer = setTimeout(() => setGone(true), GONE_DELAY_MS);
    return () => clearTimeout(timer);
  }, [missing]);
  return gone;
}

function Live({ org }: { org: string }) {
  useLiveEvents(org);
  usePushSync(org);
  return null;
}

/**
 * The shell, as Mono draws it: the rail full height on the left, and beside it a column with the top
 * bar and the page. On a phone the rail goes and the tab bar holds the places.
 */
export function AppShell({ org }: { org: string }) {
  const band = useBandHeight();
  if (useGone(org)) return <OrgGone />;

  return (
    <div className="app-shell flex min-h-dvh bg-page">
      <a
        href="#main"
        className="sr-only rounded-md bg-primary px-3 py-1 text-body text-on-primary focus:not-sr-only focus:fixed focus:top-3 focus:left-3 focus:z-50"
      >
        Skip to content
      </a>
      <Live org={org} />
      <Rail org={org} />
      <div data-page-column className="relative flex min-w-0 flex-1 flex-col">
        {/* The night band a page asks for (page-band.ts): behind the top bar and the page's top, the whole column wide. */}
        {band !== null ? <div aria-hidden="true" className="band-night pointer-events-none absolute inset-x-0 top-0" style={{ height: band }} /> : null}
        <TopBar org={org} night={band !== null} />
        <main id="main" tabIndex={-1} className="relative min-w-0 flex-1 pb-[calc(var(--tabbar-height)+env(safe-area-inset-bottom)+32px)] outline-none md:pb-16">
          <div className="page-frame mx-auto w-full max-w-(--page-max) px-4 pt-6 sm:px-6 md:pt-8 lg:px-10 lg:pt-6">
            <Outlet />
          </div>
        </main>
      </div>
      <TabBar org={org} />
      <CommandPalette org={org} />
      <Shortcuts org={org} />
      <FreezeDialogs org={org} />
    </div>
  );
}
