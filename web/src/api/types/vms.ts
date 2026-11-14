// ---- Disposable VMs (GET /api/orgs/{org}/vms), read only ----

export interface Vm {
  id: string;
  lane: string;
  state: 'creating' | 'booting' | 'ready' | 'deleting' | 'deleted' | 'failed' | string;
  profile: string;
  server_type: string;
  location: string;
  created_at: string;
  ready_at: string | null;
  expires_at: string;
  minutes_left: number | null;
  price_hourly_eur: number | null;
  cost_eur_so_far: number | null;
  /** What the agent said it is for: data. */
  purpose: string;
  started_by: string | null;
}

export interface VmList {
  available: boolean;
  reason?: string;
  vms: Vm[];
}
