/**
 * The six first jobs (startup.md, step 1). Each one is a starting point, not a product: which
 * sites it usually needs, what it asks you about first, and a starter message the person copies
 * into their AI. Social posting is one of six, so nobody concludes agentos is a posting tool.
 */
import type { LucideIcon } from 'lucide-react';
import { ClipboardList, Mail, Megaphone, Receipt, Search, Sparkles } from 'lucide-react';
import type { GoalKey } from '@/api/types';
import type { SetupAsks } from './setup';

export interface Goal {
  key: GoalKey;
  title: string;
  /** One example in the customer's own words. */
  example: string;
  icon: LucideIcon;
  /** The sites a person usually starts with, picked for them on the accounts step. */
  sites: string[];
  /** More sites worth offering for this job, not picked. */
  offer: string[];
  /** What this job usually does that needs a yes, for the try step's "it asks you before it ...". */
  asksBefore: string;
  /** Read only: nothing it does needs a yes. */
  readOnly?: boolean;
  /** What it may ask you about at first (startup.md's "Asks first before" column). */
  asks: SetupAsks;
}

const ALL: SetupAsks = { post: true, pay: true, delete: true };
const POSTS: SetupAsks = { post: true, pay: false, delete: false };

export const GOALS: Goal[] = [
  {
    key: 'post',
    asks: POSTS,
    title: 'Post for a business',
    example: 'Instagram for my coffee shop every Tuesday',
    icon: Megaphone,
    sites: ['instagram', 'facebook'],
    offer: ['linkedin', 'x', 'tiktok', 'blog'],
    asksBefore: 'posts',
  },
  {
    key: 'email',
    asks: POSTS,
    title: 'Handle email',
    example: 'Sort my inbox and draft replies for me to check',
    icon: Mail,
    sites: ['gmail'],
    offer: ['mail'],
    asksBefore: 'sends anything',
  },
  {
    key: 'invoices',
    asks: ALL,
    title: 'Collect invoices and receipts',
    example: 'Download them each month for my bookkeeper',
    icon: Receipt,
    sites: [],
    offer: ['gmail', 'shopify', 'google'],
    asksBefore: 'pays or deletes anything',
  },
  {
    key: 'research',
    asks: { post: false, pay: false, delete: false },
    title: 'Research and keep a list',
    example: 'Track competitor prices every Monday',
    icon: Search,
    sites: [],
    offer: ['google', 'etsy', 'reddit'],
    asksBefore: 'changes anything',
    readOnly: true,
  },
  {
    key: 'forms',
    asks: { post: true, pay: true, delete: false },
    title: 'Fill in forms on websites',
    example: 'Update our listings, book, order',
    icon: ClipboardList,
    sites: [],
    offer: ['google', 'shopify', 'etsy'],
    asksBefore: 'submits or pays',
  },
  {
    key: 'other',
    asks: ALL,
    title: 'Something else',
    example: 'Describe it in your own words',
    icon: Sparkles,
    sites: [],
    offer: ['instagram', 'gmail', 'google'],
    asksBefore: 'posts, sends, pays or deletes',
  },
];

export const goalOf = (key: GoalKey | null | undefined): Goal | null => GOALS.find((goal) => goal.key === key) ?? null;

/**
 * What the person copies into their AI for the first try. It names agentos (so the AI reaches
 * for it), the business, a real site, and ends with "ask me first", so the first approval is the
 * point of the exercise.
 */
export function starterMessage(goal: Goal | null, business: string, site: string | null, own?: string | null): string {
  const where = site ?? 'the website';
  const a = /^[aeiou]/i.test(where) ? 'an' : 'a';
  switch (goal?.key) {
    case 'post':
      return `Using agentos, write ${a} ${where} post for ${business} about what is new this week. Pick a photo we already have, keep it short and friendly. Turn on Ask me first for this account, so I see it before it goes out.`;
    case 'email':
      return `Using agentos, open ${business}'s ${where}, sort today's new mail into what needs me and what can wait, and draft a reply to the most urgent one. Turn on Ask me first for this account, so I see it before it goes out.`;
    case 'invoices':
      return `Using agentos, open ${where} for ${business}, find last month's invoices and receipts, and save them in one folder for my bookkeeper. Ask me before you pay or delete anything.`;
    case 'research':
      return `Using agentos, look up three competitors of ${business} on ${where}, note their prices for our main products, and keep the list so you can update it every Monday. Only read, change nothing.`;
    case 'forms':
      return `Using agentos, open ${where} for ${business} and fill in the form I describe next, but stop before the last step. Turn on Ask me first for this account, so I see it before it is submitted.`;
    default:
      return own?.trim()
        ? `Using agentos, for ${business}: ${own.trim().replace(/\.?$/, '.')} Turn on Ask me first for this account, so I see it before anything goes out.`
        : `Using agentos, open ${where} for ${business} and tell me what you see there. Turn on Ask me first for this account, so I see anything before it goes out.`;
  }
}
