"""`python -m openajar.ops ...`: the ops commands without the root CLI (openajar upgrade uses it to
run the new code's migrations, whatever the root command mounts)."""

from openajar.ops.cli import app

app(prog_name="openajar")
