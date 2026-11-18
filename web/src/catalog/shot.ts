/**
 * A drawn product page for the catalog's picture components: another brand's page, so its colours are
 * data, not this app's style (the catalog marks it data-studio="ignore", and the design rules list
 * this file as a reviewed exception).
 */
export function shot(version: 'before' | 'after'): string {
  const badge = version === 'before' ? '#4d8dff' : '#2563eb';
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="360" height="480" viewBox="0 0 360 480">
<rect width="360" height="480" fill="#ffffff"/>
<rect x="0" y="0" width="360" height="36" fill="#f4f4f5"/>
<rect x="12" y="12" width="64" height="12" rx="3" fill="#18181b"/>
<rect x="24" y="56" width="312" height="220" rx="12" fill="#fde7dc"/>
<ellipse cx="180" cy="190" rx="110" ry="46" fill="#ef6c3a"/>
<rect x="24" y="300" width="96" height="28" rx="14" fill="${badge}"/>
<rect x="36" y="310" width="72" height="8" rx="4" fill="#ffffff"/>
<rect x="132" y="304" width="160" height="12" rx="6" fill="#18181b"/>
<g fill="#e4e4e7">${[0, 1, 2, 3, 4, 5].map((i) => `<rect x="${24 + i * 52}" y="360" width="44" height="40" rx="8"/>`).join('')}</g>
<rect x="24" y="420" width="312" height="44" rx="10" fill="${badge}"/>
</svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}
