"""Fakes and harnesses the tests share, kept out of the shipped package.

tests/ is on sys.path while its conftest loads, so a test anywhere under it imports these as
`testkit.<name>`: brains (BrainService harnesses over the fake CLIs), memory, pilot (scripted
deciders) and workspaces. The fakes `agentos dev up` and the demos need at runtime live in
agentos.dev instead.
"""
