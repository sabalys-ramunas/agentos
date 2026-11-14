import { asPrincipal, knownAgent, principalId, principalName, routineName } from './principal';

describe('principalName', () => {
  it('names the agents we know', () => {
    expect(principalName({ kind: 'agent', id: 'claude-code', thread: 'T_01M6' })).toBe('Claude Code');
    expect(principalName('claude-app')).toBe('Claude');
    expect(principalName('codex')).toBe('Codex');
    expect(principalName({ kind: 'agent', id: 'chatgpt-app' })).toBe('ChatGPT');
  });

  it('names an agent connected from a computer by the agent, then the computer', () => {
    const agent = { kind: 'agent' as const, id: 'claude-code@alex-macbook-pro-3', thread: null };
    expect(principalName(agent)).toBe('Claude Code (on alex macbook pro 3)');
    expect(knownAgent(agent)).toBe('claude-code');
    expect(principalName({ kind: 'agent', id: 'my-bot@vm2' })).toBe('My bot (on vm2)');
  });

  it('names the computer when the node itself asks', () => {
    // The memory watchdog asks as `{ kind: 'node' }`: never "undefined asked".
    const node = { kind: 'node', id: 'memory-watchdog', thread: null } as unknown as Parameters<typeof principalName>[0];
    expect(principalName(node)).toBe('This computer');
    expect(principalName(node, { inSentence: true })).toBe('this computer');
  });

  it('reads the live object and the bare id the same way', () => {
    // The live Activity feed sends this shape. Rendering it raw crashed the page (React #31).
    const live = { kind: 'flow' as const, id: 'solstice.medium.post', thread: null };
    expect(principalName(live)).toBe(principalName('solstice.medium.post'));
    expect(principalName(live)).toBe('Medium post routine');
    expect(principalId(live)).toBe('solstice.medium.post');
  });

  it('names a routine in a sentence', () => {
    expect(principalName({ kind: 'flow', id: 'weekly-posts' }, { inSentence: true })).toBe('the weekly posts routine');
    expect(principalName({ kind: 'flow', id: 'aster.instagram.post' })).toBe('Instagram post routine');
    expect(principalName('flow')).toBe('A routine');
  });

  it('names a person from the org, and the reader as You', () => {
    const people = new Map([['usr_01m6k0h3', 'Demo Owner']]);
    expect(principalName('usr_01m6k0h3', { people })).toBe('Demo Owner');
    expect(principalName({ kind: 'human', id: 'usr_01m6k0h3' }, { me: { id: 'usr_01m6k0h3', name: 'Demo Owner' }, people })).toBe('You');
    // Never a bare id.
    expect(principalName('usr_unknown')).toBe('A person');
    expect(principalName(null)).toBe('Someone');
  });

  it('names a member by the name the node sends, and never shows a member id', () => {
    // The node names who asked in words on the principal. Without a name, the id is still never words.
    const named = { kind: 'human' as const, id: 'm_w4htizzaz6vmeev5', name: 'Vee Marsh' };
    expect(principalName(named)).toBe('Vee Marsh');
    expect(principalName(named, { inSentence: true })).toBe('Vee Marsh');
    const unnamed = { kind: 'human' as const, id: 'm_w4htizzaz6vmeev5', name: null };
    expect(principalName(unnamed)).toBe('Someone on your team');
    expect(principalName(unnamed, { inSentence: true })).toBe('someone on your team');
    expect(principalName('m_w4htizzaz6vmeev5')).toBe('Someone on your team');
    expect(principalName({ kind: 'human', id: 'owner' })).toBe('The owner');
    expect(principalName({ kind: 'human', id: 'owner' }, { inSentence: true })).toBe('the owner');
  });

  it('infers the kind from the id, as the cloud does', () => {
    expect(asPrincipal('usr_1').kind).toBe('human');
    expect(asPrincipal('aster.blog.check').kind).toBe('flow');
    expect(asPrincipal('claude-code').kind).toBe('agent');
    expect(knownAgent('claude-code')).toBe('claude-code');
    expect(knownAgent('aster.blog.check')).toBeNull();
  });

  it('makes a routine name from a flow id or target', () => {
    expect(routineName('flow:aster.instagram.post@1')).toBe('Instagram post');
    expect(routineName('harbor.blog.publish')).toBe('Blog post');
    expect(routineName('meridian.reddit.check')).toBe('Reddit check');
  });
});
