"""Names vulture (`make deadcode`) reports that are used, though no code names them.

Each line is a use vulture can see. Add one only for a framework hook, a name a caller reaches by
string, a re-export, or a parameter a protocol or dunder method must take; never to keep dead code.
"""

# ruff: noqa: B018, F821

# parameters a duck-typed or dunder signature must accept
js  # agentos.dev.lanes._FormlessLocator.evaluate(js, arg): Playwright's Locator.evaluate
et  # agentos.git.agent.*.__aexit__(et, e, tb)
memo  # agentos.vault.secret.Secret.__deepcopy__(memo)

# re-exported for callers of the older API (tests/unit/test_interaction.py imports it from here)
classify_signal  # agentos.node.interaction
