"""Workflows: a trigger, then steps, then what happens with the result (docs/WORKFLOWS.md).

Not a second engine. Triggers are events routes and schedules, a run is a runner `Run` of kind
`workflow`, and an action step is an ordinary flow or connector run that passes the same gate as
any other. This package adds the definition and its checks (`models`, `check`), the walk that
carries a run from step to step (`engine`), sending to people (`sends`, `mail`), and how people and
agents reach all of it (`service`, `api`, `tools`, `cli`).
"""
