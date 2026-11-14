/**
 * An account's files for the account page: a few files an agent put in and a file its browser downloaded, per
 * account, invented. Built once per org on first use, so an upload or a delete sticks until the page reloads.
 * The limits are the node's own defaults, and the same checks apply: a file is only stored, and an empty
 * file, a web page or an oversized one is refused the way the node refuses it.
 */
import { http, HttpResponse } from 'msw';
import type { AccountFile, FileStatus } from '@/api/types';
import type { OrgData } from './seed';
import type { Helpers } from './s3';

const MB = 1024 * 1024;
const PUT_BYTES = 200 * MB;
const DOWNLOAD_BYTES = 200 * MB;
const KINDS: Record<string, { kind: string; mime: string }> = {
  'image/png': { kind: 'image', mime: 'image/png' },
  'image/jpeg': { kind: 'image', mime: 'image/jpeg' },
  'image/webp': { kind: 'image', mime: 'image/webp' },
  'image/gif': { kind: 'image', mime: 'image/gif' },
  'video/mp4': { kind: 'video', mime: 'video/mp4' },
  'application/pdf': { kind: 'pdf', mime: 'application/pdf' },
  'text/csv': { kind: 'csv', mime: 'text/csv' },
  'text/plain': { kind: 'txt', mime: 'text/plain' },
};

interface FileRow extends AccountFile {
  bytes: string;
}

interface FilesStore {
  byAccount: Map<string, FileRow[]>;
}

const stores = new WeakMap<OrgData, FilesStore>();

function seeded(now: number): Map<string, FileRow[]> {
  const at = (minutes: number) => new Date(now - minutes * 60_000).toISOString();
  return new Map([
    [
      'aster/instagram',
      [
        {
          id: 'in_3f9a0c1b2d4e', box: 'inbox', name: 'autumn-latte.jpg', status: 'complete', size: 482_113, size_text: '470.8 KB',
          kind: 'image', mime: 'image/jpeg', source: 'put', created_at: at(95), finished_at: at(95), error: null, bytes: 'autumn latte',
        },
      ],
    ],
    [
      'aster/reddit',
      [
        {
          id: 'out_9b1c7d2e4f60', box: 'outbox', name: 'post-insights.csv', status: 'complete', size: 2_311, size_text: '2.3 KB',
          kind: 'csv', mime: 'text/csv', source: 'download', created_at: at(240), finished_at: at(240), error: null, bytes: 'day,reach\n',
        },
      ],
    ],
  ]);
}

function storeOf(data: OrgData, now = Date.now()): FilesStore {
  const known = stores.get(data);
  if (known) return known;
  const store: FilesStore = { byAccount: data.org.slug === 'northwind-group' ? seeded(now) : new Map() };
  stores.set(data, store);
  return store;
}

const rowsOf = (store: FilesStore, lane: string) => store.byAccount.get(lane) ?? [];

function publicOf(row: FileRow): AccountFile {
  const { bytes: _bytes, ...rest } = row;
  return rest;
}

function sizeWords(size: number): string {
  if (size < 1024) return `${size} bytes`;
  if (size < MB) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / MB).toFixed(1)} MB`;
}

/** The files routes of an account. The lane must be a browser account on this computer. */
export function filesHandlers({ org, failure }: Pick<Helpers, 'org' | 'failure'>) {
  const notFound = () => failure(404, 'lane_not_found', 'No account has that name. Check the name, or it may have been removed.');
  const accountOf = (data: OrgData, ws: string, name: string) => data.lanes.find((lane) => lane.id === `${ws}/${name}`);
  const base = '/api/orgs/:org/lanes/:ws/:name/files';
  return [
    http.get(
      base,
      org((data, { params }) => {
        const lane = accountOf(data, String(params.ws), String(params.name));
        if (!lane) return notFound();
        const rows = rowsOf(storeOf(data), lane.id);
        return HttpResponse.json({
          lane: lane.id,
          inbox: rows.filter((row) => row.box === 'inbox').map(publicOf),
          outbox: rows.filter((row) => row.box === 'outbox').map(publicOf),
          limits: { put_bytes: PUT_BYTES, put: '200.0 MB', download_bytes: DOWNLOAD_BYTES, download: '200.0 MB' },
        });
      }),
    ),
    http.post(
      base,
      org(async (data, { params, request }) => {
        const lane = accountOf(data, String(params.ws), String(params.name));
        if (!lane) return notFound();
        const url = new URL(request.url);
        const name = (url.searchParams.get('name') ?? '').trim();
        if (!name) return failure(400, 'invalid', 'Say the file name, so the site can show it.');
        const type = (request.headers.get('content-type') ?? '').split(';')[0]!.trim();
        const bytes = await request.arrayBuffer();
        if (bytes.byteLength === 0) return failure(400, 'invalid', 'The file is empty.');
        if (bytes.byteLength > PUT_BYTES) return failure(400, 'invalid', 'The file is larger than the 200.0 MB limit.');
        if (type === 'text/html' || type === 'image/svg+xml') {
          return failure(400, 'invalid', 'That is a web page or SVG markup, not a file the inbox takes (images, videos, PDF, CSV or plain text).');
        }
        const known = KINDS[type];
        if (!known) return failure(400, 'invalid', `The file is ${type || 'an unrecognized type'}; the inbox takes only image, video, pdf, csv and txt (judged by the bytes, not the name).`);
        const store = storeOf(data);
        const row: FileRow = {
          id: `in_${Math.random().toString(16).slice(2, 14).padEnd(12, '0')}`, box: 'inbox', name: name.replace(/[/\\]/g, ''), status: 'complete' as FileStatus,
          size: bytes.byteLength, size_text: sizeWords(bytes.byteLength), kind: known.kind, mime: known.mime, source: 'put',
          created_at: new Date().toISOString(), finished_at: new Date().toISOString(), error: null, bytes: '',
        };
        store.byAccount.set(lane.id, [...rowsOf(store, lane.id), row]);
        return HttpResponse.json({ lane: lane.id, file: publicOf(row) });
      }),
    ),
    http.get(
      `${base}/:id/download`,
      org((data, { params }) => {
        const lane = accountOf(data, String(params.ws), String(params.name));
        if (!lane) return notFound();
        const row = rowsOf(storeOf(data), lane.id).find((item) => item.id === params.id);
        if (!row) return failure(404, 'not_found', 'No file has that id. See the account\'s files.');
        return new HttpResponse(row.bytes || row.name, {
          headers: {
            'content-type': 'application/octet-stream',
            'content-disposition': `attachment; filename="${row.name}"`,
            'x-content-type-options': 'nosniff',
          },
        });
      }),
    ),
    http.delete(
      `${base}/:id`,
      org((data, { params }) => {
        const lane = accountOf(data, String(params.ws), String(params.name));
        if (!lane) return notFound();
        const store = storeOf(data);
        const row = rowsOf(store, lane.id).find((item) => item.id === params.id);
        if (!row) return failure(404, 'not_found', 'No file has that id. See the account\'s files.');
        store.byAccount.set(lane.id, rowsOf(store, lane.id).filter((item) => item.id !== row.id));
        return HttpResponse.json({ lane: lane.id, deleted: publicOf(row) });
      }),
    ),
  ];
}
