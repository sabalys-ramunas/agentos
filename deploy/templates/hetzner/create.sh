#!/usr/bin/env bash
# Creates a Hetzner Cloud server from the openajar node template. It calls the hcloud CLI, which
# already holds your API token (hcloud context create). This script never reads, prints or stores
# a token, and it writes no files: the user data goes to Hetzner exactly as it is in FILE.
#
#   deploy/templates/hetzner/create.sh --name NAME --type TYPE --location LOCATION --image IMAGE \
#     --ssh-key KEY_NAME --user-data FILE
#
# Every option is required and there are no defaults: server types, locations and prices change,
# so look them up first (hcloud server-type list, hcloud location list, hcloud image list).
# --ssh-key names a key already in your Hetzner project. It is required because without a key
# hcloud makes a root password and prints it.
#
# FILE is a copy of cloud-init.yaml with the variables filled in, chmod 600, outside the checkout.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: create.sh --name NAME --type TYPE --location LOCATION --image IMAGE --ssh-key KEY_NAME --user-data FILE

Every option is required. docs/NODE-TEMPLATES.md has the steps.
EOF
}
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

name="" type="" location="" image="" ssh_key="" user_data=""
[ "$#" -gt 0 ] || { usage; exit 2; }
while [ "$#" -gt 0 ]; do
  case "$1" in
    --name|--type|--location|--image|--ssh-key|--user-data)
      [ "$#" -ge 2 ] || { echo "error: $1 needs a value" >&2; usage; exit 2; }
      case "$1" in
        --name) name="$2" ;;
        --type) type="$2" ;;
        --location) location="$2" ;;
        --image) image="$2" ;;
        --ssh-key) ssh_key="$2" ;;
        --user-data) user_data="$2" ;;
      esac
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unknown option $1" >&2; usage; exit 2 ;;
  esac
done

missing=""
for opt in name type location image ssh_key user_data; do
  [ -n "${!opt}" ] || missing="$missing --${opt//_/-}"
done
[ -z "$missing" ] || { echo "error: missing:$missing" >&2; usage; exit 2; }

[[ "$name" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] \
  || die "--name must be a hostname: lowercase letters, digits and dashes"

# The user data holds the admin key and maybe a join token: it must be a cloud-config file, private,
# finished (no CHANGE-ME left) and with the admin key filled in.
[ -f "$user_data" ] && [ -r "$user_data" ] || die "--user-data: $user_data is not a readable file"
IFS= read -r first_line < "$user_data" || true
[ "$first_line" = "#cloud-config" ] || die "--user-data: the first line must be #cloud-config"
[ -z "$(find "$user_data" -maxdepth 0 \( -perm -040 -o -perm -004 \))" ] \
  || die "--user-data: $user_data is readable by other users; chmod 600 it (it may hold a join token)"
[ "$(grep -c 'CHANGE-ME' "$user_data" || true)" = 0 ] \
  || die "--user-data: variables still hold CHANGE-ME; fill in $user_data first"
grep -q '^[[:space:]]*ADMIN_SSH_KEY=.' "$user_data" || die "--user-data: ADMIN_SSH_KEY is empty"

command -v hcloud > /dev/null 2>&1 || die "hcloud is not installed (https://github.com/hetznercloud/cli)"

hcloud server create --name "$name" --type "$type" --location "$location" --image "$image" \
  --ssh-key "$ssh_key" --user-data-from-file "$user_data"

cat <<EOF

Created $name. The first boot takes several minutes, because it builds openajar.
Find its address with: hcloud server ip $name
Then sign in as the admin user (ADMIN_USER, agentos-admin unless you changed it), wait with
  cloud-init status --wait
and read ~/openajar-doctor.txt. docs/NODE-TEMPLATES.md has the rest.
EOF
