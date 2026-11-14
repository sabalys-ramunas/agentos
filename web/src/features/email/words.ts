/** What a person reads about email, in one place. No word from the engine reaches these strings. */
import type { Lead, Mailbox, SuppressedLead } from '@/api/types';

export type ConnectStage = 'address' | 'found' | 'password' | 'hand-over' | 'waiting' | 'checks' | 'done' | 'failed';

export const GOOGLE_APP_PASSWORDS = 'https://myaccount.google.com/apppasswords';
export const GOOGLE_SECURITY = 'https://myaccount.google.com/security';

export type Ending = 'wrong-password' | 'two-step-off' | 'imap-blocked' | 'certificate';
export type Check = 'waiting' | 'running' | 'yes' | 'no' | 'stopped';

export interface EndingWords {
  title: string;
  body: string;
  /** Where the picture breaks, and what each check came to. */
  read: Check;
  send: Check;
  caption: string;
}

export function endingWords(ending: Ending, provider: string, domain: string, computer: string): EndingWords {
  switch (ending) {
    case 'wrong-password':
      return {
        title: `${provider} did not accept that password`,
        body: `This usually means the normal ${provider} password went in, and not the 16-letter app password. A new app password takes a minute.`,
        read: 'no',
        send: 'no',
        caption: `${provider} said no at the door, so nothing was saved. The password that was tried is gone from ${computer}.`,
      };
    case 'two-step-off':
      return {
        title: 'Google needs 2-Step Verification first',
        body: 'Google only offers app passwords once 2-Step Verification is on. Turning it on takes about two minutes, and you will want your phone beside you.',
        read: 'waiting',
        send: 'waiting',
        caption: 'Nothing has been tried yet. This step happens at Google, and this page waits for you.',
      };
    case 'imap-blocked':
      return {
        title: 'Your company\'s Google admin has this switched off',
        body: `The password is right and this mailbox can send. But reading mail from outside apps is turned off for ${domain}, and only your admin can turn it on.`,
        read: 'no',
        send: 'yes',
        caption: 'Sending works and reading is blocked, so nothing was saved. A mailbox has to do both.',
      };
    case 'certificate':
      return {
        title: 'This mail server could not prove who it is',
        body: `Its security certificate did not check out, so ${computer} stopped before signing in. Your password never left ${computer}. This is usually an expired certificate, and whoever runs your mail can fix it.`,
        read: 'stopped',
        send: 'stopped',
        caption: `${computer} stopped at the door. It will not go on until the server can prove who it is.`,
      };
  }
}

export const adminNote = (address: string, domain: string) =>
  `Hello,\n\nCould you turn on IMAP access for ${address}? It is blocked for ${domain} in the Google Workspace admin console, so I cannot connect my mailbox to an app I use for work.\n\nThank you`;

export const mailCompanyNote = (domain: string, host: string) =>
  `Hello,\n\nThe security certificate on ${host} does not check out, so mail apps refuse to connect for ${domain}. Could you take a look and renew it if it has expired?\n\nThank you`;

export const HEALTH_WORD: Record<Mailbox['health'], string> = {
  good: 'All good',
  unchecked: 'Not read yet',
  cannot_send: 'Cannot send',
  cannot_read: 'Cannot read',
  needs_password: 'Needs a new password',
};

export const LOOK_WORDS: Record<Exclude<Lead['look'], null>, { short: string; long: string }> = {
  personal_address: { short: 'Personal address', long: 'This looks like a private address, not the company\'s. Writing to a person at home is different from writing to a business.' },
  other_name: { short: 'Not the company\'s name', long: 'The address is not on the company\'s own name. It came from a page, and a stranger wrote that page, so it is worth reading twice.' },
  shared_inbox: { short: 'Shared inbox', long: 'This goes to a shared inbox, so it may not reach this person.' },
};

export const SUPPRESSED_WORD: Record<SuppressedLead['why'], string> = {
  unsubscribed: 'Unsubscribed',
  asked: 'Asked not to hear from you',
  bounced: 'The address stopped working',
};

const COUNTRY: Record<string, string> = { EE: 'Estonia', LV: 'Latvia', LT: 'Lithuania', FI: 'Finland', SE: 'Sweden' };
export const countryName = (code: string) => COUNTRY[code] ?? code;

/** "Estonia, Latvia and Sweden". */
export function inWords(items: string[]): string {
  if (items.length < 2) return items.join('');
  return `${items.slice(0, -1).join(', ')} and ${items.at(-1)}`;
}

/** "2 days ago", spelled out: this page is read by people who do not write "2d". */
export function agoWords(iso: string, now = Date.now()): string {
  const minutes = Math.max(1, Math.round((now - new Date(iso).getTime()) / 60_000));
  if (minutes < 60) return `${minutes} ${minutes === 1 ? 'minute' : 'minutes'} ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} ${hours === 1 ? 'hour' : 'hours'} ago`;
  const days = Math.round(hours / 24);
  if (days < 14) return `${days} ${days === 1 ? 'day' : 'days'} ago`;
  return `on ${new Date(iso).toLocaleDateString('en-GB', { day: 'numeric', month: 'long' })}`;
}

export const firstName = (name: string) => name.split(' ')[0] ?? name;

/** The address split at the @, so the part that says who it belongs to can stand out. */
export function addressParts(address: string): { local: string; domain: string } {
  const at = address.indexOf('@');
  return at < 0 ? { local: address, domain: '' } : { local: address.slice(0, at), domain: address.slice(at) };
}

export const looksLikeAddress = (value: string) => /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(value.trim());

/** What the address already gives away, while a person is still typing. */
export function hintFor(value: string): { word: string; site: string } | null {
  const domain = value.split('@')[1]?.toLowerCase() ?? '';
  if (domain.startsWith('gmail.') || domain.startsWith('googlemail.')) return { word: 'Looks like Gmail', site: 'gmail' };
  if (domain.startsWith('icloud.')) return { word: 'Looks like iCloud', site: 'icloud' };
  return null;
}
