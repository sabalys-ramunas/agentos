/**
 * Stand-in images for lane screenshots and post previews. Plain SVG wireframes, on purpose:
 * no third-party logo or login page is ever drawn, not even as a mock.
 */
function hashText(text: string): number {
  let value = 0;
  for (const char of text) value = (value * 31 + char.charCodeAt(0)) >>> 0;
  return value;
}

export function laneScreenshot(id: string, attention: 'none' | 'captcha' | 'logged_out'): string {
  const seed = hashText(id);
  const rows = [0, 1, 2]
    .map((row) => {
      const y = 96 + row * 196;
      const width = 340 + ((seed >> row) % 5) * 28;
      return `
      <rect x="248" y="${y}" width="472" height="176" rx="10" fill="#fff" stroke="#e8e8e8"/>
      <circle cx="276" cy="${y + 28}" r="12" fill="#e4e4e4"/>
      <rect x="298" y="${y + 18}" width="${120 + (seed % 60)}" height="8" rx="4" fill="#d4d4d4"/>
      <rect x="298" y="${y + 32}" width="72" height="6" rx="3" fill="#ececec"/>
      <rect x="264" y="${y + 56}" width="${width}" height="7" rx="3.5" fill="#e4e4e4"/>
      <rect x="264" y="${y + 72}" width="${width - 60}" height="7" rx="3.5" fill="#e4e4e4"/>
      <rect x="264" y="${y + 96}" width="440" height="64" rx="6" fill="#f2f2f2"/>`;
    })
    .join('');
  const overlay =
    attention === 'none'
      ? ''
      : `<rect width="960" height="600" fill="#000" fill-opacity=".32"/>
         <rect x="320" y="212" width="320" height="176" rx="12" fill="#fff"/>
         <rect x="352" y="244" width="176" height="10" rx="5" fill="#bdbdbd"/>
         <rect x="352" y="268" width="256" height="7" rx="3.5" fill="#e0e0e0"/>
         <rect x="352" y="284" width="208" height="7" rx="3.5" fill="#e0e0e0"/>
         <rect x="352" y="324" width="256" height="32" rx="6" fill="#ececec"/>`;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 600" width="960" height="600">
    <rect width="960" height="600" fill="#f7f7f7"/>
    <rect width="960" height="56" fill="#fff"/><rect y="56" width="960" height="1" fill="#e8e8e8"/>
    <rect x="24" y="20" width="96" height="16" rx="4" fill="#d4d4d4"/>
    <rect x="368" y="16" width="224" height="24" rx="12" fill="#f2f2f2"/>
    <circle cx="908" cy="28" r="12" fill="#e4e4e4"/>
    <rect x="32" y="96" width="160" height="10" rx="5" fill="#e0e0e0"/>
    <rect x="32" y="124" width="120" height="10" rx="5" fill="#e8e8e8"/>
    <rect x="32" y="152" width="140" height="10" rx="5" fill="#e8e8e8"/>
    <rect x="32" y="180" width="104" height="10" rx="5" fill="#e8e8e8"/>
    ${rows}
    <rect x="760" y="96" width="168" height="232" rx="10" fill="#fff" stroke="#e8e8e8"/>
    <rect x="776" y="116" width="96" height="8" rx="4" fill="#d4d4d4"/>
    <rect x="776" y="140" width="136" height="6" rx="3" fill="#ececec"/>
    <rect x="776" y="156" width="120" height="6" rx="3" fill="#ececec"/>
    <rect x="776" y="172" width="128" height="6" rx="3" fill="#ececec"/>
    ${overlay}
  </svg>`;
}

const PALETTES: [string, string, string][] = [
  ['#e8ddcf', '#b08d62', '#5f4a33'],
  ['#d9e2ea', '#7f9cb3', '#3b5468'],
  ['#dbe7e2', '#86ae9f', '#3c6157'],
  ['#e6dee9', '#a48cb3', '#5c4669'],
];

export function postPreview(id: string): string {
  const seed = hashText(id);
  const [paper, mid, ink] = PALETTES[seed % PALETTES.length] as [string, string, string];
  const offset = (seed % 7) * 12;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 640" width="640" height="640">
    <rect width="640" height="640" fill="${paper}"/>
    <circle cx="${420 + offset}" cy="${208 - offset / 2}" r="120" fill="${mid}"/>
    <rect y="424" width="640" height="216" fill="${mid}" fill-opacity=".45"/>
    <path d="M0 ${464 - offset} C160 ${408 + offset} 320 ${512 - offset} 640 ${432 + offset} V640 H0 Z" fill="${ink}" fill-opacity=".82"/>
  </svg>`;
}
