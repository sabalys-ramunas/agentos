"""The red-team bench (`agentos lane redteam`): a detector site built like one behind a bot defence,
lanes that visit it the way agents drive them, and detectors that try to catch them
(docs/design/redteam.md).

    server     the site and what its server records (headers in order, the sensor's batches)
    scenario   the agent's visit, through the production PlaywrightInteraction
    detectors  the verdicts, pure functions over a recorded visit
    runner     backends (this machine, a node test slot), the report and its history
"""
