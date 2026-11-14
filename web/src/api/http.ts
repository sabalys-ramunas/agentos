/**
 * The one place the panel talks to the network. It owns the error shape, the CSRF header,
 * the central 401 handling, and the step-up flow: a `403 step_up_required` from any route
 * opens the re-auth dialog and retries the request, so no screen ever handles it.
 */

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly body: Record<string, unknown>;
  /** Seconds, from the Retry-After header of a 429. */
  readonly retryAfter: number | null;

  constructor(status: number, code: string, message: string, body: Record<string, unknown> = {}, retryAfter: number | null = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.body = body;
    this.retryAfter = retryAfter;
  }
}

/**
 * A list route answering 404 for an org the caller can reach means the route is not served
 * here yet. It becomes this, so a screen can say so calmly instead of showing a failure.
 */
export const NOT_SERVED = 'not_served';

export function isNotServed(error: unknown): boolean {
  return error instanceof ApiError && error.code === NOT_SERVED;
}

/**
 * A node may not answer a report yet, because the wire additions are optional. That reads as
 * null, so a screen can say so truthfully and never confuse it with an empty list.
 */
export async function reported<T>(load: Promise<T | null>): Promise<T | null> {
  try {
    return await load;
  } catch (error) {
    if (error instanceof ApiError && error.code === 'unsupported') return null;
    throw error;
  }
}

/**
 * A 404 that means the route is missing. The node marks its own (`not_served`). The cloud answers a path it does
 * not route with the bare "Not found." envelope, and so does a router with no envelope at all. Any other 404 is
 * the app's answer about one thing, such as a name that is not there, and a screen shows it as an error instead
 * of saying the part is on another machine.
 */
export function isRouteMissing(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.status !== 404) return false;
  if (error.body.not_served === true || error.code === 'error') return true;
  return error.code === 'not_found' && error.message === 'Not found.';
}

export async function served<T>(load: Promise<T>): Promise<T> {
  try {
    return await load;
  } catch (error) {
    if (isRouteMissing(error)) throw new ApiError(404, NOT_SERVED, 'Not served yet.');
    throw error;
  }
}

/** Thrown when the person closes the step-up dialog. Callers stay silent about it. */
export const STEP_UP_CANCELLED = 'step_up_cancelled';

export function isCancelled(error: unknown): boolean {
  return error instanceof ApiError && error.code === STEP_UP_CANCELLED;
}

type StepUpHandler = (reason?: string) => Promise<void>;
type UnauthenticatedHandler = () => void;

let csrfToken: string | null = null;
let stepUpHandler: StepUpHandler | null = null;
let unauthenticatedHandler: UnauthenticatedHandler | null = null;
let pendingStepUp: Promise<void> | null = null;

export function setCsrfToken(token: string | null) {
  csrfToken = token;
}

/** Registered once by the step-up dialog. Resolves when re-auth succeeded. */
export function onStepUpRequired(handler: StepUpHandler | null) {
  stepUpHandler = handler;
}

export function onUnauthenticated(handler: UnauthenticatedHandler | null) {
  unauthenticatedHandler = handler;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  /** Set for routes where a 401 is an answer, not an expired session. */
  allowUnauthenticated?: boolean;
  signal?: AbortSignal;
  /** Said in the step-up dialog, when this request needs more than the usual words. */
  stepUpReason?: string;
}

const SAFE_METHODS = new Set(['GET', 'HEAD']);

async function toApiError(response: Response): Promise<ApiError> {
  let body: Record<string, unknown> = {};
  try {
    body = (await response.json()) as Record<string, unknown>;
  } catch {
    // A proxy or a crashed upstream can answer without JSON. Fall through to the default.
  }
  const error = (body.error ?? {}) as Record<string, unknown>;
  const retryAfter = Number(response.headers.get('retry-after'));
  return new ApiError(
    response.status,
    typeof error.code === 'string' ? error.code : 'error',
    typeof error.message === 'string' ? error.message : 'Something went wrong. Try again.',
    // Extra fields such as `limit` and `current` may sit beside the error or inside it.
    { ...error, ...body },
    Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : null,
  );
}

/**
 * A tab that signed in elsewhere, or opened on a link, can hold no token or an old one.
 * The token is derived from the session, so asking for the session is all it takes.
 */
async function refreshCsrf(): Promise<boolean> {
  const response = await fetch('/api/session', { credentials: 'same-origin' }).catch(() => null);
  if (!response?.ok) return false;
  const session = (await response.json()) as { csrf?: string };
  csrfToken = session.csrf ?? null;
  return csrfToken !== null;
}

function runStepUp(reason?: string): Promise<void> {
  if (!stepUpHandler) {
    return Promise.reject(new ApiError(403, 'step_up_required', 'Confirm it is you to continue.'));
  }
  // Parallel requests that all need step-up share one dialog.
  pendingStepUp ??= stepUpHandler(reason).finally(() => {
    pendingStepUp = null;
  });
  return pendingStepUp;
}

export async function http<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return send<T>(path, options, false);
}

export interface Download {
  blob: Blob;
  /** From Content-Disposition, or null when the server named none. */
  filename: string | null;
}

/**
 * A file from the API, through the same CSRF, 401 and step-up handling as everything else. A
 * plain link could not run the step-up dialog, and would save the error as the file.
 */
export async function download(path: string, options: RequestOptions = {}): Promise<Download> {
  return send<Download>(path, { ...options, raw: true }, false);
}

function filenameOf(response: Response): string | null {
  const match = /filename="?([^";]+)"?/i.exec(response.headers.get('content-disposition') ?? '');
  return match?.[1] ?? null;
}

async function send<T>(path: string, options: RequestOptions & { raw?: boolean }, isRetry: boolean): Promise<T> {
  const method = options.method ?? 'GET';
  const headers: Record<string, string> = { Accept: 'application/json' };
  // A file goes up as its own bytes, with its own type. Everything else is JSON.
  const file = options.body instanceof Blob ? options.body : null;
  if (file) headers['Content-Type'] = file.type || 'application/octet-stream';
  else if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  if (!SAFE_METHODS.has(method) && csrfToken) headers['X-Agentos-CSRF'] = csrfToken;

  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      credentials: 'same-origin',
      signal: options.signal,
      body: file ?? (options.body === undefined ? undefined : JSON.stringify(options.body)),
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
    throw new ApiError(0, 'network', 'Could not reach agentos. Check your connection.');
  }

  if (response.ok && options.raw) {
    return { blob: await response.blob(), filename: filenameOf(response) } as T;
  }

  if (response.ok) {
    const text = await response.text();
    return (text ? JSON.parse(text) : undefined) as T;
  }

  const error = await toApiError(response);

  if (error.code === 'step_up_required' && !isRetry) {
    await runStepUp(options.stepUpReason);
    return send<T>(path, options, true);
  }

  if (error.code === 'csrf_failed' && !isRetry && (await refreshCsrf())) {
    return send<T>(path, options, true);
  }

  if (response.status === 401 && !options.allowUnauthenticated) {
    unauthenticatedHandler?.();
  }

  throw error;
}
