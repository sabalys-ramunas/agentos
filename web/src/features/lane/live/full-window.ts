import { useEffect, useState } from 'react';
import { isTyping } from '@/lib/keyboard';

/**
 * The live view filling the window. The page behind it stops scrolling, and Escape leaves,
 * unless the key is headed into the lane or a field. Focus stays on the button that toggled
 * it, which is the same button both ways.
 */
export function useFullWindow() {
  const [expanded, setExpanded] = useState(false);
  useEffect(() => {
    if (!expanded) return;
    const { overflow } = document.body.style;
    document.body.style.overflow = 'hidden';
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !event.defaultPrevented && !isTyping(event)) setExpanded(false);
    };
    window.addEventListener('keydown', onKey);
    return () => {
      document.body.style.overflow = overflow;
      window.removeEventListener('keydown', onKey);
    };
  }, [expanded]);
  return [expanded, setExpanded] as const;
}
