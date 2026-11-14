"""`python -m agentos.client connect mynode`: the client commands before the main CLI mounts them."""

from agentos.client.cli import app

app(prog_name="agentos")
