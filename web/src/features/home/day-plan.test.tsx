import { screen, within } from '@testing-library/react';
import type { PlanItem } from '@/api/types';
import { renderApp } from '@/test/render';
import { planSentence } from './day-plan';

const item = (section: PlanItem['section'], text = 'x'): PlanItem => ({ section, lane: null, text });

describe('the day plan sentence', () => {
  it('leads with what is blocked, then what waits in the queue, then what there is to look at', () => {
    expect(planSentence([item('blocked'), item('blocked'), item('queue')])).toBe('2 things are blocked today.');
    expect(planSentence([item('blocked')])).toBe('1 thing is blocked today.');
    expect(planSentence([item('queue'), item('due')])).toBe('1 thing waits in the queue.');
    expect(planSentence([item('queue'), item('queue')])).toBe('2 things wait in the queue.');
    expect(planSentence([item('due')])).toBe('1 thing to look at today.');
    expect(planSentence([item('due'), item('upkeep'), item('programs')])).toBe('3 things to look at today.');
  });
});

describe('the day plan on Home', () => {
  it('shows what the day holds under what needs a person, as a draft that sends nothing', async () => {
    renderApp('/northwind-group');
    const day = within(await screen.findByRole('region', { name: 'The day' }, { timeout: 4000 }));
    expect(day.getByText('A draft. It queues and sends nothing.')).toBeInTheDocument();
    expect(day.getByText(/^(?:\d+ things? (?:is|are) blocked today|\d+ things? (?:waits?|wait) in the queue|\d+ things? to look at today)\.$/)).toBeInTheDocument();
    expect(day.getByRole('heading', { level: 3, name: 'Blocked' })).toBeInTheDocument();
  });

  it('says a blocked account in its own words, and never with the command line', async () => {
    renderApp('/northwind-group');
    const day = within(await screen.findByRole('region', { name: 'The day' }, { timeout: 4000 }));
    expect(day.queryByText(/agentos (lane|unfreeze|plan|brains)/)).not.toBeInTheDocument();
    expect((await day.findAllByText('Needs you in the live view.')).length).toBeGreaterThan(0);
    expect(day.getAllByText(/Take over in the live view, then hand it back\./).length).toBeGreaterThan(0);
  });

  it('is left off Home when it has nothing to say', async () => {
    renderApp('/juniper-labs');
    expect(await screen.findByRole('heading', { level: 1 })).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'The day' })).not.toBeInTheDocument();
  });
});
