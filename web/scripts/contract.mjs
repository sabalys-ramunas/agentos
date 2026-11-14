#!/usr/bin/env node
/**
 * Does a node answer what the screens are written to?
 *
 * Reads every `http<T>(path...)` in src/api/client.ts with the TypeScript compiler, calls the GET
 * ones on a running node, and checks each answer against T field by field. Run it on a dev node:
 *
 *   node scripts/dev-node.mjs --port 7451            # the daemon's real services, fake lanes
 *   node scripts/contract.mjs --port 7451 [--only lanes]
 *
 * Writes are listed with whether a route answers, never sent. A path with an id takes one from the
 * list route above it where it can, and is left out, and counted, where it cannot.
 */
import { existsSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const ts = require('typescript');
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const arg = (name, fallback) => {
  const at = process.argv.indexOf(`--${name}`);
  return at > 0 ? process.argv[at + 1] : fallback;
};
const port = arg('port', '7451');
const base = arg('base', `http://127.0.0.1:${port}`);
const only = arg('only', '');
const ORG = 'local';

const config = ts.parseJsonConfigFileContent(ts.readConfigFile(`${root}/tsconfig.json`, ts.sys.readFile).config, ts.sys, root);
const program = ts.createProgram([`${root}/src/api/client.ts`], { ...config.options, noEmit: true });
const checker = program.getTypeChecker();
const source = program.getSourceFile(`${root}/src/api/client.ts`);

/** Every http<T>(...) call: its method, its path with ${...} kept as {name}, and T. */
function calls() {
  const out = [];
  const pathOf = (node) => {
    if (ts.isStringLiteralLike(node)) return node.text;
    if (ts.isBinaryExpression(node)) return pathOf(node.left);
    if (ts.isTaggedTemplateExpression(node) && ts.isTemplateExpression(node.template)) {
      const t = node.template;
      return t.head.text + t.templateSpans.map((s) => `{${s.expression.getText(source)}}${s.literal.text}`).join('');
    }
    if (ts.isTaggedTemplateExpression(node)) return node.template.text;
    if (ts.isTemplateExpression(node)) {
      return node.head.text + node.templateSpans.map((s) => `{${s.expression.getText(source)}}${s.literal.text}`).join('');
    }
    return null;
  };
  const visit = (node) => {
    if (ts.isCallExpression(node) && node.expression.getText(source) === 'http' && node.typeArguments?.length === 1) {
      const raw = node.arguments[0] && pathOf(node.arguments[0]);
      let method = 'GET';
      const options = node.arguments[1];
      if (options && ts.isObjectLiteralExpression(options)) {
        for (const prop of options.properties) {
          if (prop.name?.getText(source) === 'method' && ts.isPropertyAssignment(prop)) method = prop.initializer.getText(source).replace(/['"]/g, '');
        }
      }
      if (raw) out.push({ method, raw, type: checker.getTypeFromTypeNode(node.typeArguments[0]), typeText: node.typeArguments[0].getText(source) });
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return out;
}

/** Why `value` is not a `type`, as paths. Empty means it fits. */
function check(type, value, where, errors, seen = 0) {
  if (errors.length > 40 || seen > 12) return;
  const flags = ts.TypeFlags;
  if (type.flags & (flags.Any | flags.Unknown)) return;
  if (type.isUnion()) {
    const fits = type.types.some((part) => {
      const inner = [];
      check(part, value, where, inner, seen + 1);
      return inner.length === 0;
    });
    if (fits) return;
    const objects = type.types.filter((part) => part.flags & flags.Object);
    if (objects.length === 1 && value && typeof value === 'object') return check(objects[0], value, where, errors, seen + 1);
    errors.push(`${where}: got ${show(value)}, wants ${checker.typeToString(type)}`);
    return;
  }
  if (type.flags & flags.Null) return void (value === null || errors.push(`${where}: wants null, got ${show(value)}`));
  if (type.flags & flags.Undefined) return void (value === undefined || errors.push(`${where}: wants nothing, got ${show(value)}`));
  if (type.flags & flags.Void) return;
  if (type.flags & flags.StringLiteral) return void (value === type.value || errors.push(`${where}: wants "${type.value}", got ${show(value)}`));
  if (type.flags & flags.NumberLiteral) return void (value === type.value || errors.push(`${where}: wants ${type.value}, got ${show(value)}`));
  if (type.flags & flags.BooleanLiteral) {
    const wanted = checker.typeToString(type) === 'true';
    return void (value === wanted || errors.push(`${where}: wants ${wanted}, got ${show(value)}`));
  }
  if (type.flags & flags.StringLike) return void (typeof value === 'string' || errors.push(`${where}: wants string, got ${show(value)}`));
  if (type.flags & flags.NumberLike) return void (typeof value === 'number' || errors.push(`${where}: wants number, got ${show(value)}`));
  if (type.flags & flags.BooleanLike) return void (typeof value === 'boolean' || errors.push(`${where}: wants boolean, got ${show(value)}`));
  if (checker.isArrayType(type) || checker.isTupleType(type)) {
    if (!Array.isArray(value)) return void errors.push(`${where}: wants a list, got ${show(value)}`);
    const element = checker.getTypeArguments(type)[0];
    value.slice(0, 5).forEach((item, index) => check(element, item, `${where}[${index}]`, errors, seen + 1));
    return;
  }
  if (type.flags & flags.Object || type.isIntersection()) {
    if (value === null || typeof value !== 'object' || Array.isArray(value)) return void errors.push(`${where}: wants an object, got ${show(value)}`);
    const index = checker.getIndexTypeOfType(type, ts.IndexKind.String);
    if (index) for (const key of Object.keys(value).slice(0, 5)) check(index, value[key], `${where}.${key}`, errors, seen + 1);
    for (const prop of checker.getPropertiesOfType(type)) {
      const name = prop.getName();
      const optional = (prop.flags & ts.SymbolFlags.Optional) !== 0;
      if (!(name in value)) {
        if (!optional) errors.push(`${where}.${name}: missing`);
        continue;
      }
      if (value[name] === undefined && optional) continue;
      check(checker.getTypeOfSymbolAtLocation(prop, source), value[name], `${where}.${name}`, errors, seen + 1);
    }
  }
}

function show(value) {
  const text = JSON.stringify(value);
  return text === undefined ? 'nothing' : text.length > 60 ? `${text.slice(0, 57)}...` : text;
}

// dev-node.mjs signs in once and leaves the cookie here: a sign-in code works once.
const sessionFile = `${root}/.dev-node-${port}.json`;
if (!existsSync(sessionFile)) {
  console.error(`No ${sessionFile}. Start a node first: node scripts/dev-node.mjs --port ${port}`);
  process.exit(2);
}
const { cookie } = JSON.parse(readFileSync(sessionFile, 'utf8'));
const get = (path) => fetch(`${base}${path}`, { headers: { cookie, accept: 'application/json' }, redirect: 'manual' });

const all = calls().filter((item) => !only || item.raw.includes(only));
/** A value for each id, read from the list routes as they answer. */
const samples = { org: ORG };
const byArea = {};
const fill = (raw) => {
  let missing = null;
  const path = raw.replace(/\/([^/{]+)\/\{([^}]+)\}|\{([^}]+)\}/g, (whole, area, inArea, alone) => {
    const key = (inArea ?? alone).replace(/^.*\./, '');
    // An id belongs to the list it came from, so /nodes/{id} never takes a workspace's id.
    const value = area && key === 'id' ? byArea[area] : samples[key];
    if (value !== undefined) return `${area ? `/${area}/` : ''}${encodeURIComponent(value)}`;
    missing = key;
    return whole;
  });
  return missing ? null : path;
};
const learn = (body, raw) => {
  const first = Array.isArray(body) ? body[0] : body?.items?.[0];
  if (!first || typeof first !== 'object') return;
  const area = raw.split('/').filter((part) => part && !part.startsWith('{')).at(-1) ?? '';
  const single = area.replace(/s$/, '');
  if (first.id !== undefined) byArea[area] ??= first.id;
  if (first.id !== undefined) samples[single] ??= first.id;
  if (first.id !== undefined) samples.id ??= first.id;
  if (first.workspace !== undefined) samples.ws ??= first.workspace;
  // A workspace's own name is not an account's name, so it is never taken for one.
  if (first.name !== undefined && !Array.isArray(first.lanes)) samples.name ??= first.name;
  // A workspace lists its lanes, so an account's id comes from there when no list of lanes is read.
  const lane = Array.isArray(first.lanes) ? first.lanes[0] : undefined;
  if (lane?.workspace !== undefined) samples.ws ??= lane.workspace;
  if (lane?.name !== undefined) samples.name ??= lane.name;
};

// The ids come from the lists, which a filter may leave out of `all`: read the workspaces once for them.
const seeded = await get(`/api/orgs/${ORG}/workspaces`);
if (seeded.ok) learn(await seeded.json(), '/api/orgs/{org}/workspaces');

let fits = 0;
let wrong = 0;
let unserved = 0;
let skipped = 0;
const report = [];
const ordered = [...all].sort((a, b) => (a.raw.match(/\{/g)?.length ?? 0) - (b.raw.match(/\{/g)?.length ?? 0));
for (const item of ordered) {
  if (item.method !== 'GET') continue;
  const path = fill(item.raw);
  if (path === null) {
    skipped += 1;
    report.push(`  skip     ${item.raw}: no id to try it with`);
    continue;
  }
  // These two answer for one account, named in the query.
  const needsLane = /\/workers\/(things|accounts)$/.test(item.raw) && samples.ws && samples.name;
  const response = await get(needsLane ? `${path}?lane=${samples.ws}/${samples.name}` : path);
  const text = await response.text();
  if (response.status === 404) {
    unserved += 1;
    report.push(`  404      ${item.raw}`);
    continue;
  }
  if (!response.ok) {
    wrong += 1;
    report.push(`  ${response.status}      ${item.raw}: ${text.slice(0, 120)}`);
    continue;
  }
  let body;
  try {
    body = text ? JSON.parse(text) : undefined;
  } catch {
    wrong += 1;
    report.push(`  not json ${item.raw}`);
    continue;
  }
  learn(body, item.raw);
  const errors = [];
  check(item.type, body, '$', errors);
  if (errors.length === 0) {
    fits += 1;
    continue;
  }
  wrong += 1;
  report.push(`  shape    ${item.raw} (${item.typeText})`);
  for (const error of errors.slice(0, 12)) report.push(`             ${error}`);
  if (errors.length > 12) report.push(`             and ${errors.length - 12} more`);
}
console.log(report.join('\n'));
console.log(`\n${fits} fit, ${wrong} wrong, ${unserved} not served, ${skipped} not tried`);
process.exit(wrong + unserved > 0 ? 1 : 0);
