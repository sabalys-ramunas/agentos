/**
 * Step 4: connect your AI (startup.md, `#s4`). Claude, ChatGPT, Claude Code and Codex by name,
 * with their marks and pictures of each step. Honest about today: the Claude and ChatGPT apps
 * connect with one link once our hosted connection is live, so they say Coming soon and show how
 * it will look. Claude Code and Codex work now, with a connection that asks first exactly as the
 * accounts step said.
 */
import { useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useCreateGrant, useStatus } from '@/api/hooks';
import { isNotServed } from '@/api/http';
import { keys } from '@/api/keys';
import { AgentMark } from '@/components/brand/identity';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { CodeBlock } from '@/components/ui/misc';
import { cn } from '@/lib/cn';
import { describeError } from '@/lib/errors';
import type { KnownAgent } from '@/lib/principal';
import { AGENT_NAMES } from '@/lib/principal';
import type { StepContext } from './context';
import { StepFooter } from './context';
import { Ready, SetupLayout } from './layout';
import { AllowPicture, DoneRow, MenuPicture, PastePicture, PictureStep, TerminalPicture } from './pictures';
import { ceilingOf } from './step-accounts';
import { DEFAULT_ASKS, agentName, itemOf, mayWords, remember } from './setup';
import { InlineError } from '@/components/ui/states';

interface Tile {
  key: KnownAgent;
  where: string;
  soon: boolean;
}

const TILES: Tile[] = [
  { key: 'claude', where: 'App or claude.ai', soon: true },
  { key: 'chatgpt', where: 'App or chatgpt.com', soon: true },
  { key: 'claude-code', where: 'In a terminal', soon: false },
  { key: 'codex', where: 'In a terminal', soon: false },
];

/** What each agent's own settings call the place a connection goes. */
const APP_STEPS: Record<'claude' | 'chatgpt', { menu: string[]; lit: string; open: string; add: string }> = {
  claude: { menu: ['Profile', 'Appearance', 'Connectors', 'Billing'], lit: 'Connectors', open: 'In Claude, open Settings, then Connectors', add: 'Click Add custom connector and paste the link' },
  chatgpt: { menu: ['General', 'Personalization', 'Connectors', 'Security'], lit: 'Connectors', open: 'In ChatGPT, open Settings, then Connectors', add: 'Click Create and paste the link' },
};

function commandFor(agent: KnownAgent, url: string, token: string): string {
  if (agent === 'codex') return `export AGENTOS_TOKEN=${token}\ncodex mcp add agentos --url ${url} --bearer-token-env-var AGENTOS_TOKEN`;
  return `claude mcp add --transport http agentos ${url} --header "Authorization: Bearer ${token}"`;
}

export function AiStep({ ctx }: { ctx: StepContext }) {
  const { org, setup, workspace, business } = ctx;
  const client = useQueryClient();
  const grant = useCreateGrant(org, { inline: true });
  const [agent, setAgent] = useState<KnownAgent>('claude-code');
  const [token, setToken] = useState<string | null>(null);
  const tile = TILES.find((item) => item.key === agent) ?? { key: agent, where: '', soon: false };
  const name = AGENT_NAMES[agent];
  // On a computer the agent connects to the computer itself. A cloud sends no address, so it gets its own.
  const url = useStatus(org).data?.mcp_url ?? `https://mcp.agentos.cloud/o/${org}/mcp`;
  const item = itemOf(setup, 'ai');
  const connected = item?.done ?? false;
  const asks = setup?.asks ?? DEFAULT_ASKS;
  const businessName = business?.name ?? 'your business';

  const connect = () =>
    grant.mutate(
      { subject: agent, workspaces: workspace ? [workspace.id] : [], ceiling: ceilingOf(asks), specials: [], ttl: '30d' },
      {
        onSuccess: (result) => {
          setToken(result.token);
          remember(org, { agent });
          void client.invalidateQueries({ queryKey: keys.setup(org) });
        },
      },
    );

  const may = mayWords(asks);

  return (
    <SetupLayout
      org={org}
      business={business}
      step="ai"
      setup={setup}
      eyebrow={connected ? <Ready>{agentName(setup) ?? 'Your AI'} is connected</Ready> : undefined}
      title="Connect your AI"
      description="agentos is the computer. Your AI does the work. Connect the one you already use."
      footer={<StepFooter ctx={ctx} step="ai" note={token || connected ? 'Next, give it a first job.' : 'You can connect more than one, any time, in Settings.'} later={token || connected ? false : undefined} />}
    >
      <div className="grid gap-6">
        <div role="radiogroup" aria-label="Your AI" className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {TILES.map((item) => {
            const on = item.key === agent;
            return (
              <button
                key={item.key}
                type="button"
                role="radio"
                aria-checked={on}
                onClick={() => {
                  setAgent(item.key);
                  setToken(null);
                }}
                className={cn(
                  'flex items-center gap-3 rounded-xl border bg-bg p-3 text-left transition-[border-color,box-shadow] duration-(--duration-fast)',
                  on ? 'border-fg shadow-[0_0_0_1px_var(--foreground)]' : 'border-line hover:border-line-strong',
                )}
              >
                <AgentMark principal={{ kind: 'agent', id: item.key, thread: null }} size={32} />
                <span className="min-w-0">
                  <span className="block truncate text-14 font-medium">{AGENT_NAMES[item.key]}</span>
                  <span className="block truncate text-12 text-muted">{item.soon ? 'Coming soon' : item.where}</span>
                </span>
              </button>
            );
          })}
        </div>

        {tile.soon ? (
          <div className="grid gap-4 rounded-xl border border-dashed border-line-strong p-4 sm:p-6">
            <p className="text-14">
              <Badge className="mr-2">Coming soon</Badge>
              <span className="text-muted">
                {name} will connect with one link and an Allow button, no token to keep. Here is how it will look. Until then, Claude Code or Codex works today.
              </span>
            </p>
            <ol aria-label={`How ${name} will connect`} className="grid gap-3 opacity-70 sm:grid-cols-3">
              <PictureStep n={1} picture={<MenuPicture items={APP_STEPS[agent as 'claude' | 'chatgpt'].menu} lit="Connectors" />}>
                {APP_STEPS[agent as 'claude' | 'chatgpt'].open}
              </PictureStep>
              <PictureStep n={2} picture={<PastePicture label="Add custom connector" value={url.replace(/^https?:\/\//, '')} />}>
                {APP_STEPS[agent as 'claude' | 'chatgpt'].add}
              </PictureStep>
              <PictureStep n={3} picture={<AllowPicture agent={name} business={businessName} />}>
                Click Connect, then Allow
              </PictureStep>
            </ol>
            <Button className="justify-self-start" onClick={() => setAgent('claude-code')}>
              Use Claude Code for now
            </Button>
          </div>
        ) : (
          <div className="grid gap-4 rounded-xl border border-line p-4 sm:p-6">
            <ol aria-label={`How to connect ${name}`} className="grid gap-3 sm:grid-cols-3">
              <PictureStep n={1} picture={<AllowPicture agent={name} business={businessName} />}>
                Make a connection for {name}
              </PictureStep>
              <PictureStep n={2} picture={<TerminalPicture />}>
                Paste one line where you run {name}
              </PictureStep>
              <PictureStep n={3} picture={<MenuPicture items={['agentos', 'GitHub', 'Files']} lit="agentos" />}>
                Start a new {name} session
              </PictureStep>
            </ol>
            {isNotServed(grant.error) ? (
              <p className="rounded-lg border border-line bg-subtle px-4 py-3 text-14 text-muted">Connecting an AI is not switched on here yet. You can do it later in Settings.</p>
            ) : token ? (
              <div className="grid gap-3">
                <DoneRow>
                  Connection made for {name}. {may}
                </DoneRow>
                <CodeBlock code={commandFor(agent, url, token)} label="Copy command" />
                <p className="text-13 text-faint">This line holds a secret and is shown once. Paste it now; you can always make a new one in Settings.</p>
              </div>
            ) : (
              <div className="flex flex-wrap items-center gap-3">
                <Button variant="primary" size="lg" loading={grant.isPending} disabled={!setup || !workspace} onClick={connect}>
                  Make a connection for {name}
                </Button>
                <p className="min-w-0 flex-1 text-13 text-muted">
                  {may}
                </p>
              </div>
            )}
            {grant.isError && !isNotServed(grant.error) ? (
              <InlineError>
                {describeError(grant.error)}
              </InlineError>
            ) : null}
          </div>
        )}
      </div>
    </SetupLayout>
  );
}
