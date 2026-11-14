import { useSyncExternalStore } from 'react';

/** One open-or-closed flag the shell's parts share: a button in the top bar opens what the shell renders. */
function flag() {
  let open = false;
  const listeners = new Set<() => void>();
  const set = (next: boolean) => {
    open = next;
    listeners.forEach((listener) => listener());
  };
  const use = () =>
    useSyncExternalStore(
      (listener) => {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
      () => open,
    );
  return { set, use };
}

const palette = flag();
export const openPalette = () => palette.set(true);
export const setPaletteOpen = palette.set;
export const usePaletteOpen = palette.use;

const shortcuts = flag();
/** The "?" list of keyboard shortcuts. */
export const openShortcuts = () => shortcuts.set(true);
export const setShortcutsOpen = shortcuts.set;
export const useShortcutsOpen = shortcuts.use;

const freezing = flag();
/** The Freeze dialog: from the command bar, or the status line's popover. */
export const openFreeze = () => freezing.set(true);
export const setFreezeOpen = freezing.set;
export const useFreezeOpen = freezing.use;

const lifting = flag();
/** The Unfreeze dialog: from the command bar, or the Frozen chip in the top bar. */
export const openUnfreeze = () => lifting.set(true);
export const setUnfreezeOpen = lifting.set;
export const useUnfreezeOpen = lifting.use;
