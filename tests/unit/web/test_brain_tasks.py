"""What agents handed to an AI model, as the app reads it (web/src/features/workers/handed-to-models.tsx).
A new node has no task yet, so these check the shape the list and the detail give, and that a task which
is not there is refused the same way for stop and carry on. Who may see them is in test_api_members.py."""

from __future__ import annotations

ORG = "/api/orgs/local"


def test_the_owner_reads_the_list_of_tasks_in_the_app_shape(client) -> None:
    listed = client.get(f"{ORG}/brains/tasks")
    assert listed.status_code == 200, listed.text
    for task in listed.json():
        assert set(task) >= {"id", "status", "summary", "started_at", "finished_at", "resumable", "attempt"}
        assert task["status"] in {"queued", "starting", "running", "waiting_approval", "succeeded_unverified",
                                  "failed", "cancelled", "interrupted"}


def test_a_task_that_is_not_there_is_not_found_for_read_stop_and_carry_on(client) -> None:
    assert client.get(f"{ORG}/brains/tasks/bt_missing").status_code == 404
    assert client.post(f"{ORG}/brains/tasks/bt_missing/cancel").status_code == 404
    assert client.post(f"{ORG}/brains/tasks/bt_missing/resume", json={}).status_code == 404


def test_a_stranger_is_refused_before_any_task_is_looked_up(client) -> None:
    saved = dict(client.cookies)
    client.cookies.clear()
    try:
        assert client.get(f"{ORG}/brains/tasks").status_code == 401
        assert client.post(f"{ORG}/brains/tasks/bt_missing/cancel").status_code == 401
    finally:
        client.cookies.update(saved)
