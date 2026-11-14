"""The runner package's migration sets, for agentos.core.migrate.discover(). runner.db migrates itself in
agentos.runner.store, but for site API drafts; site API keys, cache and usage have their own database."""

from __future__ import annotations

from agentos.runner.siteapis.http_migrations import MIGRATIONS as HTTP
from agentos.runner.siteapis.migrations import MIGRATIONS as DRAFTS

MIGRATIONS = [HTTP, DRAFTS]
