import { useNavigate } from '@tanstack/react-router';
import { useEffect, useRef } from 'react';
import { isTyping } from '@/lib/keyboard';
import { Dialog } from '../ui/dialog';
import { Kbd } from '../ui/misc';
import { useAccess } from '@/api/hooks';
import { menuFor, OLD_KEYS } from './nav';
import { setShortcutsOpen, useShortcutsOpen } from './palette-store';

const SEQUENCE_MS = 1_000;

const LANE_KEYS: [string[], string][] = [[['T'], 'Take over, or hand back']];

const NEEDS_YOU_KEYS: [string[], string][] = [
  [['J'], 'Next item'],
  [['K'], 'Previous item'],
  [['A'], 'Approve'],
  [['D'], 'Deny'],
  [['X'], 'Select'],
  [['H'], 'Show the content hash'],
];

/** A key a list had before it became a tab (g m, Machines), for a role that has its place. */
function oldKey(menu: ReturnType<typeof menuFor>['all'], key: string) {
  const to = OLD_KEYS[key];
  return to && menu.some((item) => item.tabs?.some((tab) => tab.to === to)) ? to : undefined;
}

function Row({ keys, label }: { keys: string[]; label: string }) {
  return (
    <div className="flex h-8 items-center justify-between text-body">
      <span className="text-muted">{label}</span>
      <span className="flex gap-1">
        {keys.map((key) => (
          <Kbd key={key}>{key}</Kbd>
        ))}
      </span>
    </div>
  );
}

/**
 * "g then a letter" goes to a place (the letters are in nav.ts, shown in the rail and the command
 * bar), and "?" opens the list of shortcuts.
 */
export function Shortcuts({ org }: { org: string }) {
  const navigate = useNavigate();
  const help = useShortcutsOpen();
  const awaiting = useRef(false);
  const menu = menuFor(useAccess(org).role).all;
  // Read inside the key listener, so a role change needs no new listener.
  const menuRef = useRef(menu);
  menuRef.current = menu;

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    const onKey = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey || isTyping(event)) return;
      if (awaiting.current) {
        awaiting.current = false;
        clearTimeout(timer);
        // The key after g belongs to the sequence even when it is a wrong one. If it fell
        // through, "g d" would deny whatever is focused in Needs you.
        event.preventDefault();
        event.stopImmediatePropagation();
        const target = menuRef.current.find((item) => item.key === event.key)?.to ?? oldKey(menuRef.current, event.key);
        if (target) void navigate({ to: target, params: { org } });
        return;
      }
      if (event.key === 'g') {
        awaiting.current = true;
        timer = setTimeout(() => {
          awaiting.current = false;
        }, SEQUENCE_MS);
      } else if (event.key === '?') {
        setShortcutsOpen(true);
      }
    };
    // Capture, so a pending "g" wins over a screen's own single-key shortcut.
    window.addEventListener('keydown', onKey, { capture: true });
    return () => {
      clearTimeout(timer);
      window.removeEventListener('keydown', onKey, { capture: true });
    };
  }, [navigate, org]);

  return (
    <Dialog open={help} onOpenChange={setShortcutsOpen} title="Keyboard shortcuts">
      <div className="grid gap-6">
        <div>
          <h3 className="mb-2 text-secondary text-faint">Anywhere</h3>
          <Row keys={['⌘', 'K']} label="Search or run a command" />
          <Row keys={['?']} label="This list" />
          {menu.map((item) => (
            <Row key={item.to} keys={['G', item.key.toUpperCase()]} label={`Go to ${item.label}`} />
          ))}
        </div>
        <div>
          <h3 className="mb-2 text-secondary text-faint">Needs you</h3>
          {NEEDS_YOU_KEYS.map(([keys, label]) => (
            <Row key={label} keys={keys} label={label} />
          ))}
        </div>
        <div>
          <h3 className="mb-2 text-secondary text-faint">An account</h3>
          {LANE_KEYS.map(([keys, label]) => (
            <Row key={label} keys={keys} label={label} />
          ))}
        </div>
      </div>
    </Dialog>
  );
}
