/**
 * Who and whose: the business mark, people, and the agents that act. Business and people colours
 * are a fill on a small mark, never a surface (DESIGN.md). Agent marks are the agent maker's own
 * artwork, used only to say which agent it was.
 */
import { Bot, Brain, MousePointerClick, Repeat } from 'lucide-react';
import { siClaude } from 'simple-icons';
import { cn } from '@/lib/cn';
import type { KnownAgent, PrincipalLike } from '@/lib/principal';
import { asPrincipal, knownAgent, principalName } from '@/lib/principal';
import { luminance } from './site-mark';
import type { MarkSize } from './site-mark';
import { openai } from './vendored';

/** The eight identity hues, as tokens. */
export const IDENTITY_HUES = ['--ws-1', '--ws-2', '--ws-3', '--ws-4', '--ws-5', '--ws-6', '--ws-7', '--ws-8'] as const;

/** A stable hue for an id, so a person keeps one colour on every screen. */
export function hueFor(id: string): string {
  let hash = 0;
  for (const char of id) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return `var(${IDENTITY_HUES[hash % IDENTITY_HUES.length]})`;
}

const radius = (size: number) => Math.max(4, Math.round(size * 0.26));

/** The first letter of the first two words: "Demo Owner" gives "DO", "Aster Coffee" gives "A". */
export function initials(name: string, letters: 1 | 2 = 1): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  const first = words[0]?.charAt(0) ?? '?';
  const second = letters === 2 ? (words[1]?.charAt(0) ?? '') : '';
  return `${first}${second}`.toUpperCase();
}

/** Ink for a letter on a fill: white unless the fill is light. */
function inkOn(color: string): string {
  const hex = /^#?([0-9a-f]{6})$/i.exec(color)?.[1];
  return hex && luminance(hex) > 0.45 ? '#171717' : '#fff';
}

interface BusinessMarkProps {
  name: string;
  /** The workspace's colour from the API. */
  color?: string | null;
  /** An uploaded logo, when the API carries one. It wins over the initial. */
  logo?: string | null;
  size?: MarkSize;
  /** False when the name is written beside it (the usual case). */
  label?: string | false;
  className?: string;
}

/**
 * A business (workspace): its colour and initial, or its logo, at full strength. The same mark
 * on the rail, a card, an approval and a list, so Aster Coffee is always the bronze A.
 */
export function BusinessMark({ name, color, logo, size = 20, label = false, className }: BusinessMarkProps) {
  const fill = color || hueFor(name);
  const a11y = label === false ? { 'aria-hidden': true as const } : { role: 'img', 'aria-label': label };
  return (
    <span
      {...a11y}
      data-business={name}
      style={{
        width: size,
        height: size,
        borderRadius: radius(size),
        backgroundColor: logo ? undefined : fill,
        color: logo ? undefined : inkOn(fill),
        fontSize: Math.round(size * 0.5),
      }}
      className={cn(
        'inline-flex shrink-0 items-center justify-center overflow-hidden leading-none font-semibold',
        'shadow-[inset_0_0_0_1px_rgb(255_255_255/0.08)]',
        logo && 'border border-line bg-card',
        className,
      )}
    >
      {logo ? <img src={logo} alt="" className="size-full object-cover" /> : initials(name)}
    </span>
  );
}

/** A person: their photo when there is one, else initials on their own hue. */
export function PersonMark({ id, name, photo, size = 20, label = false, className }: { id: string; name: string; photo?: string | null; size?: MarkSize; label?: string | false; className?: string }) {
  const a11y = label === false ? { 'aria-hidden': true as const } : { role: 'img', 'aria-label': label };
  return (
    <span
      {...a11y}
      style={{ width: size, height: size, backgroundColor: photo ? undefined : hueFor(id), fontSize: Math.round(size * 0.42) }}
      className={cn('inline-flex shrink-0 items-center justify-center overflow-hidden rounded-full leading-none font-semibold text-identity-ink', className)}
    >
      {photo ? <img src={photo} alt="" className="size-full object-cover" /> : initials(name, 2)}
    </span>
  );
}

/** The agents' own marks on their own tiles. Codex has no public mark in a free set, so it is drawn. */
function AgentArt({ agent, size }: { agent: KnownAgent; size: number }) {
  const inner = Math.round(size * 0.66);
  if (agent === 'claude' || agent === 'claude-code') {
    return (
      <svg viewBox="0 0 24 24" width={inner} height={inner} aria-hidden="true" fill="#fff">
        <path d={siClaude.path} />
      </svg>
    );
  }
  if (agent === 'chatgpt') {
    return (
      <svg viewBox={openai.viewBox} width={inner} height={inner} aria-hidden="true" fill="#fff">
        <path d={openai.path} />
      </svg>
    );
  }
  // Codex: a terminal prompt, our drawing, labelled Codex by its name beside it.
  return (
    <svg viewBox="0 0 24 24" width={inner} height={inner} aria-hidden="true" fill="none" stroke="#fff" strokeWidth={2.25} strokeLinecap="round" strokeLinejoin="round">
      <path d="M5 7l5 5-5 5" />
      <path d="M13 17h6" />
    </svg>
  );
}

const AGENT_TILE: Record<KnownAgent, string> = {
  claude: `#${siClaude.hex}`,
  'claude-code': `#${siClaude.hex}`,
  chatgpt: '#000',
  codex: '#000',
};

interface AgentMarkProps {
  /** Who acted: an agent, a routine, a person, or a model. */
  principal: PrincipalLike;
  /** The person's name, for a person's initials. Defaults to `principalName`. */
  name?: string;
  size?: MarkSize;
  label?: string | false;
  className?: string;
}

/**
 * Who acted, as a mark: Claude and Claude Code on the coral tile, ChatGPT's blossom on black,
 * Codex's prompt on black, a routine as our repeat glyph, a person as their initials.
 */
export function AgentMark({ principal, name, size = 20, label = false, className }: AgentMarkProps) {
  const who = asPrincipal(principal);
  const display = name ?? principalName(who);
  if (who.kind === 'human') return <PersonMark id={who.id} name={display} size={size} label={label} className={className} />;
  const agent = knownAgent(who);
  const a11y = label === false ? { 'aria-hidden': true as const } : { role: 'img', 'aria-label': label ?? display };
  const box = { width: size, height: size, borderRadius: radius(size) };
  if (agent) {
    return (
      <span {...a11y} data-agent={agent} style={{ ...box, backgroundColor: AGENT_TILE[agent] }} className={cn('inline-flex shrink-0 items-center justify-center shadow-[inset_0_0_0_1px_rgb(255_255_255/0.12)]', className)}>
        <AgentArt agent={agent} size={size} />
      </span>
    );
  }
  const Icon = who.kind === 'flow' ? Repeat : who.kind === 'brain' ? Brain : who.kind === 'worker' ? MousePointerClick : Bot;
  return (
    <span {...a11y} data-agent={who.kind === 'flow' ? 'routine' : who.kind} style={box} className={cn('inline-flex shrink-0 items-center justify-center border border-line bg-inset text-muted', className)}>
      <Icon width={Math.round(size * 0.6)} height={Math.round(size * 0.6)} strokeWidth={2} />
    </span>
  );
}
