/** The first job (startup.md, step 1). `goal` on an org and on each workspace. */
export type GoalKey = 'post' | 'email' | 'invoices' | 'research' | 'forms' | 'other';

/** One line of Getting started. Each is true on the server, not because a wizard was clicked. */
export type SetupItemKey = 'goal' | 'computer' | 'account' | 'ai' | 'approval' | 'phone' | 'invite';

export interface GettingStartedItem {
  key: SetupItemKey;
  title: string;
  done: boolean;
  /** One plain sentence: what to do, or what is true ("It is connected."). */
  hint: string;
  minutes: number;
}

/** `GET /api/orgs/{org}/getting-started`, worked out from real data on every call. */
export interface GettingStarted {
  goal: { key: GoalKey; title: string } | null;
  items: GettingStartedItem[];
  done: number;
  total: number;
  complete: boolean;
}
