import { render } from '@testing-library/react';
import { StandIn, workKind } from './stand-in';

describe('the stand-in for a missing picture', () => {
  it('draws the kind of work the account does, not only posts', () => {
    expect(workKind('mail.google.com')).toBe('email');
    expect(workKind('outlook')).toBe('email');
    expect(workKind('go.xero.com')).toBe('money');
    expect(workKind('https://ib.swedbank.lt')).toBe('money');
    expect(workKind('sheets.google.com')).toBe('table');
    expect(workKind('https://www.vmi.lt/deklaravimas')).toBe('form');
    expect(workKind('acme/shopify')).toBe('shop');
    expect(workKind('instagram')).toBe('post');
    expect(workKind('https://supplier.example.org')).toBe('post');
  });

  it('shows the site, whose account it is and its last line, never an empty box', () => {
    const { container, getByText } = render(<StandIn site="xero" title="Linden Bakery" line="Collected 3 invoices · 2h ago" />);
    expect(container.querySelector('[data-stand-in="money"]')).not.toBeNull();
    expect(container.querySelector('[data-site="xero"]')).not.toBeNull();
    expect(getByText('Linden Bakery')).toBeInTheDocument();
    expect(getByText('Collected 3 invoices · 2h ago')).toBeInTheDocument();
  });
});
