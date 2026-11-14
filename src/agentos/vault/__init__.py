"""The vault: sealed logins, TOTP seeds and API keys, bound to a domain and a lane.

Runtime service name "vault" (see service.build). The model never sees a secret: they
resolve on the node into a login form field or an auth header on the bound domain,
through the pure policy in agentos.vault.policy (the No-Body-Injection rule).

Modules: service (VaultService, build), cli
(`agentos vault ...`), api (panel routes), policy, domains, store, crypto, totp,
catcher, importers, typist (the protocol the node implements), control (unseal socket). Its MCP tools
(login, vault_fill, vault_list) type into a lane, so they are agentos.node.vault_tools.
"""
