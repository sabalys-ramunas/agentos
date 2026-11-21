/**
 * Offers to install openajar once it has earned it: after the person has decided something here,
 * never on a first visit, and not again for a month after "Not now". On iPhone and iPad, where
 * there is no install dialog, it says the two taps instead.
 */
import { Share, X } from 'lucide-react';
import { useSyncExternalStore } from 'react';
import { createStored } from '@/lib/store';
import { useInstall } from '@/lib/pwa';
import { Button, IconButton } from './ui/button';
import { Logo } from './ui/misc';

const MONTH = 30 * 86_400_000;
const dismissed = createStored<string>('agentos.install.dismissed-at', '');

const EARNED = 'agentos.install.earned';
const earnedListeners = new Set<() => void>();

/** Called after a decision. The prompt may show from then on, in this tab. */
export function markInstallMoment() {
  try {
    sessionStorage.setItem(EARNED, '1');
  } catch {
    // Private mode: the prompt simply does not show.
  }
  earnedListeners.forEach((listener) => listener());
}

function useEarned() {
  return useSyncExternalStore(
    (listener) => {
      earnedListeners.add(listener);
      return () => earnedListeners.delete(listener);
    },
    () => {
      try {
        return sessionStorage.getItem(EARNED) === '1';
      } catch {
        return false;
      }
    },
    () => false,
  );
}

export function InstallPrompt() {
  const install = useInstall();
  const earned = useEarned();
  const at = Number(dismissed.use() || 0);
  const resting = at > 0 && Date.now() - at < MONTH;
  if (!earned || resting || install.installed || (!install.canPrompt && !install.manual)) return null;
  const later = () => dismissed.set(String(Date.now()));
  return (
    <aside aria-label="Install openajar" className="mb-6 flex animate-fade-in items-start gap-4 rounded-xl border border-line bg-inset p-4">
      <Logo className="size-8 shrink-0" />
      <div className="min-w-0 flex-1">
        <p className="text-label">Install openajar</p>
        <p className="mt-1 text-secondary text-muted">
          {install.manual ? (
            <>
              Tap <Share className="inline size-4 align-text-bottom" aria-label="Share" /> then Add to Home Screen. Approvals open in their own window, and push can
              reach this phone.
            </>
          ) : (
            'Approvals open in their own window, and push reaches you even when the browser is closed.'
          )}
        </p>
        {install.canPrompt ? (
          <div className="mt-3 flex gap-2">
            <Button size="sm" variant="ghost" onClick={later}>
              Not now
            </Button>
            <Button size="sm" onClick={() => void install.install().then((outcome) => outcome === 'dismissed' && later())}>
              Install
            </Button>
          </div>
        ) : null}
      </div>
      {install.manual ? (
        <IconButton label="Not now" size="sm" onClick={later}>
          <X className="size-4" />
        </IconButton>
      ) : null}
    </aside>
  );
}
