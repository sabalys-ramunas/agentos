#!/usr/bin/env node
/**
 * A node to build against: `agentos dev up --seed`, the daemon's real policy, vault, runner, brains
 * and workspaces, with fake lanes. No browser, no model, no account, nothing leaves this machine.
 *
 *   node scripts/dev-node.mjs --port 7451
 *   AGENTOS_API_TARGET=http://127.0.0.1:7451 VITE_API_MODE=live pnpm dev --port 5311
 *
 * It signs in once and writes the cookie to .dev-node-<port>.json, for scripts/contract.mjs, and
 * prints a second one-time link for a browser. Ctrl-C stops it.
 */
import { spawn } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const web = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const repo = resolve(web, '..');
const at = process.argv.indexOf('--port');
const port = at > 0 ? process.argv[at + 1] : '7451';
const python = process.env.AGENTOS_PYTHON;
const [command, ...args] = python ? [python, '-m', 'agentos.cli'] : ['uv', 'run', 'agentos'];
const node = spawn(command, [...args, 'dev', 'up', '--seed', '--port', port], {
  cwd: repo,
  env: { ...process.env, PYTHONPATH: `${repo}/src` },
  stdio: ['ignore', 'pipe', 'inherit'],
});

let text = '';
let ready = false;
node.stdout.on('data', async (chunk) => {
  text += chunk;
  process.stdout.write(chunk);
  const code = /login#code=([\w-]+)/.exec(text)?.[1];
  if (!code || ready) return;
  ready = true;
  const signIn = await fetch(`http://127.0.0.1:${port}/api/session`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ code }),
  });
  if (!signIn.ok) {
    console.error(`Could not sign in: ${signIn.status}`);
    return;
  }
  const cookie = signIn.headers.get('set-cookie').split(';')[0];
  writeFileSync(`${web}/.dev-node-${port}.json`, JSON.stringify({ cookie, port }));
  console.log(`\nSigned in. Browser: sign in with the cookie in ${web}/.dev-node-${port}.json, or run agentos panel login.`);
});
node.on('exit', (status) => process.exit(status ?? 0));
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => node.kill(signal));
