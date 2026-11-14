
// ---- Runs, Checks, Brand and VMs (web/src/api/NEW-HOOKS.md lists them with their mock scenarios) ----

/** How often a running run is read again. The node has no run events yet, so its pages poll. */
export const JOB_POLL_MS = 2_000;
export const JOB_IDLE_POLL_MS = 15_000;
/** The most of each stream a page keeps while it follows a run; older output is let go from the top. */
export const JOB_KEEP_CHARS = 2_000_000;
