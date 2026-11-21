import { Link, useMatchRoute, useNavigate } from '@tanstack/react-router';
import { Check, ChevronsUpDown, Ellipsis, Plus, Search } from 'lucide-react';
import { useState } from 'react';
import { useAccess, useSession } from '@/api/hooks';
import { cn } from '@/lib/cn';
import { roleLabel } from '@/lib/labels';
import { Count } from '../ui/badge';
import { Menu, MenuContent, MenuItem, MenuLabel, MenuSeparator, MenuTrigger, Tooltip } from '../ui/menu';
import { Kbd, Logo } from '../ui/misc';
import { useRailCounts } from './counts';
import type { RailCounts } from './counts';
import { menuFor } from './nav';
import type { NavItem } from './nav';
import { openPalette } from './palette-store';
import { StatusPill } from './status-pill';
import { TechnicalToggle } from './technical-toggle';

const exact = (to: string) => to === '/$org';

/** One of the eight identity hues for an org, from its name, so the same org always has the same mark. */
function identityOf(name: string): string {
  let sum = 0;
  for (const char of name) sum = (sum * 31 + char.charCodeAt(0)) % 9973;
  return `var(--identity-${(sum % 8) + 1})`;
}

/** The org's mark: its first letter on a rounded square in its identity hue. */
export function OrgMark({ name, className }: { name: string; className?: string }) {
  return (
    <span
      aria-hidden="true"
      style={{ background: identityOf(name) }}
      className={cn('inline-flex size-7 shrink-0 items-center justify-center rounded-md text-label font-semibold text-identity-ink', className)}
    >
      {name.trim().charAt(0).toUpperCase() || 'a'}
    </span>
  );
}

/**
 * The org and its switch. In the rail it sits at the foot on the night's glass, as the best tools keep a
 * workspace switch at the foot of the rail: the org's mark, its name and your role, and the menu opens
 * upward. On a phone (`compact`) it is the mark alone in the top bar.
 */
export function OrgSwitcher({ org, compact = false }: { org: string; compact?: boolean }) {
  const session = useSession().data;
  const navigate = useNavigate();
  const current = session?.orgs.find((item) => item.slug === org);
  const name = current?.name ?? org;
  return (
    <Menu>
      <MenuTrigger
        aria-label={`Switch org: ${name}`}
        title={compact ? undefined : name}
        className={cn(
          'flex min-w-0 items-center text-left transition-colors duration-(--duration-fast) ease-standard',
          compact
            ? 'size-10 justify-center rounded-full hover:bg-hover data-[state=open]:bg-pressed'
            : 'h-13 w-full justify-center gap-3 rounded-lg border border-side-line bg-side-foot backdrop-blur-md hover:bg-side-hover data-[state=open]:bg-side-selected lg:justify-start lg:px-2',
        )}
      >
        <OrgMark name={name} />
        {compact ? null : (
          <>
            <span data-rail-label className="hidden min-w-0 flex-1 lg:grid">
              <span className="truncate text-label font-semibold text-side">{name}</span>
              <span className="truncate text-caption text-side-faint">{current ? roleLabel[current.role] : 'Organization'}</span>
            </span>
            <ChevronsUpDown data-rail-label aria-hidden="true" className="hidden size-4 shrink-0 text-side-faint lg:block" />
          </>
        )}
      </MenuTrigger>
      <MenuContent side={compact ? 'bottom' : 'top'} align="start" sideOffset={compact ? 4 : 8} className="w-64">
        <MenuLabel>Organizations</MenuLabel>
        {session?.orgs.map((item) => (
          <MenuItem key={item.id} onSelect={() => void navigate({ to: '/$org', params: { org: item.slug } })}>
            <OrgMark name={item.name} className="size-6 text-micro" />
            <span className="min-w-0 flex-1 truncate" title={item.name}>
              {item.name}
            </span>
            <span className="text-caption text-faint">{roleLabel[item.role]}</span>
            <Check className={cn('size-4 text-accent-text', item.slug !== org && 'invisible')} />
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

/** "Search or run" with its keys: the command bar, from the rail. Night glass on the sidebar. */
function SearchField() {
  return (
    <Tooltip label="Search or run" side="right">
      <button
        type="button"
        onClick={openPalette}
        aria-label="Search or run a command"
        aria-keyshortcuts="Meta+K Control+K"
        className={cn(
          'mt-4 mb-2 flex h-9 w-full items-center justify-center gap-2 rounded-lg border border-side-line bg-side-hover text-side-faint lg:justify-start lg:pr-2 lg:pl-3',
          'transition-colors duration-(--duration-fast) ease-standard hover:bg-side-selected hover:text-side-muted',
        )}
      >
        <Search className="size-4 shrink-0" aria-hidden="true" />
        <span data-rail-label className="hidden min-w-0 flex-1 truncate text-left text-secondary lg:block">
          Search or run
        </span>
        <span data-rail-label className="hidden lg:inline-flex" aria-hidden="true">
          <Kbd>⌘K</Kbd>
        </span>
      </button>
    </Tooltip>
  );
}

/** The number beside a place, if it has one: what waits for you in violet, runs and environments quietly. */
function railCount(item: NavItem, counts: RailCounts): { value: number; tone: 'accent' | 'quiet'; words: string } | null {
  if (item.to === '/$org/needs-you' && counts.needsYou > 0) return { value: counts.needsYou, tone: 'accent', words: `${counts.needsYou} waiting` };
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
          'group relative flex h-9 items-center justify-center gap-3 rounded-lg border border-transparent text-body font-medium text-side-muted lg:justify-start lg:pr-2 lg:pl-3',
          'transition-colors duration-(--duration-fast) ease-standard hover:bg-side-hover hover:text-side',
          'data-[status=active]:border-side-line data-[status=active]:bg-side-selected data-[status=active]:font-semibold data-[status=active]:text-side',
        )}
      >
        <item.icon className="size-[18px] shrink-0 text-side-faint group-hover:text-side-muted group-data-[status=active]:text-side" aria-hidden="true" />
        <span data-rail-label className="sr-only lg:not-sr-only lg:min-w-0 lg:flex-1 lg:truncate">
          {item.label}
        </span>
        {count ? (
          <>
            {count.tone === 'accent' ? (
              <Count
                data-rail-count
                value={count.value}
                tone="accent"
                aria-label={count.words}
                className="hidden lg:inline-flex lg:group-hover:hidden lg:group-focus-visible:hidden"
              />
            ) : (
              <span
                data-rail-count
                aria-label={count.words}
                // on the selected place the number steps up to the secondary words, which keep 4.5:1 on its fill
                className="hidden font-mono text-code-caption text-side-faint tabular group-data-[status=active]:text-side-muted lg:inline lg:group-hover:hidden lg:group-focus-visible:hidden"
              >
                {count.value}
              </span>
            )}
            {/* The icon rail has no room for a number: a dot says something is there. */}
            <span
              aria-hidden="true"
              className={cn('absolute top-1 right-1 size-2 rounded-full lg:hidden', count.tone === 'accent' ? 'bg-primary' : 'bg-side-faint')}
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
 * The rail, full height on the left, on the night sky with the sunrise at its foot (Dawn's Full). The mark
 * and the name alone at the top, Search or run, the places in their groups, and at the foot the machines'
 * status line, the Technical details switch and the org on its glass. Icons only from 768 to 1023 px.
 */
export function Rail({ org }: { org: string }) {
  const counts = useRailCounts(org);
  const { role } = useAccess(org);
  const menu = menuFor(role);
  // One group needs no heading: an approver's three places are simply the menu.
  const headed = menu.sections.length > 1;
  return (
    <div className="app-rail hidden w-16 shrink-0 md:block lg:w-(--rail-width)">
      <div data-rail-body className="rail-night sticky top-0 flex h-dvh flex-col overflow-y-auto px-2 pt-4 pb-3 lg:px-3">
        <Link to="/$org" params={{ org }} aria-label="openajar, Home" className="flex h-9 items-center justify-center gap-3 rounded-lg lg:justify-start lg:px-2">
          <Logo className="size-[30px]" />
          <span data-rail-label className="hidden text-card-title tracking-[-0.02em] text-side lg:block">
            openajar
          </span>
        </Link>
        <SearchField />
        <nav aria-label="Main" className="flex flex-col">
          {menu.sections.map((section, index) =>
            headed ? (
              <div key={section.group} role="group" aria-labelledby={`rail-${section.group}`} className="flex flex-col">
                <p
                  id={`rail-${section.group}`}
                  data-rail-label
                  className={cn('text-caption text-side-faint max-lg:sr-only lg:px-3 lg:pt-4 lg:pb-1', index === 0 && 'lg:pt-3')}
                >
                  {section.label}
                </p>
                {/* On the icon rail a hairline stands for the heading. */}
                {index > 0 ? <span data-rail-hairline aria-hidden="true" className="mx-auto my-2 block h-px w-7 bg-side-line lg:hidden" /> : null}
                {section.items.map((item) => (
                  <RailLink key={item.to} org={org} item={item} counts={counts} />
                ))}
              </div>
            ) : (
              section.items.map((item) => <RailLink key={item.to} org={org} item={item} counts={counts} />)
            ),
          )}
        </nav>
        <div className="mt-auto grid gap-2 pt-6">
          {/* An approver has no machines to watch (startup.md B5). */}
          {role !== 'approver' ? <StatusPill org={org} variant="line" /> : null}
          <TechnicalToggle />
          <OrgSwitcher org={org} />
        </div>
      </div>
    </div>
  );
}

const tabClass =
  'relative flex min-w-0 flex-col items-center justify-center gap-1 text-micro text-muted data-[status=active]:font-semibold data-[status=active]:text-accent-text data-[status=active]:[&_svg]:text-accent-text';

/** Home, Environments, Runs, Checks and Needs you at the thumb; the rest under More. */
export function TabBar({ org }: { org: string }) {
  const counts = useRailCounts(org);
  const matchRoute = useMatchRoute();
  const [more, setMore] = useState(false);
  const menu = menuFor(useAccess(org).role);
  const inMore = menu.more.some((item) => matchRoute({ to: item.to, params: { org }, fuzzy: true }));
  const columns = menu.tabs.length + (menu.more.length ? 1 : 0);
  return (
    <nav aria-label="Main" className="safe-bottom fixed inset-x-0 bottom-0 z-30 border-t border-line bg-page/90 backdrop-blur-md md:hidden">
      <div className="grid h-(--tabbar-height)" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
        {menu.tabs.map((item) => {
          const count = railCount(item, counts);
          return (
            <Link key={item.to} to={item.to} params={{ org }} activeOptions={{ exact: exact(item.to) }} className={tabClass}>
              <span className="relative">
                <item.icon className="size-5" aria-hidden="true" />
                {count?.tone === 'accent' ? <span aria-hidden="true" className="absolute -top-1 -right-1 size-2 rounded-full border border-page bg-primary" /> : null}
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
