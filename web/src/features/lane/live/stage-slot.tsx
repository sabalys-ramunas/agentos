/**
 * On a stage (an environment's live screen), the screen's own tools (zoom back to fit, the keyboard field,
 * moving the picture) sit in the row under it, beside the control pill, rather than in a toolbar of their own.
 * The row is the control frame's; the tools are the screen's. The frame gives the row's spot here, and the
 * screen draws its tools into it.
 */
import { createContext, useContext } from 'react';
import type { ReactNode } from 'react';
import { createPortal } from 'react-dom';

export const StageToolsSlot = createContext<HTMLElement | null>(null);

/** Draws its children in the stage's row, or nowhere when the screen is not on a stage. */
export function StageTools({ children }: { children: ReactNode }) {
  const slot = useContext(StageToolsSlot);
  return slot ? createPortal(children, slot) : null;
}
