/**
 * What the routines really are in the demo: the sentence, the steps and weeks of honest history,
 * so the pages can be judged on something like a working business. Built once per org, on first
 * use, and worked out on the way out so a retry or a saved edit shows everywhere at once.
 */
import type {
  Flow,
  RoutineCatalog,
  RoutineDay,
  RoutineField,
  RoutineList,
  RoutineOperation,
  RoutineOutput,
  RoutineRun,
  RoutineStep,
  RoutineStepRun,
  RoutineStepStatus,
  RoutineTemplate,
  RoutineTrigger,
  RunStatus,
} from '@/api/types';
import { flatSteps } from '@/lib/routines';
import { postPreview } from './art';
import { isOnline, isSupported } from './ask-first';
import type { OrgData } from './seed';

const HOUR = 3_600_000;
const DAY = 24 * HOUR;

const pictures = new Map<string, string>();
const picture = (id: string) => {
  const known = pictures.get(id);
  if (known) return known;
  const made = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(postPreview(id))}`;
  pictures.set(id, made);
  return made;
};

function randomFrom(text: string) {
  let state = 0;
  for (const char of text) state = (state * 31 + char.charCodeAt(0)) >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let mixed = Math.imul(state ^ (state >>> 15), 1 | state);
    mixed = (mixed + Math.imul(mixed ^ (mixed >>> 7), 61 | mixed)) ^ mixed;
    return ((mixed ^ (mixed >>> 14)) >>> 0) / 4294967296;
  };
}

const pick = <T,>(items: readonly T[], index: number): T => items[index % items.length] as T;

function cronValues(field: string, min: number, max: number): Set<number> {
  const values = new Set<number>();
  for (const part of field.split(',')) {
    const [range = '*', step = '1'] = part.split('/');
    const [from, to] = range === '*' ? [min, max] : range.includes('-') ? range.split('-').map(Number) : [Number(range), Number(range)];
    for (let value = from ?? min; value <= (to ?? max); value += Number(step)) values.add(value);
  }
  return values;
}

/** Every fire of a cron line between two instants, walked a day at a time. */
function firesBetween(line: string, from: number, to: number): number[] {
  const [minute = '0', hour = '0', dayOfMonth = '*', month = '*', weekday = '*'] = line.trim().split(/\s+/);
  const minutes = cronValues(minute, 0, 59);
  const hours = cronValues(hour, 0, 23);
  const dates = cronValues(dayOfMonth, 1, 31);
  const months = cronValues(month, 1, 12);
  const days = cronValues(weekday, 0, 7);
  const fires: number[] = [];
  const cursor = new Date(from);
  cursor.setHours(0, 0, 0, 0);
  for (; cursor.getTime() <= to; cursor.setDate(cursor.getDate() + 1)) {
    const day = cursor.getDay();
    if (!dates.has(cursor.getDate()) || !months.has(cursor.getMonth() + 1) || !(days.has(day) || (day === 0 && days.has(7)))) continue;
    for (const h of hours) for (const m of minutes) {
      const at = new Date(cursor);
      at.setHours(h, m, 0, 0);
      if (at.getTime() >= from && at.getTime() <= to) fires.push(at.getTime());
    }
  }
  return fires.sort((a, b) => a - b);
}

const text = (key: string, label: string, untrusted = false): RoutineField => ({ key, label, type: 'text', ...(untrusted ? { untrusted } : {}) });
const number = (key: string, label: string): RoutineField => ({ key, label, type: 'number' });
const list = (key: string, label: string): RoutineField => ({ key, label, type: 'list' });

const op = (id: string, name: string, lane: string, effect: RoutineOperation['effect'], produces: RoutineField[], tags: string[], rest: Partial<RoutineOperation> = {}): RoutineOperation => ({
  id,
  name,
  lane,
  workspace: lane.split('/')[0] ?? '',
  effect,
  params: {},
  produces,
  sends: false,
  tags,
  ...rest,
});

const message = { subject: { type: 'string', label: 'Subject', required: true }, body: { type: 'string', label: 'Message', required: true } } as const;

const OPERATIONS: RoutineOperation[] = [
  op('harbor.xero.overdue', 'Find overdue invoices', 'harbor/xero', 'read', [list('invoices', 'Overdue invoices'), text('invoices_number', 'Invoice number'), text('invoices_customer', 'Customer'), number('invoices_amount', 'Amount'), number('invoices_days', 'Days overdue'), number('count', 'How many')], ['invoices.overdue']),
  op('harbor.xero.remind', 'Send an invoice reminder', 'harbor/xero', 'commit', [text('sent_to', 'Sent to')], ['invoices.remind', 'mail.send'], { sends: true, params: message }),
  op('harbor.xero.bills', 'Collect supplier bills', 'harbor/xero', 'read', [list('bills', 'Bills due'), number('total', 'Total due')], ['bills.due']),
  op('harbor.xero.pay', 'Pay a bill', 'harbor/xero', 'pay', [text('reference', 'Payment reference')], ['bills.pay']),
  op('harbor.shopify.stock', 'Check the stock', 'harbor/shopify', 'read', [number('left', 'Stock left'), text('item', 'Item')], ['shop.stock']),
  op('harbor.shopify.email', 'Email a customer', 'harbor/shopify', 'commit', [text('sent_to', 'Sent to')], ['mail.send', 'shop.email'], { sends: true, params: message }),
  op('harbor.shopify.old-orders', 'Find old orders', 'harbor/shopify', 'read', [list('orders', 'Old orders'), number('count', 'How many')], ['shop.old']),
  op('harbor.shopify.delete', 'Delete orders', 'harbor/shopify', 'irreversible', [number('deleted', 'Deleted')], ['shop.delete']),
  op('harbor.blog.publish', 'Publish on the blog', 'harbor/blog', 'commit', [text('url', 'Address')], ['post.publish'], { params: { title: { type: 'string', label: 'Title', required: true }, body: message.body } }),
  op('harbor.facebook.post', 'Post on Facebook', 'harbor/facebook', 'commit', [text('url', 'Address')], ['post.publish'], { params: { caption: { type: 'string', label: 'Caption', required: true } } }),
  op('aster.sheets.stock', 'Read the stock sheet', 'aster/sheets', 'read', [number('kilos', 'Kilos left'), text('blend', 'Blend')], ['shop.stock']),
  op('aster.sheets.plan', 'Read the content plan', 'aster/sheets', 'read', [text('idea', 'Idea for today'), text('photo', 'Photo')], ['content.plan']),
  op('aster.portal.order', 'Reorder from the supplier', 'aster/portal', 'commit', [text('order_number', 'Order number'), number('total', 'Total')], ['supplier.order']),
  op('aster.gmail.draft', 'Write the newsletter', 'aster/gmail', 'draft', [text('subject', 'Subject'), text('body', 'Newsletter text')], ['mail.draft']),
  op('aster.gmail.send', 'Send an email', 'aster/gmail', 'commit', [number('sent', 'Sent'), number('skipped', 'Skipped')], ['mail.send'], { sends: true, params: message }),
  op('aster.instagram.post', 'Post on Instagram', 'aster/instagram', 'commit', [text('url', 'Address')], ['post.publish'], { params: { caption: { type: 'string', label: 'Caption', required: true } } }),
  op('aster.linkedin.post', 'Post on LinkedIn', 'aster/linkedin', 'commit', [text('url', 'Address')], ['post.publish'], { params: { caption: { type: 'string', label: 'Caption', required: true } } }),
  op('meridian.booking.confirmed', 'Read the booking', 'meridian/booking', 'read', [text('guest', 'Guest'), text('trip', 'Trip'), text('dates', 'Dates')], ['booking.read']),
  op('meridian.booking.supplier-form', 'Fill in the supplier form', 'meridian/booking', 'commit', [text('reference', 'Form reference')], ['form.fill']),
  op('meridian.instagram.post', 'Post on Instagram', 'meridian/instagram', 'commit', [text('url', 'Address')], ['post.publish'], { params: { caption: { type: 'string', label: 'Caption', required: true } } }),
  op('meridian.facebook.boost', 'Boost a post', 'meridian/facebook', 'pay', [text('reference', 'Boost reference')], ['ads.boost']),
  op('solstice.sheets.leads', 'Read the leads sheet', 'solstice/sheets', 'read', [list('leads', 'Quiet leads'), text('leads_name', 'Gallery'), text('leads_contact', 'Contact'), number('count', 'How many')], ['leads.quiet']),
  op('solstice.outlook.send', 'Send an email', 'solstice/outlook', 'commit', [number('sent', 'Sent')], ['mail.send'], { sends: true, params: message }),
  op('solstice.medium.publish', 'Publish on Medium', 'solstice/medium', 'commit', [text('url', 'Address')], ['post.publish'], { params: { title: { type: 'string', label: 'Title', required: true } } }),
  op('solstice.reddit.read', 'Read new posts', 'solstice/reddit', 'read', [list('posts', 'New posts'), text('digest', 'What matters', true)], ['page.read']),
  op('personal.mail.read', 'Read the inbox', 'personal/mail', 'read', [text('digest', 'The digest', true), number('count', 'Emails read')], ['mail.read']),
];

const LISTS: RoutineList[] = [
  { id: 'list.harbor.customers', name: 'Customers with an open invoice', workspace: 'harbor', size: 212, suppressed: 6 },
  { id: 'list.harbor.orders', name: 'People who ordered', workspace: 'harbor', size: 1840, suppressed: 31 },
  { id: 'list.aster.subscribers', name: 'Newsletter subscribers', workspace: 'aster', size: 1284, suppressed: 14 },
  { id: 'list.aster.new', name: 'New subscribers', workspace: 'aster', size: 37, suppressed: 0 },
  { id: 'list.solstice.leads', name: 'Galleries we have met', workspace: 'solstice', size: 86, suppressed: 3 },
];

const act = (id: string, label: string, operation: string, rest: { inputs?: Record<string, string>; list?: string } = {}): RoutineStep => ({
  id,
  kind: 'action',
  label,
  operation,
  inputs: rest.inputs ?? {},
  ...(rest.list ? { send: { list: rest.list } } : {}),
});

interface Sample {
  cause: string;
  result: string;
  output: RoutineOutput | null;
  io: Record<string, { input?: Record<string, unknown>; output?: Record<string, unknown>; short?: string; items?: string[] }>;
  /** Which way a decision went. */
  arm?: 'then' | 'otherwise';
}

interface Blueprint {
  id: string;
  name: string;
  trigger: RoutineTrigger;
  steps: RoutineStep[];
  /** Runs a week, for a routine something outside starts. */
  perWeek?: number;
  /** Most runs can be checked afterwards; a share cannot. */
  unchecked?: number;
  /** How a run can fail, and how often. */
  failure?: { step: string; note: string; rate: number };
  /** How its newest run ended, when that is the story. */
  newest?: 'stuck' | 'waiting' | 'working';
  stuckWords?: { words: string; fix: string | null };
  paused?: boolean;
  sample: (index: number) => Sample;
}

const CUSTOMERS = ['Northern Yachts', 'Baltic Marine', 'Kuršių Sailing', 'Salt & Tar Chandlers', 'Port Ropes', 'Klaipėda Boatyard', 'Tidewater Rentals'];
const INVOICES = [1182, 1187, 1191, 1196, 1203, 1208, 1214, 1219];
const ORDERS = ['Marine rope 16 mm', 'Winter boat cover, medium', 'Stainless shackles', 'Deck brush', 'Fender set', 'Rope 12 mm, 30 m'];
const PEOPLE = ['Lina Berg', 'Tomas Petrauskas', 'Erika Holm', 'Jonas Vaitkus', 'Mira Laine', 'Greta Nowak'];
const GALLERIES = ['River Gallery', 'Museum of the Coast', 'Old Mill Arts', 'Small Press Museum', 'Harbourside Collection'];
const BLENDS = ['Autumn blend', 'Huila, Colombia', 'Yirgacheffe, Ethiopia', 'House espresso'];
const NEWS = ['Our autumn blend is back', 'Saturday cupping at 10:00', 'A new Colombian from Huila', 'We are hiring a head roaster', 'The roastery is open on Sundays now'];
const CAPTIONS: Record<string, string[]> = {
  'aster.instagram.post': ['Our autumn blend is back. Washed Ethiopian with a little dried plum on the finish.', 'Fresh from the roaster: a Colombian from Huila, caramel and red apple.', 'Saturday cupping at 10:00. Bring a friend, we bring the spoons.'],
  'aster.linkedin.post': ['We are hiring a head roaster for the Riverside site.', 'What a year of small batch roasting taught us about waste.'],
  'harbor.facebook.post': ['Winter boat covers are back, in three sizes.', 'Open early on Saturdays for the sailing club.'],
  'harbor.blog.publish': ['How to whip a rope end in five minutes, from our counter.', 'Choosing a winter cover that actually lasts.'],
  'meridian.instagram.post': ['Four quiet days on the Dalmatian coast. Our autumn itinerary is open for booking.', 'Lisbon in winter: warm light, short queues, long lunches.', 'Two seats left on the March trip to Kyoto.'],
  'solstice.medium.publish': ['What we learned from a year of designing for small museums.', 'A short note on our label work, with three photos from the opening.'],
};
const SITE_WORDS: Record<string, string> = { instagram: 'Instagram', linkedin: 'LinkedIn', facebook: 'Facebook', blog: 'the blog', medium: 'Medium' };
const DOMAINS: Record<string, string> = { instagram: 'instagram.com', linkedin: 'linkedin.com', facebook: 'facebook.com', blog: 'harborsupply.example', medium: 'medium.com' };

const post = (id: string, site: string, index: number): Sample => {
  const caption = pick(CAPTIONS[id] ?? [''], index);
  return {
    cause: 'On schedule',
    result: `Posted on ${SITE_WORDS[site] ?? site}`,
    output: { kind: 'post', title: caption, image: picture(`${id}-${index % 6}`), proof: { site: DOMAINS[site] ?? site } },
    io: {
      plan: { output: { idea: caption.slice(0, 40), photo: `autumn-${(index % 6) + 1}.jpg` }, short: 'Found today\'s idea' },
      write: { input: { idea: caption.slice(0, 40) }, output: { caption }, short: `${caption.length} characters` },
      post: { input: { caption }, output: { url: `https://${DOMAINS[site] ?? site}/p/${1000 + index}` }, short: 'Live' },
    },
  };
};

const postSteps = (id: string, siteWords: string, operation: string): RoutineStep[] => {
  const plan = id.startsWith('aster') ? 'aster.sheets.plan' : operation;
  return [act('plan', "pick today's idea", plan), act('write', 'write a caption', plan), act('post', `post it on ${siteWords}`, operation)];
};

const BLUEPRINTS: Blueprint[] = [
  {
    id: 'harbor.xero.invoice-reminders',
    name: 'Invoice reminders',
    trigger: { kind: 'schedule', cron: '0 9 * * 1-5', label: 'every weekday at 09:00' },
    steps: [
      act('find', 'find overdue invoices', 'harbor.xero.overdue'),
      { id: 'each', kind: 'loop', label: 'for each invoice', over: '{{find.invoices}}', item: 'invoice', body: [
        act('remind', 'send a reminder', 'harbor.xero.remind', { list: 'list.harbor.customers', inputs: { subject: 'Invoice {{each.number}} is {{each.days}} days overdue', body: 'Hello {{each.customer}},\n\nA friendly note that invoice {{each.number}} is still open. You can pay it from the link in this email.\n\nHarbor Supply' } }),
      ] },
    ],
    failure: { step: 'remind', note: 'Xero asked the account to slow down, so the rest were left for tomorrow.', rate: 0.05 },
    sample: (index) => {
      const count = 1 + (index % 4);
      const customer = pick(CUSTOMERS, index);
      const invoice = pick(INVOICES, index);
      const days = 3 + (index % 11);
      const amount = 180 + ((index * 67) % 900);
      return {
        cause: 'On schedule',
        result: count === 1 ? '1 reminder sent' : `${count} reminders sent`,
        output: { kind: 'email', title: `Invoice ${invoice} is ${days} days overdue`, who: customer, body: `Hello ${customer}, a friendly note that invoice ${invoice} for €${amount}.00 is still open. You can pay it from the link in this email.`, more: count > 1 ? `and ${count - 1} more` : null, proof: { site: 'go.xero.com' } },
        io: {
          find: { output: { count, invoices: Array.from({ length: count }, (_, at) => `INV-${pick(INVOICES, index + at)}`) }, short: `${count} overdue` },
          each: { input: { list: `${count} invoices` }, short: `${count} of ${count}`, items: Array.from({ length: count }, (_, at) => `INV-${pick(INVOICES, index + at)}, ${pick(CUSTOMERS, index + at)}`) },
          remind: { input: { to: customer, invoice: `INV-${invoice}`, amount: `€${amount}.00` }, output: { sent_to: customer }, short: `${count} sent` },
        },
      };
    },
  },
  {
    id: 'harbor.shopify.new-order',
    name: 'New order handling',
    trigger: { kind: 'webhook', label: 'when a new order arrives', hook: { url: 'https://cloud.openajar.example/in/h_7Yq2', secret_hint: 'whsec_...8f2a', last_received_at: null } },
    steps: [
      act('stock', 'check the stock', 'harbor.shopify.stock'),
      { id: 'enough', kind: 'branch', label: 'if it is in stock', when: { left: '{{stock.left}}', op: 'more_than', right: '0' }, then: [
        act('thanks', 'email the customer', 'harbor.shopify.email', { list: 'list.harbor.orders', inputs: { subject: 'Your order is on its way', body: 'Hello {{trigger.customer}},\n\nThank you for your order. It leaves us tomorrow.' } }),
      ], otherwise: [
        { id: 'tell', kind: 'notify', label: 'tell you', message: '{{stock.item}} has run out. Order {{trigger.order_id}} is waiting.' },
      ] },
    ],
    perWeek: 26,
    failure: { step: 'stock', note: 'Shopify did not answer in time. Nothing was sent to the customer.', rate: 0.02 },
    sample: (index) => {
      const order = 1400 + index;
      const item = pick(ORDERS, index);
      const person = pick(PEOPLE, index);
      const left = index % 11 === 4 ? 0 : 3 + (index % 40);
      const inStock = left > 0;
      return {
        cause: `Order #${order} arrived`,
        result: inStock ? `Told ${person} it is on its way` : `${item} ran out, so you were told`,
        output: inStock
          ? { kind: 'email', title: 'Your order is on its way', who: person, body: `Hello ${person.split(' ')[0]}, thank you for your order. Your ${item.toLowerCase()} leaves us tomorrow.`, proof: { site: 'harborsupply.myshopify.com' } }
          : { kind: 'order', title: `Order #${order}`, who: person, body: `${item} is out of stock. The order is waiting for you.` },
        arm: inStock ? 'then' : 'otherwise',
        io: {
          trigger: { output: { order_id: `#${order}`, customer: person, item, note: index % 3 ? null : 'Please leave it at the back door.' } },
          stock: { input: { item }, output: { left, item }, short: inStock ? `${left} left` : 'None left' },
          enough: { input: { 'Stock left': left }, output: { answer: inStock ? 'Yes' : 'No' }, short: inStock ? 'In stock' : 'Out of stock' },
          thanks: { input: { to: person }, output: { sent_to: person }, short: 'Sent' },
          tell: { output: { told: 'You' }, short: 'Told you' },
        },
      };
    },
  },
  {
    id: 'aster.portal.bean-order',
    name: 'Weekly bean order',
    trigger: { kind: 'schedule', cron: '0 8 * * 1', label: 'every Monday at 08:00' },
    steps: [
      act('stock', 'read the stock sheet', 'aster.sheets.stock'),
      { id: 'low', kind: 'branch', label: 'if it is running low', when: { left: '{{stock.kilos}}', op: 'less_than', right: '40' }, then: [act('order', 'reorder from the supplier', 'aster.portal.order')], otherwise: [] },
      { id: 'tell', kind: 'notify', label: 'tell you what it did', message: '{{stock.kilos}} kg of {{stock.blend}} left.' },
    ],
    newest: 'stuck',
    stuckWords: { words: 'The supplier changed their order page, and the routine can no longer find the Reorder button.', fix: null },
    failure: { step: 'order', note: 'The supplier changed their order page, and the routine can no longer find the Reorder button.', rate: 0.03 },
    sample: (index) => {
      const blend = pick(BLENDS, index);
      const total = 420 + ((index * 53) % 300);
      return {
        cause: 'On schedule',
        result: `Ordered 30 kg of ${blend}`,
        output: { kind: 'order', title: `Order ${5100 + index} with Nordic Beans`, who: blend, amount: total, currency: 'EUR', body: '30 kg, delivery on Thursday.', proof: { site: 'orders.nordicbeans.example' } },
        arm: 'then',
        io: {
          stock: { output: { kilos: 12 + (index % 20), blend }, short: `${12 + (index % 20)} kg left` },
          low: { input: { 'Kilos left': 12 + (index % 20) }, output: { answer: 'Yes' }, short: 'Running low' },
          order: { input: { blend, kilos: 30 }, output: { order_number: `${5100 + index}`, total: `€${total}.00` }, short: `€${total} ordered` },
          tell: { short: 'Told you' },
        },
      };
    },
  },
  {
    id: 'solstice.outlook.outreach',
    name: 'Gallery follow-ups',
    trigger: { kind: 'schedule', cron: '0 10 * * 2', label: 'every Tuesday at 10:00' },
    steps: [
      act('leads', 'find galleries that went quiet', 'solstice.sheets.leads'),
      { id: 'each', kind: 'loop', label: 'for each gallery', over: '{{leads.leads}}', item: 'gallery', body: [
        act('follow', 'send a follow-up', 'solstice.outlook.send', { list: 'list.solstice.leads', inputs: { subject: 'Following up on our chat', body: 'Hello {{each.contact}},\n\nI wanted to follow up on our conversation about {{each.name}}.' } }),
        { id: 'pause', kind: 'wait', label: 'wait an hour', amount: 1, unit: 'hours' },
      ] },
    ],
    newest: 'stuck',
    stuckWords: { words: 'Outlook signed the account out, so the follow-ups did not go.', fix: 'Sign in again' },
    failure: { step: 'follow', note: 'Outlook signed the account out, so the follow-ups did not go.', rate: 0.04 },
    sample: (index) => {
      const gallery = pick(GALLERIES, index);
      const count = 2 + (index % 4);
      return {
        cause: 'On schedule',
        result: `${count} follow-ups sent`,
        output: { kind: 'email', title: `Following up on our chat about ${gallery}`, who: gallery, body: 'Hello, I wanted to follow up on our conversation about your new wayfinding. Would next week suit you for a short call?', more: `and ${count - 1} more`, proof: { site: 'outlook.office.com' } },
        io: {
          leads: { output: { count }, short: `${count} went quiet` },
          each: { short: `${count} of ${count}`, items: Array.from({ length: count }, (_, at) => pick(GALLERIES, index + at)) },
          follow: { input: { to: gallery }, output: { sent: count }, short: `${count} sent` },
          pause: { short: 'Waited 1 hour' },
        },
      };
    },
  },
  {
    id: 'meridian.booking.supplier-form',
    name: 'Supplier forms for new bookings',
    trigger: { kind: 'email', lane: 'meridian/booking', label: 'when a booking is confirmed' },
    steps: [
      act('read', 'read the booking', 'meridian.booking.confirmed'),
      act('form', 'fill in the supplier form', 'meridian.booking.supplier-form'),
      { id: 'tell', kind: 'notify', label: 'tell you', message: '{{read.guest}} is booked on {{read.trip}}.' },
    ],
    perWeek: 5,
    sample: (index) => {
      const person = pick(PEOPLE, index);
      const trip = pick(['Dalmatian coast, autumn', 'Lisbon in winter', 'Kyoto in March', 'Dolomites walking week'], index);
      return {
        cause: `${person} booked ${trip}`,
        result: `Form sent for ${person}`,
        output: { kind: 'form', title: `Supplier form for ${person}`, who: trip, body: `Guests 2 · Dietary: none · Transfer: yes`, proof: { site: 'admin.booking.com' } },
        io: {
          read: { output: { guest: person, trip, dates: 'In 3 weeks' }, short: person },
          form: { input: { guest: person, trip }, output: { reference: `SF-${800 + index}` }, short: `SF-${800 + index}` },
          tell: { short: 'Told you' },
        },
      };
    },
  },
  {
    id: 'aster.gmail.newsletter',
    name: 'Weekly newsletter',
    trigger: { kind: 'schedule', cron: '0 10 * * 4', label: 'every Thursday at 10:00' },
    steps: [
      act('write', 'write the newsletter', 'aster.gmail.draft'),
      { id: 'read', kind: 'ask', label: 'ask you to read it', question: 'Is this newsletter ready to go to {{list}}?' },
      act('send', 'send it to your subscribers', 'aster.gmail.send', { list: 'list.aster.subscribers', inputs: { subject: '{{write.subject}}', body: '{{write.body}}' } }),
    ],
    newest: 'waiting',
    sample: (index) => {
      const title = pick(NEWS, index);
      const sent = 1240 + (index % 30);
      return {
        cause: 'On schedule',
        result: `Sent to ${sent.toLocaleString('en')} subscribers`,
        output: { kind: 'email', title, who: `${sent.toLocaleString('en')} subscribers`, body: 'This week at the roastery: a washed Ethiopian with a little dried plum on the finish, and a seat for you at Saturday\'s cupping.', image: picture(`news-${index % 6}`), proof: { site: 'mail.google.com' } },
        io: {
          write: { output: { subject: title, body: 'This week at the roastery...' }, short: `"${title}"` },
          read: { output: { answer: 'Yes' }, short: 'You said yes' },
          send: { input: { list: 'Newsletter subscribers', subject: title }, output: { sent, skipped: 14 }, short: `${sent.toLocaleString('en')} sent, 14 skipped` },
        },
      };
    },
  },
  {
    id: 'aster.gmail.welcome-series',
    name: 'Welcome email',
    trigger: { kind: 'webhook', label: 'when someone subscribes', hook: { url: 'https://cloud.openajar.example/in/h_9aQz', secret_hint: 'whsec_...41cd', last_received_at: null } },
    steps: [
      { id: 'pause', kind: 'wait', label: 'wait a day', amount: 1, unit: 'days' },
      act('hello', 'send the welcome email', 'aster.gmail.send', { list: 'list.aster.new', inputs: { subject: 'Welcome to Aster Coffee', body: 'Hello {{trigger.name}},\n\nThanks for joining us.' } }),
    ],
    perWeek: 9,
    sample: (index) => {
      const person = pick(PEOPLE, index);
      return {
        cause: `${person} subscribed`,
        result: `Welcomed ${person}`,
        output: { kind: 'email', title: 'Welcome to Aster Coffee', who: person, body: `Hello ${person.split(' ')[0]}, thanks for joining us. Here is what we are roasting this week.`, proof: { site: 'mail.google.com' } },
        io: {
          trigger: { output: { name: person, email: 'hidden' } },
          pause: { short: 'Waited 1 day' },
          hello: { input: { to: person }, output: { sent: 1, skipped: 0 }, short: '1 sent' },
        },
      };
    },
  },
  {
    id: 'harbor.xero.pay-suppliers',
    name: 'Pay the suppliers',
    trigger: { kind: 'schedule', cron: '0 7 1 * *', label: 'on the 1st of each month' },
    steps: [
      act('bills', 'collect the bills that are due', 'harbor.xero.bills'),
      act('pay', 'pay them', 'harbor.xero.pay'),
    ],
    sample: (index) => {
      const total = 2100 + ((index * 311) % 1900);
      return {
        cause: 'On schedule',
        result: `€${total.toLocaleString('en')} paid to 4 suppliers`,
        output: { kind: 'payment', title: 'Monthly supplier run', who: '4 suppliers', amount: total, currency: 'EUR', proof: { site: 'go.xero.com' } },
        io: {
          bills: { output: { bills: ['Nordic Ropeworks', 'Baltic Canvas', 'Port Fasteners', 'Sea Paints'], total: `€${total}.00` }, short: '4 bills due' },
          pay: { input: { total: `€${total}.00` }, output: { reference: `PAY-${300 + index}` }, short: 'You said yes, then paid' },
        },
      };
    },
  },
  {
    id: 'harbor.shopify.delete-old-orders',
    name: 'Clear out old orders',
    trigger: { kind: 'schedule', cron: '0 6 1 1,4,7,10 *', label: 'every quarter' },
    steps: [
      act('find', 'find orders older than 7 years', 'harbor.shopify.old-orders'),
      act('delete', 'delete them', 'harbor.shopify.delete'),
    ],
    sample: (index) => ({
      cause: 'On schedule',
      result: `${40 + index} old orders deleted`,
      output: { kind: 'files', title: `${40 + index} orders deleted`, body: 'All older than 7 years. Kept for your records in the yearly export.', proof: { site: 'harborsupply.myshopify.com' } },
      io: { find: { output: { count: 40 + index }, short: `${40 + index} found` }, delete: { input: { count: 40 + index }, output: { deleted: 40 + index }, short: `${40 + index} deleted` } },
    }),
  },
  {
    id: 'aster.instagram.post',
    name: 'Instagram posts',
    trigger: { kind: 'schedule', cron: '0 9 * * 1,3,5', label: 'Mondays, Wednesdays and Fridays at 09:00' },
    steps: postSteps('aster.instagram.post', 'Instagram', 'aster.instagram.post'),
    failure: { step: 'post', note: 'Instagram asked for a robot check, so the post did not go out.', rate: 0.06 },
    sample: (index) => post('aster.instagram.post', 'instagram', index),
  },
  {
    id: 'aster.linkedin.post',
    name: 'LinkedIn update',
    trigger: { kind: 'schedule', cron: '0 11 * * 2', label: 'every Tuesday at 11:00' },
    steps: postSteps('aster.linkedin.post', 'LinkedIn', 'aster.linkedin.post'),
    sample: (index) => post('aster.linkedin.post', 'linkedin', index),
  },
  {
    id: 'harbor.facebook.post',
    name: 'Facebook posts',
    trigger: { kind: 'schedule', cron: '0 12 * * 3', label: 'every Wednesday at 12:00' },
    steps: postSteps('harbor.facebook.post', 'Facebook', 'harbor.facebook.post'),
    sample: (index) => post('harbor.facebook.post', 'facebook', index),
  },
  {
    id: 'harbor.blog.publish',
    name: 'Blog article',
    trigger: { kind: 'schedule', cron: '30 8 * * 1', label: 'every Monday at 08:30' },
    steps: [
      act('write', 'write the article', 'harbor.blog.publish'),
      { id: 'read', kind: 'ask', label: 'ask you to read it', question: 'Is this article ready to publish?' },
      act('post', 'publish it', 'harbor.blog.publish'),
    ],
    sample: (index) => post('harbor.blog.publish', 'blog', index),
  },
  {
    id: 'meridian.instagram.post',
    name: 'Trip of the day',
    trigger: { kind: 'schedule', cron: '0 18 * * *', label: 'every day at 18:00' },
    steps: postSteps('meridian.instagram.post', 'Instagram', 'meridian.instagram.post'),
    failure: { step: 'post', note: 'Instagram did not accept the photo, so nothing was posted.', rate: 0.05 },
    sample: (index) => post('meridian.instagram.post', 'instagram', index),
  },
  {
    id: 'meridian.facebook.boost',
    name: 'Boost the best post',
    trigger: { kind: 'schedule', cron: '0 9 * * 1', label: 'every Monday at 09:00' },
    steps: [act('boost', 'boost last week\'s best post for $40', 'meridian.facebook.boost')],
    sample: (index) => ({
      cause: 'On schedule',
      result: '$40 boost started',
      output: { kind: 'payment', title: 'Boost for 5 days', who: 'Coastal travellers', amount: 40, currency: 'USD', proof: { site: 'facebook.com' } },
      io: { boost: { input: { budget: '$40' }, output: { reference: `BST-${index}` }, short: 'You said yes, then paid' } },
    }),
  },
  {
    id: 'solstice.medium.publish',
    name: 'Studio notes on Medium',
    trigger: { kind: 'schedule', cron: '0 14 * * 5', label: 'every Friday at 14:00' },
    steps: [act('post', 'publish the next note', 'solstice.medium.publish')],
    unchecked: 0.3,
    sample: (index) => post('solstice.medium.publish', 'medium', index),
  },
  {
    id: 'solstice.reddit.read',
    name: 'Typography watch',
    trigger: { kind: 'page_change', url: 'reddit.com/r/typography', lane: 'solstice/reddit', label: 'when r/typography has new posts' },
    steps: [
      act('read', 'read them', 'solstice.reddit.read'),
      { id: 'tell', kind: 'notify', label: 'tell you what matters', message: '{{read.digest}}' },
    ],
    perWeek: 8,
    unchecked: 1,
    sample: (index) => ({
      cause: 'New posts appeared',
      result: `${2 + (index % 5)} new posts read`,
      output: { kind: 'digest', title: `${2 + (index % 5)} new posts`, body: 'A thread on wayfinding for small museums is worth a look. The rest is font identification.' },
      io: { read: { output: { digest: 'A thread on wayfinding for small museums is worth a look.' }, short: `${2 + (index % 5)} new posts` }, tell: { short: 'Told you' } },
    }),
  },
  {
    id: 'personal.mail.digest',
    name: 'Morning digest',
    trigger: { kind: 'schedule', cron: '0 7 * * *', label: 'every morning at 07:00' },
    steps: [
      act('read', 'read the inbox', 'personal.mail.read'),
      { id: 'tell', kind: 'notify', label: 'send you a digest', message: '{{read.digest}}' },
    ],
    paused: true,
    unchecked: 1,
    sample: (index) => ({
      cause: 'On schedule',
      result: `${12 + (index % 20)} emails read`,
      output: { kind: 'digest', title: `${12 + (index % 20)} emails overnight`, body: 'Two need a reply: the accountant and the school. Everything else can wait.' },
      io: { read: { output: { count: 12 + (index % 20) }, short: `${12 + (index % 20)} emails` }, tell: { short: 'Told you' } },
    }),
  },
];

const TRIGGER_FIELDS: RoutineCatalog['trigger_fields'] = {
  schedule: [text('when', 'When it started')],
  webhook: [text('order_id', 'Order number'), text('customer', 'Customer'), text('item', 'What they ordered'), text('note', 'Their note', true), text('name', 'Their name')],
  email: [text('from', 'Who wrote'), text('subject', 'Subject', true), text('body', 'The email', true)],
  page_change: [text('url', 'The page'), text('what', 'What changed', true)],
  site_change: [{ key: 'added', label: 'New rows', type: 'list', untrusted: true }, { key: 'removed', label: 'Rows that went', type: 'list', untrusted: true }, { key: 'changed', label: 'Rows that changed', type: 'list', untrusted: true }, number('count', 'How many rows')],
  manual: [],
};

const tagged = (id: string, label: string, tag: string, rest: Partial<Extract<RoutineStep, { kind: 'action' }>> = {}): RoutineStep => ({ id, kind: 'action', label, operation: `tag:${tag}`, inputs: {}, ...rest });

const TEMPLATES: RoutineTemplate[] = [
  {
    id: 'order',
    name: 'New order handling',
    sentence: 'When a new order arrives, check the stock, then email the customer.',
    note: 'For a shop. Nobody waits to hear from you.',
    icon: 'order',
    trigger: { kind: 'webhook', label: 'when a new order arrives' },
    steps: [
      tagged('stock', 'check the stock', 'shop.stock'),
      { id: 'enough', kind: 'branch', label: 'if it is in stock', when: { left: null, op: 'more_than', right: '0' }, then: [tagged('thanks', 'email the customer', 'mail.send', { send: { list: null } })], otherwise: [{ id: 'tell', kind: 'notify', label: 'tell you', message: 'Something ran out.' }] },
    ],
  },
  {
    id: 'invoice',
    name: 'Invoice reminders',
    sentence: 'Every weekday at 09:00, find overdue invoices, then for each one, send a reminder.',
    note: 'Get paid without chasing anyone yourself.',
    icon: 'invoice',
    trigger: { kind: 'schedule', cron: '0 9 * * 1-5', label: 'every weekday at 09:00' },
    steps: [
      tagged('find', 'find overdue invoices', 'invoices.overdue'),
      { id: 'each', kind: 'loop', label: 'for each invoice', over: '{{find.invoices}}', item: 'invoice', body: [tagged('remind', 'send a reminder', 'invoices.remind', { send: { list: null } })] },
    ],
  },
  {
    id: 'newsletter',
    name: 'Newsletter',
    sentence: 'Every Thursday at 10:00, write the newsletter, ask you to read it, then send it to your subscribers.',
    note: 'You stay the editor. It does the rest.',
    icon: 'newsletter',
    trigger: { kind: 'schedule', cron: '0 10 * * 4', label: 'every Thursday at 10:00' },
    steps: [
      tagged('write', 'write the newsletter', 'mail.draft'),
      { id: 'read', kind: 'ask', label: 'ask you to read it', question: 'Is this newsletter ready to go?' },
      tagged('send', 'send it to your subscribers', 'mail.send', { send: { list: null } }),
    ],
  },
  {
    id: 'outreach',
    name: 'Customer outreach',
    sentence: 'Every Tuesday at 10:00, find people who went quiet, then for each one, send a follow-up.',
    note: 'To your own list of people you already know.',
    icon: 'outreach',
    trigger: { kind: 'schedule', cron: '0 10 * * 2', label: 'every Tuesday at 10:00' },
    steps: [
      tagged('find', 'find people who went quiet', 'leads.quiet'),
      { id: 'each', kind: 'loop', label: 'for each person', over: '{{find.leads}}', item: 'person', body: [tagged('follow', 'send a follow-up', 'mail.send', { send: { list: null } })] },
    ],
  },
  {
    id: 'post',
    name: 'Social posts',
    sentence: 'Every Friday at 09:00, pick an idea, ask you to read it, then post it.',
    note: 'One post, on the accounts you choose.',
    icon: 'post',
    trigger: { kind: 'schedule', cron: '0 9 * * 5', label: 'every Friday at 09:00' },
    steps: [
      tagged('plan', 'pick an idea', 'content.plan'),
      { id: 'read', kind: 'ask', label: 'ask you to read it', question: 'Is this post ready to go out?' },
      tagged('post', 'post it', 'post.publish'),
    ],
  },
  {
    id: 'webhook',
    name: 'Webhook job',
    sentence: 'When your app sends a webhook, run a job, then tell you how it went.',
    note: 'For developers. Signed, replay-safe, and defined as a file.',
    icon: 'webhook',
    developer: true,
    trigger: { kind: 'webhook', label: 'when your app sends a webhook' },
    steps: [
      { id: 'job', kind: 'action', label: 'run a job', operation: null, inputs: {} },
      { id: 'tell', kind: 'notify', label: 'tell you how it went', message: 'Done.' },
    ],
  },
];

interface OrgRoutines {
  runs: Map<string, RoutineRun[]>;
  cleared: Set<string>;
  edits: Map<string, Partial<Blueprint>>;
}

const memory = new WeakMap<OrgData, OrgRoutines>();

const OPEN: RunStatus[] = ['running', 'waiting_approval', 'queued'];

function eventTimes(blueprint: Blueprint, now: number, random: () => number): number[] {
  const out: number[] = [];
  const weeks = 10;
  const total = Math.round((blueprint.perWeek ?? 2) * weeks);
  for (let index = 0; index < total; index += 1) {
    const day = now - random() * weeks * 7 * DAY;
    const at = new Date(day);
    at.setHours(8 + Math.floor(random() * 12), Math.floor(random() * 60), 0, 0);
    if (at.getTime() < now) out.push(at.getTime());
  }
  return out.sort((a, b) => a - b);
}

function stepRuns(blueprint: Blueprint, sample: Sample, start: number, failAt: string | null, note: string | null, random: () => number, open: RunStatus | null): { steps: RoutineStepRun[]; end: number } {
  const out: RoutineStepRun[] = [];
  let cursor = start;
  let stopped = false;
  const skippedArm = sample.arm === 'otherwise' ? 'then' : 'otherwise';
  const walk = (steps: RoutineStep[], skipped = false) => {
    for (const step of steps) {
      const io = sample.io[step.id];
      const skip = skipped || stopped;
      const took = step.kind === 'wait' ? 0 : 400 + random() * (step.kind === 'action' ? 5200 : 300);
      const failing = !skip && step.id === failAt;
      const asking = !skip && open === 'waiting_approval' && step.kind === 'ask';
      const status: RoutineStepStatus = skip ? 'skipped' : failing ? 'failed' : asking ? 'waiting_approval' : step.kind === 'action' ? 'succeeded_verified' : 'succeeded_unverified';
      out.push({
        step: step.id,
        label: step.label,
        kind: step.kind,
        status,
        started_at: skip ? null : new Date(cursor).toISOString(),
        finished_at: skip || asking ? null : new Date(cursor + took).toISOString(),
        input: skip ? null : (io?.input ?? null),
        output: skip || failing || asking ? null : (io?.output ?? null),
        short: skip ? null : failing ? 'Did not work' : asking ? 'Waiting for you' : (io?.short ?? null),
        note: failing ? note : skip && !stopped ? 'This path was not taken.' : stopped ? 'Not reached.' : null,
        attempts: 1,
        ...(io?.items && !skip ? { items: io.items.map((label) => ({ label, status: 'succeeded_verified' as const })) } : {}),
      });
      if (!skip) cursor += took;
      if (failing || asking) stopped = true;
      if (failing) for (const earlier of out) if (earlier.kind === 'loop') Object.assign(earlier, { short: 'Stopped partway', status: 'partial', items: undefined });
      if (step.kind === 'branch') {
        walk(step.then, skipped || skippedArm === 'then');
        walk(step.otherwise, skipped || skippedArm === 'otherwise');
      }
      if (step.kind === 'loop') walk(step.body, skipped);
    }
  };
  walk(blueprint.steps);
  return { steps: out, end: cursor };
}

function makeRuns(blueprint: Blueprint, now: number): RoutineRun[] {
  const random = randomFrom(blueprint.id);
  const times = blueprint.trigger.kind === 'schedule' && blueprint.trigger.cron ? firesBetween(blueprint.trigger.cron, now - 120 * DAY, now) : eventTimes(blueprint, now, random);
  const lastPaused = blueprint.paused ? now - 9 * DAY : Infinity;
  const fires = times.filter((at) => at < lastPaused).slice(-300);
  const runs = fires.map((at, index): RoutineRun => {
    const newest = index === fires.length - 1;
    const story = newest ? blueprint.newest : undefined;
    // Something that failed and nobody dealt with is stuck, so only the story ends on a failure.
    const failed = story === 'stuck' || (!story && !newest && blueprint.failure ? random() < blueprint.failure.rate : false);
    const partial = !failed && !story && !newest && random() < 0.025;
    const sample = blueprint.sample(index);
    const open: RunStatus | null = story === 'waiting' ? 'waiting_approval' : null;
    const note = story === 'stuck' ? (blueprint.stuckWords?.words ?? blueprint.failure?.note ?? null) : (blueprint.failure?.note ?? null);
    const { steps, end } = stepRuns(blueprint, sample, at + 800, failed ? (blueprint.failure?.step ?? null) : null, note, random, open);
    const unchecked = random() < (blueprint.unchecked ?? 0.1);
    const status: RunStatus = open ?? (failed ? 'failed' : partial ? 'partial' : unchecked ? 'succeeded_unverified' : 'succeeded_verified');
    const output = failed || open ? null : sample.output && (unchecked ? { ...sample.output, proof: null } : sample.output);
    return {
      id: `run_${blueprint.id.replaceAll('.', '_')}_${index}`,
      flow: blueprint.id,
      version: 1 + Math.floor((index / Math.max(fires.length, 1)) * 3),
      status,
      cause: sample.cause,
      result: open ? 'Waiting for your yes' : failed ? (note ?? 'It did not finish') : partial ? `${sample.result}, then stopped` : sample.result,
      by: 'trigger',
      started_at: new Date(at).toISOString(),
      finished_at: open ? null : new Date(end).toISOString(),
      output,
      steps,
      cost: { tokens: 800 + Math.floor(random() * 4000), usd: Math.round((0.02 + random() * 0.09) * 100) / 100 },
    };
  });
  return runs.reverse();
}

function routinesOf(data: OrgData, now = Date.now()): OrgRoutines {
  const known = memory.get(data);
  if (known) return known;
  const made: OrgRoutines = { runs: new Map(), cleared: new Set(), edits: new Map() };
  for (const blueprint of BLUEPRINTS) if (data.flows.some((flow) => flow.id === blueprint.id)) made.runs.set(blueprint.id, makeRuns(blueprint, now));
  memory.set(data, made);
  return made;
}

export const blueprintOf = (id: string) => BLUEPRINTS.find((item) => item.id === id);

export const routineRuns = (data: OrgData, flow: string) => routinesOf(data).runs.get(flow) ?? [];

function nextFire(blueprint: Blueprint, now: number): string | null {
  if (blueprint.paused || blueprint.trigger.kind !== 'schedule' || !blueprint.trigger.cron) return null;
  const at = firesBetween(blueprint.trigger.cron, now, now + 100 * DAY)[0];
  return at ? new Date(at).toISOString() : null;
}

const sitesOf = (steps: RoutineStep[], trigger: RoutineTrigger) => {
  const lanes = [trigger.lane, ...flatSteps(steps).map((step) => (step.kind === 'action' ? OPERATIONS.find((item) => item.id === step.operation)?.lane : null))];
  return [...new Set(lanes.filter((lane): lane is string => Boolean(lane)).map((lane) => lane.split('/')[1] ?? lane))];
};

function alwaysAsks(steps: RoutineStep[]): Flow['always_asks_steps'] {
  return flatSteps(steps).flatMap((step): NonNullable<Flow['always_asks_steps']> => {
    if (step.kind === 'ask') return [{ step: step.id, because: 'ask_step' }];
    const effect = step.kind === 'action' ? OPERATIONS.find((item) => item.id === step.operation)?.effect : null;
    return effect === 'pay' || effect === 'irreversible' ? [{ step: step.id, because: effect }] : [];
  });
}

function sendLimits(steps: RoutineStep[], id: string): Flow['send_limits'] {
  return flatSteps(steps).flatMap((step) => {
    if (step.kind !== 'action' || !OPERATIONS.find((item) => item.id === step.operation)?.sends) return [];
    const known = LISTS.find((item) => item.id === step.send?.list);
    return [{ step: step.id, list: known?.name ?? null, list_size: known?.size ?? 0, daily_limit: 200, sent_today: id === 'aster.gmail.newsletter' ? 0 : (known?.size ?? 0) % 37, suppressed: known?.suppressed ?? 0, unsubscribe: true as const }];
  });
}

/** The routine, as the cloud answers it: its steps, its next run, and what became of the last one. */
export function withRoutine(data: OrgData, flow: Flow): Flow {
  const blueprint = blueprintOf(flow.id);
  if (!blueprint) return { ...flow, name: null, trigger: null, steps: null, sites: [flow.lane.split('/')[1] ?? ''], enabled: true, next_at: null, latest: null, stuck: null, waiting: null };
  const state = routinesOf(data);
  const edit = state.edits.get(flow.id);
  const steps = edit?.steps ?? blueprint.steps;
  const trigger = edit?.trigger ?? blueprint.trigger;
  const runs = state.runs.get(flow.id) ?? [];
  const newest = runs[0];
  const finished = runs.find((run) => !OPEN.includes(run.status));
  const failedStep = newest?.status === 'failed' ? newest.steps.find((step) => step.status === 'failed') : null;
  const lane = failedStep && OPERATIONS.find((item) => item.id === (flatSteps(steps).find((step) => step.id === failedStep.step) as { operation?: string } | undefined)?.operation)?.lane;
  const node = data.nodes.find((item) => item.id === data.lanes.find((candidate) => candidate.id === flow.lane)?.node);
  return {
    ...flow,
    name: edit?.name ?? blueprint.name,
    trigger,
    steps,
    sites: sitesOf(steps, trigger),
    enabled: !blueprint.paused,
    next_at: nextFire(blueprint, Date.now()),
    last_run: newest ? { id: newest.id, status: newest.status, started_at: newest.started_at, cost: newest.cost } : flow.last_run,
    recent_runs: runs.slice(0, 20).map((run) => ({ id: run.id, status: run.status, at: run.started_at })),
    latest: finished ? { at: finished.finished_at ?? finished.started_at, result: finished.result ?? '', status: finished.status } : null,
    stuck: failedStep && newest && !state.cleared.has(newest.id) && blueprint.newest === 'stuck' ? { step: failedStep.step, label: failedStep.label, words: failedStep.note ?? 'It did not finish.', run: newest.id, lane: lane ?? null, fix: blueprint.stuckWords?.fix ?? null } : null,
    waiting: newest?.status === 'waiting_approval' ? { run: newest.id, words: `Waiting for you to read it`, approval_id: null } : null,
    always_asks_steps: alwaysAsks(steps),
    send_limits: sendLimits(steps, flow.id),
    steps_supported: isSupported(node) && isOnline(node) ? true : isSupported(node),
  };
}

export function routineCatalog(data: OrgData): RoutineCatalog {
  const has = (workspace: string) => data.workspaces.some((item) => item.id === workspace);
  return {
    operations: OPERATIONS.filter((item) => has(item.workspace)),
    lists: LISTS.filter((item) => has(item.workspace)),
    templates: TEMPLATES,
    trigger_fields: TRIGGER_FIELDS,
  };
}

/** The line through now: what ran, and what is coming, for a day either side. */
export function routineDay(data: OrgData, now = Date.now()): RoutineDay {
  const state = routinesOf(data);
  const midnight = new Date(now);
  midnight.setHours(0, 0, 0, 0);
  const marks: RoutineDay['marks'] = [];
  let ranToday = 0;
  let checkedToday = 0;
  for (const flow of data.flows) {
    const blueprint = blueprintOf(flow.id);
    if (!blueprint) continue;
    const site = sitesOf(blueprint.steps, blueprint.trigger).at(-1) ?? '';
    for (const run of state.runs.get(flow.id) ?? []) {
      const at = Date.parse(run.started_at);
      if (at < now - DAY) break;
      marks.push({ flow: flow.id, at: run.started_at, site, status: run.status });
      if (at >= midnight.getTime()) {
        ranToday += 1;
        if (run.status === 'succeeded_verified') checkedToday += 1;
      }
    }
    if (!blueprint.paused && blueprint.trigger.cron) {
      for (const at of firesBetween(blueprint.trigger.cron, now, now + DAY)) marks.push({ flow: flow.id, at: new Date(at).toISOString(), site, status: null });
    }
  }
  return { marks: marks.sort((a, b) => a.at.localeCompare(b.at)), ran_today: ranToday, checked_today: checkedToday };
}

/** A failed step tried again: it works, the run finishes, and the routine is no longer stuck. */
export function retryRun(data: OrgData, flow: string, id: string): RoutineRun | null {
  const state = routinesOf(data);
  const run = state.runs.get(flow)?.find((item) => item.id === id);
  if (!run) return null;
  const blueprint = blueprintOf(flow);
  const failed = run.steps.find((step) => step.status === 'failed');
  if (!failed || !blueprint) return null;
  let reached = false;
  run.steps = run.steps.map((step) => {
    if (step.step === failed.step) reached = true;
    if (!reached || step.note === 'This path was not taken.') return step;
    const now = new Date().toISOString();
    return { ...step, status: 'succeeded_unverified', started_at: now, finished_at: now, note: null, attempts: step.attempts + (step.step === failed.step ? 1 : 0), short: 'Worked on the second try' };
  });
  run.status = 'succeeded_unverified';
  run.result = 'Finished after a retry';
  run.by = 'retry';
  run.finished_at = new Date().toISOString();
  state.cleared.add(run.id);
  return run;
}

export function saveRoutine(data: OrgData, flow: string, edit: Pick<Blueprint, 'name' | 'trigger' | 'steps'>) {
  routinesOf(data).edits.set(flow, edit);
}
