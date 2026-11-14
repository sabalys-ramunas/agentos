"""The chat brain's runner: one call to an OpenAI-compatible chat-completions endpoint.

The other brains are CLIs someone else ships (`claude -p`, `codex exec`). Codex dropped its "chat"
wire API, so nothing we can drive speaks chat completions, and this small script is that CLI. It
keeps the same contract, so the shim, its limits and the fallback chain treat it like any brain:

  the prompt arrives on stdin, the API key in AGENTOS_CHAT_API_KEY (through the shim's private
  stdin path, never argv), and one JSON object per line goes to stdout:

    {"type":"chat.started","model":..,"version":..}
    {"type":"chat.completed","text":..,"finish_reason":..,"usage":{"prompt_tokens":..,..},
     "usage_estimated":false}
    {"type":"chat.failed","status":429,"message":..,"retry_after":30,"limit":null}

`status` is the endpoint's HTTP status, or null when it never answered (down, timed out, not JSON).

This is a bounded job, not an agent: one prompt, one reply, no tools and no lane, so a brain on
this runner has `tools: false` and its grant token is never handed to it. No retry here: a retried
POST can bill twice, and the fallback chain decides what happens next.

Budget: the reply's usage arrives only with the reply, so the shim's budget check comes too late
to stop a call. The ceiling goes into the request instead: `max_tokens` is what the budget still
pays for after the prompt, and a prompt the budget cannot pay for is refused before any call.

Standard library only, and started with `python -I`: it runs under the system Python, because the
brain user cannot read agentosd's venv.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.request

VERSION = "1.0.0"
API_KEY_ENV = "AGENTOS_CHAT_API_KEY"
MAX_BODY = 8 * 1024 * 1024
CHARS_PER_TOKEN = 3  # counts high on purpose: a guess must not let a budget overrun


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def fail(message: str, *, status: int | None = None, retry_after: str | None = None,
         limit: str | None = None) -> int:
    emit({"type": "chat.failed", "status": status, "message": message[:600], "retry_after": retry_after,
          "limit": limit})
    return 1


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would resend the Authorization header to wherever a redirect points."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def output_ceiling(prompt_tokens: int, args: argparse.Namespace) -> int | None:
    """The most reply tokens the budget still pays for, or None when it cannot pay for the prompt."""
    if args.max_usd is None:
        return args.max_output_tokens
    left = args.max_usd - prompt_tokens * args.price_in / 1e6
    if left <= 0:
        return None
    if not args.price_out:
        return args.max_output_tokens
    tokens = int(left * 1e6 / args.price_out)
    return min(tokens, args.max_output_tokens) if tokens >= 1 else None


def error_text(body: bytes) -> str:
    """The provider's own message when it sent the usual {"error": {"message": ..}}, else the body."""
    text = body.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except ValueError:
        return text
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])
    return str(err) if err else text


def call(args: argparse.Namespace, key: str, messages: list[dict], max_tokens: int) -> int:
    body = json.dumps({"model": args.model, "messages": messages, "max_tokens": max_tokens, "stream": False})
    request = urllib.request.Request(
        args.base_url.rstrip("/") + "/chat/completions", data=body.encode(), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Authorization": f"Bearer {key}", "User-Agent": f"agentos-chat/{VERSION}"})
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=args.timeout) as resp:
            raw = resp.read(MAX_BODY)
    except urllib.error.HTTPError as exc:
        return fail(error_text(exc.read(MAX_BODY)) or f"HTTP {exc.code}", status=exc.code,
                    retry_after=exc.headers.get("Retry-After"))
    except OSError as exc:  # URLError, a refused connection, a timeout, a TLS failure
        reason = getattr(exc, "reason", None) or exc
        return fail(f"the endpoint did not answer: {reason}")
    try:
        data = json.loads(raw)
        choice = data["choices"][0]
        text = choice["message"].get("content")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return fail("the endpoint's reply is not a chat completion")
    finish = choice.get("finish_reason")
    if not text:
        return fail(f"the endpoint returned no text (finish_reason {finish})", status=200)
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    estimated = not usage.get("prompt_tokens") and not usage.get("completion_tokens")
    if estimated:  # some servers leave usage out; the budget must still move
        usage = {"prompt_tokens": sum(estimate_tokens(m["content"]) for m in messages),
                 "completion_tokens": estimate_tokens(text)}
    emit({"type": "chat.completed", "text": text, "finish_reason": finish, "usage": usage,
          "usage_estimated": estimated})
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="agentos-chat", description=__doc__.splitlines()[0])
    ap.add_argument("--version", action="version", version=f"agentos-chat {VERSION}")
    ap.add_argument("--base-url", help="the endpoint, up to and including /v1")
    ap.add_argument("--model", help="the model name the endpoint expects")
    ap.add_argument("--system", default="", help="the system message")
    ap.add_argument("--max-output-tokens", type=int, default=4096, help="the most reply tokens to ask for")
    ap.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for the endpoint")
    ap.add_argument("--max-usd", type=float, default=None, help="what this call may spend at most")
    ap.add_argument("--price-in", type=float, default=0.0, help="USD per million input tokens")
    ap.add_argument("--price-out", type=float, default=0.0, help="USD per million output tokens")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    key = os.environ.pop(API_KEY_ENV, "")
    if not args.base_url or not args.model:
        return fail("--base-url and --model are required")
    if not key:
        return fail(f"no API key in {API_KEY_ENV}", status=401)
    prompt = sys.stdin.read()
    messages = [{"role": "system", "content": args.system}] if args.system else []
    messages.append({"role": "user", "content": prompt})
    emit({"type": "chat.started", "model": args.model, "version": VERSION})
    ceiling = output_ceiling(sum(estimate_tokens(m["content"]) for m in messages), args)
    if ceiling is None:
        return fail(f"${args.max_usd:.4f} does not cover this prompt", limit="limit_cost")
    return call(args, key, messages, ceiling)


if __name__ == "__main__":
    sys.exit(main())
