#!/usr/bin/env node
/**
 * Rewrites the pre-Mono class names (the design rules' `legacy-token`) to the Mono ones, so a branch
 * made before the cleanup can catch up in one step:
 *
 *   node scripts/codemod-legacy-tokens.mjs            rewrite every .ts and .tsx under src/
 *   node scripts/codemod-legacy-tokens.mjs --dry      say what would change, write nothing
 *   node scripts/codemod-legacy-tokens.mjs src/a.tsx  only these files
 *
 * The colours are exact renames (`bg-bg` is `bg-card`: the same token). A size (`text-13`) becomes the
 * type role of the same size and line height (`text-secondary`). A role also sets a weight and a letter
 * spacing, so, to draw the same page:
 *   - a role whose weight differs from what the element had keeps that weight: when the element sets
 *     none, the codemod adds the one it had (its tag's, as h2's 600, or the nearest parent's in the same
 *     file, or the body's 400);
 *   - an explicit weight that equals the role's is dropped (`text-14 font-medium` is `text-label`);
 *   - `text-blue` on a link or a button is `link` (ink with the underline, as app.css gave it).
 * Letter spacing then follows the role: Mono's tracking, a fraction of a pixel from the old scale's.
 * scripts/visual-compare.mjs shows what moved (with `--no-tracking`, nothing else may).
 *
 * The parser is TypeScript's, so only string literals and template pieces change, never a comment or a
 * name. Built names (`text-${size}`) cannot be seen: it lists them for a person to look at.
 */
import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

const WEB = join(fileURLToPath(new URL('.', import.meta.url)), '..');

/** Old colour word -> the Mono word for the same token. */
const COLOURS = { bg: 'card', subtle: 'inset', 'fill-1': 'hover', 'fill-2': 'pressed', 'fill-3': 'fill-strong', green: 'ok', amber: 'warn', red: 'bad', blue: 'fg' };
const COLOUR_UTILITY = /^(bg|text|border(?:-[xytrblse])?|fill|stroke|ring|ring-offset|outline|divide|decoration|accent|caret|from|via|to|shadow|placeholder)-(bg|subtle|fill-[123]|green|amber|red|blue)$/;

/** The type roles' weights (web/tokens/build-tokens.py, TYPE), for the roles this codemod writes or meets. */
const ROLE_WEIGHT = {
  display: 600, 'page-title': 600, 'section-title': 600, 'page-title-compact': 600, lede: 400, 'card-title': 600, 'table-header': 500,
  label: 500, body: 400, secondary: 400, caption: 400, number: 400, stat: 600, code: 400, 'code-caption': 400, eyebrow: 500, micro: 500,
  heading: 600, headline: 600,
};
const WEIGHT_CLASS = { 'font-normal': 400, 'font-medium': 500, 'font-semibold': 600, 'font-bold': 700 };
const CLASS_OF_WEIGHT = { 400: 'font-normal', 500: 'font-medium', 600: 'font-semibold', 700: 'font-bold' };
/** Elements with a weight of their own: app.css sets h1 to h4 to 600; the browser draws th, b and strong bold. */
const TAG_WEIGHT = { h1: 600, h2: 600, h3: 600, h4: 600, th: 700, b: 700, strong: 700 };

/** The role for an old size, given the weight the element sets itself (if any). */
function roleFor(size, weight) {
  switch (size) {
    case '11': return 'micro';
    case '12': return 'caption';
    case '13': return 'secondary';
    case '14': return weight === 500 ? 'label' : 'body';
    case '16': return weight === 600 ? 'section-title' : 'lede';
    case '20': return 'heading';
    case '24': return 'page-title-compact';
    case '32': return 'headline';
    default: return null;
  }
}

/** A class split into its variants (`md:hover:`), `!`, the utility and an opacity (`/90`). */
function split(token) {
  let depth = 0;
  let start = 0;
  for (let index = 0; index < token.length; index += 1) {
    const char = token[index];
    if (char === '[' || char === '(') depth += 1;
    else if (char === ']' || char === ')') depth -= 1;
    else if (char === ':' && depth === 0) start = index + 1;
  }
  const variants = token.slice(0, start);
  let rest = token.slice(start);
  const bang = rest.startsWith('!') ? '!' : '';
  rest = rest.slice(bang.length);
  const slash = /^([a-z0-9-]+)(\/\d+)?$/.exec(rest);
  return slash ? { variants, bang, base: slash[1], alpha: slash[2] ?? '' } : { variants, bang, base: rest, alpha: '' };
}

const legacySize = (token) => {
  const { variants, base } = split(token);
  const size = /^text-(11|12|13|14|16|20|24|32)$/.exec(base);
  return size ? { variants, size: size[1] } : null;
};

/** The string pieces of a file, each with where its text sits between the quotes. */
function pieces(source) {
  const out = [];
  const visit = (node) => {
    let open = 0;
    let close = 0;
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) [open, close] = [1, 1];
    else if (ts.isTemplateHead(node)) [open, close] = [1, 2];
    else if (ts.isTemplateMiddle(node)) [open, close] = [1, 2];
    else if (ts.isTemplateTail(node)) [open, close] = [1, 1];
    if (open) {
      const start = node.getStart(source) + open;
      const end = node.end - close;
      // Import paths and object keys are not classes.
      if (!ts.isImportDeclaration(node.parent) && !ts.isExportDeclaration(node.parent)) out.push({ node, start, end, text: source.text.slice(start, end) });
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return out;
}

/** The piece's scope: the className attribute it is in, else the call (cn, clsx) it is in, else itself. */
function scopeOf(node) {
  let call = null;
  for (let up = node.parent; up; up = up.parent) {
    if (ts.isJsxAttribute(up)) return up;
    if (ts.isCallExpression(up) && /^(cn|clsx|cva|twMerge)$/.test(up.expression.getText())) call ??= up;
    if (ts.isBlock(up) || ts.isSourceFile(up) || ts.isVariableDeclaration(up) || ts.isPropertyAssignment(up) || ts.isReturnStatement(up)) break;
  }
  return call ?? node;
}

const tokensIn = (text) => text.split(/\s+/).filter(Boolean);

/** The weight a class list sets on its own element, without variants: explicit, or from a role. */
function weightOf(tokens, roles = true) {
  for (const token of tokens) if (token in WEIGHT_CLASS) return WEIGHT_CLASS[token];
  if (!roles) return null;
  for (const token of tokens) {
    const role = /^text-([a-z-]+)$/.exec(token);
    if (role && role[1] in ROLE_WEIGHT) return ROLE_WEIGHT[role[1]];
  }
  return null;
}

/** The JSX element a className attribute belongs to. */
const elementOf = (attribute) => attribute.parent?.parent;

/** The weight an element has from its own tag and className, or null when it inherits one. */
function ownWeight(element, classesOf) {
  const tag = element.tagName.getText();
  const className = element.attributes.properties.find((p) => ts.isJsxAttribute(p) && p.name.getText() === 'className');
  return (className && weightOf(classesOf(className))) || TAG_WEIGHT[tag] || null;
}

/** The weight an element would have without a type role: its tag's, or what the nearest parent in this file sets, or 400 (the body's). */
function inheritedWeight(attribute, classesOf) {
  const element = elementOf(attribute);
  if (element && TAG_WEIGHT[element.tagName.getText()]) return TAG_WEIGHT[element.tagName.getText()];
  // An opening element's parent is its own JsxElement: start above it.
  const start = element && ts.isJsxOpeningElement(element) ? element.parent.parent : element?.parent;
  for (let up = start; up; up = up.parent) {
    if (ts.isArrowFunction(up) || ts.isFunctionDeclaration(up) || ts.isFunctionExpression(up)) break;
    if (!ts.isJsxElement(up)) continue;
    const weight = ownWeight(up.openingElement, classesOf);
    if (weight) return weight;
  }
  return 400;
}

function tagOf(scope) {
  if (!ts.isJsxAttribute(scope)) return null;
  const element = elementOf(scope);
  return element && (ts.isJsxOpeningElement(element) || ts.isJsxSelfClosingElement(element)) ? element.tagName.getText() : null;
}

/** Rewrites one file's text. Returns the new text and what it did. */
export function rewrite(file, text) {
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const all = pieces(source);
  const byScope = new Map();
  for (const piece of all) {
    const scope = scopeOf(piece.node);
    piece.scope = scope;
    byScope.set(scope, [...(byScope.get(scope) ?? []), piece]);
  }
  const classesOf = (scope) => (byScope.get(scope) ?? []).flatMap((piece) => tokensIn(piece.text));
  const edits = [];
  const notes = [];
  let count = 0;
  for (const [scope, list] of byScope) {
    const scopeTokens = list.flatMap((piece) => tokensIn(piece.text));
    const sizes = scopeTokens.map(legacySize).filter(Boolean);
    const colours = scopeTokens.filter((token) => COLOUR_UTILITY.test(split(token).base));
    if (!sizes.length && !colours.length) continue;
    const explicit = weightOf(scopeTokens, false);
    const tag = tagOf(scope);
    const wrote = [];
    const next = list.map((piece) => {
      let changed = false;
      const out = piece.text.replace(/\S+/g, (token) => {
        const size = legacySize(token);
        if (size) {
          const role = roleFor(size.size, explicit);
          wrote.push(role);
          changed = true;
          count += 1;
          return `${size.variants}text-${role}`;
        }
        const parts = split(token);
        const colour = COLOUR_UTILITY.exec(parts.base);
        if (!colour) return token;
        changed = true;
        count += 1;
        if (parts.base === 'text-blue' && !parts.variants && (tag === 'a' || tag === 'button' || tag === 'Link')) return 'link';
        return `${parts.variants}${parts.bang}${colour[1]}-${COLOURS[colour[2]]}${parts.alpha}`;
      });
      return { piece, out, changed };
    });
    if (wrote.length) {
      const roles = [...wrote, ...scopeTokens.map((t) => /^text-([a-z-]+)$/.exec(split(t).base)?.[1]).filter((r) => r && r in ROLE_WEIGHT && !wrote.includes(r))];
      const weights = new Set(roles.map((role) => ROLE_WEIGHT[role]));
      const first = next.find((entry) => entry.changed && /\btext-/.test(entry.out)) ?? next[0];
      if (explicit === null) {
        // The element had the weight it inherited; a role would set its own.
        const had = ts.isJsxAttribute(scope) ? inheritedWeight(scope, classesOf) : 400;
        if ([...weights].some((weight) => weight !== had)) first.out = `${first.out} ${CLASS_OF_WEIGHT[had]}`;
      } else if (list.length === 1 && weights.size === 1 && weights.has(explicit)) {
        // `text-14 font-medium` is `text-label`: the role has the weight.
        first.out = first.out.replace(new RegExp(`(^|\\s)${CLASS_OF_WEIGHT[explicit]}(?=\\s|$)`), '').replace(/^\s+/, '');
      }
    }
    for (const entry of next) if (entry.changed || entry.out !== entry.piece.text) edits.push({ start: entry.piece.start, end: entry.piece.end, text: entry.out });
  }
  for (const piece of all) {
    if (/(?:^|\s)(?:[a-z-]+:)*(?:text|bg|border)-$/.test(piece.text) && ts.isTemplateHead(piece.node)) {
      notes.push(`${file}:${source.getLineAndCharacterOfPosition(piece.start).line + 1}: a built class name; check it by hand`);
    }
  }
  edits.sort((a, b) => b.start - a.start);
  let result = text;
  for (const edit of edits) result = result.slice(0, edit.start) + edit.text + result.slice(edit.end);
  return { text: result, count, notes };
}

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}

function main() {
  const args = process.argv.slice(2);
  const dry = args.includes('--dry');
  const named = args.filter((arg) => !arg.startsWith('--'));
  // src/test/design holds the old names as the rules' data, so it is left alone.
  const files = named.length
    ? named.map((file) => join(process.cwd(), file))
    : walk(join(WEB, 'src')).filter((file) => /\.tsx?$/.test(file) && !relative(WEB, file).startsWith('src/test/'));
  let total = 0;
  let touched = 0;
  for (const file of files) {
    const before = readFileSync(file, 'utf8');
    const { text, count, notes } = rewrite(relative(WEB, file), before);
    for (const note of notes) console.log(note);
    if (!count) continue;
    total += count;
    touched += 1;
    if (dry) console.log(`${relative(WEB, file)}: ${count}`);
    else writeFileSync(file, text);
  }
  console.log(`${dry ? 'Would rewrite' : 'Rewrote'} ${total} class names in ${touched} files.`);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) main();
