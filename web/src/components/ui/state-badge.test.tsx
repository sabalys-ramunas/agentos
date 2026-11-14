import { render, screen } from '@testing-library/react';
import { ConditionBadge, StateBadge, StateGlyph } from './state-badge';

describe('StateBadge', () => {
  it.each([
    ['good', 'All good'],
    ['waiting', 'Waiting for you'],
    ['stuck', 'Stuck'],
    ['sleeping', 'Sleeping'],
    ['working', 'Working now'],
  ] as const)('shows %s as a shaped glyph and the word %s', (state, word) => {
    const { container } = render(<StateBadge state={state} />);
    expect(screen.getByText(word)).toBeInTheDocument();
    const glyph = container.querySelector('svg')!;
    expect(glyph).toHaveAttribute('aria-hidden', 'true');
    expect(glyph).toHaveAttribute('data-state', state);
  });

  it('draws a different shape for each, not only a colour', () => {
    const shapes = (['good', 'waiting', 'stuck', 'sleeping', 'working'] as const).map((state) => {
      const { container } = render(<StateGlyph state={state} />);
      return [...container.querySelectorAll('svg > *')].map((node) => `${node.tagName}:${node.getAttribute('d') ?? node.getAttribute('r') ?? node.getAttribute('rx')}`).join('|');
    });
    expect(new Set(shapes).size).toBe(5);
  });

  it('takes a count instead of the word, and shows the sentence', () => {
    render(<StateBadge state="waiting" label="3 for you" sentence="3 posts to check" detail />);
    expect(screen.getByText('3 for you')).toBeInTheDocument();
    expect(screen.getByText(/3 posts to check/)).toBeInTheDocument();
  });

  it('shows a condition with its sentence as a tooltip', () => {
    const { container } = render(<ConditionBadge condition={{ state: 'stuck', sentence: 'Signed in as someone else' }} />);
    expect(screen.getByText('Stuck')).toBeInTheDocument();
    expect(container.firstElementChild).toHaveAttribute('title', 'Signed in as someone else');
  });
});
