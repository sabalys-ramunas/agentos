import type { ReactNode } from 'react';
import { useMemo } from 'react';
import { encode } from 'uqr';

/** Dark on white in both themes, because a phone's camera reads it that way. */
export function Qr({ value, label, className }: { value: string; label: string; className?: string }) {
  const code = useMemo(() => encode(value, { ecc: 'M', border: 2 }), [value]);
  const cells: ReactNode[] = [];
  code.data.forEach((row, y) =>
    row.forEach((dark, x) => {
      if (dark) cells.push(<rect key={`${x}-${y}`} x={x} y={y} width="1.02" height="1.02" />);
    }),
  );
  return (
    <svg role="img" aria-label={label} viewBox={`0 0 ${code.size} ${code.size}`} className={className ?? 'size-36 rounded-lg bg-[#fff]'} fill="#000">
      {cells}
    </svg>
  );
}
