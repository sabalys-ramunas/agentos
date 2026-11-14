import { Link, useMatchRoute, useNavigate } from '@tanstack/react-router';
import { Check, ChevronsUpDown, Ellipsis, Plus, Search } from 'lucide-react';
import { useState } from 'react';
import { useAccess, useSession } from '@/api/hooks';
import { cn } from '@/lib/cn';
import { roleLabel } from '@/lib/labels';
import { Count } from '../ui/badge';
import { Menu, MenuContent, MenuItem, MenuLabel, MenuSeparator, MenuTrigger, Tooltip } from '../ui/menu';
import { Kbd } from '../ui/misc';
import { useRailCounts } from './counts';
import type { RailCounts } from './counts';
import { menuFor } from './nav';
import type { NavItem } from './nav';
import { openPalette } from './palette-store';
import { StatusPill } from './status-pill';
import { TechnicalToggle } from './technical-toggle';

const exact = (to: string) => to === '/$org';

/** The org's mark: its first letter in an ink circle. */
export function OrgMark({ name, className }: { name: string; className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={cn('inline-flex size-6 shrink-0 items-center justify-center rounded-full bg-fg text-micro font-semibold text-on-primary', className)}
    >
      {name.trim().charAt(0).toUpperCase() || 'a'}
    </span>
  );
}

/** The org at the top of the rail: its mark and name, and every other org a person belongs to. */
export function OrgSwitcher({ org, compact = false }: { org: string; compact?: boolean }) {
  const session = useSession().data;
  const navigate = useNavigate();
  const current = session?.orgs.find((item) => item.slug === org);
  const name = current?.name ?? org;
  return (
    <Menu>
      <MenuTrigger
        aria-label={`Switch org: ${name}`}
        title={compact ? undefined : `${name} (${org})`}
        className={cn(
          'flex h-9 min-w-0 items-center gap-2 rounded-md text-left transition-colors duration-(--duration-fast) ease-standard hover:bg-hover data-[state=open]:bg-pressed',
          compact ? 'w-10 justify-center' : 'w-full justify-center px-0 lg:justify-start lg:px-2',
        )}
      >
        <OrgMark name={name} />
        {compact ? null : (
          <>
            <span data-rail-label className="hidden min-w-0 flex-1 truncate text-card-title lg:block">
              {name}
            </span>
            <ChevronsUpDown data-rail-label className="hidden size-3 shrink-0 text-faint lg:block" />
          </>
        )}
      </MenuTrigger>
      <MenuContent className="w-64">
        <MenuLabel>Orgs</MenuLabel>
        {session?.orgs.map((item) => (
          <MenuItem key={item.id} onSelect={() => void navigate({ to: '/$org', params: { org: item.slug } })}>
            <span className="min-w-0 flex-1 truncate">{item.name}</span>
            <span className="text-caption text-faint">{roleLabel[item.role]}</span>
            <Check className={cn('size-4 text-muted', item.slug !== org && 'invisible')} />
          </MenuItem>
        ))}
        <MenuSeparator />
        <MenuItem onSelect={() => void navigate({ to: '/onboarding' })}>
          <Plus className="size-4 text-faint" />
          Create org
        </MenuItem>
      </MenuContent>
    </Menu>
  );
}

/** "Search or run" with its keys: the command bar, from the rail. */
function SearchField() {
  return (
    <Tooltip label="Search or run" side="right">
      <button
        type="button"
        onClick={openPalette}
        aria-label="Search or run a command"
        aria-keyshortcuts="Meta+K Control+K"
        className={cn(
          'mt-2 mb-3 flex h-8 w-full items-center justify-center gap-2 rounded-md border border-line bg-card text-faint shadow-lift lg:justify-start lg:pr-1 lg:pl-2',
          'transition-colors duration-(--duration-fast) ease-standard hover:border-line-strong hover:text-muted',
        )}
      >
        <Search className="size-4 shrink-0" aria-hidden="true" />
        <span data-rail-label className="hidden min-w-0 flex-1 truncate text-left text-secondary lg:block">
          Search or run
        </span>
        <span data-rail-label className="hidden gap-1 lg:inline-flex" aria-hidden="true">
          <Kbd>⌘</Kbd>
          <Kbd>K</Kbd>
        </span>
      </button>
    </Tooltip>
  );
}

/** The number beside a place, if it has one: things waiting in ink, runs going quietly. */
function railCount(item: NavItem, counts: RailCounts): { value: number; tone: 'ink' | 'quiet'; words: string } | null {
  if (item.to === '/$org/needs-you' && counts.needsYou > 0) return { value: counts.needsYou, tone: 'ink', words: `${counts.needsYou} waiting` };
  if (item.to === '/$org/runs' && counts.running) return { value: counts.running, tone: 'quiet', words: `${counts.running} running` };
  if (item.to === '/$org/environments' && counts.environments) return { value: counts.environments, tone: 'quiet', words: `${counts.environments} open` };
  return null;
}

function RailLink({ org, item, counts }: { org: string; item: NavItem; counts: RailCounts }) {
  const count = railCount(item, counts);
  return (
    <Tooltip label={count ? `${item.label}, ${count.words}` : item.label} side="right">
      <Link
        to={item.to}
        params={{ org }}
        activeOptions={{ exact: exact(item.to) }}
        aria-keyshortcuts={`G ${item.key.toUpperCase()}`}
        className={cn(
          'group relative flex h-8 items-center justify-center gap-2 rounded-md text-body text-muted lg:justify-start lg:pr-1 lg:pl-2',
          'transition-colors duration-(--duration-fast) ease-standard hover:bg-hover hover:text-fg',
          'data-[status=active]:bg-pressed data-[status=active]:font-medium data-[status=active]:text-fg',
        )}
      >
        <item.icon className="size-4 shrink-0" aria-hidden="true" />
        <span data-rail-label className="sr-only lg:not-sr-only lg:min-w-0 lg:flex-1 lg:truncate">
          {item.label}
        </span>
        {count ? (
          <>
            <Count
              data-rail-count
              value={count.value}
              tone={count.tone}
              aria-label={count.words}
              // on the active row (bg-pressed) the quiet count's text-faint falls under 4.5:1: use muted
              className="hidden lg:inline-flex lg:group-hover:hidden lg:group-focus-visible:hidden group-data-[status=active]:text-muted"
            />
            {/* The icon rail has no room for a number: a dot says something is there. */}
            <span
              aria-hidden="true"
              className={cn('absolute top-1 right-1 size-2 rounded-full border border-page lg:hidden', count.tone === 'ink' ? 'bg-fg' : 'bg-faint')}
            />
          </>
        ) : null}
        <span data-rail-count aria-hidden="true" className="hidden gap-1 lg:group-hover:inline-flex lg:group-focus-visible:inline-flex">
          <Kbd>G</Kbd>
          <Kbd>{item.key.toUpperCase()}</Kbd>
        </span>
      </Link>
    </Tooltip>
  );
}

/**
 * The rail, full height on the left: the org, Search or run, the places in their groups, and at the
 * foot the machines' status line and the Technical details switch. Icons only from 768 to 1023 px.
 */
export function Rail({ org }: { org: string }) {
  const counts = useRailCounts(org);
  const { role } = useAccess(org);
  const menu = menuFor(role);
  // One group needs no heading: an approver's three places are simply the menu.
  const headed = menu.sections.length > 1;
  return (
    <div className="app-rail hidden w-16 shrink-0 border-r border-line bg-page md:block lg:w-(--rail-width)">
      <div data-rail-body className="sticky top-0 flex h-dvh flex-col overflow-y-auto px-2 py-3 lg:px-3">
        <OrgSwitcher org={org} />
        <SearchField />
        <nav aria-label="Main" className="flex flex-col gap-px">
          {menu.sections.map((section, index) =>
            headed ? (
              <div key={section.group} role="group" aria-labelledby={`rail-${section.group}`} className="flex flex-col gap-px">
                <p
                  id={`rail-${section.group}`}
                  data-rail-label
                  className={cn(
                    'text-caption text-faint max-lg:sr-only lg:px-2 lg:pt-5 lg:pb-1',
                    index === 0 && 'lg:pt-2',
                  )}
                >
                  {section.label}
                </p>
                {/* On the icon rail a hairline stands for the heading. */}
                {index > 0 ? <span data-rail-hairline aria-hidden="true" className="mx-auto my-2 block h-px w-7 bg-line lg:hidden" /> : null}
                {section.items.map((item) => (
                  <RailLink key={item.to} org={org} item={item} counts={counts} />
                ))}
              </div>
            ) : (
              section.items.map((item) => <RailLink key={item.to} org={org} item={item} counts={counts} />)
            ),
          )}
        </nav>
        <div className="mt-auto grid gap-2 pt-3">
          {/* An approver has no machines to watch (startup.md B5). */}
          {role !== 'approver' ? <StatusPill org={org} variant="line" /> : null}
          <TechnicalToggle />
        </div>
      </div>
    </div>
  );
}

const tabClass =
  'relative flex min-w-0 flex-col items-center justify-center gap-1 text-micro text-muted data-[status=active]:text-fg data-[status=active]:[&_svg]:text-fg';

/** Home, Runs, Checks, Machines and Needs you at the thumb; the rest under More. */
export function TabBar({ org }: { org: string }) {
  const counts = useRailCounts(org);
  const matchRoute = useMatchRoute();
  const [more, setMore] = useState(false);
  const menu = menuFor(useAccess(org).role);
  const inMore = menu.more.some((item) => matchRoute({ to: item.to, params: { org }, fuzzy: true }));
  const columns = menu.tabs.length + (menu.more.length ? 1 : 0);
  return (
    <nav aria-label="Main" className="safe-bottom fixed inset-x-0 bottom-0 z-30 border-t border-line bg-page md:hidden">
      <div className="grid h-(--tabbar-height)" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
        {menu.tabs.map((item) => {
          const count = railCount(item, counts);
          return (
            <Link key={item.to} to={item.to} params={{ org }} activeOptions={{ exact: exact(item.to) }} className={tabClass}>
              <span className="relative">
                <item.icon className="size-5" aria-hidden="true" />
                {count?.tone === 'ink' ? <span aria-hidden="true" className="absolute -top-1 -right-1 size-2 rounded-full border border-page bg-fg" /> : null}
              </span>
              <span className="max-w-full truncate">{item.label}</span>
              {count ? <span className="sr-only">, {count.words}</span> : null}
            </Link>
          );
        })}
        {menu.more.length ? (
          <Menu open={more} onOpenChange={setMore}>
            <MenuTrigger
              className={tabClass}
              data-status={inMore ? 'active' : undefined}
              aria-label={`More: ${menu.more.map((item) => item.label).join(', ')}`}
            >
              <Ellipsis className="size-5" aria-hidden="true" />
              More
            </MenuTrigger>
            <MenuContent align="end" side="top" sideOffset={8} className="w-56">
              {menu.more.map((item) => (
                <MenuItem key={item.to} asChild>
                  <Link to={item.to} params={{ org }} className="h-10">
                    <item.icon className="size-4 text-faint" aria-hidden="true" />
                    {item.label}
                  </Link>
                </MenuItem>
              ))}
            </MenuContent>
          </Menu>
        ) : null}
      </div>
    </nav>
  );
}
