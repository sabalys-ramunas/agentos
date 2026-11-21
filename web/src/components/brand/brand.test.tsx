import { render, screen } from '@testing-library/react';
import { AgentMark, BusinessMark, PersonMark, hueFor, initials } from './identity';
import { SiteMark, brandFill, hasBrandMark } from './site-mark';

describe('SiteMark', () => {
  it.each([
    ['instagram', 'Instagram'],
    ['facebook.com', 'Facebook'],
    ['https://www.linkedin.com/company/aster', 'LinkedIn'],
    ['aster/reddit', 'Reddit'],
    ['medium', 'Medium'],
    ['x.com', 'X'],
    ['twitter.com', 'X'],
    ['youtube', 'YouTube'],
    ['tiktok', 'TikTok'],
    ['mail.google.com', 'Gmail'],
    ['go.xero.com', 'Xero'],
    ['dashboard.stripe.com', 'Stripe'],
    ['squareup.com', 'Square'],
    ['qbo.intuit.com', 'QuickBooks'],
    ['admin.booking.com', 'Booking.com'],
    ['sheets.google.com', 'Google Sheets'],
    ['drive.google.com', 'Google Drive'],
    ['notion.so', 'Notion'],
    ['acme/shopify', 'Shopify'],
  ])('draws the real mark for %s, named %s', (site, name) => {
    render(<SiteMark site={site} />);
    const mark = screen.getByRole('img', { name });
    expect(mark.querySelector('path')?.getAttribute('d')?.length).toBeGreaterThan(50);
    expect(hasBrandMark(site)).toBe(true);
  });

  it('draws a globe with the initial for a site it does not know', () => {
    render(<SiteMark site="https://shop.example.org/cart" />);
    const mark = screen.getByRole('img', { name: 'shop.example.org' });
    expect(mark).toHaveAttribute('data-site', 'unknown');
    expect(mark).toHaveTextContent('S');
  });

  it('names a bank, a government portal and Outlook with plain marks of ours', () => {
    render(
      <>
        <SiteMark site="https://ib.swedbank.lt/login" />
        <SiteMark site="https://www.vmi.lt/deklaravimas" />
        <SiteMark site="https://www.tax.service.gov.uk/" />
        <SiteMark site="outlook.office.com" />
      </>,
    );
    expect(screen.getByRole('img', { name: 'Bank' })).toHaveAttribute('data-site', 'bank');
    expect(screen.getAllByRole('img', { name: 'Government portal' })).toHaveLength(2);
    expect(screen.getByRole('img', { name: 'Outlook' })).toHaveAttribute('data-site', 'outlook');
    expect(hasBrandMark('outlook')).toBe(false);
  });

  it('uses our own marks for our own kinds of lane', () => {
    render(<SiteMark site="blog" />);
    expect(screen.getByRole('img', { name: 'Blog' })).toHaveAttribute('data-site', 'blog');
  });

  it('draws in the brand colour, monochrome on request, and never black on the dark theme', () => {
    expect(brandFill({ title: 'Instagram', hex: 'FF0069', path: '' }, 'brand')).toBe('#FF0069');
    expect(brandFill({ title: 'X', hex: '000000', path: '' }, 'brand')).toBe('var(--text-primary)');
    expect(brandFill({ title: 'Instagram', hex: 'FF0069', path: '' }, 'mono')).toBe('currentColor');
  });

  it('sizes the mark and hides it when a name sits beside it', () => {
    const { container } = render(<SiteMark site="reddit" size={32} tile label={false} />);
    const mark = container.firstElementChild as HTMLElement;
    expect(mark).toHaveAttribute('aria-hidden', 'true');
    expect(mark.style.width).toBe('32px');
  });
});

describe('BusinessMark and PersonMark', () => {
  it('fills the business colour with its initial', () => {
    render(<BusinessMark name="Aster Coffee" color="#a0763f" label="Aster Coffee" />);
    const mark = screen.getByRole('img', { name: 'Aster Coffee' });
    expect(mark).toHaveTextContent('A');
    expect(mark.style.backgroundColor).toBe('rgb(160, 118, 63)');
    expect(mark.style.color).toBe('rgb(255, 255, 255)');
  });

  it('shows an uploaded logo instead of the initial', () => {
    const { container } = render(<BusinessMark name="Aster Coffee" color="#a0763f" logo="/logo.png" />);
    expect(container.querySelector('img')).toHaveAttribute('src', '/logo.png');
  });

  it('gives a person one colour everywhere, from their id', () => {
    expect(hueFor('usr_1')).toBe(hueFor('usr_1'));
    expect(hueFor('usr_1')).toMatch(/^var\(--ws-[1-8]\)$/);
    expect(initials('Demo Owner', 2)).toBe('DO');
    render(<PersonMark id="usr_1" name="Demo Owner" label="Demo Owner" />);
    expect(screen.getByRole('img', { name: 'Demo Owner' })).toHaveTextContent('DO');
  });
});

describe('AgentMark', () => {
  it.each([
    [{ kind: 'agent' as const, id: 'claude-code' }, 'claude-code', 'Claude Code'],
    ['claude-app', 'claude', 'Claude'],
    ['chatgpt-app', 'chatgpt', 'ChatGPT'],
    ['codex', 'codex', 'Codex'],
    [{ kind: 'flow' as const, id: 'weekly-posts' }, 'routine', 'Weekly posts routine'],
  ])('marks %j as %s', (principal, kind, name) => {
    const { container } = render(<AgentMark principal={principal} />);
    // Beside its name by default, so hidden from screen readers.
    expect(container.firstElementChild).toHaveAttribute('data-agent', kind);
    expect(container.firstElementChild).toHaveAttribute('aria-hidden', 'true');
    render(<AgentMark principal={principal} label={name} />);
    expect(screen.getByRole('img', { name })).toHaveAttribute('data-agent', kind);
  });

  it('marks a person with their initials', () => {
    render(<AgentMark principal="usr_9" name="Mara Lindqvist" label="Mara Lindqvist" />);
    expect(screen.getByRole('img', { name: 'Mara Lindqvist' })).toHaveTextContent('ML');
  });
});
