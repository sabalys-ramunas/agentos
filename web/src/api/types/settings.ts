export interface QuietHours {
  enabled: boolean;
  from: string;
  to: string;
  timezone: string;
  /** Payments and irreversible actions still come through. */
  urgent_breaks_through: boolean;
}

export interface NotificationSettings {
  /** The zone in use: the person's own, else the org's, else UTC. */
  timezone?: string;
  timezone_source?: 'person' | 'org' | 'default';
  /** Ready to show: "08:00, Vilnius time". */
  digest?: { time: string; timezone: string; label: string };
  push: boolean;
  email_digest: 'off' | 'daily' | 'weekly';
  /** Email at once for payments and irreversible actions. */
  email_urgent: boolean;
  quiet_hours: QuietHours;
  /** Routine approvals wait this long, so several arrive as one push. */
  batch_minutes: number;
}

export interface PushConfig {
  available: boolean;
  reason: 'not_configured' | 'plan' | null;
  public_key: string | null;
}

export interface PushDevice {
  id: string;
  label: string;
  created_at: string;
  last_sent_at: string | null;
}

export type WebhookEvent = 'approval.requested' | 'approval.decided' | 'approval.expired' | 'run.failed';

export interface WebhookDelivery {
  id: string;
  kind: WebhookEvent | 'ping';
  state: 'pending' | 'sending' | 'delivered' | 'failed' | 'dropped';
  attempts: number;
  /** The receiver's HTTP status, when it answered. */
  status: number | null;
  error: string | null;
  next_attempt_at: string | null;
  created_at: string;
  delivered_at: string | null;
}

export interface Webhook {
  id: string;
  /** Scheme, host and port only. The full URL never comes back. */
  origin: string;
  events: WebhookEvent[];
  enabled: boolean;
  created_at: string;
  last_delivery: WebhookDelivery | null;
}

export interface Retention {
  /** A computer has no plan, so it sends none. */
  plan?: string;
  retention_days: number;
  /** Longer than `retention_days` while a downgrade's hold lasts. */
  kept_days: number;
  hold_until: string | null;
  oldest_at: string | null;
  can_export: boolean;
}
