# Benchmarks, soak and chaos

Numbers and breakage tests on a real node, run in a dev slot next to the owner's lanes. Everything
here uses `agentos.dev.slotnode`: the production agentosd (`agentos.node.daemon.serve`, real
Camoufox, Xvfb, x11vnc, policy and MCP) in a throwaway home under `.node-home/`, on the slot's
displays and ports. Two seams keep it on the node:

- Every lane goes through a stand-in proxy on loopback (basic auth, like a paid proxy). It is
  also the only web the lanes see: it serves `tests/fixtures/site/` as `http://site.bench.example`,
  answers the exit check at `http://ip.bench.example/ip` with an address the test picks, refuses
  CONNECT and never forwards. Stopping it is how a proxy goes away.
- Camoufox's geoip lookup is off and the node's own IP is a TEST-NET address, not a lookup.

Agents call with their own grant (`agentos grants issue`, kind agent, ceiling draft), not the node
token: the node token is the owner, and a human passes the hold gate.

A fresh venv on the node has no Camoufox fingerprint model, and the slot daemon cannot download it.
Run `uv run python -m fpgen fetch` once in that checkout (the harness says so if it is missing).

Detection (can a bot defence tell a lane is automated?) has its own bench, `agentos lane redteam`,
with its history in `bench/redteam.jsonl`: [design/redteam.md](design/redteam.md).

## Lane benchmarks

```
make bench                        # slot 53, 5 runs; appends bench/results.jsonl, then compares
BENCH_RUNS=10 AGENTOS_TEST_SLOT=54 make bench
AGENTOS_TEST_SLOT=53 scripts/node-test.sh bench --python scripts/bench_node.py --runs 2
uv run python scripts/bench_compare.py [--threshold 0.2] [--against <sha>]
```

`scripts/bench_node.py` measures one lane over MCP, as an agent sees it. Each metric is the median
of every sample over the runs (one cold wake per run; 3 screenshots, 3 acts and 5 whoami calls per
run):

| Metric | What |
|---|---|
| `first_launch_s` | the lane's very first `browser_open` (pins the fingerprint, makes the display); one sample |
| `wake_cold_s` | `browser_open` of a cold lane until it is warm: browser, display, x11vnc, exit check |
| `navigate_ms` | the first `browser_navigate` after a wake (the fixture feed) |
| `first_snapshot_ms` | the first `browser_snapshot` of that page |
| `screenshot_ms` | `browser_screenshot` of a warm lane |
| `act_ms` | `browser_act` round trip: a click with the human pause and the read-back |
| `mcp_overhead_ms` | `whoami`: transport, grant check, policy and trace, no page |
| `warm_lane_rss_mb`, `warm_lane_pss_mb` | the warm browser's whole process group (RSS counts shared pages in every process, PSS splits them) |
| `close_ms` | `browser_close` back to cold |

A result is one JSON line with the commit (and whether the checkout was dirty), the date, the
Camoufox package and browser versions, the host, the medians and each metric's min and max.
`bench_compare.py` takes the newest line and the one before it from the same host, and fails when a
metric is more than 20% slower and also above its noise floor (0.15 s, 15 ms or 25 MB).

### Baseline (2026-11-04, commit 6025f9d)

Camoufox 0.5.9, browser 158.0.1-beta.41, the node VM (2 vCPU, 3.8 GB), 5 runs:

| Metric | Median | Min | Max |
|---|---|---|---|
| first launch | 22.3 s | | |
| cold wake | 5.2 s | 3.6 s | 11.5 s |
| first navigate | 869 ms | 570 ms | 3002 ms |
| first snapshot | 480 ms | 244 ms | 664 ms |
| screenshot | 351 ms | 127 ms | 1232 ms |
| browser_act (click) | 2306 ms | 1661 ms | 3557 ms |
| MCP call (whoami) | 26 ms | 14 ms | 64 ms |
| warm lane RSS | 981 MB | 965 MB | 1160 MB |
| warm lane PSS | 480 MB | 400 MB | 728 MB |
| browser_close | 491 ms | 457 ms | 1088 ms |

The spread is wide because the node is shared: the owner's lanes and other agents' tests ran at
the same time (one owner lane's content process held 2.3 GB during the first attempts, and the bench
waited for memory). Compare medians over several runs before calling a change a regression. Most
of `act_ms` is the human pause and the read-back, by design; `first_launch_s` is mostly Camoufox
resolving and pinning the identity once.

## Soak and chaos

```
AGENTOS_TEST_SLOT=53 scripts/node-test.sh soak -m soak tests/soak -q
AGENTOS_TEST_SLOT=53 scripts/node-test.sh soak -m soak tests/soak/test_chaos.py -q
AGENTOS_SOAK_SECONDS=600 AGENTOS_TEST_SLOT=53 scripts/node-test.sh soak -m soak tests/soak/test_soak.py -q -s
```

The tests are marked `soak`, so neither the normal run nor `pytest -m node` collects them. Each
gets its own daemon; they skip when the node has less than 1.8 GB available. After every test the
slot must be empty (no browser whose profile is under the home, no Xvfb or x11vnc on the slot's
displays, no display files, no listener on the slot's lane ports), and no secret (the node token,
the agent's grant, the proxy password or its basic auth) may appear in agentosd's log files or its
output.

**Soak** (`test_soak.py`): two lanes open, navigate, snapshot, click, screenshot, fill a field and
close, over and over for an hour (`AGENTOS_SOAK_SECONDS`). After every cycle nothing of theirs may
be left; over the run agentosd's own RSS may grow at most 40 MB and its file descriptors at most 8
(the last quarter of the run against the first, after three warm-up cycles). The samples go to
`soak-report.json` next to the homes (or `AGENTOS_SOAK_REPORT`).

**Chaos** (`test_chaos.py`), each while a `browser_act` fill or a slow navigation is in flight:

| Case | What must hold |
|---|---|
| the browser is killed | the fill is reported failed (to the agent and in the action log); the browser's processes go; the next navigate brings the lane back with one browser and one display |
| agentosd is killed (SIGKILL) and started again | the restarted daemon clears the orphaned browser and display; the lane comes back cold; a captcha hold is still a hold (agents get `agent_paused`); a quarantine (the gateway's exit moved) is still a quarantine and starts no browser; the cut call has a failed end in the action log; the lane works again |
| the proxy goes away | the navigation is reported failed; nothing goes around the proxy; the lane closes; a cold open is quarantined (the exit check fails) and keeps no browser; once the proxy is back the lane opens and browses |
| the disk fills past the floor | the warm lane's fill finishes; opening another lane is refused with `resources_low` naming the disk and starts nothing; it opens once space is back |

### What the first runs found (2026-11-04)

- **A fill cut by a dying browser came back `ok: true`** ("the tab closed during the fill (a popup or
  sign-in window finishing)"). `browser_act` took any closed tab for a popup that closed itself. Now,
  when no tab is left in the browser, it raises `engine_error` saying the action may be half done
  (`node/interaction/`, test in `test_interaction_wait.py`).
- **A call cut off by a killed agentosd stayed "started" in the action log forever.** On start, the
  policy service now gives every `tool_start` without an end a `tool_end` with status error,
  "interrupted: agentosd stopped before this call finished" (`TraceStore.end_unfinished`, test in
  `test_policy_traces.py`). The chaos test fails without it.
- Holds, quarantines, the disk floor, the proxy outage and the cleanup of orphans after a crash
  behaved as designed.

### First soak run

The first hour failed: 98 cycles, and agentosd's memory kept climbing from 253 to 359 MB, a growth
of 67.5 MB between the first and last quarter, over the 40 MB limit. No process, display, port or
file descriptor leaked. A traced run put it at about 0.8 MB per lane launch: the registries keyed
weakly by context and page held values pointing back at their keys, so no entry ever went, and each
one kept a closed lane's whole Playwright connection. A closed session now drops its entries
(`forget_context`, test in `test_engine_forget.py`).

The second hour, on the fix, passed: 131 cycles and 2,358 tool calls in 3,612 s, nothing leaked, file
descriptors 42 to 42, no waits for memory.

| Cycles | agentosd RSS | Growth per cycle |
|---|---|---|
| first quarter (warm-up) | 225 to 307 MB | 2.07 MB |
| second quarter | 307 to 308 MB | 0.04 MB |
| third quarter | 308 to 310 MB | 0.03 MB |
| last quarter | 310 MB | 0.01 MB |

The chaos suite passed on the same code, 4 of 4 in about 3 minutes.
