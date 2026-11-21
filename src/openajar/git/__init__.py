"""Git credentials from the vault: clones and pushes from lanes and brains, with no key in the lane.

The owner stores a credential on the node (an SSH deploy key made there, or a fine-grained token)
bound to a host and its repositories, and declares which remotes a workspace or a lane may use.
Inside a lane or a brain, git is pointed at the node's git gateway (openajar.git.gateway): plain
HTTP on a loopback port the node serves into the lane's network namespace. The gateway keeps a
mirror of each declared repository, authorizes every fetch (read) and push (commit, or
irreversible for a force-push, a deletion or a protected branch) under the caller's grant, and
talks to the real remote itself, signing with the deploy key in its own memory (openajar.git.agent)
or sending the token in the HTTP exchange. See docs/GIT.md.

Modules:
  wire      remote URLs, repository names, pkt-lines and the receive-pack report
  remotes   the declared remotes (OPENAJAR_HOME/git/remotes.json)
  keys      key generation, credentials as vault machine secrets, pinned host keys
  agent     an SSH agent for one key, served only to the node's own ssh
  upstream  git against the real remote: mirror fetch and push
  gateway   the smart-HTTP gateway: who asks, what it may do, the mirror, the push
  service   the runtime service "git": listeners, caller tokens, the env lanes and brains get
  cli       `openajar git key ...` and `openajar git remote ...`
"""
