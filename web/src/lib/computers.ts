/**
 * Computers and AI models in words. The API has no display name for either yet, so a name is
 * read from the id the way a person would say it: `home-mini` is "Mac mini (home)", `hetzner-1`
 * is "Cloud server", `claude-max` is "Claude (Max plan)". The id itself stays behind Technical
 * details.
 */

const CLOUD = new Set(['hetzner', 'aws', 'gcp', 'azure', 'do', 'digitalocean', 'linode', 'vultr', 'ovh', 'cloud', 'server', 'vps', 'ec2', 'fly']);
const PLACES = new Set(['home', 'office', 'studio', 'shop', 'work']);

const capital = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

export function computerName(id: string): string {
  const words = id.toLowerCase().split(/[-_.\s]+/).filter(Boolean);
  const place = words.find((word) => PLACES.has(word));
  const number = words.find((word) => /^\d+$/.test(word));
  const extra = number && number !== '1' ? ` ${number}` : '';
  if (words.includes('mini') || words.includes('macmini')) return `Mac mini${extra}${place ? ` (${place})` : ''}`;
  if (words.includes('mbp') || words.includes('macbook')) return `MacBook${extra}${place ? ` (${place})` : ''}`;
  if (words.includes('imac')) return `iMac${extra}${place ? ` (${place})` : ''}`;
  if (words.includes('pi') || words.includes('raspberry')) return `Raspberry Pi${extra}${place ? ` (${place})` : ''}`;
  if (words.some((word) => CLOUD.has(word))) return `Cloud server${extra}`;
  return capital(words.join(' ')) || 'A computer';
}

const PLANS: Record<string, string> = { max: 'Max plan', pro: 'Pro plan', team: 'Team plan', plus: 'Plus plan', api: 'API' };

/** "Claude (Max plan)", "ChatGPT (Plus plan)", "Local AI". */
export function aiName(name: string, kind?: string | null): string {
  const words = name.toLowerCase().split(/[-_.\s]+/).filter(Boolean);
  if (kind === 'local' || words.some((word) => ['ollama', 'local', 'llama', 'lmstudio'].includes(word))) return 'Local AI';
  const plan = words.map((word) => PLANS[word]).find(Boolean);
  const maker = words.includes('claude') || words.includes('anthropic') ? 'Claude' : words.some((word) => ['openai', 'chatgpt', 'gpt'].includes(word)) ? 'ChatGPT' : words.includes('gemini') ? 'Gemini' : null;
  if (maker) return plan ? `${maker} (${plan})` : maker;
  return capital(words.join(' ')) || 'An AI model';
}
