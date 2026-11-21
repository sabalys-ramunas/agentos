#!/usr/bin/env bash
# First boot of an openajar node template: the Hetzner server (deploy/templates/hetzner) and the
# home box (deploy/templates/home-box). The first-boot script that cloud-init installs
# (/usr/local/sbin/agentos-first-boot) clones AGENTOS_GIT_REF and runs this file from that clone,
# as root. In order:
#
#   1. check the variables in /etc/agentos/bootstrap.env (nothing is changed before this passes)
#   2. create the admin user with its key, then turn off root and password logins over SSH
#   3. turn on unattended security upgrades
#   4. install openajar: deploy/install-node.sh --systemd --user agentos --hardened
#   5. join the controller if CONTROLLER_URL and JOIN_TOKEN are set; blank the token either way
#   6. run `openajar doctor` as the agentos user; the report goes to ~/openajar-doctor.txt
#
# The exit code is the doctor's: 0 when no check failed (warnings are still in the report).
# Running it again is safe: every step looks at what is already there.
set -euo pipefail
# cloud-init may start with a short PATH, and visudo, sshd, useradd and runuser live in the sbin
# directories. Append them, so a tool found earlier on PATH still wins.
export PATH="$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

ENV_FILE=/etc/agentos/bootstrap.env
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PREFIX=/opt/agentos
OPENAJAR_HOME=/var/lib/agentos
OPENAJAR="$PREFIX/.venv/bin/openajar"
key_check="$(mktemp)"
doctor_out="$(mktemp)"
trap 'rm -f "$key_check" "$doctor_out"' EXIT

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
# The text after the first "=" of KEY in the env file, with trailing blanks removed.
envval() { awk -F= -v k="$1" '$1 == k { sub(/^[^=]*=/, ""); print; exit }' "$ENV_FILE" | sed 's/[[:space:]]*$//'; }

[ "$(id -u)" = 0 ] || die "run as root: sudo /usr/local/sbin/agentos-first-boot"
[ -f "$ENV_FILE" ] || die "$ENV_FILE is missing"
[ -f "$SRC/deploy/install-node.sh" ] || die "$SRC is not an openajar checkout"

# ---- 1. the variables ----
ADMIN_USER="$(envval ADMIN_USER)"
ADMIN_USER="${ADMIN_USER:-agentos-admin}"
ADMIN_SSH_KEY="$(envval ADMIN_SSH_KEY)"
NODE_NAME="$(envval NODE_NAME)"
NODE_NAME="${NODE_NAME:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
REF="$(envval AGENTOS_GIT_REF)"
CONTROLLER_URL="$(envval CONTROLLER_URL)"
JOIN_TOKEN="$(envval JOIN_TOKEN)"

[[ "$ADMIN_USER" =~ ^[a-z_][a-z0-9_-]{0,30}$ && "$ADMIN_USER" != root ]] \
  || die "ADMIN_USER must be a plain user name, not root. Nothing was changed."
[[ "$NODE_NAME" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] \
  || die "NODE_NAME must be lowercase letters, digits and dashes. Nothing was changed."
[ -n "$ADMIN_SSH_KEY" ] \
  || die "ADMIN_SSH_KEY is empty. Put your public key in $ENV_FILE and run agentos-first-boot again. Nothing was changed."
printf '%s\n' "$ADMIN_SSH_KEY" > "$key_check"
ssh-keygen -l -f "$key_check" > /dev/null 2>&1 \
  || die "ADMIN_SSH_KEY is not a public key (one line, for example ssh-ed25519 AAAA...). Nothing was changed."

# ---- 2. the admin user: a key is the only way in; sudo asks for no password ----
say "Admin user $ADMIN_USER"
id -u "$ADMIN_USER" > /dev/null 2>&1 || useradd --create-home --shell /bin/bash --groups sudo "$ADMIN_USER"
passwd -l "$ADMIN_USER" > /dev/null 2>&1 || true
sudoers=/etc/sudoers.d/90-agentos-admin
printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$ADMIN_USER" > "$sudoers.tmp"
chmod 0440 "$sudoers.tmp"
visudo -cf "$sudoers.tmp" > /dev/null || { rm -f "$sudoers.tmp"; die "the sudoers line did not check out"; }
mv "$sudoers.tmp" "$sudoers"

home="$(getent passwd "$ADMIN_USER" | cut -d: -f6)"
install -d -m 0700 -o "$ADMIN_USER" -g "$ADMIN_USER" "$home/.ssh"
keys="$home/.ssh/authorized_keys"
touch "$keys"
grep -qxF "$ADMIN_SSH_KEY" "$keys" || printf '%s\n' "$ADMIN_SSH_KEY" >> "$keys"
chown "$ADMIN_USER:$ADMIN_USER" "$keys"
chmod 0600 "$keys"

say "SSH: keys only, no root login"
sshd_conf=/etc/ssh/sshd_config.d/01-agentos.conf
cat > "$sshd_conf" <<'EOF'
# openajar node template: sign in with a key as the admin user, never as root or with a password.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
if ! sshd -t; then
  rm -f "$sshd_conf"
  die "sshd rejected the new settings, so they were removed. Run sshd -t to see why."
fi
systemctl reload ssh

# ---- 3. unattended security upgrades ----
say "Unattended security upgrades"
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -q unattended-upgrades > /dev/null
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

# ---- 4. openajar, from the checkout of AGENTOS_GIT_REF ----
say "Installing openajar${REF:+ from $REF} with deploy/install-node.sh"
OPENAJAR_INIT_ARGS="--name $NODE_NAME" bash "$SRC/deploy/install-node.sh" --systemd --user agentos --hardened

# ---- 5. the controller: the token is single use, so it is blanked whatever happens ----
join="not asked"
if [ -n "$CONTROLLER_URL" ] && [ -n "$JOIN_TOKEN" ]; then
  say "Joining $CONTROLLER_URL"
  if runuser -u agentos -- env OPENAJAR_HOME="$OPENAJAR_HOME" "$OPENAJAR" node join "$CONTROLLER_URL" "$JOIN_TOKEN"; then
    join="joined $CONTROLLER_URL"
  else
    join="failed: make a new join token and run openajar node join by hand"
  fi
  sed -i 's/^JOIN_TOKEN=.*/JOIN_TOKEN=/' "$ENV_FILE"
  systemctl restart agentosd
fi

# ---- 6. the doctor, as the service user, for the admin user to read ----
say "Waiting for agentosd, then openajar doctor"
for _ in $(seq 1 60); do
  systemctl is-active --quiet agentosd && break
  sleep 2
done
sleep 10  # the MCP port opens a moment after the unit reports active
doctor_rc=0
(cd / && runuser -u agentos -- env OPENAJAR_HOME="$OPENAJAR_HOME" "$OPENAJAR" doctor) > "$doctor_out" 2>&1 \
  || doctor_rc=$?
report="$home/openajar-doctor.txt"
{
  echo "openajar node $NODE_NAME, installed${REF:+ from $REF} at $(date -u +%Y-%m-%dT%H:%MZ)"
  echo "doctor exit code: $doctor_rc (0: no check failed; read the warnings below)"
  echo "controller: $join"
  echo
  cat "$doctor_out"
} > "$report"
chown "$ADMIN_USER:$ADMIN_USER" "$report"
chmod 0600 "$report"

say "Doctor exit $doctor_rc. The report is $report (read it as $ADMIN_USER)."
exit "$doctor_rc"
