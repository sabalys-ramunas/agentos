import cases from './agent-names.cases.json';
import { agentSaid, agentVia, agentView, agentWhere, productName } from './agent-names';
import { principalName } from './principal';

describe("an agent's name, the server's rule (openajar.core.agent_names)", () => {
  it.each(cases.cases)('reads $subject with $client', ({ subject, client, said, via, where, principal }) => {
    expect(agentView(subject, client)).toEqual({ name: subject, client, said, via, where });
    expect(principalName({ kind: 'agent', id: subject, thread: null })).toBe(principal);
  });

  it('shows what the node sends over its own reading of the raw values', () => {
    const sent = { name: 'claude-code', client: 'claude-code 2.1.328', said: 'Release bot', via: null };
    expect(agentSaid(sent)).toBe('Release bot');
    expect(agentVia(sent)).toBeNull();
  });

  it('never shows a version, a raw host or a long name', () => {
    expect(productName('claude-code 2.1.328')).toBe('Claude Code');
    expect(productName('Claude Code')).toBe('Claude Code');
    expect(productName(null)).toBeNull();
    expect(productName('   ')).toBeNull();
    expect(productName('x'.repeat(200))?.length).toBeLessThanOrEqual(32);
    expect(agentWhere('claude-code@Mac-mini_2')).toBe('Mac mini 2');
    expect(agentWhere('claude-code')).toBeNull();
  });
});
