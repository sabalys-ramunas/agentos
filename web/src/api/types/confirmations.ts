import type { ConfirmLevel, ConfirmMethodId } from './approvals';

/** The setting, as the contract writes it. `when` on money stays `ask`: it is a floor. */
export interface ConfirmationSetting {
  commit: { when: 'auto' | 'ask'; level: ConfirmLevel };
  pay: { when: 'ask'; level: ConfirmLevel };
  irreversible: { when: 'ask'; level: ConfirmLevel };
  after_untrusted: { level: ConfirmLevel };
  /** Off means a yes in chat counts for nothing, wherever it comes from. */
  chat: boolean;
}

/** What a business asks for beyond the org. Levels only rise down the chain. */
export type ConfirmationOverlay = Partial<Pick<ConfirmationSetting, 'commit' | 'pay' | 'irreversible' | 'after_untrusted'>>;

export type ConfirmRow = 'commit' | 'marked' | 'after_untrusted' | 'pay' | 'irreversible';

/** One line of a day that ran on its own: a look, or something that went out. */
export interface ConfirmDayTick {
  /** Minutes after midnight. */
  minute: number;
  business: string;
  kind: 'look' | 'commit';
}

/** One thing that stopped at a person. */
export interface ConfirmDayStop {
  id: string;
  minute: number;
  row: Exclude<ConfirmRow, 'commit'>;
  business: string;
  site: string;
  title: string;
  who: string;
  said: 'yes' | 'no';
  method: ConfirmMethodId;
  /** The chat app, or the device, by name. */
  where: string | null;
}

export interface ConfirmDay {
  /** "Yesterday". */
  label: string;
  date: string;
  ticks: ConfirmDayTick[];
  stops: ConfirmDayStop[];
}

export interface ConfirmationView {
  setting: ConfirmationSetting;
  /** The system default, for the mark on each row and the way back to it. */
  system: ConfirmationSetting;
  overlays: Record<string, ConfirmationOverlay>;
  day: ConfirmDay;
  /** The chat apps that have answered here, by name. */
  clients: { name: string; agent: string; answers: number; last_at: string }[];
  /** A review link as a chat would show it, for the concept page. */
  sample_review?: string | null;
}

/** One place a person can say yes, as the registry describes it. */
export interface ConfirmMethodView {
  id: ConfirmMethodId;
  level: ConfirmLevel;
  status: 'ready' | 'planned';
  /** Set up for this person, so it would reach them today. */
  yours: boolean;
  /** What it is on this person's side: "Demo's MacBook Pro", "Claude Code, ChatGPT". */
  detail: string | null;
  last_used_at: string | null;
}
