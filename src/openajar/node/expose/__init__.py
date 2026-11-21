"""The owner's dev server in a browser lane (`openajar expose`), and a lane's port on the owner's
computer (`openajar forward`). docs/DEVELOPING.md ("Your dev server in a lane") is the how-to and
docs/THREAT-MODEL.md ("Exposes and forwards") says what this opens and what it does not.

expose, end to end:

1. `openajar expose 5173 --lane ws/name --name myapp` on the laptop asks the node, with the owner's
   device credential (`POST /api/exposes`; a grant or the node token is refused), to reserve
   `https://myapp.expose.openajar.invalid` on that lane (names: why `.invalid`, why https). The node
   answers with a socket path in `<home>/run/expose/` (mode 700, agentosd's).
2. The laptop runs `ssh -R <that path>:localhost:5173 <node>`: sshd binds the socket as the SSH
   user (mode 600). A remote one-liner hands it to agentosd's user when the SSH user is root, then
   the laptop says `ready`, and the node checks the socket (a socket, agentosd's, mode 600) before
   it routes anything.
3. The lane's browser gets one more route on its RouteDispatcher (route.LaneExposeRoute): requests
   for that origin are answered from the socket (relay) inside the browser's driver, and the lane's
   egress guard counts the host as inside its site list and every grant's egress list while it lasts
   (LaneGuard.exposed). No other request changes path: the lane's proxy, its PAC guard and its
   fingerprint stay as they were. Any route turns the HTTP cache off for the whole context while it
   exists (docs/design/egress-guard.md 3.2), so the lane caches nothing while an expose is on.
4. The laptop renews the lease every 10 s; Ctrl-C, `--for`, the laptop sleeping or the tunnel
   dropping end it (service.ExposeService). Then the route goes, and the name no longer resolves.

forward: the node opens a socket of its own (forward.Forward) that connects into the lane's network
namespace, and the laptop's `ssh -L 127.0.0.1:<local>:<socket>` reaches it.
"""

from openajar.node.expose.names import SUFFIX, host_for, origin_for
from openajar.node.expose.service import SERVICE_NAME, ExposeService, Link

__all__ = ["SERVICE_NAME", "SUFFIX", "ExposeService", "Link", "host_for", "origin_for"]
