/**
 * What the Site APIs concept shows: nine jobs and one proposal across the demo's businesses, on
 * accounts the demo already has, with a week of hourly calls and three keys. Every site is invented
 * and sits under `.example`. Built once per org, on first use, so a pause or a new key sticks until
 * the page is reloaded. An API's `lane` is one account and only ever that one.
 */
import { delay, http, HttpResponse } from 'msw';
import type {
  SiteAction,
  SiteApi,
  SiteApiDetail,
  SiteDiscovery,
  SiteEndpoint,
  SiteField,
  SiteKey,
  SiteKeyCreated,
  SiteKeyInput,
  SitePage,
  SiteRow,
  SiteUsageHour,
} from '@/api/types';
import type { OrgData } from './seed';
import type { Helpers } from './s3';

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const WEEK_HOURS = 168;

function randomFrom(text: string) {
  let state = 7;
  for (const char of text) state = (state * 31 + char.charCodeAt(0)) >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let mixed = Math.imul(state ^ (state >>> 15), 1 | state);
    mixed = (mixed + Math.imul(mixed ^ (mixed >>> 7), 61 | mixed)) ^ mixed;
    return ((mixed ^ (mixed >>> 14)) >>> 0) / 4294967296;
  };
}

const field = (key: string, label: string, kind: SiteField['kind'] = 'text'): SiteField => ({ key, label, kind });
const iso = (at: number) => new Date(at).toISOString();
const day = (at: number) => new Date(at).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
const euros = (value: number) => `€${value.toLocaleString('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const pounds = (value: number) => `£${value.toLocaleString('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

function rowsOf(fields: SiteField[], cells: string[][]): SiteRow[] {
  return cells.map((cell) => Object.fromEntries(fields.map((item, index) => [item.key, cell[index] ?? ''])));
}

/** Nordic Beans: 42 open orders, because that is what the page really holds. */
function nordicOrders(now: number) {
  const random = randomFrom('nordic');
  const coffees: [string, number][] = [
    ['Guji natural, Ethiopia', 14.8],
    ['Huila washed, Colombia', 12.4],
    ['Cerrado pulped, Brazil', 9.9],
    ['Nyeri AA, Kenya', 16.2],
    ['Antigua, Guatemala', 12.9],
    ['Sidamo decaf, Ethiopia', 13.6],
    ['House espresso blend', 10.7],
  ];
  const statuses = ['Confirmed', 'Roasting', 'Packed', 'Waiting on stock'];
  return Array.from({ length: 42 }, (_, index) => {
    const [coffee, price] = coffees[Math.floor(random() * coffees.length)] as [string, number];
    const bags = 2 + Math.floor(random() * 10);
    const ships = now + (1 + Math.floor(index / 3) + Math.floor(random() * 3)) * DAY;
    return {
      number: `A-${1042 + index * 3}`,
      coffee,
      bags: String(bags),
      total: euros(bags * 12 * price),
      ships: day(ships),
      status: statuses[index === 3 ? 3 : Math.floor(random() * 3)] as string,
    };
  });
}

function placedDates(now: number, count: number) {
  return Array.from({ length: count }, (_, index) => day(now - (3 + index) * DAY));
}

interface Seed {
  api: Omit<SiteApiDetail, 'spark' | 'calls_7d' | 'cached_pct' | 'pace' | 'computer' | 'keep_updated'> & { keep_updated?: SiteApiDetail['keep_updated'] };
  /** Hourly calls at the busiest time of day, per endpoint. */
  load: Record<string, number>;
  /** Share answered from the cache, 0 to 1. */
  cached: number;
  /** From how many hours ago calls fail, or stop. */
  brokeHoursAgo?: number;
  stopped?: boolean;
  pace: { used: number; per_hour: number; others: { label: string; used: number }[] };
}

const endpoint = (rest: Pick<SiteEndpoint, 'key' | 'title' | 'description' | 'fields' | 'rows' | 'total' | 'page'> & Partial<SiteEndpoint>): SiteEndpoint => ({
  effect: 'read',
  input: [],
  cache_s: 300,
  pace_per_hour: 60,
  expect_min: 1,
  calls_7d: 0,
  cached_pct: 0,
  p50_ms: 1800,
  ...rest,
});

function seeds(now: number): Seed[] {
  const seen = (minutes: number) => iso(now - minutes * MINUTE);

  // A supplier's portal: the flagship, and the page Teach it walks through.
  const orderFields = [field('number', 'Order'), field('coffee', 'Coffee'), field('bags', 'Bags', 'count'), field('total', 'Total', 'money'), field('ships', 'Ships', 'date'), field('status', 'Status', 'status')];
  const orders = nordicOrders(now);
  const nordicPage: SitePage = {
    url: 'https://orders.nordicbeans.example/orders?status=open',
    wordmark: 'Nordic Beans',
    nav: ['Catalogue', 'Orders', 'Invoices', 'Samples', 'Help'],
    heading: 'Your orders',
    count_words: '42 open',
    layout: 'table',
    columns: [
      { label: 'Order', field: 'number' },
      { label: 'Placed', field: null, values: placedDates(now, 42) },
      { label: 'Coffee', field: 'coffee' },
      { label: 'Bags', field: 'bags' },
      { label: 'Total', field: 'total' },
      { label: 'Ships', field: 'ships' },
      { label: 'Status', field: 'status' },
      { label: 'Buyer', field: null, values: Array.from({ length: 42 }, (_, index) => (index % 3 ? 'M. Petrauskas' : 'L. Kairys')) },
    ],
    side: ['Harvest update: Guji arrives week 47', 'Holiday cut-off is 12 December', 'New: 12 kg bags on every lot', 'Sample box is back', 'Warehouse closed 24 to 27 December', 'Price list for 2027 is out'],
    seen_at: seen(4),
  };
  const nordic: Seed = {
    api: {
      key: 'nordic-beans',
      title: 'Open orders at Nordic Beans',
      site: { name: 'Nordic Beans', domain: 'orders.nordicbeans.example' },
      words: 'Every order you have placed that has not shipped yet, with what it costs and when it leaves.',
      workspace: 'aster',
      lane: 'aster/portal',
      account: 'Aster Coffee',
      stage: 'active',
      health: 'good',
      problem: null,
      proposal: null,
      sample: {
        kind: 'rows',
        columns: ['Order', 'Coffee', 'Total', 'Ships'],
        rows: orders.slice(0, 4).map((row) => [row.number, row.coffee, row.total, row.ships]),
      },
      read_at: seen(4),
      version: '1.3.0',
      keep_updated: 'hourly',
      endpoints: [
        endpoint({
          key: 'open_orders',
          title: 'Open orders',
          description: 'Orders that have not shipped yet.',
          input: [
            { key: 'status', label: 'Which orders', type: 'choice', default: 'open', options: [{ value: 'open', label: 'All open' }, { value: 'late', label: 'Only late ones' }] },
            { key: 'limit', label: 'How many at most', type: 'number', default: '50', hint: 'Up to 100.' },
          ],
          fields: orderFields,
          rows: orders,
          total: 42,
          new_rows: [0, 2],
          expect_min: 1,
          p50_ms: 1840,
          page: nordicPage,
        }),
        endpoint({
          key: 'order_lookup',
          title: 'Look up one order',
          description: 'Types an order number into the search box and reads what comes back.',
          effect: 'draft',
          input: [{ key: 'number', label: 'Order number', type: 'text', required: true, default: 'A-1042', hint: 'Like A-1042.' }],
          fields: orderFields,
          rows: orders.slice(0, 1),
          total: 1,
          cache_s: 0,
          pace_per_hour: 30,
          p50_ms: 2610,
          page: { ...nordicPage, url: 'https://orders.nordicbeans.example/orders?q=A-1042', count_words: '1 result' },
        }),
      ],
    },
    load: { open_orders: 58, order_lookup: 6 },
    cached: 0.84,
    pace: { used: 14, per_hour: 60, others: [{ label: 'The bean order routine', used: 9 }, { label: 'Someone in the live view', used: 3 }] },
  };

  // An invoicing portal.
  const invoiceFields = [field('invoice', 'Invoice'), field('customer', 'Customer'), field('amount', 'Amount', 'money'), field('due', 'Was due', 'date'), field('days', 'Days late', 'count')];
  const invoiceCells: [string, string, number, number][] = [
    ['INV-20418', 'Ferry Lane Chandlers', 4820.5, 41],
    ['INV-20433', 'Pier Nine Marine', 3190, 33],
    ['INV-20467', 'Saltmarsh Boatworks', 2760.25, 27],
    ['INV-20471', 'Lowtide Outfitters', 1984, 22],
    ['INV-20502', 'Tern & Sons', 1640.75, 19],
    ['INV-20519', 'North Quay Rigging', 1205.2, 14],
    ['INV-20527', 'Estuary Rope Co', 980, 11],
    ['INV-20544', 'Harbourmaster Stores', 742.3, 9],
    ['INV-20550', 'Fairlead Yachts', 688, 7],
    ['INV-20561', 'Bosun Direct', 412.9, 5],
    ['INV-20563', 'Gull Rock Sailing', 268.4, 4],
    ['INV-20570', 'The Slipway', 185, 2],
  ];
  const invoices = rowsOf(invoiceFields, invoiceCells.map(([invoice, customer, amount, days]) => [invoice, customer, pounds(amount), day(now - days * DAY), String(days)]));
  const tollbook: Seed = {
    api: {
      key: 'tollbook-invoices',
      title: 'Overdue invoices at Tollbook',
      site: { name: 'Tollbook', domain: 'billing.tollbook.example' },
      words: 'Who owes you money and how late they are, so the chasing can start without anyone opening the portal.',
      workspace: 'harbor',
      lane: 'harbor/xero',
      account: 'Harbor Supply Ltd',
      stage: 'active',
      health: 'good',
      problem: null,
      proposal: null,
      sample: {
        kind: 'aging',
        total: '£18,926.30',
        note: '12 invoices, the oldest 41 days late',
        items: invoiceCells.slice(0, 4).map(([, who, amount, days]) => ({ who, amount: pounds(amount), days })),
      },
      read_at: seen(11),
      version: '2.0.1',
      endpoints: [
        endpoint({
          key: 'overdue_invoices',
          title: 'Overdue invoices',
          description: 'Everything past its due date, oldest first.',
          input: [{ key: 'older_than', label: 'At least this many days late', type: 'number', default: '1' }],
          fields: invoiceFields,
          rows: invoices,
          total: 12,
          new_rows: [11],
          cache_s: 900,
          p50_ms: 2240,
          page: {
            url: 'https://billing.tollbook.example/invoices/overdue',
            wordmark: 'Tollbook',
            nav: ['Dashboard', 'Invoices', 'Customers', 'Reports'],
            heading: 'Overdue',
            count_words: '12 invoices',
            layout: 'table',
            columns: [
              { label: 'Number', field: 'invoice' },
              { label: 'To', field: 'customer' },
              { label: 'Sent', field: null, values: invoiceCells.map(([, , , days]) => day(now - (days + 30) * DAY)) },
              { label: 'Balance', field: 'amount' },
              { label: 'Due', field: 'due' },
              { label: 'Late by', field: 'days' },
              { label: 'Reminders', field: null, values: invoiceCells.map(([, , , days]) => String(Math.min(3, Math.floor(days / 12)))) },
            ],
            side: ['Bank feed up to date', 'VAT return due 7 December', 'Two drafts not sent'],
            seen_at: seen(11),
          },
        }),
      ],
    },
    load: { overdue_invoices: 22 },
    cached: 0.91,
    pace: { used: 4, per_hour: 60, others: [{ label: 'The invoice reminders routine', used: 12 }] },
  };

  // A shop's admin: cards, not a table.
  const stockFields = [field('product', 'Product'), field('sku', 'SKU'), field('on_hand', 'On hand', 'count'), field('reorder_at', 'Reorder at', 'count'), field('bin', 'Bin')];
  const stockCells = [
    ['Stainless shackle 8 mm', 'SH-08', '14', '40', 'A3'],
    ['Polyester line 10 mm, 50 m', 'PL-10-50', '6', '12', 'C1'],
    ['Boat cover clips, pack of 10', 'BC-10', '212', '60', 'B7'],
    ['Fender 6 x 22 in', 'FD-622', '31', '20', 'D2'],
    ['Deck cleat 150 mm', 'DC-150', '9', '25', 'A5'],
    ['Marine sealant, white', 'MS-WH', '58', '30', 'E1'],
    ['Bilge pump 500 GPH', 'BP-500', '17', '10', 'F4'],
    ['Anchor swivel 10 mm', 'AS-10', '3', '15', 'A1'],
  ];
  const shelfmark: Seed = {
    api: {
      key: 'shelfmark-stock',
      title: 'Stock levels at Shelfmark',
      site: { name: 'Shelfmark', domain: 'admin.shelfmark.example' },
      words: 'How many of each product are on the shelf, and which ones are under their reorder point.',
      workspace: 'harbor',
      lane: 'harbor/shopify',
      account: 'Harbor Supply Ltd',
      stage: 'active',
      health: 'good',
      problem: null,
      proposal: null,
      sample: {
        kind: 'levels',
        items: [
          { label: 'Anchor swivel 10 mm', left: 3, reorder: 15, unit: 'left' },
          { label: 'Polyester line 10 mm', left: 6, reorder: 12, unit: 'left' },
          { label: 'Deck cleat 150 mm', left: 9, reorder: 25, unit: 'left' },
          { label: 'Stainless shackle 8 mm', left: 14, reorder: 40, unit: 'left' },
        ],
      },
      read_at: seen(2),
      version: '1.1.0',
      endpoints: [
        endpoint({
          key: 'stock_levels',
          title: 'Stock levels',
          description: 'Every product with its count and shelf.',
          input: [{ key: 'below_reorder', label: 'Only what needs reordering', type: 'choice', default: 'no', options: [{ value: 'no', label: 'Show everything' }, { value: 'yes', label: 'Only low stock' }] }],
          fields: stockFields,
          rows: rowsOf(stockFields, stockCells),
          total: 86,
          cache_s: 120,
          expect_min: 20,
          p50_ms: 1460,
          page: {
            url: 'https://admin.shelfmark.example/inventory',
            wordmark: 'Shelfmark',
            nav: ['Inventory', 'Orders', 'Suppliers', 'Settings'],
            heading: 'Inventory',
            count_words: '86 products',
            layout: 'cards',
            columns: stockFields.map((item) => ({ label: item.label, field: item.key })),
            side: [],
            seen_at: seen(2),
          },
        }),
      ],
    },
    load: { stock_levels: 46 },
    cached: 0.72,
    pace: { used: 26, per_hour: 60, others: [{ label: 'The new order routine', used: 21 }] },
  };

  // Comments on the owner's own posts.
  const commentFields = [field('who', 'Who'), field('post', 'On'), field('comment', 'Comment'), field('when', 'When', 'date')];
  const commentCells = [
    ['Rūta K.', 'Why we stopped shooting on white', 'The part about shadow as a colour changed how I light packshots.', '38m ago'],
    ['Daniel Osei', 'Pricing a studio day', 'Do you quote retouching per image or per day? We keep getting this wrong.', '2h ago'],
    ['mira_makes', 'Pricing a studio day', 'Sending this to every junior I know.', '3h ago'],
    ['Tomas V.', 'Why we stopped shooting on white', 'Which backdrop paper is in the third picture?', '6h ago'],
    ['Helen Marsh', 'A year in the new space', 'Lovely. Is the north light as good in winter?', 'Yesterday'],
    ['Priya S.', 'A year in the new space', 'Would you take on a placement in spring?', 'Yesterday'],
  ];
  const inkwell: Seed = {
    api: {
      key: 'inkwell-comments',
      title: 'Comments on your posts at Inkwell',
      site: { name: 'Inkwell', domain: 'studio.inkwell.example' },
      words: 'New comments on everything you have published, so a question never sits unanswered for a week.',
      workspace: 'solstice',
      lane: 'solstice/medium',
      account: 'Solstice Studio',
      stage: 'active',
      health: 'good',
      problem: null,
      proposal: null,
      sample: {
        kind: 'quotes',
        items: commentCells.slice(0, 3).map(([who = '', , text = '', when = '']) => ({ who, text, when })),
      },
      read_at: seen(23),
      version: '1.0.2',
      endpoints: [
        endpoint({
          key: 'new_comments',
          title: 'New comments',
          description: 'Comments from the last seven days, newest first.',
          input: [{ key: 'since', label: 'Since', type: 'date', hint: 'Leave empty for the last seven days.' }],
          fields: commentFields,
          rows: rowsOf(commentFields, commentCells),
          total: 6,
          new_rows: [0],
          cache_s: 1800,
          expect_min: null,
          p50_ms: 2070,
          page: {
            url: 'https://studio.inkwell.example/stats/responses',
            wordmark: 'Inkwell',
            nav: ['Stories', 'Responses', 'Stats'],
            heading: 'Responses',
            count_words: '6 this week',
            layout: 'thread',
            columns: commentFields.map((item) => ({ label: item.label, field: item.key })),
            side: [],
            seen_at: seen(23),
          },
        }),
      ],
    },
    load: { new_comments: 7 },
    cached: 0.88,
    pace: { used: 2, per_hour: 60, others: [] },
  };

  // A courier: the site changed shape this morning.
  const parcelFields = [field('parcel', 'Parcel'), field('to', 'Going to'), field('status', 'Status', 'status'), field('eta', 'Arrives', 'date')];
  const parcelCells = [
    ['FW-77120', 'Ferry Lane Chandlers', 'Out for delivery', 'Today'],
    ['FW-77134', 'Saltmarsh Boatworks', 'At depot, Hull', 'Tomorrow'],
    ['FW-77151', 'North Quay Rigging', 'In transit', 'Tomorrow'],
    ['FW-77163', 'Pier Nine Marine', 'Collected', day(now + 2 * DAY)],
    ['FW-77170', 'Tern & Sons', 'Label created', day(now + 3 * DAY)],
    ['FW-77188', 'The Slipway', 'Delivered', 'Yesterday'],
  ];
  const fernway: Seed = {
    api: {
      key: 'fernway-tracking',
      title: 'Parcel tracking at Fernway',
      site: { name: 'Fernway Couriers', domain: 'track.fernway.example' },
      words: 'Where each parcel you have sent is right now, and when it should arrive.',
      workspace: 'harbor',
      lane: 'harbor/blog',
      account: 'Harbor Supply Ltd',
      stage: 'active',
      health: 'changed',
      problem: {
        since: iso(now - 5 * HOUR),
        field: 'status',
        now_label: 'Latest scan',
        now_values: ['09:12 Vehicle HU-4', '07:40 Sort HUL', '22:05 Trunk 118', '16:31 Pickup', 'Awaiting', '11:02 POD'],
      },
      proposal: null,
      sample: { kind: 'journey', parcel: 'FW-77120', stops: ['Collected', 'In transit', 'Depot', 'Out for delivery', 'Delivered'], at: 3, note: 'Last good read, 5 hours ago' },
      read_at: iso(now - 5 * HOUR - 8 * MINUTE),
      version: '1.2.0',
      endpoints: [
        endpoint({
          key: 'parcels',
          title: 'Parcels on the way',
          description: 'Every parcel not yet delivered, with where it is.',
          fields: parcelFields,
          rows: rowsOf(parcelFields, parcelCells),
          total: 6,
          cache_s: 600,
          p50_ms: 1720,
          page: {
            url: 'https://track.fernway.example/account/shipments',
            wordmark: 'Fernway',
            nav: ['Shipments', 'Book', 'Addresses', 'Billing'],
            heading: 'Shipments',
            count_words: '6 active',
            layout: 'table',
            columns: [
              { label: 'Tracking no.', field: 'parcel' },
              { label: 'Recipient', field: 'to' },
              { label: 'Service', field: null, values: parcelCells.map((_, index) => (index % 2 ? 'Next day' : 'Economy')) },
              { label: 'Status', field: 'status' },
              { label: 'Estimated', field: 'eta' },
            ],
            side: ['Book before 16:00 for next day', 'New depot in Hull'],
            seen_at: seen(9),
          },
        }),
      ],
    },
    load: { parcels: 18 },
    cached: 0.76,
    brokeHoursAgo: 5,
    pace: { used: 3, per_hour: 60, others: [] },
  };

  // Needs a person: the session ended.
  const bookingFields = [field('date', 'Date', 'date'), field('room', 'Room'), field('client', 'Client'), field('hours', 'Hours', 'count')];
  const bookingCells = [
    [day(now + DAY), 'North cyc', 'Halden Ceramics', '6'],
    [day(now + 2 * DAY), 'Daylight loft', 'Oriel Skincare', '8'],
    [day(now + 2 * DAY), 'North cyc', 'Marrow Press', '4'],
    [day(now + 5 * DAY), 'Daylight loft', 'Fen & Flax', '8'],
    [day(now + 6 * DAY), 'Small set', 'Oriel Skincare', '3'],
  ];
  const lumenhall: Seed = {
    api: {
      key: 'lumenhall-bookings',
      title: 'Studio bookings at Lumenhall',
      site: { name: 'Lumenhall', domain: 'bookings.lumenhall.example' },
      words: 'Which rooms are booked next week and by whom, for the studio calendar.',
      workspace: 'solstice',
      lane: 'solstice/facebook',
      account: 'Solstice Studio',
      stage: 'active',
      health: 'sign_in',
      problem: { since: iso(now - 2 * HOUR) },
      proposal: null,
      sample: { kind: 'rows', columns: ['Date', 'Room', 'Client'], rows: bookingCells.slice(0, 3).map(([date = '', room = '', client = '']) => [date, room, client]) },
      read_at: iso(now - 2 * HOUR - 14 * MINUTE),
      version: '1.0.0',
      endpoints: [
        endpoint({
          key: 'bookings',
          title: 'Bookings',
          description: 'Every booking in the next fourteen days.',
          fields: bookingFields,
          rows: rowsOf(bookingFields, bookingCells),
          total: 5,
          cache_s: 3600,
          p50_ms: 1950,
          page: {
            url: 'https://bookings.lumenhall.example/calendar/list',
            wordmark: 'Lumenhall',
            nav: ['Calendar', 'Rooms', 'Clients'],
            heading: 'Next 14 days',
            count_words: '5 bookings',
            layout: 'table',
            columns: [
              { label: 'Date', field: 'date' },
              { label: 'Room', field: 'room' },
              { label: 'Client', field: 'client' },
              { label: 'Hours', field: 'hours' },
              { label: 'Deposit', field: null, values: bookingCells.map((_, index) => (index % 2 ? 'Paid' : 'Due')) },
            ],
            side: [],
            seen_at: iso(now - 2 * HOUR - 14 * MINUTE),
          },
        }),
      ],
    },
    load: { bookings: 6 },
    cached: 0.9,
    brokeHoursAgo: 2,
    pace: { used: 0, per_hour: 60, others: [] },
  };

  // Needs a person: the site asked its own question.
  const rateFields = [field('route', 'Route'), field('rate', 'Per pallet', 'money'), field('valid', 'Valid until', 'date')];
  const rateCells = [
    ['Hull to Rotterdam', '£118.00', day(now + 9 * DAY)],
    ['Hull to Gdańsk', '£164.50', day(now + 9 * DAY)],
    ['Felixstowe to Antwerp', '£102.00', day(now + 16 * DAY)],
    ['Immingham to Gothenburg', '£187.25', day(now + 16 * DAY)],
  ];
  const marlowe: Seed = {
    api: {
      key: 'marlowe-rates',
      title: 'Freight rates at Marlowe',
      site: { name: 'Marlowe Freight', domain: 'shippers.marlowe.example' },
      words: 'This week\'s pallet rates on your routes, so quotes go out with the right number.',
      workspace: 'harbor',
      lane: 'harbor/reddit',
      account: 'Harbor Supply Ltd',
      stage: 'active',
      health: 'site_check',
      problem: { since: iso(now - 40 * MINUTE) },
      proposal: null,
      sample: { kind: 'rows', columns: ['Route', 'Per pallet'], rows: rateCells.slice(0, 3).map(([route = '', rate = '']) => [route, rate]) },
      read_at: iso(now - 58 * MINUTE),
      version: '1.0.3',
      endpoints: [
        endpoint({
          key: 'pallet_rates',
          title: 'Pallet rates',
          description: 'The current rate for each route on your contract.',
          fields: rateFields,
          rows: rowsOf(rateFields, rateCells),
          total: 4,
          cache_s: 21600,
          p50_ms: 2380,
          page: {
            url: 'https://shippers.marlowe.example/rates/current',
            wordmark: 'Marlowe',
            nav: ['Rates', 'Bookings', 'Documents'],
            heading: 'Contract rates',
            count_words: '4 routes',
            layout: 'table',
            columns: [
              { label: 'Route', field: 'route' },
              { label: 'Rate', field: 'rate' },
              { label: 'Valid to', field: 'valid' },
              { label: 'Contract', field: null, values: rateCells.map(() => 'HS-2026') },
            ],
            side: [],
            seen_at: iso(now - 58 * MINUTE),
          },
        }),
      ],
    },
    load: { pallet_rates: 3 },
    cached: 0.95,
    brokeHoursAgo: 1,
    pace: { used: 0, per_hour: 60, others: [] },
  };

  // Resting: the site asked it to slow down, so it waits.
  const arrivalFields = [field('guest', 'Guest'), field('property', 'Property'), field('arrives', 'Arrives', 'date'), field('nights', 'Nights', 'count')];
  const arrivalCells = [
    ['Lindqvist party', 'Old Harbour Rooms', day(now + DAY), '3'],
    ['M. Adeyemi', 'Old Harbour Rooms', day(now + DAY), '2'],
    ['Fournier family', 'Cliff House', day(now + 2 * DAY), '5'],
    ['R. Novak', 'Cliff House', day(now + 3 * DAY), '1'],
    ['Okafor party', 'The Salt Store', day(now + 3 * DAY), '4'],
  ];
  const portlane: Seed = {
    api: {
      key: 'portlane-arrivals',
      title: 'Arrivals at Portlane Stays',
      site: { name: 'Portlane Stays', domain: 'partners.portlane.example' },
      words: 'Who is arriving at your properties in the next three days, for the welcome messages.',
      workspace: 'meridian',
      lane: 'meridian/reddit',
      account: 'Meridian Travel',
      stage: 'active',
      health: 'resting',
      problem: { since: iso(now - 50 * MINUTE), resumes_at: iso(now + 2 * HOUR + 10 * MINUTE) },
      proposal: null,
      sample: { kind: 'rows', columns: ['Guest', 'Property', 'Arrives'], rows: arrivalCells.slice(0, 3).map(([guest = '', property = '', arrives = '']) => [guest, property, arrives]) },
      read_at: iso(now - 52 * MINUTE),
      version: '1.1.1',
      endpoints: [
        endpoint({
          key: 'arrivals',
          title: 'Arrivals',
          description: 'Every booking that starts in the next three days.',
          fields: arrivalFields,
          rows: rowsOf(arrivalFields, arrivalCells),
          total: 5,
          cache_s: 1800,
          p50_ms: 2120,
          page: {
            url: 'https://partners.portlane.example/arrivals',
            wordmark: 'Portlane',
            nav: ['Arrivals', 'Calendar', 'Rates', 'Payouts'],
            heading: 'Arrivals',
            count_words: '5 in 3 days',
            layout: 'table',
            columns: [
              { label: 'Guest', field: 'guest' },
              { label: 'Stay at', field: 'property' },
              { label: 'Check-in', field: 'arrives' },
              { label: 'Nights', field: 'nights' },
              { label: 'Payout', field: null, values: arrivalCells.map(() => 'Pending') },
            ],
            side: [],
            seen_at: iso(now - 52 * MINUTE),
          },
        }),
      ],
    },
    load: { arrivals: 11 },
    cached: 0.68,
    brokeHoursAgo: 1,
    pace: { used: 0, per_hour: 60, others: [{ label: 'The arrivals routine', used: 0 }] },
  };

  // Paused by a person.
  const slotFields = [field('date', 'Date', 'date'), field('window', 'Window'), field('left', 'Crates left', 'count')];
  const slotCells = [
    [day(now + DAY), '06:00 to 08:00', '12'],
    [day(now + 2 * DAY), '06:00 to 08:00', '4'],
    [day(now + 3 * DAY), '09:00 to 11:00', '20'],
    [day(now + 4 * DAY), '06:00 to 08:00', '8'],
  ];
  const greenleaf: Seed = {
    api: {
      key: 'greenleaf-deliveries',
      title: 'Delivery slots at Greenleaf Dairy',
      site: { name: 'Greenleaf Dairy', domain: 'trade.greenleaf.example' },
      words: 'The next free milk delivery windows, and how many crates are left in each.',
      workspace: 'aster',
      lane: 'aster/sheets',
      account: 'Aster Coffee',
      stage: 'paused',
      health: 'paused',
      problem: { since: iso(now - 3 * DAY), by: 'Demo Owner' },
      proposal: null,
      sample: { kind: 'rows', columns: ['Date', 'Window', 'Crates left'], rows: slotCells.slice(0, 3).map(([date = '', window = '', left = '']) => [date, window, left]) },
      read_at: iso(now - 3 * DAY - HOUR),
      version: '1.0.1',
      endpoints: [
        endpoint({
          key: 'slots',
          title: 'Delivery slots',
          description: 'Free windows in the next week.',
          fields: slotFields,
          rows: rowsOf(slotFields, slotCells),
          total: 4,
          cache_s: 3600,
          p50_ms: 1610,
          page: {
            url: 'https://trade.greenleaf.example/slots',
            wordmark: 'Greenleaf',
            nav: ['Order', 'Slots', 'Invoices'],
            heading: 'Delivery slots',
            count_words: '4 free',
            layout: 'table',
            columns: [
              { label: 'Date', field: 'date' },
              { label: 'Window', field: 'window' },
              { label: 'Left', field: 'left' },
              { label: 'Van', field: null, values: slotCells.map((_, index) => `V${index + 2}`) },
            ],
            side: [],
            seen_at: iso(now - 3 * DAY - HOUR),
          },
        }),
      ],
    },
    load: { slots: 9 },
    cached: 0.8,
    stopped: true,
    brokeHoursAgo: 72,
    pace: { used: 0, per_hour: 60, others: [] },
  };

  // Fewer rows than it expected, which usually means the site changed.
  const reviewFields = [field('tour', 'Tour'), field('stars', 'Stars', 'count'), field('review', 'Review'), field('when', 'When', 'date')];
  const reviewCells = [
    ['Curonian Spit by bike', '5', 'Our guide knew every bird by name.', 'Monday'],
    ['Old Town at dusk', '4', 'Lovely walk, slightly rushed at the end.', 'Sunday'],
    ['Amber coast day trip', '5', 'The kids still talk about the museum.', 'Sunday'],
  ];
  const wayfarer: Seed = {
    api: {
      key: 'wayfarer-reviews',
      title: 'New reviews at Wayfarer',
      site: { name: 'Wayfarer Reviews', domain: 'owners.wayfarer.example' },
      words: 'What people wrote about your tours this week, for the Monday meeting.',
      workspace: 'meridian',
      lane: 'meridian/tripadvisor',
      account: 'Meridian Travel',
      stage: 'active',
      health: 'empty',
      problem: { since: iso(now - 3 * HOUR), expected: 5, found: 0 },
      proposal: null,
      sample: { kind: 'quotes', items: reviewCells.slice(0, 2).map(([who = '', , text = '', when = '']) => ({ who, text, when })) },
      read_at: iso(now - 3 * HOUR),
      version: '1.0.0',
      endpoints: [
        endpoint({
          key: 'reviews',
          title: 'New reviews',
          description: 'Reviews from the last seven days.',
          fields: reviewFields,
          rows: rowsOf(reviewFields, reviewCells),
          total: 0,
          cache_s: 3600,
          expect_min: 5,
          p50_ms: 1990,
          page: {
            url: 'https://owners.wayfarer.example/reviews?range=7d',
            wordmark: 'Wayfarer',
            nav: ['Listings', 'Reviews', 'Insights'],
            heading: 'Reviews this week',
            count_words: '0 shown',
            layout: 'table',
            columns: [
              { label: 'Tour', field: 'tour' },
              { label: 'Rating', field: 'stars' },
              { label: 'Review', field: 'review' },
              { label: 'Date', field: 'when' },
            ],
            side: [],
            seen_at: iso(now - 3 * HOUR),
          },
        }),
      ],
    },
    load: { reviews: 5 },
    cached: 0.86,
    brokeHoursAgo: 3,
    pace: { used: 1, per_hour: 60, others: [] },
  };

  // An agent's proposal: tried once, and nothing runs until a person turns it on.
  const priceFields = [field('coffee', 'Coffee'), field('per_kg', 'Per kg', 'money'), field('lot', 'Lot'), field('in_stock', 'In stock', 'status')];
  const priceCells = [
    ['Guji natural, Ethiopia', '€14.80', 'L-2291', 'Yes'],
    ['Huila washed, Colombia', '€12.40', 'L-2304', 'Yes'],
    ['Nyeri AA, Kenya', '€16.20', 'L-2277', 'Low'],
    ['Cerrado pulped, Brazil', '€9.90', 'L-2310', 'Yes'],
    ['Antigua, Guatemala', '€12.90', 'L-2298', 'Yes'],
    ['Sidamo decaf, Ethiopia', '€13.60', 'L-2283', 'Low'],
  ];
  const proposal: Seed = {
    api: {
      key: 'nordic-prices',
      title: 'Price list at Nordic Beans',
      site: { name: 'Nordic Beans', domain: 'orders.nordicbeans.example' },
      words: 'Today\'s price per kilo for every coffee in the catalogue, and what is running low.',
      workspace: 'aster',
      lane: 'aster/portal',
      account: 'Aster Coffee',
      stage: 'proposed',
      health: 'good',
      problem: null,
      proposal: {
        by: 'Claude Code',
        at: iso(now - 35 * MINUTE),
        tried_rows: 24,
        steps: ['Open orders.nordicbeans.example/catalogue', 'Read the table named Catalogue', 'Keep 4 of its 7 columns'],
      },
      sample: { kind: 'rows', columns: ['Coffee', 'Per kg', 'In stock'], rows: priceCells.slice(0, 3).map(([coffee = '', perKg = '', , stock = '']) => [coffee, perKg, stock]) },
      read_at: null,
      version: '0.1.0',
      endpoints: [
        endpoint({
          key: 'price_list',
          title: 'Price list',
          description: 'Every coffee in the catalogue.',
          fields: priceFields,
          rows: rowsOf(priceFields, priceCells),
          total: 24,
          cache_s: 3600,
          p50_ms: 1700,
          page: {
            url: 'https://orders.nordicbeans.example/catalogue',
            wordmark: 'Nordic Beans',
            nav: ['Catalogue', 'Orders', 'Invoices', 'Samples', 'Help'],
            heading: 'Catalogue',
            count_words: '24 lots',
            layout: 'table',
            columns: [
              { label: 'Coffee', field: 'coffee' },
              { label: 'Process', field: null, values: priceCells.map((_, index) => (index % 2 ? 'Washed' : 'Natural')) },
              { label: 'Per kg', field: 'per_kg' },
              { label: 'Lot', field: 'lot' },
              { label: 'Cupping', field: null, values: priceCells.map((_, index) => String(84 + (index % 4))) },
              { label: 'Stock', field: 'in_stock' },
              { label: 'Bag size', field: null, values: priceCells.map(() => '12 kg') },
            ],
            side: [],
            seen_at: seen(35),
          },
        }),
      ],
    },
    load: {},
    cached: 0,
    stopped: true,
    brokeHoursAgo: WEEK_HOURS,
    pace: { used: 1, per_hour: 60, others: [{ label: 'The bean order routine', used: 9 }] },
  };

  return [nordic, tollbook, shelfmark, inkwell, fernway, lumenhall, marlowe, portlane, greenleaf, wayfarer, proposal];
}

/** Business hours carry the week, a job at seven in the morning is a spike, and weekends go quiet. */
function shape(at: number) {
  const date = new Date(at);
  const hour = date.getHours();
  const weekend = date.getDay() === 0 || date.getDay() === 6;
  const day = hour >= 8 && hour <= 18 ? 0.55 + 0.45 * Math.sin(((hour - 8) / 10) * Math.PI) : 0.08;
  const morning = hour === 7 ? 1.25 : 0;
  return Math.max(day, morning) * (weekend ? 0.3 : 1);
}

function usageOf(seed: Seed, now: number): SiteUsageHour[] {
  const random = randomFrom(seed.api.key);
  const lastHour = Math.floor(now / HOUR) * HOUR;
  return Array.from({ length: WEEK_HOURS }, (_, index) => {
    const hoursAgo = WEEK_HOURS - 1 - index;
    const at = lastHour - hoursAgo * HOUR;
    const broken = seed.brokeHoursAgo !== undefined && hoursAgo < seed.brokeHoursAgo;
    const calls = Object.fromEntries(
      Object.entries(seed.load).map(([key, load]) => {
        if (broken && seed.stopped) return [key, { cached: 0, fresh: 0, errors: 0 }];
        const count = Math.round(load * shape(at) * (0.72 + random() * 0.56));
        if (broken) return [key, { cached: 0, fresh: 0, errors: count }];
        const cached = Math.round(count * seed.cached);
        const errors = random() > 0.97 ? 1 : 0;
        return [key, { cached, fresh: Math.max(0, count - cached - errors), errors }];
      }),
    );
    return { at: iso(at), calls };
  });
}

function sparkOf(hours: SiteUsageHour[]) {
  const spark: SiteApi['spark'] = [];
  for (let index = 0; index < hours.length; index += 6) {
    const bucket = { cached: 0, fresh: 0, errors: 0 };
    for (const hour of hours.slice(index, index + 6)) {
      for (const call of Object.values(hour.calls)) {
        bucket.cached += call.cached;
        bucket.fresh += call.fresh;
        bucket.errors += call.errors;
      }
    }
    spark.push(bucket);
  }
  return spark;
}

interface SiteStore {
  apis: Map<string, SiteApiDetail>;
  usage: Map<string, SiteUsageHour[]>;
  keys: SiteKey[];
  /** What the try box has read lately, so a second try can answer from the cache. */
  warm: Map<string, number>;
}

const stores = new WeakMap<OrgData, SiteStore>();

function hoursOfPace(random: () => number, used: number, per_hour: number) {
  return Array.from({ length: 12 }, (_, index) => (index === 11 ? used : Math.round(per_hour * (0.08 + random() * 0.3))));
}

function storeOf(data: OrgData, now = Date.now()): SiteStore {
  const known = stores.get(data);
  if (known) return known;
  const apis = new Map<string, SiteApiDetail>();
  const usage = new Map<string, SiteUsageHour[]>();
  if (data.org.slug === 'northwind-group') {
    for (const seed of seeds(now)) {
      const hours = usageOf(seed, now);
      usage.set(seed.api.key, hours);
      const spark = sparkOf(hours);
      const sum = (key: 'cached' | 'fresh' | 'errors') => spark.reduce((total, bucket) => total + bucket[key], 0);
      const calls = sum('cached') + sum('fresh') + sum('errors');
      const random = randomFrom(`${seed.api.key}.pace`);
      const lane = data.lanes.find((item) => item.id === seed.api.lane);
      apis.set(seed.api.key, {
        ...seed.api,
        keep_updated: seed.api.keep_updated ?? 'off',
        endpoints: seed.api.endpoints.map((item) => {
          const total = hours.reduce((count, hour) => {
            const call = hour.calls[item.key];
            return count + (call ? call.cached + call.fresh + call.errors : 0);
          }, 0);
          const cached = hours.reduce((count, hour) => count + (hour.calls[item.key]?.cached ?? 0), 0);
          return { ...item, calls_7d: total, cached_pct: total ? Math.round((cached / total) * 100) : 0 };
        }),
        spark,
        calls_7d: calls,
        cached_pct: calls ? Math.round((sum('cached') / calls) * 100) : 0,
        computer: lane?.node ?? 'home-mini',
        pace: { ...seed.pace, hours: hoursOfPace(random, seed.pace.used, seed.pace.per_hour) },
      });
    }
  }
  const store: SiteStore = {
    apis,
    usage,
    warm: new Map(),
    keys:
      data.org.slug === 'northwind-group'
        ? [
            { id: 'sk_stock', label: 'Stock sheet', prefix: 'aos_ab12', apis: ['nordic-beans', 'shelfmark-stock'], endpoints: null, ceiling: 'read', rate_per_min: 60, expires_at: null, last_used_at: iso(now - 4 * MINUTE), created_at: iso(now - 31 * DAY) },
            { id: 'sk_n8n', label: 'n8n order chaser', prefix: 'aos_7kq9', apis: ['nordic-beans'], endpoints: ['order_lookup'], ceiling: 'draft', rate_per_min: 30, expires_at: iso(now + 60 * DAY), last_used_at: iso(now - 52 * MINUTE), created_at: iso(now - 12 * DAY) },
            { id: 'sk_ops', label: 'Ops dashboard', prefix: 'aos_m3xt', apis: ['nordic-beans', 'tollbook-invoices', 'fernway-tracking', 'inkwell-comments'], endpoints: ['open_orders', 'overdue_invoices', 'parcels', 'new_comments'], ceiling: 'read', rate_per_min: 120, expires_at: null, last_used_at: iso(now - 3 * DAY), created_at: iso(now - 88 * DAY) },
          ]
        : [],
  };
  stores.set(data, store);
  return store;
}

/** The list carries no rows, and neither does anything else that does not need them. */
function listItem(api: SiteApiDetail): SiteApi {
  const { endpoints: _endpoints, pace: _pace, computer: _computer, keep_updated: _keep, ...item } = api;
  return item;
}

const RANK: SiteApi['health'][] = ['changed', 'sign_in', 'site_check', 'empty', 'resting', 'paused', 'good'];

function actOn(store: SiteStore, api: SiteApiDetail, action: SiteAction, now: number): SiteApiDetail | null {
  const next = (patch: Partial<SiteApiDetail>) => {
    const item = { ...api, ...patch };
    store.apis.set(api.key, item);
    return item;
  };
  switch (action.kind) {
    case 'read':
      return next({ read_at: iso(now), endpoints: api.endpoints.map((item) => ({ ...item, page: item.page && { ...item.page, seen_at: iso(now) } })), pace: { ...api.pace, used: api.pace.used + 1 } });
    case 'pause':
      return next({ stage: 'paused', health: 'paused', problem: { since: iso(now), by: 'Demo Owner' } });
    case 'resume':
    case 'accept-empty':
      return next({ stage: 'active', health: 'good', problem: null });
    case 'activate':
      return next({ stage: 'active', health: 'good', proposal: null, version: '1.0.0', read_at: iso(now) });
    case 'decline':
      store.apis.delete(api.key);
      return api;
    case 'repair': {
      const moved = api.problem?.field;
      return next({
        health: 'good',
        problem: null,
        read_at: iso(now),
        version: api.version.replace(/\.\d+$/, (patch) => `.${Number(patch.slice(1)) + 1}`),
        endpoints: api.endpoints.map((item) => ({
          ...item,
          rows: item.rows.map((row, index) => (moved ? { ...row, [moved]: api.problem?.now_values?.[index] ?? '' } : row)),
          page: item.page && { ...item.page, seen_at: iso(now), columns: item.page.columns.map((column) => (column.field === moved ? { ...column, label: api.problem?.now_label ?? column.label } : column)) },
        })),
      });
    }
    case 'keep-updated':
      return next({ keep_updated: action.every });
  }
}

const PROBLEMS: Partial<Record<SiteApi['health'], { status: number; code: string; title: string; detail: string }>> = {
  changed: { status: 502, code: 'output_contract_violation', title: 'The site changed shape', detail: 'A field no longer matched what this endpoint promises, so the value was not passed on.' },
  sign_in: { status: 503, code: 'needs_human', title: 'A person is needed', detail: 'The account needs to sign in again. Do not retry.' },
  site_check: { status: 503, code: 'needs_human', title: 'A person is needed', detail: 'The site asked the account holder a question. Do not retry.' },
  resting: { status: 503, code: 'rate_limited', title: 'The site asked for a rest', detail: 'This account is resting until retry_at.' },
  paused: { status: 409, code: 'paused', title: 'Paused', detail: 'A person paused this Site API.' },
  empty: { status: 422, code: 'empty_result', title: 'Fewer rows than expected', detail: 'The page held fewer rows than this endpoint expects, so an empty list was not returned.' },
};

function tryOn(store: SiteStore, api: SiteApiDetail, body: { endpoint: string; params: Record<string, string>; fresh: boolean }, now: number) {
  const item = api.endpoints.find((entry) => entry.key === body.endpoint) ?? api.endpoints[0];
  if (!item) return null;
  const problem = PROBLEMS[api.health];
  if (problem) {
    return {
      status: problem.status,
      ms: 38,
      headers: { 'Content-Type': 'application/problem+json', ...(api.problem?.resumes_at ? { 'Retry-After': String(Math.max(1, Math.round((Date.parse(api.problem.resumes_at) - now) / 1000))) } : {}) },
      body: {
        type: `https://agentos.example/problems/${problem.code}`,
        title: problem.title,
        status: problem.status,
        code: problem.code,
        detail: problem.detail,
        ...(api.problem?.resumes_at ? { retry_at: api.problem.resumes_at } : {}),
      },
    };
  }
  const cacheKey = `${api.key}.${item.key}.${JSON.stringify(body.params)}`;
  const last = store.warm.get(cacheKey);
  const hit = !body.fresh && item.cache_s > 0 && last !== undefined && now - last < item.cache_s * 1000;
  if (!hit) store.warm.set(cacheKey, now);
  const age = hit && last !== undefined ? Math.round((now - last) / 1000) : 0;
  const limit = Number(body.params.limit) || item.rows.length;
  const rows = item.rows.slice(0, limit);
  const ms = hit ? 28 + Math.round(Math.random() * 20) : (item.p50_ms ?? 0) + Math.round(Math.random() * 300);
  const name = item.key.split('_').at(-1) ?? 'rows';
  return {
    status: 200,
    ms,
    headers: {
      'Content-Type': 'application/json',
      Age: String(age),
      ETag: `"${api.version}-${item.key}"`,
      'Agentos-Run': hit ? 'none' : 'run_8f21c04e',
      'RateLimit-Limit': '60',
      'RateLimit-Remaining': '57',
      'RateLimit-Reset': '41',
    },
    body: {
      data: { [name]: rows },
      status: 'succeeded_verified',
      untrusted_fields: item.fields.map((entry) => `${name}[].${entry.key}`),
      private_fields: [],
      truncated: rows.length < (item.total ?? rows.length),
      stale: false,
      cache: { hit, age_s: age },
      source: { api: api.key, endpoint: item.key, version: api.version, surface: api.lane, read_at: iso(hit && last !== undefined ? last : now), run: hit ? null : 'run_8f21c04e' },
    },
  };
}

/** What Teach it finds on an account the demo has a page for. Others say honestly that they found nothing. */
function discover(store: SiteStore, lane: string): SiteDiscovery | null {
  const api = [...store.apis.values()].find((item) => item.lane === lane && item.stage !== 'proposed' && item.endpoints[0]?.page?.layout === 'table');
  const item = api?.endpoints[0];
  const page = item?.page;
  if (!api || !item || !page) return null;
  const noun = page.count_words.replace(/^\d+\s*/, '');
  const kept = api.key === 'nordic-beans' ? 'a table of 42 orders' : `a table of ${item.total} rows`;
  return {
    lane,
    api: api.key,
    site: api.site,
    page,
    rows: item.rows,
    total: item.total ?? item.rows.length,
    regions: [
      { id: 'main', words: kept, sure: 'sure', kind: 'main' },
      ...(page.side.length ? [{ id: 'side', words: `a list of ${page.side.length} notices`, sure: 'likely' as const, kind: 'side' as const }] : []),
      { id: 'nav', words: `a menu of ${page.nav.length} links`, sure: 'unsure', kind: 'nav' },
    ],
    suggestions: [
      { url: page.url, words: `${page.heading}, ${noun || 'the list'}` },
      { url: `https://${api.site.domain}/account`, words: 'Your account' },
      { url: `https://${api.site.domain}/`, words: 'The first page after signing in' },
    ],
  };
}

function makeSecret() {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  const body = btoa(String.fromCharCode(...bytes)).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
  return `aos_${body}`;
}

export function siteApiHandlers({ org, failure }: Pick<Helpers, 'org' | 'failure'>) {
  const store = (data: OrgData) => storeOf(data);
  const notFound = () => failure(404, 'not_found', 'Not found.');
  const json = async <T,>(request: Request) => (await request.clone().json()) as T;
  return [
    http.get(
      '/api/orgs/:org/site-apis',
      org((data) => {
        const items = [...store(data).apis.values()].map(listItem);
        return HttpResponse.json(items.sort((a, b) => RANK.indexOf(a.health) - RANK.indexOf(b.health) || b.calls_7d - a.calls_7d));
      }),
    ),
    http.get(
      '/api/orgs/:org/site-apis/discover',
      org(async (data, { request }) => {
        await delay(500);
        return HttpResponse.json(discover(store(data), new URL(request.url).searchParams.get('lane') ?? ''));
      }),
    ),
    http.get(
      '/api/orgs/:org/site-apis/:api',
      org((data, { params }) => {
        const api = store(data).apis.get(String(params.api));
        return api ? HttpResponse.json(api) : notFound();
      }),
    ),
    http.get(
      '/api/orgs/:org/site-apis/:api/usage',
      org((data, { params }) => HttpResponse.json(store(data).usage.get(String(params.api)) ?? [])),
    ),
    http.post(
      '/api/orgs/:org/site-apis/:api/try',
      org(async (data, { params, request }) => {
        const held = store(data);
        const api = held.apis.get(String(params.api));
        if (!api) return notFound();
        const body = await json<{ endpoint: string; params: Record<string, string>; fresh: boolean }>(request);
        const answer = tryOn(held, api, body, Date.now());
        if (!answer) return notFound();
        await delay(Math.min(answer.ms, 900));
        return HttpResponse.json(answer);
      }),
    ),
    http.post(
      '/api/orgs/:org/site-apis/:api/:action',
      org(async (data, { params, request }) => {
        const held = store(data);
        const api = held.apis.get(String(params.api));
        if (!api) return notFound();
        const action = await json<SiteAction>(request);
        await delay(action.kind === 'read' ? 900 : 120);
        return HttpResponse.json(actOn(held, api, action, Date.now()));
      }),
    ),
    http.get(
      '/api/orgs/:org/site-keys',
      org((data) => HttpResponse.json(store(data).keys)),
    ),
    http.post(
      '/api/orgs/:org/site-keys',
      org(async (data, { request }) => {
        const body = await json<SiteKeyInput>(request);
        const secret = makeSecret();
        const key: SiteKey = { ...body, id: `sk_${secret.slice(4, 12)}`, prefix: secret.slice(0, 8), last_used_at: null, created_at: iso(Date.now()) };
        store(data).keys.unshift(key);
        return HttpResponse.json({ ...key, secret } satisfies SiteKeyCreated, { status: 201 });
      }),
    ),
    http.delete(
      '/api/orgs/:org/site-keys/:id',
      org((data, { params }) => {
        const held = store(data);
        held.keys = held.keys.filter((key) => key.id !== params.id);
        return new HttpResponse(null, { status: 204 });
      }),
    ),
  ];
}
