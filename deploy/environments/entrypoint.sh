#!/bin/sh
# The docker provider's "VM": install the host key and the login key the node made for this
# machine (copied in before the container starts, under /etc/agentos-env), then run sshd.
#
# With AGENTOS_ENV_WALL=1 (agentos.vms.docker sets it, and gives the container NET_ADMIN for it) it
# first walls the machine off: new connections to private, shared and link-local addresses are
# refused, so it reaches neither this computer (host.docker.internal, the docker gateway, the
# node's loopback listeners) nor your network, while the internet still works. DNS (port 53) is the
# one exception, because Docker's resolver may forward from inside the container. Then sshd starts
# without NET_ADMIN, NET_RAW and SETPCAP in its bounding set, so nothing on the machine, root
# included, can take the wall down.
#
# With AGENTOS_ENV_NETWORK=closed as well (an environment's network "limited" or "none",
# agentos.vms.netwall) the wall is tighter: every new outbound connection is refused but loopback,
# DNS included. The machine then reaches only what the node forwards into it over its SSH connection
# (the git gateway, the node's allowlist proxy); it resolves no name itself (the proxy does). The docker provider copies this file in and runs it
# for such a machine, so an image with an older entrypoint still gets this wall.
set -eu
SEED=/etc/agentos-env
if [ ! -f "$SEED/ssh_host_ed25519_key" ] || [ ! -f "$SEED/authorized_keys" ]; then
  echo "agentos-env: no keys in $SEED; this container is made by the agentos docker provider" >&2
  exit 1
fi
rm -f /etc/ssh/ssh_host_*
install -m 600 "$SEED/ssh_host_ed25519_key" /etc/ssh/ssh_host_ed25519_key
install -m 644 "$SEED/ssh_host_ed25519_key.pub" /etc/ssh/ssh_host_ed25519_key.pub
install -d -m 700 /root/.ssh
install -m 600 "$SEED/authorized_keys" /root/.ssh/authorized_keys
mkdir -p /run/sshd

# Every rule is checked: `set -e` does not hold inside a function called from `if`.
wall() {
  iptables -F OUTPUT || return 1
  iptables -A OUTPUT -o lo -j ACCEPT || return 1
  iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT || return 1
  iptables -A OUTPUT -p udp --dport 53 -j ACCEPT || return 1
  iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT || return 1
  for net in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 \
             192.168.0.0/16 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4; do
    iptables -A OUTPUT -d "$net" -j REJECT || return 1
  done
  # IPv6: loopback only (a docker network made without --ipv6 gives the machine no route out)
  if ip6tables -F OUTPUT 2>/dev/null; then
    ip6tables -A OUTPUT -o lo -j ACCEPT || return 1
    ip6tables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT || return 1
    ip6tables -A OUTPUT -j REJECT || return 1
  elif [ -n "$(ip -6 addr show scope global 2>/dev/null)" ]; then
    echo "agentos-env: the machine has IPv6 but ip6tables does not work" >&2
    return 1
  fi
}

# Nothing out but loopback (and the replies of the node's SSH connection in). Docker's embedded
# resolver (127.0.0.11, on loopback) is refused first: it answers from outside the machine and
# forwards any name to this computer's resolvers, a way out by DNS. The docker provider also
# starts such a machine with --dns 0.0.0.0, so the resolver has nowhere to forward even without
# this rule.
closed() {
  iptables -F OUTPUT || return 1
  iptables -A OUTPUT -d 127.0.0.11 -j REJECT || return 1
  iptables -A OUTPUT -o lo -j ACCEPT || return 1
  iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT || return 1
  iptables -A OUTPUT -j REJECT || return 1
  if ip6tables -F OUTPUT 2>/dev/null; then
    ip6tables -A OUTPUT -o lo -j ACCEPT || return 1
    ip6tables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT || return 1
    ip6tables -A OUTPUT -j REJECT || return 1
  elif [ -n "$(ip -6 addr show scope global 2>/dev/null)" ]; then
    echo "agentos-env: the machine has IPv6 but ip6tables does not work" >&2
    return 1
  fi
}

set -- /usr/sbin/sshd -D -e \
  -o HostKey=/etc/ssh/ssh_host_ed25519_key \
  -o PasswordAuthentication=no \
  -o KbdInteractiveAuthentication=no \
  -o PermitRootLogin=prohibit-password \
  -o AllowTcpForwarding=yes
if [ "${AGENTOS_ENV_NETWORK:-full}" = "closed" ]; then
  if ! closed; then
    echo "agentos-env: could not close the machine's network (iptables needs NET_ADMIN); not starting" >&2
    exit 1
  fi
  exec setpriv --inh-caps=-all --bounding-set=-net_admin,-net_raw,-setpcap -- "$@"
fi
if [ "${AGENTOS_ENV_WALL:-}" = "1" ]; then
  if ! wall; then
    echo "agentos-env: could not wall the machine off (iptables needs NET_ADMIN); not starting" >&2
    exit 1
  fi
  exec setpriv --inh-caps=-all --bounding-set=-net_admin,-net_raw,-setpcap -- "$@"
fi
exec "$@"
