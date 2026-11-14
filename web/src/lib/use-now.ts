import { useEffect, useState } from 'react';

/** A clock that ticks once a second, but only while something on screen is counting. */
export function useNow(active: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    // Now, not at mount: a countdown that starts from an old clock reads over its limit.
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(timer);
  }, [active]);
  return now;
}
