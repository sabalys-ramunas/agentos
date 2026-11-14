"""Parse the brain CLIs' JSON streams into normalized transcript events, accounting and an
honest outcome. Pure Python, no pydantic, because the per-task shim imports it.

Formats were captured from the real CLIs on the node (2026-11-03):

claude 2.1.317, `claude -p --output-format stream-json --verbose`:
  {"type":"system","subtype":"init","session_id":..,"model":..,"claude_code_version":..}
  {"type":"system","subtype":"api_retry","attempt":1,"error_status":401,"error":"authentication_failed"}
  {"type":"assistant","message":{"id":..,"model":..,"usage":{..},"content":[..]},
   "error":"authentication_failed","is_api_error_message":true,"api_error_status":401}
  {"type":"rate_limit_event","rate_limit_info":{"status":..,"resetsAt":..,"rateLimitType":..}}
  {"type":"result","subtype":"success","is_error":true,"terminal_reason":"api_error",
   "api_error_status":401,"result":"Failed to authenticate. API Error: 401 ...",
   "num_turns":1,"total_cost_usd":0,"usage":{..},"session_id":..}
  Note: `subtype` says "success" even when `is_error` is true; never trust subtype alone.

codex-cli 0.166.0, `codex exec --json`:
  {"type":"thread.started","thread_id":..}  {"type":"turn.started"}
  {"type":"item.completed","item":{"id":..,"type":"agent_message"|"command_execution"|..}}
  {"type":"turn.completed","usage":{"input_tokens":..,"cached_input_tokens":..,"output_tokens":..}}
  {"type":"error","message":"Reconnecting... 2/5 (unexpected status 401 Unauthorized: ...)"}
  {"type":"turn.failed","error":{"message":"unexpected status 401 Unauthorized: ..."}}
  Usage limit text (from the binary): "You've hit your usage limit. ... or try again at <time>."

chat (our own runner, agentos.brains.chatcli): its docstring has the format.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from typing import Any

PREVIEW = 2000
RAW_KEEP = 32_000

_RATE_RE = re.compile(
    r"\b429\b|too many requests|rate[ _-]?limit|usage limit|hit your (usage )?limit|"
    r"limit reached|quota exceeded|exceeded retry limit, last status: 429", re.IGNORECASE)
_AUTH_RE = re.compile(
    r"\b401\b|\b403\b|unauthorized|not logged in|please run /login|invalid api key|"
    r"invalid_api_key|authentication[_ ]failed|failed to authenticate|oauth token (has )?expired|"
    r"token (is )?(expired|revoked|invalid)|missing bearer|log ?in again|refresh token|"
    r"authentication_error|permission_error|billing_error|credit balance is too low", re.IGNORECASE)
_SERVER_RE = re.compile(r"\b5\d\d\b|overloaded|internal server error|service unavailable|"
                        r"bad gateway|gateway timeout|server_error", re.IGNORECASE)

_UNIX_RESET_RE = re.compile(r"\|(\d{10})\b")
_IN_RE = re.compile(r"try again in ((?:\d+\s*(?:days?|hours?|minutes?|mins?|seconds?|secs?)[\s,]*(?:and\s*)?)+)",
                    re.IGNORECASE)
_IN_PART_RE = re.compile(r"(\d+)\s*(day|hour|min|sec)", re.IGNORECASE)
_AT_RE = re.compile(r"(?:resets|try again at)\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?", re.IGNORECASE)


def classify_text(text: str) -> str | None:
    """'rate_limited' | 'auth_failed' | 'server_error' | None for an error message."""
    if not text:
        return None
    if _RATE_RE.search(text):
        return "rate_limited"
    if _AUTH_RE.search(text):
        return "auth_failed"
    if _SERVER_RE.search(text):
        return "server_error"
    return None


def parse_reset(text: str, now: float | None = None) -> float | None:
    """Best-effort unix time when a rate limit resets, from a CLI message."""
    if not text:
        return None
    now = time.time() if now is None else now
    m = _UNIX_RESET_RE.search(text)
    if m:
        return float(m.group(1))
    m = _IN_RE.search(text)
    if m:
        secs = 0
        for n, unit in _IN_PART_RE.findall(m.group(1)):
            secs += int(n) * {"day": 86400, "hour": 3600, "min": 60, "sec": 1}[unit.lower()]
        if secs:
            return now + secs
    m = _AT_RE.search(text)
    if m:
        hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
        if ampm.startswith("p") and hour < 12:
            hour += 12
        if ampm.startswith("a") and hour == 12:
            hour = 0
        if 0 <= hour < 24 and 0 <= minute < 60:
            base = datetime.fromtimestamp(now)
            target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target.timestamp() <= now:
                target += timedelta(days=1)
            return target.timestamp()
    return None


def _preview(text: Any, limit: int = PREVIEW) -> str:
    if text is None:
        return ""
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + f"... [{len(s) - limit} more chars]"


class StreamParser:
    """Shared accounting. Subclasses implement `_on(obj)` returning normalized events."""

    runner = "base"

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.model: str | None = None
        self.cli_version: str | None = None
        self.turns = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.cost_usd: float | None = None
        self.result_text: str | None = None
        self.result_is_error: bool | None = None
        self.final_seen = False
        self.signal: str | None = None        # rate_limited | auth_failed | server_error
        self.signal_detail: str | None = None
        self.resets_at: float | None = None
        self.window_utilization: float | None = None
        self.limit_hit: str | None = None     # limit_turns | limit_cost reported by the CLI itself
        self.last_error: str | None = None

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_write_tokens

    def feed(self, line: str) -> list[dict[str, Any]]:
        line = line.strip()
        if not line:
            return []
        try:
            obj = json.loads(line)
        except ValueError:
            return [{"kind": "raw", "text": _preview(line)}]
        if not isinstance(obj, dict):
            return [{"kind": "raw", "text": _preview(line)}]
        events = self._on(obj)
        if len(line) <= RAW_KEEP:
            for ev in events:
                ev.setdefault("raw", obj)
                break
        else:
            for ev in events:
                ev["truncated"] = True
        return events

    def _note_signal(self, kind: str | None, detail: str) -> None:
        if not kind:
            return
        # a rate limit outranks an auth error seen while retrying, which outranks a 5xx
        rank = {"rate_limited": 3, "auth_failed": 2, "server_error": 1}
        if self.signal is None or rank[kind] >= rank[self.signal]:
            self.signal = kind
            self.signal_detail = _preview(detail, 400)
        if kind == "rate_limited" and self.resets_at is None:
            self.resets_at = parse_reset(detail)

    def _on(self, obj: dict[str, Any]) -> list[dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError

    def outcome(self, exit_code: int | None) -> tuple[str, str]:
        """(outcome, reason) once the process has exited."""
        if self.limit_hit:
            return self.limit_hit, f"the CLI stopped at its own {self.limit_hit.removeprefix('limit_')} limit"
        if self.final_seen and self.result_is_error is False:
            return "succeeded", "finished"
        text = " ".join(t for t in (self.result_text if self.result_is_error else None,
                                    self.last_error, self.signal_detail) if t)
        kind = classify_text(text) or self.signal
        if kind:
            if kind == "rate_limited" and self.resets_at is None:
                self.resets_at = parse_reset(text)
            return kind, _preview(text, 300) or kind
        if self.final_seen and self.result_is_error:
            return "failed", _preview(self.result_text or self.last_error or "the agent reported an error", 300)
        if exit_code not in (0, None):
            return "crashed", f"exited with code {exit_code} without a result" + (
                f": {_preview(self.last_error, 200)}" if self.last_error else "")
        return "crashed", "exited without a result"

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id, "model": self.model, "cli_version": self.cli_version,
            "turns": self.turns, "tokens": self.tokens,
            "usage": {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                      "cache_read_tokens": self.cache_read_tokens, "cache_write_tokens": self.cache_write_tokens},
            "cost_usd": self.cost_usd, "resets_at": self.resets_at,
            "window_utilization": self.window_utilization, "signal": self.signal,
        }


class ClaudeStreamParser(StreamParser):
    runner = "claude"

    def __init__(self) -> None:
        super().__init__()
        self._usage_by_msg: dict[str, dict[str, int]] = {}

    def _sum_usage(self) -> None:
        self.input_tokens = sum(u.get("input_tokens", 0) for u in self._usage_by_msg.values())
        self.output_tokens = sum(u.get("output_tokens", 0) for u in self._usage_by_msg.values())
        self.cache_read_tokens = sum(u.get("cache_read_input_tokens", 0) for u in self._usage_by_msg.values())
        self.cache_write_tokens = sum(u.get("cache_creation_input_tokens", 0) for u in self._usage_by_msg.values())

    def _on(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        t = obj.get("type")
        self.session_id = obj.get("session_id") or self.session_id
        if t == "system":
            sub = obj.get("subtype")
            if sub == "init":
                self.model = obj.get("model") or self.model
                self.cli_version = obj.get("claude_code_version") or self.cli_version
                return [{"kind": "system", "text": f"session started: model {self.model}"}]
            if sub == "api_retry":
                status = obj.get("error_status")
                err = str(obj.get("error") or "")
                detail = f"API retry {obj.get('attempt')}/{obj.get('max_retries')}: {status} {err}"
                if status == 429 or err == "rate_limit":
                    self._note_signal("rate_limited", detail)
                elif status in (401, 403) or err in ("authentication_failed", "billing_error"):
                    self._note_signal("auth_failed", detail)
                elif isinstance(status, int) and status >= 500:
                    self._note_signal("server_error", detail)
                return [{"kind": "retry", "text": detail}]
            return [{"kind": "system", "text": _preview(sub or "system")}]
        if t == "rate_limit_event":
            info = obj.get("rate_limit_info") or {}
            status = str(info.get("status") or "")
            util = info.get("utilization")
            if isinstance(util, (int, float)):
                self.window_utilization = float(util)
            resets = info.get("resetsAt")
            if status in ("rejected", "blocked"):
                self._note_signal("rate_limited", f"rate limit {info.get('rateLimitType') or ''} {status}")
                if isinstance(resets, (int, float)):
                    self.resets_at = float(resets) / (1000.0 if resets > 1e11 else 1.0)
            return [{"kind": "rate_limit", "text": f"rate limit {status} {info.get('rateLimitType') or ''}".strip()}]
        if t == "assistant":
            msg = obj.get("message") or {}
            mid = msg.get("id") or obj.get("uuid") or str(len(self._usage_by_msg))
            synthetic = msg.get("model") == "<synthetic>"
            if not synthetic:
                if mid not in self._usage_by_msg:
                    self.turns += 1
                    self._usage_by_msg[mid] = {}
                usage = msg.get("usage") or {}
                cur = self._usage_by_msg[mid]
                for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                          "cache_creation_input_tokens"):
                    v = usage.get(k)
                    if isinstance(v, int):
                        cur[k] = max(cur.get(k, 0), v)
                self._sum_usage()
                self.model = msg.get("model") or self.model
            err = obj.get("error")
            events = []
            for block in msg.get("content") or []:
                bt = block.get("type")
                if bt == "text":
                    events.append({"kind": "assistant", "text": _preview(block.get("text"))})
                elif bt == "tool_use":
                    events.append({"kind": "tool_use", "text": f"{block.get('name')}: "
                                   f"{_preview(block.get('input'), 400)}"})
                elif bt == "thinking":
                    events.append({"kind": "thinking", "text": _preview(block.get("thinking"), 400)})
            if err or obj.get("is_api_error_message"):
                text = " ".join(b.get("text", "") for b in msg.get("content") or [] if b.get("type") == "text")
                self.last_error = text or str(err)
                kind = {"rate_limit": "rate_limited", "authentication_failed": "auth_failed",
                        "billing_error": "auth_failed", "server_error": "server_error"}.get(str(err))
                status = obj.get("api_error_status")
                if not kind and status == 429:
                    kind = "rate_limited"
                elif not kind and status in (401, 403):
                    kind = "auth_failed"
                self._note_signal(kind or classify_text(text), text or str(err))
                events.append({"kind": "error", "text": _preview(self.last_error, 400)})
            return events or [{"kind": "assistant", "text": ""}]
        if t == "user":
            msg = obj.get("message") or {}
            out = []
            content = msg.get("content")
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result":
                    out.append({"kind": "tool_result", "text": _preview(block.get("content"), 600)})
            return out or [{"kind": "user", "text": _preview(msg.get("content"), 400)}]
        if t == "result":
            self.final_seen = True
            self.result_is_error = bool(obj.get("is_error"))
            self.result_text = obj.get("result") if isinstance(obj.get("result"), str) else None
            if isinstance(obj.get("num_turns"), int):
                self.turns = max(self.turns, obj["num_turns"])
            if isinstance(obj.get("total_cost_usd"), (int, float)):
                self.cost_usd = float(obj["total_cost_usd"])
            usage = obj.get("usage") or {}
            if usage:
                self.input_tokens = max(self.input_tokens, int(usage.get("input_tokens") or 0))
                self.output_tokens = max(self.output_tokens, int(usage.get("output_tokens") or 0))
                self.cache_read_tokens = max(self.cache_read_tokens, int(usage.get("cache_read_input_tokens") or 0))
                self.cache_write_tokens = max(self.cache_write_tokens,
                                              int(usage.get("cache_creation_input_tokens") or 0))
            sub = str(obj.get("subtype") or "")
            reason = str(obj.get("terminal_reason") or "")
            if "max_turns" in sub or "max_turns" in reason:
                self.limit_hit = "limit_turns"
            elif "budget" in sub or "budget" in reason:
                self.limit_hit = "limit_cost"
            if self.result_is_error:
                status = obj.get("api_error_status")
                if status == 429:
                    self._note_signal("rate_limited", self.result_text or "429")
                elif status in (401, 403):
                    self._note_signal("auth_failed", self.result_text or str(status))
                elif isinstance(status, int) and status >= 500:
                    self._note_signal("server_error", self.result_text or str(status))
            return [{"kind": "result", "text": _preview(self.result_text), "is_error": self.result_is_error,
                     "turns": self.turns, "cost_usd": self.cost_usd}]
        return [{"kind": str(t or "event"), "text": ""}]


CODEX_TOOL_ITEMS = frozenset({"command_execution", "mcp_tool_call", "file_change", "web_search",
                              "custom_tool_call"})


class CodexJsonParser(StreamParser):
    runner = "codex"

    def __init__(self) -> None:
        super().__init__()
        self._turn_usage: list[dict[str, int]] = []
        self._failed = False

    def _on(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        t = obj.get("type")
        if t == "thread.started":
            self.session_id = obj.get("thread_id") or self.session_id
            return [{"kind": "system", "text": f"thread started {self.session_id}"}]
        if t == "turn.started":
            return [{"kind": "system", "text": "turn started"}]
        if t in ("item.started", "item.updated", "item.completed"):
            item = obj.get("item") or {}
            it = item.get("type")
            if t != "item.completed":
                return [{"kind": "progress", "text": f"{it} {item.get('status') or ''}".strip()}]
            if it == "agent_message":
                self.turns += 1
                self.result_text = item.get("text")
                return [{"kind": "assistant", "text": _preview(item.get("text"))}]
            if it in CODEX_TOOL_ITEMS:
                self.turns += 1
                label = item.get("command") or item.get("tool") or item.get("query") or it
                out = item.get("aggregated_output")
                return [{"kind": "tool_use", "text": f"{it}: {_preview(label, 300)}"
                         + (f" -> {_preview(out, 400)}" if out else "")}]
            if it == "reasoning":
                return [{"kind": "thinking", "text": _preview(item.get("text"), 400)}]
            if it == "error":
                msg = str(item.get("message") or "")
                self.last_error = msg
                self._note_signal(classify_text(msg), msg)
                return [{"kind": "error", "text": _preview(msg, 400)}]
            return [{"kind": "event", "text": str(it)}]
        if t == "turn.completed":
            usage = obj.get("usage") or {}
            self._turn_usage.append({k: int(v) for k, v in usage.items() if isinstance(v, int)})
            self.input_tokens = sum(u.get("input_tokens", 0) for u in self._turn_usage)
            self.output_tokens = sum(u.get("output_tokens", 0) for u in self._turn_usage)
            self.cache_read_tokens = sum(u.get("cached_input_tokens", 0) for u in self._turn_usage)
            self.final_seen = True
            if not self._failed:
                self.result_is_error = False
            return [{"kind": "result", "text": _preview(self.result_text), "is_error": False,
                     "turns": self.turns}]
        if t == "turn.failed":
            msg = str((obj.get("error") or {}).get("message") or "turn failed")
            self._failed = True
            self.final_seen = True
            self.result_is_error = True
            self.result_text = msg
            self.last_error = msg
            self._note_signal(classify_text(msg), msg)
            return [{"kind": "result", "text": _preview(msg), "is_error": True, "turns": self.turns}]
        if t == "error":
            msg = str(obj.get("message") or "")
            self.last_error = msg
            self._note_signal(classify_text(msg), msg)
            kind = "retry" if msg.startswith("Reconnecting") else "error"
            return [{"kind": kind, "text": _preview(msg, 400)}]
        return [{"kind": str(t or "event"), "text": ""}]


class ChatJsonParser(StreamParser):
    """The runner reports an HTTP status, so the outcome comes from that and not from guessing at
    the provider's wording: a message naming a model `x-429` is not a rate limit."""

    runner = "chat"

    def __init__(self) -> None:
        super().__init__()
        self._failed: tuple[str, str] | None = None

    @staticmethod
    def _kind(status: int | None) -> str:
        if status == 429:
            return "rate_limited"
        if status in (401, 403):
            return "auth_failed"
        # down, a timeout, a redirect we refuse, a path that does not exist: this endpoint cannot
        # serve the task and another brain may. Any other 4xx is the request itself: it fails here.
        if status is None or status >= 500 or status == 404 or 300 <= status < 400:
            return "server_error"
        return "failed"

    def _on(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        t = obj.get("type")
        if t == "chat.started":
            self.model = obj.get("model")
            self.cli_version = obj.get("version")
            return [{"kind": "system", "text": f"calling {self.model}"}]
        if t == "chat.completed":
            usage = obj.get("usage") or {}
            self.input_tokens = int(usage.get("prompt_tokens") or 0)
            self.output_tokens = int(usage.get("completion_tokens") or 0)
            self.turns = 1
            self.result_text = obj.get("text")
            self.final_seen = True
            self.result_is_error = False
            if obj.get("finish_reason") == "length":  # cut off: never reported as a finished reply
                self.limit_hit = "limit_tokens"
            note = " (usage estimated: the endpoint reported none)" if obj.get("usage_estimated") else ""
            return [{"kind": "assistant", "text": _preview(self.result_text)},
                    {"kind": "result", "text": _preview(self.result_text) + note, "is_error": False, "turns": 1}]
        if t == "chat.failed":
            msg = str(obj.get("message") or "the call failed")
            status = obj.get("status")
            status = status if isinstance(status, int) else None
            self.final_seen = True
            self.result_is_error = True
            self.last_error = msg
            if obj.get("limit"):
                self.limit_hit = str(obj["limit"])
            kind = self._kind(status)
            self._failed = (kind, _preview(msg, 300))
            if kind != "failed":
                self._note_signal(kind, msg)
            retry_after = str(obj.get("retry_after") or "")
            if kind == "rate_limited" and retry_after.isdigit():
                self.resets_at = time.time() + int(retry_after)
            return [{"kind": "error", "text": _preview(msg, 400)},
                    {"kind": "result", "text": _preview(msg), "is_error": True, "turns": 0}]
        return [{"kind": str(t or "event"), "text": ""}]

    def outcome(self, exit_code: int | None) -> tuple[str, str]:
        if self._failed and not self.limit_hit:
            return self._failed
        return super().outcome(exit_code)


def make_parser(runner: str) -> StreamParser:
    if runner == "claude":
        return ClaudeStreamParser()
    if runner == "codex":
        return CodexJsonParser()
    if runner == "chat":
        return ChatJsonParser()
    raise ValueError(f"unknown runner {runner!r}")
