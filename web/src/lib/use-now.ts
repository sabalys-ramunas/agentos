import { useEffect, useState } from 'react';

/** A clock that ticks once a second (or every `everyMs`), but only while something on screen is counting. */
export function useNow(active: boolean, everyMs = 1_000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    // Now, not at mount: a countdown that starts from an old clock reads over its limit.
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), everyMs);
    return () => clearInterval(timer);
  }, [active, everyMs]);
  return now;
}
