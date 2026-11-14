"""Stream parsing against lines captured from the real CLIs on the node (2026-11-03)."""

import json
import time

from agentos.brains.parsers import ClaudeStreamParser, CodexJsonParser, classify_text, parse_reset

# claude 2.1.317, no credential at all (trimmed to the fields that matter, values verbatim)
CLAUDE_NOT_LOGGED_IN = [
    ('{"type":"system","subtype":"init","cwd":"/w","session_id":"903ad58e-feca-4b2f-a38c-86587bc4f703",'
    '"tools":["Task","Bash"],"mcp_servers":[],"model":"claude-sonnet-5-5","permissionMode":"dontAsk",'
    '"apiKeySource":"none","claude_code_version":"2.1.317"}'),
    ('{"type":"assistant","message":{"id":"f6de052a-6ee1-49a8-be85-723ecfdd8a72","model":"<synthetic>",'
    '"role":"assistant","stop_reason":"stop_sequence","type":"message","usage":{"input_tokens":0,'
    '"output_tokens":0,"cache_creation_input_tokens":0,"cache_read_input_tokens":0},"content":[{"type":"text",'
    '"text":"Not logged in · Please run /login"}]},"parent_tool_use_id":null,'
    '"session_id":"903ad58e-feca-4b2f-a38c-86587bc4f703","error":"authentication_failed","is_api_error_message":true}'),
    ('{"duration_api_ms":0,"stop_reason":"stop_sequence","session_id":"903ad58e-feca-4b2f-a38c-86587bc4f703",'
    '"total_cost_usd":0,"usage":{"input_tokens":0,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,'
    '"output_tokens":0},"modelUsage":{},"permission_denials":[],"terminal_reason":"api_error","is_error":true,'
    '"num_turns":1,"subtype":"success","api_error_status":null,"result":"Not logged in · Please run /login",'
    '"type":"result","duration_ms":257}'),
]

# claude 2.1.317 with an invalid OAuth token
CLAUDE_BAD_TOKEN = [
    ('{"type":"system","subtype":"api_retry","attempt":1,"max_retries":10,"retry_delay_ms":522,"error_status":401,'
    '"error":"authentication_failed","session_id":"7ac67cde-c864-4758-9f44-aa9311020eed"}'),
    ('{"type":"system","subtype":"api_retry","attempt":2,"max_retries":10,"retry_delay_ms":1048,"error_status":401,'
    '"error":"authentication_failed","session_id":"7ac67cde-c864-4758-9f44-aa9311020eed"}'),
    ('{"type":"assistant","message":{"id":"ceba532b","model":"<synthetic>","role":"assistant","type":"message",'
    '"usage":{"input_tokens":0,"output_tokens":0},"content":[{"type":"text","text":"Failed to authenticate. '
    'API Error: 401 OAuth access token is invalid."}]},"session_id":"7ac67cde","error":"authentication_failed",'
    '"request_id":"req_011CggmePtJXJMoTATJqeJpg","is_api_error_message":true,"api_error_status":401,'
    '"api_error_origin":"first_party"}'),
    ('{"type":"result","subtype":"success","is_error":true,"num_turns":1,"terminal_reason":"api_error",'
    '"api_error_status":401,"result":"Failed to authenticate. API Error: 401 OAuth access token is invalid.",'
    '"total_cost_usd":0,"session_id":"7ac67cde"}'),
]

# codex-cli 0.166.0 with no login (trimmed to 2 of the 10 retries)
CODEX_NO_AUTH = [
    '{"type":"thread.started","thread_id":"01a1a381-3449-7aa1-b7a6-2a781ea26c77"}',
    '{"type":"turn.started"}',
    ('{"type":"error","message":"Reconnecting... 2/5 (unexpected status 401 Unauthorized: Missing bearer or basic '
    'authentication in header, url: wss://api.openai.com/v1/responses, cf-ray: a54ec65e8d5f817b-HEL)"}'),
    ('{"type":"item.completed","item":{"id":"item_0","type":"error","message":"Falling back from WebSockets to '
    'HTTPS transport. unexpected status 401 Unauthorized: Missing bearer or basic authentication in header"}}'),
    ('{"type":"turn.failed","error":{"message":"unexpected status 401 Unauthorized: Missing bearer or basic '
    'authentication in header, url: https://api.openai.com/v1/responses, cf-ray: a54ec6b3fea52b48-HEL"}}'),
]

# codex-cli 0.166.0 with an invalid API key (one retry kept)
CODEX_BAD_KEY = [
    '{"type":"thread.started","thread_id":"01a1a381-6e8e-7642-8d5c-378751fa6a59"}',
    ('{"type":"error","message":"Reconnecting... 1/5 (unexpected status 401 Unauthorized: Incorrect API key '
    'provided: sk-agent***********alid., auth error: 401, auth error code: invalid_api_key)"}'),
    ('{"type":"turn.failed","error":{"message":"unexpected status 401 Unauthorized: Incorrect API key provided: '
    'sk-agent***********alid. auth error code: invalid_api_key"}}'),
]


def feed(parser, lines):
    events = []
    for line in lines:
        events += parser.feed(line)
    return events


def test_claude_not_logged_in_is_auth_failed_despite_subtype_success():
    p = ClaudeStreamParser()
    events = feed(p, CLAUDE_NOT_LOGGED_IN)
    assert p.session_id == "903ad58e-feca-4b2f-a38c-86587bc4f703"
    assert p.cli_version == "2.1.317"
    assert p.turns == 1  # from the result's num_turns; the synthetic message is not a model turn
    outcome, reason = p.outcome(1)
    assert outcome == "auth_failed" and "Not logged in" in reason
    assert [e["kind"] for e in events] == ["system", "assistant", "error", "result"]


def test_claude_invalid_token_is_auth_failed():
    p = ClaudeStreamParser()
    feed(p, CLAUDE_BAD_TOKEN)
    assert p.outcome(1)[0] == "auth_failed"


def test_claude_success_accounting_dedupes_message_ids():
    p = ClaudeStreamParser()
    msg = {"type": "assistant", "session_id": "s1", "message": {"id": "m1", "model": "claude-sonnet-5-5",
           "usage": {"input_tokens": 1000, "output_tokens": 10}, "content": [{"type": "text", "text": "hi"}]}}
    p.feed(json.dumps(msg))
    msg["message"]["usage"] = {"input_tokens": 1000, "output_tokens": 50}
    msg["message"]["content"] = [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]
    p.feed(json.dumps(msg))  # same message id streamed again with more output
    p.feed(json.dumps({"type": "assistant", "message": {"id": "m2", "model": "x",
           "usage": {"input_tokens": 1100, "output_tokens": 20, "cache_read_input_tokens": 900}, "content": []}}))
    assert p.turns == 2
    assert p.input_tokens == 2100 and p.output_tokens == 70 and p.cache_read_tokens == 900
    p.feed(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
                       "result": "done", "total_cost_usd": 0.042, "usage": {"input_tokens": 2100,
                       "output_tokens": 70}}))
    assert p.outcome(0) == ("succeeded", "finished")
    assert p.cost_usd == 0.042 and p.result_text == "done"


def test_claude_rate_limit_from_api_retry_and_result():
    p = ClaudeStreamParser()
    p.feed('{"type":"system","subtype":"api_retry","attempt":1,"error_status":429,"error":"rate_limit"}')
    p.feed('{"type":"result","is_error":true,"api_error_status":429,"result":"API Error: 429 rate_limit_error"}')
    assert p.outcome(1)[0] == "rate_limited"


def test_claude_usage_limit_event_sets_reset_time():
    p = ClaudeStreamParser()
    resets = int(time.time()) + 7200
    p.feed(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "resetsAt": resets, "rateLimitType": "five_hour", "utilization": 1.0}}))
    p.feed(json.dumps({"type": "result", "is_error": True, "result": "You've hit your limit · resets 3pm"}))
    assert p.outcome(1)[0] == "rate_limited"
    assert p.resets_at == resets and p.window_utilization == 1.0


def test_claude_max_turns_and_budget_reported_by_cli():
    p = ClaudeStreamParser()
    p.feed('{"type":"result","subtype":"error_max_turns","is_error":true,"num_turns":5,"terminal_reason":"max_turns"}')
    assert p.outcome(1)[0] == "limit_turns"
    p = ClaudeStreamParser()
    p.feed('{"type":"result","subtype":"error_max_budget_usd","is_error":true}')
    assert p.outcome(1)[0] == "limit_cost"


def test_claude_server_error_and_plain_failure_and_crash():
    p = ClaudeStreamParser()
    p.feed('{"type":"result","is_error":true,"api_error_status":529,"result":"API Error: 529 Overloaded"}')
    assert p.outcome(1)[0] == "server_error"
    p = ClaudeStreamParser()
    p.feed('{"type":"result","subtype":"error_during_execution","is_error":true,"result":"tests keep failing"}')
    assert p.outcome(1)[0] == "failed"
    p = ClaudeStreamParser()
    p.feed('{"type":"system","subtype":"init","model":"m"}')
    assert p.outcome(-9)[0] == "crashed"


def test_non_json_and_huge_lines():
    p = ClaudeStreamParser()
    assert p.feed("not json at all")[0]["kind"] == "raw"
    big = json.dumps({"type": "assistant", "message": {"id": "m", "model": "x", "content": [
        {"type": "text", "text": "z" * 100_000}]}})
    ev = p.feed(big)[0]
    assert ev.get("truncated") and "raw" not in ev and len(ev["text"]) < 3000


def test_codex_no_auth_is_auth_failed():
    p = CodexJsonParser()
    feed(p, CODEX_NO_AUTH)
    assert p.session_id == "01a1a381-3449-7aa1-b7a6-2a781ea26c77"
    assert p.outcome(1)[0] == "auth_failed"


def test_codex_invalid_key_is_auth_failed():
    p = CodexJsonParser()
    feed(p, CODEX_BAD_KEY)
    assert p.outcome(1)[0] == "auth_failed"


def test_codex_success_accounting():
    p = CodexJsonParser()
    feed(p, ['{"type":"thread.started","thread_id":"t1"}', '{"type":"turn.started"}',
             ('{"type":"item.completed","item":{"id":"i0","type":"command_execution","command":"ls",'
             '"aggregated_output":"a\\n","exit_code":0,"status":"completed"}}'),
             '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"Done."}}',
             '{"type":"turn.completed","usage":{"input_tokens":2000,"cached_input_tokens":500,"output_tokens":120}}'])
    assert p.outcome(0) == ("succeeded", "finished")
    assert p.turns == 2 and p.input_tokens == 2000 and p.output_tokens == 120 and p.cache_read_tokens == 500
    assert p.result_text == "Done."


def test_codex_usage_limit_with_reset():
    p = CodexJsonParser()
    now = time.time()
    msg = "You've hit your usage limit. Upgrade to Pro, or try again in 2 hours 5 minutes."
    feed(p, [json.dumps({"type": "error", "message": msg}),
             json.dumps({"type": "turn.failed", "error": {"message": msg}})])
    assert p.outcome(1)[0] == "rate_limited"
    assert abs(p.resets_at - (now + 2 * 3600 + 5 * 60)) < 5


def test_codex_429_after_retries():
    p = CodexJsonParser()
    p.feed('{"type":"turn.failed","error":{"message":"exceeded retry limit, last status: 429 Too Many Requests"}}')
    assert p.outcome(1)[0] == "rate_limited"


def test_classify_and_reset_helpers():
    assert classify_text("OAuth token has expired") == "auth_failed"
    assert classify_text("Your refresh token was already used. Please log out and sign in again.") == "auth_failed"
    assert classify_text("Claude AI usage limit reached|1767225600") == "rate_limited"
    assert classify_text("all good") is None
    assert parse_reset("Claude AI usage limit reached|1767225600") == 1767225600.0
    now = 1_000_000.0
    assert parse_reset("try again in 3 days 1 hour", now) == now + 3 * 86400 + 3600
    t = parse_reset("You've hit your limit · resets 3pm (UTC)", now)
    assert t is not None and t > now
    assert parse_reset("nothing here") is None
