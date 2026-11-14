"""The tool `profile_learn_brand`: learn a product's or business's brand from its live site and
propose it to the owner. It lives in the profiles toolset (agentos.profiles.tools lists it)."""

from __future__ import annotations

from typing import Any

from agentos.core.errors import ServiceUnavailable
from agentos.core.models import Effect

STUDIO_SERVICE = "studio"
DIFF_SHOWN = 8


def replace_patch(old: Any, new: Any) -> Any:
    """A change that, merged over `old` the way profile_propose merges (dicts merge, None
    removes), leaves exactly `new`."""
    if isinstance(old, dict) and isinstance(new, dict):
        out: dict[str, Any] = {k: None for k in old if k not in new}
        for k, v in new.items():
            out[k] = replace_patch(old[k], v) if k in old else v
        return out
    return new


def brand_change(old_brand: dict[str, Any] | None, learned: dict[str, Any]) -> dict[str, Any]:
    """The brand change: the learned tokens and theme replace the old ones; fonts and components
    are lists, which replace anyway. Anything not learned is left as it is."""
    old_brand = old_brand or {}
    change: dict[str, Any] = {}
    for key, value in learned.items():
        change[key] = replace_patch(old_brand.get(key), value) if key in ("tokens", "theme") else value
    return change


async def profile_learn_brand(ctx: Any, *, profile: str, url: str, pages: list[str] | None = None) -> dict:
    """Learn a product's or business's brand from its live site and propose it as a draft for the
    owner. Opens url (and up to 4 more pages of the same site, as paths or urls) in the review
    browser in light and dark at 360 and 1280 px. Takes the site's own custom properties as tokens
    where it declares them; where it does not, clusters the colours, spacing, radii and type it
    uses, labelled heuristic. Finds how it turns dark and its component hints. Returns counts
    per category with coverage and one-offs, the palette, what is uncertain, and the proposal's
    version (state draft). Nothing changes until the owner approves; nothing is proposed when no
    consistent system was found.

    Args:
        profile: The product or business profile, as "workspace/profile".
        url: The site's address (http or https), such as http://localhost:3000/.
        pages: Up to 4 more pages of the same site, such as ["/pricing", "/settings"].
    """
    from agentos.profiles.errors import ProfileInvalid
    from agentos.profiles.tools import _check_reach, _svc

    from .learn import learn_brand, site_pages

    ref = profile.strip()
    svc = _svc(ctx)
    # _check_reach goes through covers_workspace (a frozen workspace is refused) and, to read,
    # ctx.can_access_lane on the profile's accounts
    _check_reach(ctx, svc, ref, write=True)
    base = svc.base_definition(ref)
    kind = getattr(base.kind, "value", base.kind)
    if kind not in ("product", "business"):
        raise ProfileInvalid(f"{ref} is a {kind} profile; a brand belongs to a product or business profile")
    urls = site_pages(url, pages)
    await ctx.authorize(Effect.DRAFT, summary=f"learn the brand of profile {ref} from {urls[0][:120]}")
    studio = ctx.runtime.get(STUDIO_SERVICE)
    if studio is None:
        raise ServiceUnavailable("the review browser (studio) is not running on this node: start the node with the "
                                 "studio extra installed (uv sync --extra studio), then call again")
    draft = await learn_brand(studio.browser, url, pages=urls[1:])
    out: dict[str, Any] = {"profile": ref, **draft.summary(), "counts": draft.counts()}
    if not draft.has_system or not draft.brand:
        out["proposal"] = None
        out["next"] = ("No consistent design system was found, so nothing was proposed. Ask the owner for the brand's "
                       "tokens, or call again with pages that show more of the design.")
        return out
    old = base.canonical().get("brand")
    saved = svc.propose(ref, {"brand": brand_change(old, draft.brand)}, draft.reason(), by=ctx.principal)
    diff = saved.get("diff") or []
    out["proposal"] = {"version": saved["version"], "state": saved["state"], "changes": len(diff),
                       "diff": diff[:DIFF_SHOWN]}
    if getattr(studio, "where", None) == "local":
        where = (f"on this computer, with: agentos studio profile show {ref} --version {saved['version']}, then "
                 f"agentos studio profile approve {ref} --version {saved['version']}")
    else:
        where = f"in the panel (Profiles), or on the node with: agentos profile approve {ref} --version {saved['version']}"
    out["owner_action"] = f"The owner approves or rejects it {where}."
    out["next"] = (f"Proposed as draft version {saved['version']}; only the owner can approve it, {where}. Tell the "
                   "owner that, and what is uncertain. Keep using the approved profile (profile_use) meanwhile.")
    return out
