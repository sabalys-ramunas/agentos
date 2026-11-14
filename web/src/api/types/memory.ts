// Memory: what agents remember about a workspace's work. The owner in person reads, confirms, reviews and
// forgets it (src/agentos/api/routes_memory.py, shaped by src/agentos/api/web/memory.py). Its text was written
// by agents after reading pages, so it is always shown as plain text and never as markup or a link.

/** Who wrote it, and how far it is trusted: the owner, agentos itself, an agent, or an agent that read a page. */
export type MemoryTrust = 'owner' | 'node' | 'agent' | 'untrusted';
export type MemoryType = 'fact' | 'document' | 'decision' | 'preference' | 'contact' | 'run_summary';
export type MemoryState = 'live' | 'expired' | 'forgotten';

/** One workspace that has memory, against the caps, with what waits for a look. */
export interface MemoryWorkspace {
  workspace: string;
  live_entries: number;
  live_entries_cap: number;
  content_bytes: number;
  content_bytes_cap: number;
  /** Agent and untrusted entries nobody has looked at yet. */
  waiting: number;
  /** The record's hash chain checks out. False means it was changed outside agentos: say so, plainly. */
  chain_ok: boolean;
  /** Entries erased for good. Their titles and text are gone; the record of the erasing stays. */
  forgotten: number;
}

export interface MemoryWorkspaces {
  /** False on a computer without the memory service: the screen says so, and offers nothing to change. */
  available: boolean;
  workspaces: MemoryWorkspace[];
}

/** An entry in a list or a search, with its title and a snippet, never its whole text. */
export interface MemoryEntry {
  id: string;
  workspace: string;
  type: MemoryType;
  /** Null once it is forgotten. */
  title: string | null;
  trust: MemoryTrust;
  state: MemoryState;
  suspicious: boolean;
  pinned: boolean;
  labels: string[];
  /** The account it was written on, when a lane wrote it. */
  lane: string | null;
  writer: string;
  at: string;
  version: number;
  snippet?: string | null;
  /** Not in a search hit: whether somebody has looked at it yet. */
  reviewed?: boolean | null;
  expires_at?: string | null;
  /** Why the node will not show it here (for example, it is private to another grant). */
  withheld?: string;
}

export interface MemoryList {
  available: boolean;
  /** Null when the list spans every workspace. */
  workspace: string | null;
  /** Newest first, across the workspaces asked for. */
  entries: MemoryEntry[];
}

export interface MemoryHistoryItem {
  version: number;
  at: string;
  trust: MemoryTrust;
  title: string | null;
  forgotten: boolean;
}

/** What an agent read before it wrote the entry, as the node recorded it. */
export interface MemoryEvidence {
  at: string | null;
  tool: string | null;
  summary: string | null;
}

/** One entry in full. `body` and `title` are text, and null once the entry is forgotten. */
export interface MemoryDetail {
  id: string;
  workspace: string;
  type: MemoryType;
  state: MemoryState;
  version: number;
  current: number;
  trust: MemoryTrust;
  labels: string[];
  pinned: boolean;
  suspicious: boolean;
  flagged: string[];
  writer: string;
  lane: string | null;
  created_at: string;
  updated_at: string;
  reviewed_at: string | null;
  expires_at: string | null;
  title: string | null;
  body: string | null;
  attrs: Record<string, unknown> | null;
  /** The node's own note for an untrusted entry. The app says it in its own words instead. */
  warning?: string | null;
  withheld?: string;
  forgotten: { at: string; by: string; reason: string } | null;
  confirmed_by: string | null;
  evidence: MemoryEvidence[];
  history: MemoryHistoryItem[];
}

export interface MemoryConfirmed {
  id: string;
  workspace: string;
  version: number;
  trust: MemoryTrust;
}

export interface MemoryReviewed {
  id: string;
  reviewed_at: string;
}

export interface MemoryForgotten {
  id: string;
  state: 'forgotten';
  already: boolean;
  at: string | null;
}
