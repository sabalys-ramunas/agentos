/**
 * An agent's name as a person reads it, the server's rule (openajar.core.agent_names) for the few places that
 * have only the raw values: a principal's id (`claude-code@alex-macbook-pro-3`) and an older node's
 * environment, which sends no `said`. A known client by its product name, an unknown one cleaned up, never a
 * version, and the computer only as words. Both sides pass the cases in agent-names.cases.json.
 */

import type { AgentNamed } from '@/api/types';

/** What an agent or its MCP client calls itself (grant subject or clientInfo.name), as a person knows it. */
const KNOWN: Record<string, string> = {
  'claude-code': 'Claude Code',
  claude: 'Claude',
  'claude-ai': 'Claude',
  'claude-app': 'Claude',
  'claude-desktop': 'Claude',
  codex: 'Codex',
  'codex-cli': 'Codex',
  'codex-mcp-client': 'Codex',
  chatgpt: 'ChatGPT',
  'chatgpt-app': 'ChatGPT',
  openai: 'ChatGPT',
  t3: 'T3 Code',
  't3-code': 'T3 Code',
  t3code: 'T3 Code',
  cursor: 'Cursor',
  'cursor-vscode': 'Cursor',
  'gemini-cli': 'Gemini CLI',
  vscode: 'VS Code',
  'visual-studio-code': 'VS Code',
  windsurf: 'Windsurf',
  zed: 'Zed',
};
/** Names that say no one in particular: the environment's default, and the owner's own token. */
const UNNAMED = new Set(['', 'an agent', 'agent', 'owner']);
const MAX_NAME = 32;

const plain = (text: string) =>
  text
    .replaceAll(/[-_]/g, ' ')
    .replaceAll(/[\p{Cc}\p{Cf}]/gu, '')
    .split(/\s+/)
    .filter(Boolean)
    .join(' ');

/** "claude-code 2.1.328", "claude-code" or "claude-code@mac-mini" as "Claude Code"; null when it says no one. */
export function productName(raw: string | null | undefined): string | null {
  if (typeof raw !== 'string') return null;
  const words = (raw.split('@')[0] ?? '').split(/\s+/).filter(Boolean);
  if (words.length > 1 && /^\d/.test(words[words.length - 1] ?? '')) words.pop();
  const base = words.join(' ');
  if (UNNAMED.has(base.trim().toLowerCase())) return null;
  const known = KNOWN[base.toLowerCase().replaceAll('_', '-').split(/\s+/).join('-')];
  if (known) return known;
  const name = plain(base).slice(0, MAX_NAME).trim();
  return name ? name.charAt(0).toUpperCase() + name.slice(1) : null;
}

/** The computer a grant was made for, in words: `claude-code@alex-macbook-pro-3` is "alex macbook pro 3". */
export function agentWhere(subject: string | null | undefined): string | null {
  if (typeof subject !== 'string' || !subject.includes('@')) return null;
  return plain(subject.slice(subject.indexOf('@') + 1)).slice(0, MAX_NAME).trim() || null;
}


/** Who an agent is in one name ("Claude Code"): its subject's product, else its client's, else "An agent". */
export function agentSaid(agent: AgentNamed): string {
  return agent.said || productName(agent.name) || productName(agent.client) || 'An agent';
}

/** The app it came in through, when that is another product than the agent ("T3 Code"), else null. */
export function agentVia(agent: AgentNamed): string | null {
  if (agent.said) return agent.via ?? null;
  const came = productName(agent.name) ? productName(agent.client) : null; // known only by its client: none
  return came && came !== agentSaid(agent) ? came : null;
}

/** An environment's `agent` as the node sends it, from the raw subject and client (the mocks build it here). */
export function agentView(subject: string, client: string | null): Required<AgentNamed> {
  const raw = { name: subject, client };
  return { name: subject, client, said: agentSaid(raw), via: agentVia(raw), where: agentWhere(subject) };
}
