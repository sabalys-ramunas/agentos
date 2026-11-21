"""Structured logs under real systemd and journald: a transient unit logs through
openajar.core.logs (it finds the journal from JOURNAL_STREAM), and `openajar logs --source
journal` reads the lines back, filtered on their ids, with nothing secret in the journal.

Run on the node:  OPENAJAR_TEST_SLOT=44 scripts/node-test.sh logs tests/node/test_logs_node.py
The unit is a transient `openajar-logtest-<slot>-<n>.service` (removed when it exits); it never
touches the agentosd service, /var/lib/agentos or the production journal's other units.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(not sys.platform.startswith("linux") or shutil.which("systemd-run") is None
                       or os.geteuid() != 0, reason="needs systemd-run as root on a Linux node"),
]

ROOT = Path(__file__).resolve().parents[2]
SECRET = "proxy-pass-" + secrets.token_hex(4)

SCRIPT = """
import logging, os
from openajar.core import logs
logs.setup(None, journal=None)  # under systemd-run, stderr is the journal stream
assert logs.under_systemd(), "JOURNAL_STREAM should name stderr"
secret = os.environ["LOGTEST_SECRET"]
logs.add_scrubber(lambda text: text.replace(secret, "[redacted]"))
log = logging.getLogger("openajar.logtest")
with logs.log_context(lane="biz-a/instagram", grant="g_logtest", request="rq_logtest"):
    log.info("opened the lane with %s", secret)
    with logs.log_context(run="run_logtest"):
        log.warning("slow step")
with logs.log_context(lane="personal/reddit"):
    log.error("other lane")
"""


def journal(*args: str) -> list[dict]:
    out = subprocess.run(["journalctl", *args, "-o", "json", "--no-pager", "-q"], capture_output=True, text=True,
                         check=True)
    return [json.loads(raw) for raw in out.stdout.splitlines() if raw.strip()]


def openajar_logs(*args: str) -> subprocess.CompletedProcess:
    # a home of its own, so a fallback to the log files could never read the production node's
    env = {**os.environ, "OPENAJAR_HOME": str(ROOT / ".logtest-home")}
    return subprocess.run([sys.executable, "-m", "openajar.cli", "logs", *args], check=False, capture_output=True,
                          text=True, cwd=ROOT, timeout=60, env=env)


@pytest.fixture(scope="module")
def unit(tmp_path_factory) -> str:
    slot = os.environ.get("OPENAJAR_TEST_SLOT") or os.environ.get("OPENAJAR_TEST_DISPLAY_BASE", "440")[:2]
    name = f"openajar-logtest-{slot}-{secrets.token_hex(3)}"
    script = tmp_path_factory.mktemp("logs") / "logtest.py"
    script.write_text(SCRIPT)
    # The secret goes in the environment: systemd logs a transient unit's command line (its
    # default description), and that line is not ours to scrub.
    subprocess.run(["systemd-run", f"--unit={name}", "--wait", "--collect", "--quiet",
                    "--description=openajar log test", f"--setenv=LOGTEST_SECRET={SECRET}",
                    f"--working-directory={ROOT}", sys.executable, str(script)], check=True, timeout=60)
    for _ in range(50):  # journald writes asynchronously
        if len(journal("-u", name)) >= 3:
            break
        time.sleep(0.1)
    return name


def test_lines_reach_the_journal_as_json_with_ids_and_priority(unit):
    entries = journal("-u", unit)
    ours = [json.loads(e["MESSAGE"]) for e in entries if str(e.get("MESSAGE", "")).startswith("{")]
    assert [line["msg"] for line in ours] == ["opened the lane with [redacted]", "slow step", "other lane"]
    assert ours[1]["lane"] == "biz-a/instagram" and ours[1]["run"] == "run_logtest" and ours[1]["grant"] == "g_logtest"
    priorities = {json.loads(e["MESSAGE"])["msg"]: e["PRIORITY"] for e in entries
                  if str(e.get("MESSAGE", "")).startswith("{")}
    assert priorities == {"opened the lane with [redacted]": "6", "slow step": "4", "other lane": "3"}
    assert [json.loads(e["MESSAGE"])["msg"] for e in journal("-u", unit, "-p", "warning")] == ["slow step",
                                                                                               "other lane"]
    assert all(SECRET not in json.dumps(e) for e in entries)


def test_openajar_logs_reads_and_filters_the_journal(unit):
    by_lane = openajar_logs("--unit", unit, "--source", "journal", "--lane", "biz-a/*", "--json")
    assert by_lane.returncode == 0, by_lane.stderr
    assert [json.loads(raw)["msg"] for raw in by_lane.stdout.splitlines()] == ["opened the lane with [redacted]",
                                                                               "slow step"]
    by_run = openajar_logs("--unit", unit, "--source", "journal", "--run", "run_logtest")
    assert by_run.returncode == 0 and len(by_run.stdout.splitlines()) == 1
    assert "WARN  openajar.logtest [lane=biz-a/instagram grant=g_logtest run=run_logtest request=rq_logtest]" in \
        by_run.stdout
    errors = openajar_logs("--unit", unit, "--source", "journal", "--level", "error", "--since", "10m", "--json")
    assert [json.loads(raw)["lane"] for raw in errors.stdout.splitlines()] == ["personal/reddit"]
    auto = openajar_logs("--unit", unit, "--grant", "g_logtest", "--json")  # auto finds the unit's journal
    assert auto.returncode == 0 and len(auto.stdout.splitlines()) == 2
    assert SECRET not in by_lane.stdout + by_run.stdout + errors.stdout + auto.stdout
