import { Link, useRouterState } from '@tanstack/react-router';
import { cn } from '@/lib/cn';
import { placeOf } from './nav';

/**
 * The two lists of a place that holds two (nav.ts `tabs`): Computers is Environments and Machines, Jobs that
 * repeat is Routines and Email, Runs and checks is Runs and Checks. The shell draws them on top of the list's
 * page, so every state of the page (loading, empty, refused) still has the way to the other list.
 *
 * Each tab is its own address (`/environments`, `/machines`), so an old link opens the right one. They are
 * links in a `nav` named after the place, drawn as the `Tab` words on one hairline; the open one is violet.
 */
export function PlaceTabs({ org }: { org: string }) {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const place = placeOf(pathname);
  const section = pathname.split('/').slice(2).join('/');
  const open = place?.tabs?.find((tab) => tab.to === `/$org/${section}`);
  if (!place?.tabs || !open) return null;
  return (
    <nav aria-label={place.label} data-place-tabs className="scrollbar-none mb-6 flex gap-6 overflow-x-auto border-b border-line">
      {place.tabs.map((tab) => (
        <Link
          key={tab.to}
          to={tab.to}
          params={{ org }}
          activeOptions={{ exact: true, includeSearch: false }}
          className={cn(
            'focus-inset -mb-px flex h-11 items-center border-b-2 border-transparent text-label whitespace-nowrap text-muted',
            'transition-colors duration-(--duration-fast) ease-standard hover:text-fg',
            'data-[status=active]:border-accent data-[status=active]:font-semibold data-[status=active]:text-accent-text',
          )}
        >
          {tab.label}
        </Link>
      ))}
    </nav>
  );
}
