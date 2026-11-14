"""Planning: one read-only view of lanes and engagement programs, and a day-plan draft from it.

    state.py   reads the node's stores without changing them and joins them per lane
    draft.py   turns that state into a day plan (deterministic rules, one seam for a brain)
    cli.py     `agentos plan status` and `agentos plan draft`

Nothing here acts. It imports no lane manager, browser, runner or publish path, and a test holds it
to that: whatever a future brain writes for the plan is a draft, and goes to the owner through the
normal path (queued for the commit gate), never out of this package.
"""
