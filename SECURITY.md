# Security policy

agentos rents cloud machines for agents, holds git credentials, logged-in browser profiles and
a password vault, and has the permission to act on real accounts, so we take reports seriously
and answer them quickly.

## Reporting a vulnerability

Please **do not** open a public issue, discussion or pull request for a security problem.

Report it privately through GitHub:
**[Report a vulnerability](https://github.com/sabalys-ramunas/agentos/security/advisories/new)**
(the **Security** tab of this repository, then **Report a vulnerability**). Only the maintainers
see it. If that form does not work for you, open an issue that says only "please open a private
channel for a security report" and @sabalys-ramunas will reach out; put no details in it.

Include what you found, how to reproduce it, the version or commit, and what an attacker
gains. We will acknowledge within 3 working days, agree on a fix and a disclosure date with
you, and credit you in the release notes unless you prefer otherwise.

## In scope

- The node daemon (`agentosd`), the controller, the MCP server, the web app and its API.
- Environments: any way a key, a token or an account reaches the VM, an environment's gateway
  token reaches another repository, one environment reaches another or the node, or an agent
  reaches an environment it did not open.
- Grants, approvals, "Ask me first" and the audit log: anything that lets an agent act beyond
  its grant, approve its own request, or hide what it did.
- The vault: any way a secret reaches the model, a log, a trace or another domain.
- Lane isolation: one lane reading another lane's profile, cookies or files.
- The installer and the systemd units.

## Out of scope

- A site detecting or blocking automation. That is a compatibility question, not a
  vulnerability in agentos.
- Attacks that need root on the node, or the node's token (it is the owner's key).
- Problems in third-party dependencies with no agentos-specific impact (report those upstream).

## Supported versions

agentos is alpha. Only the latest commit on `main` gets security fixes.
