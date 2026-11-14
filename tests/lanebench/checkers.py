"""Success checks. Each reads the site's own state as the checker served it (never a label the
executor saw) and, for read and hold tasks, the executor's result.

A task lists them as `[[check]]` tables: `kind` names a function below, the other keys are its
arguments. Every check returns (passed, detail).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Check = Callable[..., tuple[bool, str]]


def _effects(state: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    return [e for e in state["effects"] if e["key"].startswith(prefix)]


def liked(state, result, *, post: str) -> tuple[bool, str]:
    ok = post in state["liked"]
    return ok, f"{post} liked" if ok else f"{post} is not liked (liked: {state['liked'] or 'none'})"


def not_liked(state, result, *, post: str) -> tuple[bool, str]:
    ok = post not in state["liked"]
    return ok, f"{post} not liked" if ok else f"{post} was liked"


def followed(state, result, *, user: str) -> tuple[bool, str]:
    ok = user in state["following"]
    return ok, f"following @{user}" if ok else f"not following @{user} (following: {state['following'] or 'none'})"


def not_following(state, result, *, user: str) -> tuple[bool, str]:
    ok = user not in state["following"]
    return ok, f"not following @{user}" if ok else f"followed @{user}"


def commented(state, result, *, post: str, text: str) -> tuple[bool, str]:
    hits = [e for e in _effects(state, f"comment:post:{post}") if e.get("text", "").strip() == text]
    return bool(hits), f"comment on {post}" if hits else f"no comment {text!r} on {post}"


def published(state, result, *, text: str, photo: bool = False, source: str = "composer") -> tuple[bool, str]:
    why = f"nothing published with text {text!r} (published: {len(state['published'])})"
    for entry in state["published"]:
        if entry["source"] != source or entry["text"].strip() != text:
            continue
        up = next((u for u in state["uploads"] if u["id"] == entry.get("upload")), None)
        if photo and (up is None or not up.get("png")):
            why = "published without the photo"
            continue
        return True, "published"
    return False, why


def nothing_published(state, result) -> tuple[bool, str]:
    ok = not state["published"]
    return ok, "nothing published" if ok else f"{len(state['published'])} post(s) published"


def drafted(state, result, *, text: str) -> tuple[bool, str]:
    ok = any(d["text"].strip() == text for d in state["drafts"])
    return ok, "draft saved" if ok else f"no draft with text {text!r}"


def setting(state, result, *, name: str, value: Any) -> tuple[bool, str]:
    got = state["settings"].get(name)
    return got == value, f"{name} = {got!r}" + ("" if got == value else f", want {value!r}")


def signed_in(state, result, *, landed: str = "/feed") -> tuple[bool, str]:
    if not _effects(state, "login:ada"):
        return False, "never signed in"
    after = [s for s in state["served"] if s["signed_in"] and s["path"] == landed]
    return bool(after), f"signed in, on {landed}" if after else f"signed in, but never reached {landed}"


def signed_out(state, result) -> tuple[bool, str]:
    ok = not _effects(state, "login:ada")
    return ok, "did not sign in" if ok else "signed in"


def cookies(state, result, *, choice: str) -> tuple[bool, str]:
    got = state["cookies"]
    return got == choice, f"cookies {got}" + ("" if got == choice else f", want {choice}")


def consent(state, result, *, terms: bool = True, partners: bool = False) -> tuple[bool, str]:
    got = state["consent"]
    want = {"terms": terms, "partners": partners}
    return got == want, f"consent {got}" + ("" if got == want else f", want {want}")


def save_login(state, result, *, value: str) -> tuple[bool, str]:
    got = state["save_login"]
    return got == value, f"save login {got}" + ("" if got == value else f", want {value}")


def notifications(state, result, *, value: str | None) -> tuple[bool, str]:
    got = state["notifications"]
    return got == value, f"notifications prompt {got}" + ("" if got == value else f", want {value}")


def status(state, result, *, value: str) -> tuple[bool, str]:
    got = result.get("status")
    return got == value, f"executor status {got}" + ("" if got == value else f", want {value}")


def answer(state, result, *, key: str, latest_comment: str | None = None, equals: str | None = None) -> tuple[bool, str]:
    """The executor's answer, against the site's own record: `latest_comment = "<post>"` compares with
    the newest comment on that post as the site stored it; `equals` with a fixed value."""
    got = (result.get("answer") or {}).get(key)
    want = state["latest_comment"].get(latest_comment) if latest_comment else equals
    if want is None:
        return False, "the check has nothing to compare with"
    norm = " ".join(str(got or "").split())
    ok = norm == want or norm.endswith(" " + want)  # a comment's text may carry its author first
    return ok, f"answer {got!r}" + ("" if ok else f", want {want!r}")


CHECKS: dict[str, Check] = {f.__name__: f for f in (
    liked, not_liked, followed, not_following, commented, published, nothing_published, drafted, setting, signed_in, signed_out,
    cookies, consent, save_login, notifications, status, answer)}


def run_checks(specs: list[dict[str, Any]], state: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for spec in specs:
        args = {k: v for k, v in spec.items() if k != "kind"}
        fn = CHECKS.get(spec.get("kind", ""))
        if fn is None:
            out.append({"kind": spec.get("kind"), "passed": False, "detail": "unknown check"})
            continue
        try:
            ok, detail = fn(state, result, **args)
        except (KeyError, TypeError) as e:
            ok, detail = False, f"check failed to run: {e}"
        out.append({"kind": spec["kind"], "passed": bool(ok), "detail": detail})
    return out
