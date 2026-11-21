/**
 * Who did something, in words. The API sends a principal as `{ kind, id, thread }` on approvals,
 * questions, runs and (live) traces, and as the bare id on topology events and some mock data.
 * Nothing renders either form raw: every screen goes through `principalName`.
 */
import type { Principal } from '@/api/types';
import { SITES, siteName } from './sites';

export type PrincipalLike = Principal | string;

/** The agents we know by name. Their marks are in `components/brand/agent-mark`. */
export type KnownAgent = 'claude-code' | 'claude' | 'codex' | 'chatgpt';

const AGENT_IDS: Record<string, KnownAgent> = {
  'claude-code': 'claude-code',
  claude: 'claude',
  'claude-app': 'claude',
  'claude-desktop': 'claude',
  'claude-ai': 'claude',
  codex: 'codex',
  'codex-cli': 'codex',
  chatgpt: 'chatgpt',
  'chatgpt-app': 'chatgpt',
  openai: 'chatgpt',
};

/** A member's id as the node writes it, such as `m_w4htizzaz6vmeev5`: never shown as words. */
const MEMBER_ID = /^m_[a-z0-9]+$/;

export const AGENT_NAMES: Record<KnownAgent, string> = {
  'claude-code': 'Claude Code',
  claude: 'Claude',
  codex: 'Codex',
  chatgpt: 'ChatGPT',
};

/** The same rule the cloud uses (`fleet/shapes.py principal_out`): the id's form says the kind. */
function kindOf(id: string): Principal['kind'] {
  if (id.startsWith('usr_') || MEMBER_ID.test(id)) return 'human';
  if (id === 'flow' || id.includes('.')) return 'flow';
  return 'agent';
}

export function asPrincipal(principal: PrincipalLike): Principal {
  if (typeof principal === 'string') return { kind: kindOf(principal), id: principal, thread: null };
  return principal;
}

export const principalId = (principal: PrincipalLike): string => (typeof principal === 'string' ? principal : principal.id);

/**
 * An agent named for the computer it runs on: `openajar connect` names a grant `claude-code@alex-macbook-pro-3`.
 * The part before `@` is the agent, the rest is where it runs.
 */
function agentParts(id: string): { base: string; host: string } {
  const at = id.indexOf('@');
  return at < 0 ? { base: id, host: '' } : { base: id.slice(0, at), host: id.slice(at + 1) };
}

export function knownAgent(principal: PrincipalLike): KnownAgent | null {
  const { kind, id } = asPrincipal(principal);
  if (kind !== 'agent') return null;
  return AGENT_IDS[agentParts(id).base.toLowerCase()] ?? null;
}

const VERB_NOUNS: Record<string, string> = {
  post: 'post',
  publish: 'post',
  check: 'check',
  reply: 'reply',
  replies: 'reply',
  read: 'check',
  sync: 'sync',
  digest: 'summary',
  summary: 'summary',
};

const tidy = (word: string) => word.replaceAll(/[-_]/g, ' ').trim();
const capital = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

/**
 * A routine's name from its flow id. Flow ids read `<workspace>.<site>.<verb>`, so
 * `aster.instagram.post` gives "Instagram post", and `weekly-posts` gives "Weekly posts".
 */
export function routineName(flowId: string): string {
  const id = flowId.replace(/^flow:/, '').replace(/@.*$/, '');
  const parts = id.split('.').filter(Boolean);
  if (parts.length >= 3) {
    const [, site = '', verb = ''] = parts;
    const where = SITES.has(site) ? siteName(site) : capital(tidy(site));
    return `${where} ${VERB_NOUNS[verb] ?? tidy(verb)}`.trim();
  }
  if (parts.length === 2) return capital(`${tidy(parts[0] ?? '')} ${tidy(parts[1] ?? '')}`.trim());
  if (!id || id === 'flow') return 'A routine';
  return capital(tidy(id));
}

export interface PeopleLookup {
  /** The signed-in person, who reads as "You". */
  me?: { id: string; name: string } | null;
  /** Everyone else in the org, by user id. */
  people?: ReadonlyMap<string, string>;
}

export interface NameOptions extends PeopleLookup {
  /**
   * In a sentence ("Asked by the Instagram post routine") or on its own ("Instagram post
   * routine"). On its own is the default.
   */
  inSentence?: boolean;
}

/**
 * Names a principal in words: "Claude Code", "Codex", "the weekly posts routine", "Demo Owner",
 * "You". Never an object, never a bare `usr_` id.
 */
export function principalName(principal: PrincipalLike | null | undefined, options: NameOptions = {}): string {
  if (!principal) return options.inSentence ? 'someone' : 'Someone';
  const { kind, id } = asPrincipal(principal);
  switch (kind) {
    case 'agent': {
      // An agent that acts on the owner's own token is recorded as "owner": it is an agent, never the owner.
      if (!id || id.toLowerCase() === 'owner') return options.inSentence ? 'an agent' : 'An agent';
      const { base, host } = agentParts(id);
      const known = AGENT_IDS[base.toLowerCase()];
      const name = known ? AGENT_NAMES[known] : capital(tidy(base)) || 'An agent';
      const where = tidy(host).replaceAll(/\s+/g, ' ');
      return where ? `${name} (on ${where})` : name;
    }
    case 'worker':
      return options.inSentence ? `the ${id} worker` : `The ${id} worker`;
    case 'flow': {
      const name = `${routineName(id)} routine`.replace(/^A routine routine$/, 'A routine');
      if (name === 'A routine') return options.inSentence ? 'a routine' : name;
      return options.inSentence ? `the ${name.charAt(0).toLowerCase()}${name.slice(1)}` : name;
    }
    case 'human': {
      if (options.me && options.me.id === id) return options.inSentence ? 'you' : 'You';
      if (id === 'owner') return options.inSentence ? 'the owner' : 'The owner';
      // The node names a member in words on the principal; the org's list is the other way to know them.
      const named = options.people?.get(id) ?? (typeof principal === 'string' ? null : principal.name);
      if (named) return named;
      // A member's id is never shown: the app says who, as far as it knows.
      if (MEMBER_ID.test(id)) return options.inSentence ? 'someone on your team' : 'Someone on your team';
      // A display name already (mock data and lane control send these), not an id.
      if (id && !id.startsWith('usr_')) return id;
      return options.inSentence ? 'a person' : 'A person';
    }
    case 'brain':
      return options.inSentence ? 'the AI model' : 'The AI model';
  }
  // A kind the app has no words for: the node itself asks some questions (its memory watchdog is `node`).
  return options.inSentence ? 'this computer' : 'This computer';
}
