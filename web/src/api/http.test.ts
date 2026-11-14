import { http as mock, HttpResponse } from 'msw';
import { server } from '@/test/setup';
import { ApiError, http, isCancelled, isNotServed, NOT_SERVED, onStepUpRequired, onUnauthenticated, served, setCsrfToken, STEP_UP_CANCELLED } from './http';

afterEach(() => {
  onStepUpRequired(null);
  onUnauthenticated(null);
  setCsrfToken(null);
});

const stepUp = () =>
  HttpResponse.json({ error: { code: 'step_up_required', message: 'Confirm it is you.' } }, { status: 403 });

describe('the error shape', () => {
  it('turns the contract error body into an ApiError', async () => {
    server.use(mock.get('/api/x', () => HttpResponse.json({ error: { code: 'plan_limit', message: 'Over.' }, limit: 2, current: 3 }, { status: 402 })));
    const error = await http('/api/x').catch((value: unknown) => value);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 402, code: 'plan_limit', message: 'Over.', body: { limit: 2, current: 3 } });
  });

  it('survives an upstream that does not answer JSON', async () => {
    server.use(mock.get('/api/x', () => new HttpResponse('Bad gateway', { status: 502 })));
    await expect(http('/api/x')).rejects.toMatchObject({ status: 502, code: 'error' });
  });

  it('reports a network failure plainly', async () => {
    server.use(mock.get('/api/x', () => HttpResponse.error()));
    await expect(http('/api/x')).rejects.toMatchObject({ code: 'network' });
  });
});

describe('CSRF', () => {
  it('sends the header on writes and not on reads', async () => {
    setCsrfToken('token-1');
    const seen: (string | null)[] = [];
    const capture = ({ request }: { request: Request }) => {
      seen.push(request.headers.get('X-Agentos-CSRF'));
      return HttpResponse.json({});
    };
    server.use(mock.get('/api/x', capture), mock.post('/api/x', capture));
    await http('/api/x');
    await http('/api/x', { method: 'POST', body: {} });
    expect(seen).toEqual([null, 'token-1']);
  });
});

describe('CSRF refresh', () => {
  it('fetches a fresh token and retries once after csrf_failed', async () => {
    setCsrfToken('stale');
    const seen: (string | null)[] = [];
    server.use(
      mock.get('/api/session', () => HttpResponse.json({ csrf: 'fresh' })),
      mock.post('/api/x', ({ request }) => {
        const token = request.headers.get('X-Agentos-CSRF');
        seen.push(token);
        return token === 'fresh'
          ? HttpResponse.json({ ok: true })
          : HttpResponse.json({ error: { code: 'csrf_failed', message: 'No.' } }, { status: 403 });
      }),
    );
    await expect(http('/api/x', { method: 'POST', body: {} })).resolves.toEqual({ ok: true });
    expect(seen).toEqual(['stale', 'fresh']);
  });
});

describe('rate limits', () => {
  it('reads Retry-After from a 429', async () => {
    server.use(
      mock.get('/api/x', () =>
        HttpResponse.json({ error: { code: 'rate_limited', message: 'Slow down.' } }, { status: 429, headers: { 'Retry-After': '120' } }),
      ),
    );
    await expect(http('/api/x')).rejects.toMatchObject({ code: 'rate_limited', retryAfter: 120 });
  });
});

describe('step-up', () => {
  it('runs the dialog once and retries the request', async () => {
    let calls = 0;
    server.use(mock.delete('/api/x', () => (++calls === 1 ? stepUp() : HttpResponse.json({ ok: true }))));
    const dialog = vi.fn(() => Promise.resolve());
    onStepUpRequired(dialog);
    await expect(http('/api/x', { method: 'DELETE' })).resolves.toEqual({ ok: true });
    expect(dialog).toHaveBeenCalledTimes(1);
    expect(calls).toBe(2);
  });

  it('shares one dialog between parallel requests', async () => {
    const passed = new Set<string>();
    let open: (() => void) | undefined;
    server.use(
      mock.delete('/api/:id', ({ params }) => {
        const id = String(params.id);
        if (passed.has(id)) return HttpResponse.json({ id });
        passed.add(id);
        return stepUp();
      }),
    );
    const dialog = vi.fn(() => new Promise<void>((resolve) => (open = resolve)));
    onStepUpRequired(dialog);
    const both = Promise.all([http('/api/a', { method: 'DELETE' }), http('/api/b', { method: 'DELETE' })]);
    await vi.waitFor(() => expect(open).toBeDefined());
    await new Promise((resolve) => setTimeout(resolve, 20));
    open?.();
    await expect(both).resolves.toEqual([{ id: 'a' }, { id: 'b' }]);
    expect(dialog).toHaveBeenCalledTimes(1);
  });

  it('does not loop if the server still refuses after re-auth', async () => {
    let calls = 0;
    server.use(
      mock.delete('/api/x', () => {
        calls += 1;
        return stepUp();
      }),
    );
    onStepUpRequired(() => Promise.resolve());
    await expect(http('/api/x', { method: 'DELETE' })).rejects.toMatchObject({ code: 'step_up_required' });
    expect(calls).toBe(2);
  });

  it('passes a cancelled dialog through as a silent error', async () => {
    server.use(mock.delete('/api/x', stepUp));
    onStepUpRequired(() => Promise.reject(new ApiError(403, STEP_UP_CANCELLED, 'Cancelled.')));
    const error = await http('/api/x', { method: 'DELETE' }).catch((value: unknown) => value);
    expect(isCancelled(error)).toBe(true);
  });
});

describe('401', () => {
  it('reports an expired session', async () => {
    server.use(mock.get('/api/x', () => HttpResponse.json({ error: { code: 'unauthenticated', message: 'No.' } }, { status: 401 })));
    const handler = vi.fn();
    onUnauthenticated(handler);
    await expect(http('/api/x')).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('stays quiet where a 401 is the answer', async () => {
    server.use(mock.get('/api/session', () => HttpResponse.json({ error: { code: 'unauthenticated', message: 'No.' } }, { status: 401 })));
    const handler = vi.fn();
    onUnauthenticated(handler);
    await expect(http('/api/session', { allowUnauthenticated: true })).rejects.toBeInstanceOf(ApiError);
    expect(handler).not.toHaveBeenCalled();
  });
});

describe('not served', () => {
  it('says a route is not served only when the node marks it, or when no answer came with it', async () => {
    server.use(
      mock.get('/api/gone', () => HttpResponse.json({ error: { code: 'not_found', message: 'No such API route.' }, not_served: true }, { status: 404 })),
      mock.get('/api/bare', () => new HttpResponse('Not Found', { status: 404 })),
      mock.get('/api/unrouted', () => HttpResponse.json({ error: { code: 'not_found', message: 'Not found.' } }, { status: 404 })),
    );
    await expect(served(http('/api/gone'))).rejects.toMatchObject({ status: 404, code: NOT_SERVED });
    await expect(served(http('/api/bare'))).rejects.toMatchObject({ status: 404, code: NOT_SERVED });
    await expect(served(http('/api/unrouted'))).rejects.toMatchObject({ status: 404, code: NOT_SERVED });
    expect(isNotServed(await served(http('/api/gone')).catch((value: unknown) => value))).toBe(true);
  });

  it('shows an answer about one thing, a 404 with its own words, as the error it is', async () => {
    server.use(mock.get('/api/list/x', () => HttpResponse.json({ error: { code: 'not_found', message: 'There is no list with that name on this computer.' } }, { status: 404 })));
    const error = await served(http('/api/list/x')).catch((value: unknown) => value);
    expect(error).toMatchObject({ status: 404, code: 'not_found', message: 'There is no list with that name on this computer.' });
    expect(isNotServed(error)).toBe(false);
  });
});
