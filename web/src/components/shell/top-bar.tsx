import { BusinessMark } from '../brand/identity';
import { Link, useNavigate, useRouterState } from '@tanstack/react-router';
import { Check, ChevronsUpDown, Keyboard, Laptop, LogOut, Moon, Search, Sun, User } from 'lucide-react';
import { useSyncExternalStore } from 'react';
import { useAccess, useSignOut, useSession, useWorkspaces } from '@/api/hooks';
import { useLiveConnection } from '@/api/live';
import { useActiveWorkspace } from '@/lib/active-workspace';
import { cn } from '@/lib/cn';
import { setTheme, useTheme } from '@/lib/theme';
import type { ThemeChoice } from '@/lib/theme';
import { IconButton } from '../ui/button';
import { Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger, Tooltip } from '../ui/menu';
import { Avatar } from '../ui/misc';
import { StatusDot } from '../ui/status';
import { NotificationsBell } from './notifications';
import { useCurrentCrumb } from './crumb';
import { FrozenChip } from './freeze-control';
import { placeOf, tabOf } from './nav';
import { openPalette, openShortcuts } from './palette-store';
import { OrgSwitcher } from './rail';
import { StatusPill } from './status-pill';

const chip =
  'inline-flex h-9 items-center gap-2 rounded-full px-3 text-secondary text-muted transition-colors ' +
  'duration-(--duration-fast) ease-standard hover:bg-hover hover:text-fg data-[state=open]:bg-pressed data-[state=open]:text-fg';

/** The crumb's root: which business the app is showing, and the switch to another. */
function WorkspaceSwitcher({ org }: { org: string }) {
  const workspaces = useWorkspaces(org).data ?? [];
  const [active, setActive] = useActiveWorkspace(org);
  const current = workspaces.find((item) => item.id === active);
  return (
    <Menu>
      <MenuTrigger className={cn(chip, '-ml-3 min-w-0 text-muted')} aria-label="Switch workspace">
        {current ? <BusinessMark name={current.name} color={current.color} size={16} /> : null}
        <span className="max-w-32 truncate sm:max-w-48">{current?.name ?? 'All workspaces'}</span>
        <ChevronsUpDown className="size-3 shrink-0 text-faint" aria-hidden="true" />
      </MenuTrigger>
      <MenuContent className="w-64">
        <MenuItem onSelect={() => setActive('')}>
          <span className="flex-1">All workspaces</span>
          <Check className={cn('size-4 text-accent-text', active !== '' && 'invisible')} />
        </MenuItem>
        {workspaces.length ? <MenuSeparator /> : null}
        {workspaces.map((item) => (
          <MenuItem key={item.id} onSelect={() => setActive(item.id)}>
            <BusinessMark name={item.name} color={item.color} />
            <span className="min-w-0 flex-1 truncate">{item.name}</span>
            <Check className={cn('size-4 text-accent-text', item.id !== active && 'invisible')} />
          </MenuItem>
        ))}
      </MenuContent>
    </Menu>
  );
}

const Sep = () => (
  <span aria-hidden="true" className="shrink-0 px-1 text-secondary text-disabled">
    /
  </span>
);

/** The leaf page's id from the address: a run, a check, a machine, an account (`aster/instagram`). */
function useAddressCrumb(): string | null {
  const params = useRouterState({ select: (state) => state.matches.at(-1)?.params as Record<string, string> | undefined });
  if (!params) return null;
  const parts = Object.entries(params)
    .filter(([key]) => key !== 'org' && key !== '_splat')
    .map(([, value]) => value)
    .filter(Boolean);
  return parts.length ? parts.join('/') : null;
}

/**
 * Where you are: the workspace, the place, and the page's own name. The place is a link back when
 * you are deeper in it. On a phone only the workspace shows: the page's title and the tab bar say the rest.
 */
function Crumbs({ org }: { org: string }) {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const place = placeOf(pathname);
  const override = useCurrentCrumb();
  const address = useAddressCrumb();
  const leaf = override ?? (address ? { label: address, mono: true } : null);
  // A place with two lists (Computers) links back to the list the page is in (Machines, for one machine).
  const back = tabOf(pathname)?.to ?? place?.to;
  const atPlace = back ? pathname.replace(/\/$/, '') === back.replace('$org', org).replace(/\/$/, '') : false;
  return (
    <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-1">
      <WorkspaceSwitcher org={org} />
      {place ? (
        <span className="hidden min-w-0 items-center gap-1 md:flex">
          <Sep />
          {atPlace || !leaf ? (
            <span aria-current={atPlace ? 'page' : undefined} className="truncate text-secondary font-semibold text-fg">
              {place.label}
            </span>
          ) : (
            <Link to={back ?? place.to} params={{ org }} className="shrink-0 rounded-sm text-secondary whitespace-nowrap text-muted hover:text-fg">
              {place.label}
            </Link>
          )}
          {leaf && !atPlace ? (
            <>
              <Sep />
              <span aria-current="page" title={leaf.label} className={cn('min-w-0 truncate font-semibold text-fg', leaf.mono ? 'font-mono text-code-caption' : 'text-secondary')}>
                {leaf.label}
              </span>
            </>
          ) : null}
        </span>
      ) : null}
    </nav>
  );
}

/**
 * The event stream: Live while it is open, so a person can trust what is on screen without
 * reloading. A node that refuses the stream has none to lose, so then it says nothing.
 */
function LivePill() {
  const connection = useLiveConnection();
  if (connection === 'none') return null;
  const [tone, words] = connection === 'open' ? (['live', 'Live'] as const) : connection === 'reconnecting' ? (['warn', 'Reconnecting'] as const) : (['idle', 'Connecting'] as const);
  return (
    <span
      role="status"
      className="hidden h-6 shrink-0 items-center gap-2 rounded-full border border-line bg-card px-2 text-caption text-muted shadow-lift sm:inline-flex"
    >
      <StatusDot tone={tone} />
      {words}
    </span>
  );
}

const dark = typeof window !== 'undefined' && typeof window.matchMedia === 'function' ? window.matchMedia('(prefers-color-scheme: dark)') : null;

function useSystemDark() {
  return useSyncExternalStore(
    (listener) => {
      dark?.addEventListener('change', listener);
      return () => dark?.removeEventListener('change', listener);
    },
    () => dark?.matches ?? false,
  );
}

/** One press between light and dark. "Same as this device" stays in the account menu. */
function ThemeButton() {
  const choice = useTheme();
  const systemDark = useSystemDark();
  const isDark = choice === 'dark' || (choice === 'system' && systemDark);
  const label = isDark ? 'Switch to light' : 'Switch to dark';
  return (
    <Tooltip label={label} side="bottom">
      <IconButton label={label} onClick={() => setTheme(isDark ? 'light' : 'dark')}>
        {isDark ? <Sun className="size-[18px]" /> : <Moon className="size-[18px]" />}
      </IconButton>
    </Tooltip>
  );
}


const THEMES: { value: ThemeChoice; label: string; icon: typeof Sun }[] = [
  { value: 'system', label: 'System', icon: Laptop },
  { value: 'light', label: 'Light', icon: Sun },
  { value: 'dark', label: 'Dark', icon: Moon },
];

function ThemeSwitch() {
  const theme = useTheme();
  return (
    <div role="radiogroup" aria-label="Theme" className="flex rounded-full border border-line p-px">
      {THEMES.map(({ value, label, icon: Icon }) => (
        <button
          key={value}
          type="button"
          role="radio"
          aria-checked={theme === value}
          aria-label={label}
          onClick={() => setTheme(value)}
          className={cn(
            'inline-flex size-6 items-center justify-center rounded-full text-faint transition-colors duration-(--duration-fast) ease-standard hover:text-fg',
            theme === value && 'bg-pressed text-fg',
          )}
        >
          <Icon className="size-3" />
        </button>
      ))}
    </div>
  );
}

function AccountMenu({ org }: { org: string }) {
  const user = useSession().data?.user;
  const signOut = useSignOut();
  const navigate = useNavigate();
  return (
    <Menu>
      <MenuTrigger aria-label="Account" className="ml-1 inline-flex size-9 shrink-0 items-center justify-center rounded-full max-md:size-10">
        <Avatar name={user?.name ?? ''} me className="size-8" />
      </MenuTrigger>
      <MenuContent align="end" className="w-64">
        <div className="px-2 py-2">
          <p className="truncate text-body font-medium">{user?.name}</p>
          <p className="truncate text-secondary text-faint">{user?.email}</p>
        </div>
        <MenuSeparator />
        <MenuItem onSelect={() => void navigate({ to: '/$org/settings/account', params: { org } })}>
          <User className="size-4 text-faint" />
          Account
        </MenuItem>
        <MenuItem onSelect={openShortcuts}>
          <Keyboard className="size-4 text-faint" />
          Keyboard shortcuts
        </MenuItem>
        <div className="flex h-10 items-center gap-2 px-2 text-body">
          <Sun className="size-4 text-faint" />
          <span className="flex-1">Theme</span>
          <ThemeSwitch />
        </div>
        <MenuSeparator />
        <MenuItem onSelect={() => signOut.mutate(undefined, { onSuccess: () => void navigate({ to: '/sign-in' }) })}>
          <LogOut className="size-4 text-faint" />
          Sign out
        </MenuItem>
      </MenuContent>
    </Menu>
  );
}

/**
 * The top of the page column: where you are on the left; on the right a freeze if one holds, Live,
 * the keys, the theme, what needs you and your account. There is no clock: the browser's clock is not
 * the machine's, and a number that ticks every second pulls the eye from the one thing allowed to move
 * (work that is running). Times on a page say "2 min ago" and keep themselves fresh.
 */
/** `night`: the page has the night band under the top bar (page-band.ts), so the bar takes the night too. */
export function TopBar({ org, night = false }: { org: string; night?: boolean }) {
  const { role } = useAccess(org);
  return (
    <header
      data-night={night || undefined}
      className={cn(
        'sticky top-0 z-30 flex h-(--topbar-height) items-center gap-2 px-2 backdrop-blur-md sm:px-4 lg:pr-8 lg:pl-10',
        night ? 'top-night' : 'bg-page/85',
      )}
    >
      {/* On a phone the rail is gone, so the org's switch comes up here. */}
      <span className="md:hidden">
        <OrgSwitcher org={org} compact />
      </span>
      <Crumbs org={org} />
      <div className="ml-auto flex shrink-0 items-center gap-1">
        <FrozenChip org={org} />
        <LivePill />
        <span className="md:hidden">
          <IconButton label="Search or run a command" onClick={openPalette} className="max-md:size-10">
            <Search className="size-4" />
          </IconButton>
        </span>
        {/* The machines' state on a phone, where there is no rail to hold its line. */}
        {role !== 'approver' ? (
          <span className="md:hidden">
            <StatusPill org={org} variant="dot" />
          </span>
        ) : null}
        <span className="hidden lg:inline-flex">
          <Tooltip label="Keyboard shortcuts" side="bottom">
            <IconButton label="Keyboard shortcuts" onClick={openShortcuts} aria-keyshortcuts="?">
              <Keyboard className="size-4" />
            </IconButton>
          </Tooltip>
        </span>
        <span className="hidden sm:inline-flex">
          <ThemeButton />
        </span>
        <NotificationsBell org={org} triggerClass={chip} />
        <AccountMenu org={org} />
      </div>
    </header>
  );
}
