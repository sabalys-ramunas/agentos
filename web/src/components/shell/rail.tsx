import { Link, useNavigate, useRouterState } from '@tanstack/react-router';
import { Check, ChevronsUpDown, Ellipsis, Plus, Search } from 'lucide-react';
import { useRef, useState } from 'react';
import { Dialog } from 'radix-ui';
import { useAccess, useSession } from '@/api/hooks';
import { cn } from '@/lib/cn';
import { roleLabel } from '@/lib/labels';
import { Count } from '../ui/badge';
import { Menu, MenuContent, MenuItem, MenuLabel, MenuSeparator, MenuTrigger, Tooltip } from '../ui/menu';
import { Kbd, Logo } from '../ui/misc';
import { useRailCounts } from './counts';
import type { RailCounts } from './counts';
import { useUsedPlaces } from './developer-places';
import { menuFor, placeOf } from './nav';
import type { MenuSection, Menu as PlaceMenu, NavItem } from './nav';
import { openPalette } from './palette-store';
import { StatusPill } from './status-pill';
import { TechnicalToggle } from './technical-toggle';

/**
 * The place the page belongs to (nav.ts `placeOf`), so a place is lit on every page under it: Computers on
 * Machines and on one machine, Accounts on one account. Its link says aria-current (page when it is that very page).
 */
function useHere(): NavItem['to'] | undefined {
  return useRouterState({ select: (state) => placeOf(state.location.pathname)?.to });
}

const hereProps = (here: boolean) => (here ? { 'data-status': 'active', 'aria-current': true } : {});

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
 * upward. On a phone (`compact`) it is the mark alone in the top bar, and at the foot of the More sheet (`wide`) it is
 * the rail's card with its words at every width.
 */
export function OrgSwitcher({ org, compact = false, wide = false }: { org: string; compact?: boolean; wide?: boolean }) {
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
            : cn(
                'h-13 w-full gap-3 rounded-lg border border-side-line bg-side-foot backdrop-blur-md hover:bg-side-hover data-[state=open]:bg-side-selected',
                wide ? 'justify-start px-2' : 'justify-center lg:justify-start lg:px-2',
              ),
        )}
      >
        <OrgMark name={name} />
        {compact ? null : (
          <>
            <span data-rail-label className={cn('min-w-0 flex-1', wide ? 'grid' : 'hidden lg:grid')}>
              <span className="truncate text-label font-semibold text-side">{name}</span>
              <span className="truncate text-caption text-side-faint">{current ? roleLabel[current.role] : 'Organization'}</span>
            </span>
            <ChevronsUpDown data-rail-label aria-hidden="true" className={cn('size-4 shrink-0 text-side-faint', wide ? 'block' : 'hidden lg:block')} />
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

/** The number beside a place, if it has one: what waits for you in violet, runs and open environments quietly. */
function railCount(item: NavItem, counts: RailCounts): { value: number; tone: 'accent' | 'quiet'; words: string } | null {
  if (item.to === '/$org/needs-you' && counts.needsYou > 0) return { value: counts.needsYou, tone: 'accent', words: `${counts.needsYou} waiting` };
  if (item.to === '/$org/runs' && counts.running) return { value: counts.running, tone: 'quiet', words: `${counts.running} running` };
  // Computers counts the environments open now: the agents' computers that are working.
  if (item.to === '/$org/environments' && counts.environments) return { value: counts.environments, tone: 'quiet', words: `${counts.environments} open` };
  return null;
}

/** This person's menu, with the developer places only once they are used (developer-places.ts). */
function useMenu(org: string): PlaceMenu {
  const { role } = useAccess(org);
  return menuFor(role, useUsedPlaces(org, role !== 'approver'));
}

function RailLink({ org, item, counts, here }: { org: string; item: NavItem; counts: RailCounts; here: boolean }) {
  const count = railCount(item, counts);
  return (
    <Tooltip label={count ? `${item.label}, ${count.words}` : item.label} side="right">
      <Link
        to={item.to}
        params={{ org }}
        activeOptions={{ exact: true }}
        {...hereProps(here)}
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
  const menu = useMenu(org);
  const here = useHere();
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
              <div
                key={section.group}
                role="group"
                aria-labelledby={section.label ? `rail-${section.group}` : undefined}
                aria-label={section.label ? undefined : section.group === 'end' ? 'Settings' : 'Everyday'}
                className={cn('flex flex-col', section.group === 'end' && 'lg:pt-6')}
              >
                {section.label ? (
                  <p id={`rail-${section.group}`} data-rail-label className="text-caption text-side-faint max-lg:sr-only lg:px-3 lg:pt-4 lg:pb-1">
                    {section.label}
                  </p>
                ) : null}
                {/* On the icon rail a hairline stands for the heading. */}
                {index > 0 ? <span data-rail-hairline aria-hidden="true" className="mx-auto my-2 block h-px w-7 bg-side-line lg:hidden" /> : null}
                {section.items.map((item) => (
                  <RailLink key={item.to} org={org} item={item} counts={counts} here={here === item.to} />
                ))}
              </div>
            ) : (
              section.items.map((item) => <RailLink key={item.to} org={org} item={item} counts={counts} here={here === item.to} />)
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

// Each tab is as wide as its words need, plus an equal share of the rest, so "What happened" fits whole at
// 390 px beside "Home". Below that it is cut with an ellipsis, never wrapped.
const tabClass =
  'relative flex min-w-0 flex-auto flex-col items-center justify-center gap-1 px-1 text-micro text-muted data-[status=active]:font-semibold data-[status=active]:text-accent-text data-[status=active]:[&_svg]:text-accent-text';

/**
 * The places that are not tabs, as tiles in a sheet from the bottom (phone-nav.png), in the rail's own groups
 * (Your business, For developers, then Settings): the tab bar stays in view under it, the place you are on is
 * violet, and the sidebar's foot (the machines and the organisation) sits at its end on the night.
 */
function MoreSheet({
  org,
  sections,
  here,
  open,
  onOpenChange,
  trigger,
}: {
  org: string;
  sections: MenuSection[];
  here: NavItem['to'] | undefined;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  trigger: React.RefObject<HTMLButtonElement | null>;
}) {
  const { role } = useAccess(org);
  const items = sections.flatMap((section) => section.items);
  const active = items.some((item) => item.to === here);
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Trigger
        ref={trigger}
        className={tabClass}
        data-status={active || open ? 'active' : undefined}
        aria-label={`More: ${items.map((item) => item.label).join(', ')}`}
      >
        <Ellipsis className="size-5" aria-hidden="true" />
        More
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="above-tabbar fixed inset-x-0 top-0 z-40 bg-overlay data-[state=closed]:animate-fade-out data-[state=open]:animate-fade-in md:hidden" />
        <Dialog.Content
          aria-describedby={undefined}
          // A tap on More while the sheet is open closes it, rather than closing and opening it again.
          onPointerDownOutside={(event) => {
            if (trigger.current?.contains(event.target as Node)) event.preventDefault();
          }}
          className="above-tabbar fixed inset-x-0 z-40 max-h-[calc(100dvh-120px)] overflow-y-auto rounded-t-2xl border-t border-line bg-raised px-4 pt-2 pb-4 shadow-dialog outline-none data-[state=closed]:animate-sheet-out data-[state=open]:animate-sheet-in md:hidden"
        >
          <span aria-hidden="true" className="mx-auto mb-3 block h-1 w-9 rounded-full bg-line-strong" />
          <Dialog.Title className="sr-only">More</Dialog.Title>
          <nav aria-label="More" className="grid gap-3">
            {sections.map((section) => (
              <div
                key={section.group}
                role="group"
                aria-labelledby={section.label ? `more-${section.group}` : undefined}
                aria-label={section.label ? undefined : 'Settings'}
                className="grid gap-2"
              >
                {section.label ? (
                  <p id={`more-${section.group}`} className="px-1 text-caption text-faint">
                    {section.label}
                  </p>
                ) : null}
                <ul className="grid grid-cols-3 gap-2">
                  {section.items.map((item) => (
                    <li key={item.to}>
                      <Link
                        to={item.to}
                        params={{ org }}
                        activeOptions={{ exact: true }}
                        {...hereProps(here === item.to)}
                        onClick={() => onOpenChange(false)}
                        className={cn(
                          'flex h-20 flex-col items-center justify-center gap-1 rounded-xl border border-transparent bg-inset px-2 text-center text-label leading-tight text-fg',
                          'transition-colors duration-(--duration-fast) ease-standard hover:bg-hover active:bg-pressed',
                          'data-[status=active]:border-accent-line data-[status=active]:bg-accent-soft data-[status=active]:text-accent-text',
                        )}
                      >
                        <item.icon className="size-5 shrink-0" aria-hidden="true" />
                        {/* two lines at most, so the new places' longer names (Jobs that repeat) are read whole */}
                        <span className="line-clamp-2 max-w-full text-balance">{item.label}</span>
                      </Link>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </nav>
          <div className="rail-night mt-4 grid gap-1 rounded-xl border border-side-line p-2">
            {role !== 'approver' ? <StatusPill org={org} variant="line" wide /> : null}
            <OrgSwitcher org={org} wide />
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/** Home, Needs you, Computers and What happened at the thumb; the rest under More. */
export function TabBar({ org }: { org: string }) {
  const counts = useRailCounts(org);
  const here = useHere();
  const [more, setMore] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useMenu(org);
  // The More sheet keeps the rail's groups, with only the places that are not tabs.
  const moreSections = menu.sections
    .map((section) => ({ ...section, items: section.items.filter((item) => menu.more.includes(item)) }))
    .filter((section) => section.items.length > 0);
  return (
    // In reach while the More sheet is open over the page (the sheet stops above it).
    <nav aria-label="Main" className="safe-bottom pointer-events-auto fixed inset-x-0 bottom-0 z-30 border-t border-line bg-page/90 backdrop-blur-md md:hidden">
      <div className="flex h-(--tabbar-height)">
        {menu.tabs.map((item) => {
          const count = railCount(item, counts);
          return (
            <Link
              key={item.to}
              to={item.to}
              params={{ org }}
              activeOptions={{ exact: true }}
              {...hereProps(here === item.to)}
              className={tabClass}
              onClick={() => setMore(false)}
            >
              <span className="relative">
                <item.icon className="size-5" aria-hidden="true" />
                {count?.tone === 'accent' ? <span aria-hidden="true" className="absolute -top-1 -right-1 size-2 rounded-full border border-page bg-primary" /> : null}
              </span>
              <span className="max-w-full truncate">{item.label}</span>
              {count ? <span className="sr-only">, {count.words}</span> : null}
            </Link>
          );
        })}
        {moreSections.length ? <MoreSheet org={org} sections={moreSections} here={here} open={more} onOpenChange={setMore} trigger={trigger} /> : null}
      </div>
    </nav>
  );
}
