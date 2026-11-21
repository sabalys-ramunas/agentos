/**
 * The agent's name on an environment's live view ("Claude Code"), as the node's own live view page says it
 * (openajar.node.viewer_state.env_agent_name): the name the environment gives, unless that is only "an agent",
 * then the MCP client it came in with, without its version. Never a guess.
 */
const CLIENTS: Record<string, string> = {
  'claude-code': 'Claude Code',
  'claude-ai': 'Claude',
  claude: 'Claude',
  codex: 'Codex',
  'codex-mcp-client': 'Codex',
  cursor: 'Cursor',
  'cursor-vscode': 'Cursor',
  'gemini-cli': 'Gemini CLI',
  vscode: 'VS Code',
  'visual-studio-code': 'VS Code',
  windsurf: 'Windsurf',
  zed: 'Zed',
};
const UNNAMED = new Set(['', 'an agent', 'agent', 'owner']);

export function liveAgentName(agent: { name: string; client?: string | null }): string {
  if (!UNNAMED.has(agent.name.trim().toLowerCase())) return agent.name;
  const words = (agent.client ?? '').trim().split(/\s+/).filter(Boolean);
  if (words.length > 1 && /^\d/.test(words[words.length - 1] ?? '')) words.pop();
  const raw = words.join(' ');
  if (!raw) return agent.name;
  const known = CLIENTS[raw.toLowerCase()];
  if (known) return known;
  const plain = raw.replace(/[-_]+/g, ' ').slice(0, 32).trim();
  return plain.charAt(0).toUpperCase() + plain.slice(1);
}
