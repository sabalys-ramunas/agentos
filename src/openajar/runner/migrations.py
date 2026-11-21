"""The runner package's migration sets, for openajar.core.migrate.discover(). runner.db migrates itself in
openajar.runner.store, but for site API drafts; site API keys, cache and usage have their own database."""

from __future__ import annotations

from openajar.runner.siteapis.http_migrations import MIGRATIONS as HTTP
from openajar.runner.siteapis.migrations import MIGRATIONS as DRAFTS

MIGRATIONS = [HTTP, DRAFTS]
