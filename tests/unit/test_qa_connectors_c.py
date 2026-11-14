"""QA bug hunt on connectors Brief C (surfaces, one tool per operation, approvals, pace).

Each xfail pins one defect found in the recent merges.
"""

from __future__ import annotations

import pytest
from connector_samples import LANE, bind, commit_op
from test_connector_surfaces import _harness

from agentos.core.models import RunStatus
from agentos.runner.connectors.surfaces import PaceLimited
from agentos.runner.fakes import OWNER


def test_qa_connectors_1_a_cancelled_run_that_already_posted_counts_toward_pace(tmp_path):
    h = _harness(tmp_path, {"post_photo": commit_op()})
    bind(h)
    fv = h.flows.get("connector.social.post_photo")
    run, created = h.runner.submit(
        flow_id=fv.id, params={"image": "in_0123456789ab", "caption": "Autumn"}, surface=LANE, principal=OWNER
    )
    assert created
    # the state the executor leaves when a cancel lands after the commit step (executor._run_all)
    stored = h.runner.get(run.id)
    stored.status = RunStatus.CANCELLED
    stored.error = {"code": "cancelled", "message": "cancelled", "writes_done": 1}
    assert h.store.save_run(stored, expect={RunStatus.QUEUED})
    # commit_op allows one post per 90 minutes on the surface, so a second post is refused
    with pytest.raises(PaceLimited):
        h.runner.check_connector(fv, LANE)
