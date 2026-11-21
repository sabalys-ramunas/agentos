"""terminal_exec's notes: a git push that timed out may have landed (T1 nearly pushed again), and one call
never runs past what an MCP client waits for."""

from __future__ import annotations

from openajar.env.tools import exec_note


def test_a_timed_out_push_says_it_may_have_landed_and_how_to_check():
    note = exec_note("git add -A && git commit -qm x && git push -u origin ux-test/t1 2>&1 | tail -15", True, 60, 60)
    assert "may have landed" in note and "git ls-remote" in note and "environment()" in note
    assert exec_note("git push origin x", False, 60, 60) is None
    note = exec_note("make test", True, 60, 60)  # any timeout says what happened and what to do instead
    assert "still running after 60 s" in note and "terminal_job_start" in note and "may have landed" not in note


def test_a_long_timeout_is_cut_and_says_what_to_use_instead():
    note = exec_note("pnpm build", True, 600, 200)
    assert "you asked for 600 s; one call runs at most 200 s" in note and "terminal_job_start" in note
    assert "—" not in note


def test_a_timed_out_command_has_no_exit_code_of_its_own_and_no_stray_wrapper_line():
    from openajar.env.models import ExecResult
    from openajar.env.tools import timed_out_result

    res = ExecResult(exit_code=255, stdout="", duration_s=60.1, timed_out=True,
                     stderr="warning: slow\nsetsid: child 803 did not exit normally: No such file or directory\n")
    assert timed_out_result(res) == {"exit_code": None, "signal": "SIGTERM", "stderr": "warning: slow\n"}
