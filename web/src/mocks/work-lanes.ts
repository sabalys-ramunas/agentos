/**
 * Accounts beyond social posting, for the demo org (brief, "Owner's note"): the inbox, the books,
 * the bank, a supplier portal, a shop, bookings, reviews and the tax office. History's examples and
 * the approvals in Needs you happen on these, so Home and Accounts show a believable mix of work.
 */
import type { Lane } from '@/api/types';
import type { MockDb } from './seed';

const MINUTE = 60_000;

/** [lane id, its address, warm, minutes since last used, handle] */
const WORK: [string, string, boolean, number, string][] = [
  ['aster/gmail', 'mail.google.com', true, 22, 'hello@astercoffee.example'],
  ['aster/bank', 'ib.swedbank.lt', false, 95, 'Aster Coffee UAB'],
  ['aster/sheets', 'sheets.google.com', false, 140, 'hello@astercoffee.example'],
  ['aster/portal', 'orders.nordicbeans.example', false, 300, 'Aster Coffee'],
  ['harbor/xero', 'go.xero.com', true, 48, 'Harbor Supply Ltd'],
  ['harbor/quickbooks', 'qbo.intuit.com', false, 1440, 'Harbor Supply Ltd'],
  ['harbor/shopify', 'harborsupply.myshopify.com', true, 12, 'harborsupply'],
  ['harbor/gov', 'www.tax.service.gov.uk', false, 260, 'Harbor Supply Ltd'],
  ['meridian/booking', 'admin.booking.com', false, 75, 'Meridian Travel'],
  ['meridian/tripadvisor', 'tripadvisor.com', false, 33, 'Meridian Travel'],
  ['solstice/sheets', 'sheets.google.com', false, 180, 'studio@solstice.example'],
  ['solstice/outlook', 'outlook.office.com', true, 64, 'studio@solstice.example'],
  ['personal/gov', 'www.vmi.lt', false, 2880, 'Demo Owner'],
];

export function withWorkLanes(db: MockDb, now = Date.now()) {
  const data = db.orgs.get('northwind-group');
  if (!data) return;
  for (const [id, host, warm, minutes, handle] of WORK) {
    if (data.lanes.some((lane) => lane.id === id)) continue;
    const [workspace = '', name = ''] = id.split('/');
    const sibling = data.lanes.find((lane) => lane.workspace === workspace && lane.kind === 'browser');
    if (!sibling) continue;
    const lane: Lane = {
      ...sibling,
      id,
      name,
      state: warm ? 'warm' : 'cold',
      signal: 'ok',
      quarantine_reason: null,
      hold: null,
      hold_until: null,
      signal_detail: null,
      control: { ...sibling.control, state: 'agent', by: null, by_id: null, since: null, takeback_requested: false, takeback_reason: null, takeback_by: null },
      last_used: new Date(now - minutes * MINUTE).toISOString(),
      account: { site: host, user_id: handle, source: 'profile', status: 'match' },
      readiness: 'ready',
      readiness_note: null,
      login_url: `https://${host}/login`,
      viewer: warm ? sibling.viewer : null,
    };
    data.lanes.push(lane);
  }
}
