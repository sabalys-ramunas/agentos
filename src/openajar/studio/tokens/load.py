"""Token sets from a Design Tokens file (DTCG 2025.10, with the resolver or a simple themes map),
a CSS custom properties file, a Tailwind v4 theme, or a live page (see page.py).

Every value is normalised: colours to oklab(...), lengths to px, weights and unitless line
heights to numbers, font stacks to a comma list, shadows to px layers with oklab colours.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openajar.core.errors import InvalidRequest

from ..contracts import Scale, Theme, Token, TokenCategory, TokenSet
from .color import from_space, parse_color
from .units import (
    ROOT_PX,
    ShadowLayer,
    _split_top,
    fmt_num,
    fmt_px,
    parse_length,
    parse_number,
    parse_shadow,
    parse_weight,
    shadow_css,
)

C = TokenCategory
LENGTH_CATS = (C.SPACE, C.RADIUS, C.FONT_SIZE, C.BORDER_WIDTH)


@dataclass
class _Raw:
    name: str
    category: TokenCategory
    value: str
    css_var: str | None
    semantic: bool


# --- shared helpers -----------------------------------------------------------------------

def kebab(s: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"-\1", s).lower().replace("_", "-").replace(" ", "-")


def category_from_name(name: str) -> TokenCategory | None:
    """A guess from a token or custom property name, for lengths and numbers whose type does
    not say what they are for."""
    n = kebab(name).replace(".", "-")
    if re.search(r"line-?height|leading", n):
        return C.LINE_HEIGHT
    # a type scale named by size: --type-14 (or --font-14, font.14) is a font size, --type-14-line
    # its line height
    if re.search(r"(^|-)(type|text|font|fs)-\d+(\.\d+)?-line$", n):
        return C.LINE_HEIGHT
    if re.search(r"(^|-)(type|font|fs)-\d+(\.\d+)?$", n):
        return C.FONT_SIZE
    if re.search(r"radius|radii|rounded|corner", n):
        return C.RADIUS
    if re.search(r"border-?width|stroke-?width|outline-?width|(^|-)border(-|$).*width", n):
        return C.BORDER_WIDTH
    if re.search(r"font-?weight|(^|-)weight(-|$)", n):
        return C.FONT_WEIGHT
    if re.search(r"font-?family|(^|-)family(-|$)|(^|-)font-(sans|serif|mono|body|heading|display|ui)", n):
        return C.FONT_FAMILY
    if re.search(r"font-?size|text-?size|(^|-)(font|text|type|typography)(-.*)?-size|(^|-)fs-", n):
        return C.FONT_SIZE
    if re.search(r"shadow|elevation", n):
        return C.SHADOW
    if re.search(r"space|spacing|gap|gutter|padding|margin|inset|(^|-)stack(-|$)|(^|-)size(-|$)", n):
        return C.SPACE
    if re.search(r"color|colour|(^|-)(bg|fg|background|foreground|surface|text|border|accent|brand)(-|$)", n):
        return C.COLOR
    return None


def font_css(families: list[str] | str) -> str:
    items = families if isinstance(families, list) else [f.strip() for f in _split_top(families)]
    out = []
    for f in items:
        f = str(f).strip().strip("'\"")
        out.append(f'"{f}"' if (" " in f or not re.fullmatch(r"[A-Za-z0-9-]+", f)) and f else f)
    return ", ".join(x for x in out if x)


def canonical(category: TokenCategory, value: Any, *, root_px: float = ROOT_PX) -> str | None:
    """A token value in canonical form, or None when it cannot be read as that category."""
    if category == C.COLOR:
        if isinstance(value, dict):
            c = _dtcg_color(value)
        else:
            c = parse_color(str(value))
        return c.css() if c else None
    if category in LENGTH_CATS:
        px = _dtcg_dimension(value, root_px) if isinstance(value, dict) else parse_length(value, root_px=root_px)
        if px is None and isinstance(value, (int, float)) and not isinstance(value, bool):
            px = float(value)   # a bare number in a dimension-like token is taken as px
        return fmt_px(px) if px is not None else None
    if category == C.LINE_HEIGHT:
        if isinstance(value, dict):
            px = _dtcg_dimension(value, root_px)
            return fmt_px(px) if px is not None else None
        n = parse_number(value)
        if n is not None:
            return fmt_num(n)
        if isinstance(value, str) and value.strip().endswith("%"):
            try:
                return fmt_num(float(value.strip()[:-1]) / 100)
            except ValueError:
                return None
        px = parse_length(value, root_px=root_px)
        return fmt_px(px) if px is not None else None
    if category == C.FONT_WEIGHT:
        w = parse_weight(value)
        return fmt_num(w) if w is not None else None
    if category == C.FONT_FAMILY:
        if isinstance(value, (list, str)) and value:
            return font_css(value)
        return None
    if category == C.SHADOW:
        layers = _dtcg_shadow(value, root_px) if isinstance(value, (dict, list)) else parse_shadow(str(value), root_px=root_px)
        return shadow_css(layers) if layers else None
    return None


def _dtcg_color(v: dict):
    if "colorSpace" in v and "components" in v:
        try:
            return from_space(v["colorSpace"], v["components"], float(v.get("alpha", 1)))
        except (ValueError, TypeError):
            pass
    if isinstance(v.get("hex"), str):
        c = parse_color(v["hex"])
        if c and "alpha" in v:
            from .color import OKLab
            c = OKLab(c.L, c.a, c.b, float(v["alpha"]))
        return c
    return None


def _dtcg_dimension(v: dict, root_px: float) -> float | None:
    if "value" in v and "unit" in v:
        return parse_length(f"{v['value']}{v['unit']}", root_px=root_px)
    return None


def _dtcg_shadow(v, root_px: float) -> list[ShadowLayer] | None:
    items = v if isinstance(v, list) else [v]
    out = []
    for item in items:
        if not isinstance(item, dict):
            return None

        def length(key, item=item):
            x = item.get(key, 0)
            if isinstance(x, dict):
                return _dtcg_dimension(x, root_px)
            return parse_length(x, root_px=root_px) if not isinstance(x, (int, float)) else float(x)

        color = item.get("color")
        c = _dtcg_color(color) if isinstance(color, dict) else parse_color(str(color)) if color else None
        vals = [length(k) for k in ("offsetX", "offsetY", "blur", "spread")]
        if any(x is None for x in vals):
            return None
        out.append(ShadowLayer(*vals, color=c, inset=bool(item.get("inset", False))))
    return out


def _merge_themes(per_theme: dict[str, dict[str, _Raw]]) -> list[Token]:
    """One Token per name when every theme agrees, else one per theme. `base` holds tokens that
    apply in every theme."""
    base = per_theme.get("base", {})
    themed = {t: per_theme[t] for t in ("light", "dark") if t in per_theme}
    tokens: list[Token] = []
    if not themed:
        for r in base.values():
            tokens.append(Token(name=r.name, category=r.category, value=r.value, css_var=r.css_var, semantic=r.semantic))
        return tokens
    names: list[str] = list(base)
    for d in themed.values():
        names += [n for n in d if n not in names]
    for n in names:
        variants: dict[str, _Raw] = {}
        for t in ("light", "dark"):
            r = themed.get(t, {}).get(n) or base.get(n)
            if r is not None:
                variants[t] = r
        if not variants:
            continue
        values = {(r.value, r.category) for r in variants.values()}
        if len(values) == 1 and len(variants) == 2:
            r = next(iter(variants.values()))
            semantic = any(v.semantic for v in variants.values())
            tokens.append(Token(name=n, category=r.category, value=r.value, css_var=r.css_var, semantic=semantic))
        else:
            for t, r in variants.items():
                tokens.append(Token(name=n, category=r.category, value=r.value, themes=[Theme(t)],
                                    css_var=r.css_var, semantic=r.semantic or len(variants) > 1))
    return tokens


# --- DTCG ---------------------------------------------------------------------------------

_ALIAS = re.compile(r"^\{([^{}]+)\}$")
_TYPE_CAT = {"color": C.COLOR, "fontFamily": C.FONT_FAMILY, "fontWeight": C.FONT_WEIGHT, "shadow": C.SHADOW}


def _walk_dtcg(tree: dict, path: list[str], inherited_type: str | None, out: dict[str, dict]):
    group_type = tree.get("$type", inherited_type)
    for key, node in tree.items():
        if key.startswith("$") or not isinstance(node, dict):
            continue
        if "$value" in node:
            out[".".join([*path, key])] = {"type": node.get("$type", group_type), "value": node.get("$value"),
                                            "ext": node.get("$extensions") or {}}
        else:
            _walk_dtcg(node, [*path, key], group_type, out)


def _pointer_to_name(ref: str) -> str | None:
    if not ref.startswith("#/"):
        return None
    parts = [p.replace("~1", "/").replace("~0", "~") for p in ref[2:].split("/")]
    if parts and parts[-1] == "$value":
        parts = parts[:-1]
    return ".".join(parts)


def _alias_target(value) -> str | None:
    if isinstance(value, str):
        m = _ALIAS.match(value.strip())
        return m.group(1) if m else None
    if isinstance(value, dict) and set(value) == {"$ref"} and isinstance(value["$ref"], str):
        return _pointer_to_name(value["$ref"])
    return None


def _resolve_dtcg(flat: dict[str, dict]) -> dict[str, tuple[str | None, Any, bool]]:
    """name -> (type, resolved value, is_alias)."""
    done: dict[str, tuple[str | None, Any, bool]] = {}

    def deep(value, seen):
        target = _alias_target(value)
        if target is not None:
            return res(target, seen)[1]
        if isinstance(value, dict):
            return {k: deep(v, seen) for k, v in value.items()}
        if isinstance(value, list):
            return [deep(v, seen) for v in value]
        return value

    def res(name, seen=()):
        if name in done:
            return done[name]
        if name in seen or name not in flat:
            raise InvalidRequest(f"invalid: the token alias {{{name}}} does not resolve"
                                 + (" (it refers back to itself)" if name in seen else "")
                                 + ". fix: point it at a token that exists in the file.")
        node = flat[name]
        target = _alias_target(node["value"])
        if target is not None:
            t, v, _ = res(target, (*seen, name))
            out = (node["type"] or t, v, True)
        else:
            out = (node["type"], deep(node["value"], (*seen, name)), False)
        done[name] = out
        return out

    for n in flat:
        res(n)
    return done


def _dtcg_raws(tree: dict, root_px: float) -> dict[str, _Raw]:
    flat: dict[str, dict] = {}
    _walk_dtcg(tree, [], None, flat)
    resolved = _resolve_dtcg(flat)
    out: dict[str, _Raw] = {}
    for name, (typ, value, is_alias) in resolved.items():
        ext = flat[name].get("ext") or {}
        css_var = None
        for key in ("agentos", "com.agentos", "openajar", "com.openajar"):  # legacy-name: the old keys too
            if isinstance(ext.get(key), dict) and isinstance(ext[key].get("cssVar"), str):
                css_var = ext[key]["cssVar"]
        css_var = css_var or "--" + "-".join(kebab(p) for p in name.split("."))
        parts: list[tuple[str, TokenCategory, Any, str]] = []
        if typ == "typography" and isinstance(value, dict):
            for key, cat in (("fontFamily", C.FONT_FAMILY), ("fontSize", C.FONT_SIZE), ("fontWeight", C.FONT_WEIGHT),
                             ("lineHeight", C.LINE_HEIGHT)):
                if key in value:
                    parts.append((f"{name}.{key}", cat, value[key], f"{css_var}-{kebab(key)}"))
        elif typ == "border" and isinstance(value, dict):
            if "color" in value:
                parts.append((f"{name}.color", C.COLOR, value["color"], f"{css_var}-color"))
            if "width" in value:
                parts.append((f"{name}.width", C.BORDER_WIDTH, value["width"], f"{css_var}-width"))
        else:
            cat = _TYPE_CAT.get(typ or "")
            if cat is None and typ in ("dimension", "number", None):
                cat = category_from_name(name)
                if typ == "number" and cat not in (C.LINE_HEIGHT, C.FONT_WEIGHT):
                    cat = None
            if cat is None:
                continue
            parts.append((name, cat, value, css_var))
        for pname, cat, v, var in parts:
            canon = canonical(cat, v, root_px=root_px)
            if canon is not None:
                out[pname] = _Raw(pname, cat, canon, var, is_alias)
    return out


def _read_json(src, base_dir: Path | None) -> tuple[Any, Path | None]:
    if isinstance(src, dict):
        return src, base_dir
    p = Path(src)
    if not p.is_absolute() and base_dir is not None:
        p = base_dir / p
    try:
        return json.loads(p.read_text()), p.parent
    except (OSError, json.JSONDecodeError) as e:
        raise InvalidRequest(f"invalid: could not read the token file {p.name}: {e}. "
                             "fix: pass a readable Design Tokens JSON file.") from e


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and "$value" not in v:
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _resolver_trees(doc: dict, base_dir: Path | None) -> dict[str, dict]:
    """DTCG Resolver 2025.10: walk `resolutionOrder`; a modifier whose contexts include light and
    dark gives the themes, any other modifier takes its default context."""

    def source_tree(src) -> dict:
        if isinstance(src, dict) and set(src) == {"$ref"}:
            ref = src["$ref"]
            if ref.startswith("#/"):
                node: Any = doc
                for part in ref[2:].split("/"):
                    node = node[part]
                return set_tree(node)
            data, _ = _read_json(ref, base_dir)
            return data
        return src if isinstance(src, dict) else {}

    def set_tree(node) -> dict:
        tree: dict = {}
        for s in node.get("sources", []) if isinstance(node, dict) else []:
            tree = _deep_merge(tree, source_tree(s))
        return tree

    def context_tree(ctx) -> dict:
        items = ctx if isinstance(ctx, list) else [ctx]
        tree: dict = {}
        for s in items:
            tree = _deep_merge(tree, source_tree(s))
        return tree

    common: dict = {}
    themes: dict[str, dict] = {}
    for entry in doc.get("resolutionOrder", []):
        node = entry
        if isinstance(entry, dict) and set(entry) == {"$ref"} and entry["$ref"].startswith("#/"):
            node = doc
            for part in entry["$ref"][2:].split("/"):
                node = node[part]
        if isinstance(node, dict) and "contexts" in node:
            ctxs = {str(k).lower(): v for k, v in node["contexts"].items()}
            if "light" in ctxs and "dark" in ctxs:
                for t in ("light", "dark"):
                    themes[t] = _deep_merge(themes.get(t, common), context_tree(ctxs[t]))
                continue
            default = str(node.get("default", next(iter(ctxs), ""))).lower()
            layer = context_tree(ctxs.get(default, []))
        else:
            layer = set_tree(node)
        common = _deep_merge(common, layer)
        for t, tree in themes.items():
            themes[t] = _deep_merge(tree, layer)
    if not themes:
        return {"base": common}
    return themes


def from_dtcg(source, *, themes: dict[str, Any] | None = None, root_px: float = ROOT_PX,
              name: str | None = None) -> TokenSet:
    """A Design Tokens (DTCG 2025.10) file or dict. Light and dark come from:

    - a resolver document (it has `resolutionOrder`): a modifier with `light` and `dark` contexts;
    - `themes={"light": file_or_dict, "dark": file_or_dict}`, each laid over `source`;
    - a simple themes map: a JSON object whose only keys are `base`, `light` and `dark`, each a
      token tree or a file path.
    """
    doc, base_dir = _read_json(source, None)
    if not isinstance(doc, dict):
        raise InvalidRequest("invalid: a Design Tokens file is a JSON object. fix: pass the token tree.")
    label = name or (Path(source).name if isinstance(source, (str, Path)) else "dtcg")
    keys = {k for k in doc if not k.startswith("$")}
    scales = []
    ext = doc.get("$extensions") if isinstance(doc.get("$extensions"), dict) else {}
    own = ext.get("openajar") if isinstance(ext.get("openajar"), dict) else ext.get("agentos")  # legacy-name
    for item in (own or {}).get("scales", []) if isinstance(own, dict) else []:
        try:
            scales.append(Scale.model_validate(item))
        except ValueError:
            continue
    if "resolutionOrder" in doc:
        trees = _resolver_trees(doc, base_dir)
    elif keys and keys <= {"base", "light", "dark"} and ("light" in keys or "dark" in keys):
        base = _read_json(doc["base"], base_dir)[0] if "base" in doc else {}
        trees = {t: _deep_merge(base, _read_json(doc[t], base_dir)[0]) for t in ("light", "dark") if t in doc}
    elif themes:
        trees = {t: _deep_merge(doc, _read_json(v, base_dir)[0]) for t, v in themes.items()}
    else:
        trees = {"base": doc}
    per_theme = {t: _dtcg_raws(tree, root_px) for t, tree in trees.items()}
    return TokenSet(tokens=_merge_themes(per_theme), scales=scales, source=label)


# --- CSS custom properties ----------------------------------------------------------------

def parse_css_blocks(text: str) -> list[tuple[list[str], dict[str, str]]]:
    """Rules with their chain of preludes (`@media ...`, `@layer ...`, the selector) and the
    custom property declarations in them. Comments are dropped; nesting is followed."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    out: list[tuple[list[str], dict[str, str]]] = []

    def parse(i: int, chain: list[str]) -> int:
        buf = []
        decls: dict[str, str] = {}
        while i < len(text):
            ch = text[i]
            if ch == "{":
                prelude = "".join(buf).strip()
                # declarations before a nested rule end at the last ';'
                if ";" in prelude:
                    head, prelude = prelude.rsplit(";", 1)
                    _decls(head, decls)
                    prelude = prelude.strip()
                buf = []
                i = parse(i + 1, [*chain, prelude])
            elif ch == "}":
                _decls("".join(buf), decls)
                if decls:
                    out.append((chain, decls))
                return i + 1
            else:
                buf.append(ch)
                i += 1
        _decls("".join(buf), decls)
        if decls and chain:
            out.append((chain, decls))
        return i

    def _decls(chunk: str, decls: dict[str, str]):
        for part in _split_top(chunk, ";"):
            if ":" in part:
                k, v = part.split(":", 1)
                k = k.strip()
                if k.startswith("--"):
                    decls[k] = v.strip().removesuffix("!important").strip()

    parse(0, [])
    return out


_ROOTISH = re.compile(
    r"^(:root|html|:host|body|@theme.*|\*|"
    r"(:root|html|body)?(\.[\w-]*(dark|light|theme)[\w-]*|\[[^\]]*(theme|mode|scheme|dark|light)[^\]]*\])+"
    r"|:root(\.[\w-]+|\[[^\]]+\])+|html(\.[\w-]+|\[[^\]]+\])+|:host\([^)]*\)|:host-context\([^)]*\))$", re.IGNORECASE)


def theme_of_chain(chain: list[str]) -> str | None:
    """`base`, `light` or `dark` for a rule chain whose scope is the root or a theme scope;
    None for any other scope (a component's own variables are not tokens)."""
    theme = "base"
    for part in chain:
        p = part.strip()
        if p.startswith("@"):
            low = p.lower()
            if low.startswith("@theme"):
                continue
            if "prefers-color-scheme" in low:
                theme = "dark" if "dark" in low else "light"
            continue
        sels = [s.strip() for s in p.split(",") if s.strip()]
        if not sels or not all(_ROOTISH.match(s) for s in sels):
            return None
        low = p.lower()
        if "dark" in low:
            theme = "dark"
        elif "light" in low and theme == "base" and not any(s in (":root", "html", ":host") for s in sels):
            theme = "light"
    return theme


_VAR = re.compile(r"var\(\s*(--[\w-]+)\s*(?:,\s*((?:[^()]|\([^()]*\))*))?\)")


def pick_light_dark(value: str, theme: str) -> str:
    """`light-dark(a, b)` read for one theme: a in light, b in dark, nested ones too. Any other
    text is returned as it is."""
    low = value.lower()
    i = low.find("light-dark(")
    if i < 0 or theme not in ("light", "dark"):
        return value
    start = i + len("light-dark(")
    depth, j = 1, start
    while j < len(value) and depth:
        if value[j] == "(":
            depth += 1
        elif value[j] == ")":
            depth -= 1
        j += 1
    if depth:
        return value
    parts = _split_top(value[start:j - 1])
    if len(parts) != 2:
        return value
    chosen = parts[0] if theme == "light" else parts[1]
    return pick_light_dark(value[:i] + chosen + value[j:], theme)


def resolve_vars(value: str, env: dict[str, str], seen: tuple = ()) -> str | None:
    """Substitute var() references from `env`; None when one cannot be resolved."""
    def sub(m):
        name, fallback = m.group(1), m.group(2)
        if name in seen:
            raise ValueError
        if name in env:
            r = resolve_vars(env[name], env, (*seen, name))
            if r is None:
                raise ValueError
            return r
        if fallback is not None:
            r = resolve_vars(fallback, env, seen)
            if r is None:
                raise ValueError
            return r
        raise ValueError

    try:
        prev = None
        while prev != value and "var(" in value:
            prev = value
            value = _VAR.sub(sub, value)
        return value
    except ValueError:
        return None


TAILWIND_PREFIXES: list[tuple[str, TokenCategory | None]] = [
    ("--color-", C.COLOR),
    ("--radius-", C.RADIUS),
    ("--font-weight-", C.FONT_WEIGHT),
    ("--font-", C.FONT_FAMILY),
    ("--leading-", C.LINE_HEIGHT),
    ("--inset-shadow-", C.SHADOW),
    ("--shadow-", C.SHADOW),
    ("--text-", C.FONT_SIZE),
]


def _tailwind_category(var: str) -> TokenCategory | None:
    if var.startswith("--text-") and "--" in var[2:]:
        tail = var[2:].split("--", 1)[1]
        return C.LINE_HEIGHT if tail == "line-height" else None
    for prefix, cat in TAILWIND_PREFIXES:
        if var.startswith(prefix):
            return cat
    return None


def _var_name(var: str) -> str:
    return var.lstrip("-").replace("--", ".").replace("-", ".")


def raws_from_decls(per_theme_decls: dict[str, dict[str, str]], *, tailwind: bool, root_px: float,
                    canon_extra: dict[str, str] | None = None,
                    uses: dict[str, list[str]] | None = None) -> tuple[dict[str, dict[str, _Raw]], list[Scale]]:
    """Custom property declarations by theme, resolved and categorised. `canon_extra` maps a
    resolved value text the Python parser cannot read (color-mix, light-dark) to a computed
    colour the page gave for it. `uses` says what the page uses a variable for ({"--r-md":
    ["radius"]}): a length whose name says nothing is categorised by that use."""
    base = per_theme_decls.get("base", {})
    scales: list[Scale] = []
    out: dict[str, dict[str, _Raw]] = {}
    themes = dict(per_theme_decls)
    if any("light-dark(" in (v or "").lower() for d in per_theme_decls.values() for v in d.values()):
        # light-dark() holds both themes in one declaration: read it as a light and a dark value
        themes.setdefault("light", {})
        themes.setdefault("dark", {})
    for theme, decls in themes.items():
        env = dict(base)
        env.update(decls)
        raws: dict[str, _Raw] = {}
        for var, raw in env.items():
            resolved = resolve_vars(raw, env)
            if resolved is None:
                continue
            if theme in ("light", "dark"):
                resolved = pick_light_dark(resolved, theme)
            if canon_extra and resolved in canon_extra:
                resolved = canon_extra[resolved]
            if tailwind and var == "--spacing":
                # the step of the spacing scale, and a token too, so the probe can perturb it
                # and trace calc(var(--spacing) * n) back to it
                px = parse_length(resolved, root_px=root_px)
                if px and theme == "base":
                    scales.append(Scale(category=C.SPACE, step_px=px))
                if px:
                    raws["spacing"] = _Raw("spacing", C.SPACE, fmt_px(px), var, False)
                continue
            cat = _category_of(var, resolved, tailwind=tailwind, uses=uses, root_px=root_px)
            if cat is None:
                continue
            canon = canonical(cat, resolved, root_px=root_px)
            if canon is None and cat == C.FONT_FAMILY:
                canon = font_css(resolved)
            if canon is None:
                continue
            raws[_var_name(var)] = _Raw(_var_name(var), cat, canon, var, "var(" in raw)
        out[theme] = raws
    if tailwind and scales:
        scales.append(Scale(category=C.BORDER_WIDTH, step_px=1, max_px=16))
    return out, scales


_BY_USE = {"radius": C.RADIUS}
_SPACE_NAME = re.compile(r"space|spacing|gap|gutter|padding|margin")


def _category_of(var: str, resolved: str, *, tailwind: bool, uses: dict[str, list[str]] | None,
                 root_px: float) -> TokenCategory | None:
    """What a custom property is a token of, or None when it is none."""
    if tailwind:
        cat = _tailwind_category(var)
        if cat == C.FONT_FAMILY and parse_length(resolved, root_px=root_px) is not None:
            # Tailwind's --font-* are families, but a length is no family: --font-13: 13px is a
            # size scale of the app's own, read as in plain CSS
            cat = _plain_category(var, resolved)
        if cat is None and not var.startswith(("--tw-", "--text-", "--color-", "--default-")):
            # the app's own variables beside Tailwind's (--background, --type-14): read them as in plain CSS
            cat = _plain_category(var, resolved)
    else:
        cat = _plain_category(var, resolved)
    used_for = [_BY_USE[u] for u in (uses or {}).get(var, []) if u in _BY_USE]
    if used_for and (cat is None or (cat == C.SPACE and not _SPACE_NAME.search(var))) \
            and parse_length(resolved, root_px=root_px) is not None:
        cat = used_for[0]
    return cat


def _plain_category(var: str, resolved: str) -> TokenCategory | None:
    """A value that reads as a colour is a colour token whatever its name (--color-shadow);
    otherwise the name says what it is for, but never a colour the value is not."""
    if parse_color(resolved) is not None:
        return C.COLOR
    cat = category_from_name(var)
    return None if cat == C.COLOR else cat


def decls_by_theme(blocks: list[tuple[list[str], dict[str, str]]]) -> dict[str, dict[str, str]]:
    per: dict[str, dict[str, str]] = {}
    for chain, decls in blocks:
        theme = theme_of_chain(chain)
        if theme is None:
            continue
        per.setdefault(theme, {}).update(decls)
    if "light" in per and "base" not in per:
        per["base"] = {}
    per.setdefault("base", {})
    return per


def from_css(text: str, *, tailwind: bool | None = None, root_px: float = ROOT_PX, name: str = "css") -> TokenSet:
    """A CSS file of custom properties (Style Dictionary output, a hand-written tokens.css, or a
    Tailwind v4 theme). Root-level declarations are the base; `.dark`, `[data-theme=dark]` and
    `@media (prefers-color-scheme: dark)` give the dark theme. Component-level variables are
    not tokens and are left out."""
    blocks = parse_css_blocks(text)
    if tailwind is None:
        tailwind = "@theme" in text or bool(re.search(r"--spacing\s*:", text))
    per, scales = raws_from_decls(decls_by_theme(blocks), tailwind=tailwind, root_px=root_px)
    return TokenSet(tokens=_merge_themes(per), scales=scales, source=name)


def from_tailwind(text: str, *, root_px: float = ROOT_PX, name: str = "tailwind") -> TokenSet:
    """A Tailwind v4 theme: `@theme { ... }` source or the built `:root, :host { ... }` layer.
    `--spacing` becomes a scale (any multiple of it), border widths any whole px."""
    return from_css(text, tailwind=True, root_px=root_px, name=name)


def load_tokens(source, *, fmt: str | None = None, themes: dict[str, Any] | None = None,
                root_px: float = ROOT_PX) -> TokenSet:
    """A TokenSet from a file path, a dict (DTCG) or CSS text. `fmt` is `dtcg`, `css` or
    `tailwind`; without it the file name and content decide. For a live page use
    `tokens_from_page(page)`."""
    if isinstance(source, TokenSet):
        return source
    if isinstance(source, dict):
        return from_dtcg(source, themes=themes, root_px=root_px)
    if isinstance(source, Path) or (isinstance(source, str) and "\n" not in source and "{" not in source):
        p = Path(source)
        if not p.is_file():
            raise InvalidRequest(f"invalid: no token file at {p}. fix: pass the path of a .json or .css token file.")
        if fmt == "dtcg" or (fmt is None and p.suffix.lower() in (".json", ".tokens")):
            return from_dtcg(p, themes=themes, root_px=root_px)
        text = p.read_text()
        if fmt == "tailwind":
            return from_tailwind(text, root_px=root_px, name=p.name)
        return from_css(text, root_px=root_px, name=p.name)
    text = str(source)
    if fmt == "dtcg" or (fmt is None and text.lstrip().startswith("{") and text.rstrip().endswith("}")
                         and '"' in text and ":" in text and "$value" in text):
        return from_dtcg(json.loads(text), themes=themes, root_px=root_px)
    if fmt == "tailwind":
        return from_tailwind(text, root_px=root_px)
    return from_css(text, root_px=root_px)
