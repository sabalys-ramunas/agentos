import { screen, within } from '@testing-library/react';
import type { Brain, Status } from '@/api/types';
import { aiName, computerName } from '@/lib/computers';
import { renderApp } from '@/test/render';
import { summarize } from './status-pill';

const status = (nodes: Status['nodes']) => ({ nodes }) as Status;
const brain = (name: string, state: Brain['state'], kind: Brain['kind'] = 'subscription') => ({ name, state, kind }) as Brain;
const up = { id: 'home-mini', state: 'up', degraded: false } as const;

describe('the status line', () => {
  it('says the worst thing first, in words', () => {
    const brains = [brain('claude-max', 'ready')];
    expect(summarize(status([up]), brains, 1, true).text).toBe('Reconnecting');
    expect(summarize(status([up, { id: 'hetzner-1', state: 'down', degraded: false }]), brains, 1, false)).toMatchObject({ tone: 'bad', text: 'Cloud server is offline' });
    expect(summarize(status([up]), [brain('claude-max', 'down'), brain('ollama-local', 'ready', 'local')], 0, false).text).toBe('Using backup AI');
    expect(summarize(status([{ ...up, degraded: true }]), brains, 0, false).text).toBe('Mac mini (home) is running slowly');
    expect(summarize(status([up]), brains, 1, false)).toMatchObject({ tone: 'working', text: '1 working now · all systems fine', short: '1 working' });
    expect(summarize(status([up]), brains, 0, false)).toMatchObject({ tone: 'ok', text: 'All systems fine' });
    expect(summarize(status([]), brains, 0, false).text).toBe('No machine yet');
  });

  it('never calls things fine beside what waits for a person', () => {
    const brains = [brain('claude-max', 'ready')];
    const waiting = (needs: number) => ({ nodes: [up], needs_you: needs }) as Status;
    expect(summarize(waiting(7), brains, 0, false)).toMatchObject({ tone: 'warn', text: '7 need you · machines fine', short: '7 need you' });
    expect(summarize(waiting(2), brains, 1, false).text).toBe('2 need you · 1 working now · machines fine');
    expect(summarize(waiting(0), brains, 0, false).text).toBe('All systems fine');
  });

  it('never calls a machine fine when it is frozen or its own checks fail', () => {
    const brains = [brain('claude-max', 'ready')];
    const waiting = (needs: number) => ({ nodes: [up], needs_you: needs }) as Status;
    expect(summarize(waiting(3), brains, 0, false, { frozen: true, bad: 0, warn: 0 })).toMatchObject({ tone: 'bad', short: 'Frozen' });
    expect(summarize(waiting(3), brains, 0, false, { frozen: false, bad: 1, warn: 0 }).text).toBe('3 need you · Mac mini (home) needs fixing');
    expect(summarize(waiting(0), brains, 0, false, { frozen: false, bad: 0, warn: 2 })).toMatchObject({ tone: 'warn', text: 'Mac mini (home) is worth a look' });
    expect(summarize(waiting(3), brains, 0, false, { frozen: false, bad: 0, warn: 0 }).text).toBe('3 need you · machines fine');
    expect(summarize(status([up]), brains, 0, false, { frozen: false, bad: 0, warn: 0 })).toMatchObject({ tone: 'ok', text: 'All systems fine' });
  });

  it('names machines and AI models the way a person says them', () => {
    expect(computerName('home-mini')).toBe('Mac mini (home)');
    expect(computerName('hetzner-1')).toBe('Cloud server');
    expect(computerName('hetzner-2')).toBe('Cloud server 2');
    expect(computerName('office-box')).toBe('Office box');
    expect(aiName('claude-max')).toBe('Claude (Max plan)');
    expect(aiName('ollama-local', 'local')).toBe('Local AI');
    expect(aiName('anthropic-api')).toBe('Claude (API)');
  });

  it('replaces the brain, cost and node chips, and keeps the depth one click away', async () => {
    const { user } = renderApp('/northwind-group');
    const pill = (await screen.findAllByRole('button', { name: /^System: / }, { timeout: 3000 }))[0]!;
    expect(screen.queryByRole('button', { name: 'Brain' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Cost today' })).not.toBeInTheDocument();
    await user.click(pill);
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Machines')).toBeInTheDocument();
    expect(within(dialog).getByText('AI usage today')).toBeInTheDocument();
    expect(within(dialog).queryByText('claude-max', { exact: false })).not.toBeInTheDocument();
    expect(within(dialog).getByRole('link', { name: 'Open Machines' })).toHaveAttribute('href', '/northwind-group/machines');
  });
});
