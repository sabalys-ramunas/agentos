import { liveAgentName } from './agent-name';

describe("the agent's name on the live view", () => {
  it.each([
    [{ name: 'an agent', client: 'claude-code 2.1.0' }, 'Claude Code'],
    [{ name: 'an agent', client: 'codex-mcp-client' }, 'Codex'],
    [{ name: 'an agent', client: 'my_tool 0.3' }, 'My tool'],
    [{ name: 'an agent', client: null }, 'an agent'],
    [{ name: 'Release bot', client: 'claude-code 2.1.0' }, 'Release bot'],
  ])('reads %j as %s', (agent, shown) => {
    expect(liveAgentName(agent)).toBe(shown);
  });
});
