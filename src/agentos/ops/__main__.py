"""`python -m agentos.ops ...`: the ops commands without the root CLI (agentos upgrade uses it to
run the new code's migrations, whatever the root command mounts)."""

from agentos.ops.cli import app

app(prog_name="agentos")
