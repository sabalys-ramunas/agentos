"""`python -m openajar.client connect mynode`: the client commands before the main CLI mounts them."""

from openajar.client.cli import app

app(prog_name="openajar")
