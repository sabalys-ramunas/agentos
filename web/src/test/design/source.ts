/**
 * Reads one source file with the TypeScript parser, so a rule sees strings, JSX and style objects and
 * never a comment or a name: what the design rules in rules.ts look at.
 */
import ts from 'typescript';

export interface Text {
  text: string;
  line: number;
}

export interface StyleProp {
  key: string;
  /** The literal value written out, or null when it is computed (a variable, a call, a template with ${}). */
  literal: string | null;
  line: number;
}

export interface JsxElement {
  tag: string;
  attrs: Map<string, string | null>;
  line: number;
}

export interface Parsed {
  /** Every string literal and template piece: where class names, colours and var() live. */
  strings: Text[];
  /** Words a person reads: JSX text and the user-facing attributes. */
  words: Text[];
  styles: StyleProp[];
  elements: JsxElement[];
  /** `navigator.clipboard.writeText`, written out. */
  clipboard: number[];
  /** Named imports: `import { Segmented } from '@/components/charts/small'`. */
  imports: Array<{ module: string; name: string; line: number }>;
}

const READ_ATTRS = new Set(['title', 'aria-label', 'placeholder', 'alt', 'label', 'description', 'hint', 'meta']);

function lineOf(source: ts.SourceFile, node: ts.Node): number {
  return source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
}

function literalOf(node: ts.Expression): string | null {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return node.text;
  if (ts.isNumericLiteral(node)) return node.text;
  if (ts.isPrefixUnaryExpression(node) && ts.isNumericLiteral(node.operand)) return `-${node.operand.text}`;
  return null;
}

function attrValue(attr: ts.JsxAttribute): string | null {
  const init = attr.initializer;
  if (!init) return '';
  if (ts.isStringLiteral(init)) return init.text;
  if (ts.isJsxExpression(init) && init.expression) return literalOf(init.expression);
  return null;
}

function styleProps(source: ts.SourceFile, attr: ts.JsxAttribute, out: StyleProp[]) {
  const init = attr.initializer;
  if (!init || !ts.isJsxExpression(init) || !init.expression || !ts.isObjectLiteralExpression(init.expression)) return;
  for (const prop of init.expression.properties) {
    if (!ts.isPropertyAssignment(prop)) continue;
    const key = ts.isIdentifier(prop.name) || ts.isStringLiteral(prop.name) ? prop.name.text : '';
    out.push({ key, literal: literalOf(prop.initializer), line: lineOf(source, prop) });
  }
}

function element(source: ts.SourceFile, node: ts.JsxOpeningElement | ts.JsxSelfClosingElement, parsed: Parsed) {
  const attrs = new Map<string, string | null>();
  for (const attr of node.attributes.properties) {
    if (!ts.isJsxAttribute(attr)) continue;
    const name = attr.name.getText(source);
    attrs.set(name, attrValue(attr));
    if (name === 'style') styleProps(source, attr, parsed.styles);
    const value = attrs.get(name);
    if (READ_ATTRS.has(name) && value) parsed.words.push({ text: value, line: lineOf(source, attr) });
  }
  parsed.elements.push({ tag: node.tagName.getText(source), attrs, line: lineOf(source, node) });
}

export function parse(path: string, text: string): Parsed {
  const kind = path.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const source = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true, kind);
  const parsed: Parsed = { strings: [], words: [], styles: [], elements: [], clipboard: [], imports: [] };
  const visit = (node: ts.Node) => {
    if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier) && node.importClause?.namedBindings && ts.isNamedImports(node.importClause.namedBindings)) {
      for (const spec of node.importClause.namedBindings.elements) parsed.imports.push({ module: node.moduleSpecifier.text, name: (spec.propertyName ?? spec.name).text, line: lineOf(source, spec) });
    }
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      // An import path is not a class or a colour.
      if (!ts.isImportDeclaration(node.parent) && !ts.isExportDeclaration(node.parent)) parsed.strings.push({ text: node.text, line: lineOf(source, node) });
    } else if (ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node)) {
      parsed.strings.push({ text: node.text, line: lineOf(source, node) });
    } else if (ts.isJsxText(node) && node.text.trim()) {
      parsed.words.push({ text: node.text.trim(), line: lineOf(source, node) });
    } else if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
      element(source, node, parsed);
    } else if (ts.isPropertyAccessExpression(node) && node.name.text === 'writeText' && /navigator\??\.clipboard/.test(node.expression.getText(source))) {
      parsed.clipboard.push(lineOf(source, node));
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return parsed;
}
