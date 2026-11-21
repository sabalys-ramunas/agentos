"""Site APIs: a site you can use in a browser as a typed HTTP API (docs/SITE-APIS.md).

A site API is a connector the owner made for their own use, and an endpoint is one of its
operations: it compiles through openajar.runner.connectors to an ordinary `connector.<api>.<endpoint>`
flow, so runs, approvals, health, pace, schedules and workflow steps work unchanged.

    models     the definition, its states, and what it compiles to
    discover   candidates from the accessibility outline, in pure Python
    store      drafts (migrations: site_api_drafts)
    service    SiteApiService: drafts, compile, activate, pause, remove
    tools      the agent tools (site_api_discover, site_api_try, site_api_propose, site_apis_list)
    cli, api   the owner's CLI and panel routes
    keys, cache, usage, http, openapi    the /v1/sites surface
"""
