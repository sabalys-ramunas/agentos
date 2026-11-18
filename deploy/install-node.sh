#!/usr/bin/env bash
# Install or upgrade an agentos node on Ubuntu 24.04. Safe to run again.
#
#   sudo deploy/install-node.sh --systemd        production: /opt/agentos, /var/lib/agentos, agentosd.service
#   sudo deploy/install-node.sh --systemd --user agentos --hardened
#                                                the same, run as the unprivileged `agentos` user with the
#                                                hardened unit (deploy/agentosd-hardened.service)
#
#   INSTALL_PREFIX=/srv/agentos-dev/mcp AGENTOS_HOME=/srv/agentos-dev/mcp/.agentos-home \
#     AGENTOS_INIT_ARGS="--mcp-port 7430 --display-base 130 --viewer-base 6130" \
#     deploy/install-node.sh --dev               a dev copy: no /opt, no systemd
#
# Options:
#   --systemd        install, enable and (re)start agentosd.service (paths follow INSTALL_PREFIX
#                    and AGENTOS_HOME)
#   --dev            also install the dev dependency group (pytest, ruff) for node tests
#   --skip-apt       skip apt packages and Playwright's system deps (already installed, or not root)
#   --skip-browser   skip the Camoufox browser download
#   --user NAME      run agentosd as this system user: creates it (home = AGENTOS_HOME, no shell), gives
#                    it AGENTOS_HOME, and puts its Camoufox browser in AGENTOS_HOME/.cache
#   --hardened       install deploy/agentosd-hardened.service (needs --user): read-only system and code,
#                    private /tmp and /dev, no capabilities, memory caps. Also creates the brain user
#                    and installs deploy/agentos-runas.service, the root helper that starts brains as
#                    that user for the unprivileged agentosd (an install without --hardened removes it)
#   --no-restart     with --systemd: install and enable the unit but do not (re)start it (agentos upgrade
#                    runs migrations first, then starts it)
#   --yes            with --systemd: restart agentosd even when lanes other sessions hold are open. Without
#                    it the installer names those lanes (`agentos node restart --check`), installs the code,
#                    does not restart, and exits 3; restart later with `agentos node restart`
#   --render-unit P  write the unit as it would be installed to P and change nothing in systemd (to
#                    check it with `systemd-analyze verify P` in a dev prefix)
#   --backup-timer   with --systemd: also install agentos-backup.timer (daily; needs
#                    AGENTOS_HOME/secrets/backup.pass, chmod 600)
#
# With --systemd it also writes /usr/local/bin/agentos, a small wrapper that runs the CLI with the
# node's AGENTOS_HOME, as the service user when root calls it (root only for the commands that need
# root: upgrade, backup restore, migrate-service-user). So `sudo agentos doctor` works on the node.
#
# The web app is built here when Node.js is installed. Without it, a build that came with the
# source (a web/dist built from the same sources) is used; with neither, the node has no web UI
# and the installer says how to add one. MCP and the API work.
#
# Environment:
#   INSTALL_PREFIX     code and .venv (default /opt/agentos)
#   AGENTOS_HOME       node data: node.toml, agentos.db, secrets, lanes (default /var/lib/agentos)
#   AGENTOS_INIT_ARGS  extra arguments for `agentos node init` on first install
#   AGENTOS_BRAIN_USER the user brains run as with --hardened (default agentos-brain)
#   UV_PYTHON          Python for the venv (default 3.12)
set -euo pipefail

INSTALL_PREFIX="${INSTALL_PREFIX:-/opt/agentos}"
AGENTOS_HOME="${AGENTOS_HOME:-/var/lib/agentos}"
AGENTOS_INIT_ARGS="${AGENTOS_INIT_ARGS:-}"
BRAIN_USER="${AGENTOS_BRAIN_USER:-agentos-brain}"
export UV_PYTHON="${UV_PYTHON:-3.12}"
APT_PACKAGES=(xvfb x11vnc xdotool x11-xkb-utils xclip fonts-liberation fonts-noto-color-emoji imagemagick rsync curl
              bubblewrap e2fsprogs iproute2 util-linux logrotate  # bubblewrap to util-linux: env lanes
              ffmpeg)  # video proofs of an environment's browser on this node (browser = "node")

WITH_SYSTEMD=0
WITH_DEV=0
SKIP_APT=0
SKIP_BROWSER=0
SERVICE_USER=""
HARDENED=0
NO_RESTART=0
YES=0
RESTART_HELD=0
RENDER_UNIT=""
BACKUP_TIMER=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --systemd) WITH_SYSTEMD=1 ;;
    --dev) WITH_DEV=1 ;;
    --skip-apt) SKIP_APT=1 ;;
    --skip-browser) SKIP_BROWSER=1 ;;
    --user) [ "$#" -ge 2 ] || { echo "--user needs a name" >&2; exit 2; }; SERVICE_USER="$2"; shift ;;
    --user=*) SERVICE_USER="${1#--user=}" ;;
    --hardened) HARDENED=1 ;;
    --no-restart) NO_RESTART=1 ;;
    --yes|-y) YES=1 ;;
    --render-unit) [ "$#" -ge 2 ] || { echo "--render-unit needs a path" >&2; exit 2; }; RENDER_UNIT="$2"; shift ;;
    --backup-timer) BACKUP_TIMER=1 ;;
    -h|--help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
warn() { printf 'warning: %s\n' "$*" >&2; }

# Code and venv must be readable by a service user; AGENTOS_HOME is made private (700) below.
umask 022

if [ -n "$SERVICE_USER" ]; then
  [[ "$SERVICE_USER" =~ ^[a-z_][a-z0-9_-]{0,30}$ ]] || die "--user must be a plain system user name"
  [ "$SERVICE_USER" != root ] || die "--user root is the default; leave --user out"
fi
[ "$HARDENED" = 0 ] || [ -n "$SERVICE_USER" ] || die "--hardened needs --user (the hardened unit never runs as root)"
if [ "$HARDENED" = 1 ]; then
  [[ "$BRAIN_USER" =~ ^[a-z_][a-z0-9_-]{0,30}$ ]] || die "AGENTOS_BRAIN_USER must be a plain system user name"
  [ "$BRAIN_USER" != root ] && [ "$BRAIN_USER" != "$SERVICE_USER" ] \
    || die "AGENTOS_BRAIN_USER must differ from root and from --user (brains must not read AGENTOS_HOME)"
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$SRC_DIR/pyproject.toml" ] && grep -q '^name = "agentos"' "$SRC_DIR/pyproject.toml" \
  || die "run this from an agentos checkout ($SRC_DIR has no agentos pyproject.toml)"

# Normalize before any guard or rsync --delete: a trailing slash, "..", or a symlink must not
# change what the guards see (a trailing slash on AGENTOS_HOME once made rsync --delete wipe
# the token, the database and every lane profile). Pure bash, so it also runs on macOS.
normpath() {  # lexical absolute path: no ".", ".." or empty segments, no trailing "/"
  local path="$1" seg out=""
  local -a segs=() kept=()
  case "$path" in /*) ;; *) path="$PWD/$path" ;; esac
  IFS=/ read -r -a segs <<< "$path"
  for seg in ${segs[@]+"${segs[@]}"}; do
    case "$seg" in
      ""|.) ;;
      ..) if [ "${#kept[@]}" -gt 0 ]; then unset "kept[$((${#kept[@]} - 1))]"; fi ;;
      *) kept+=("$seg") ;;
    esac
  done
  for seg in ${kept[@]+"${kept[@]}"}; do out="$out/$seg"; done
  printf '%s\n' "${out:-/}"
}
physpath() {  # normpath with the longest existing directory prefix resolved through symlinks
  local head rest=""
  head="$(normpath "$1")"
  while [ "$head" != / ] && [ ! -d "$head" ]; do rest="/${head##*/}$rest"; head="${head%/*}"; head="${head:-/}"; done
  head="$(cd -P -- "$head" && pwd -P)"
  normpath "$head$rest"
}
refuse_system_path() {  # $1 = variable name, $2 = path
  case "$2" in
    /|/root|/home|/usr|/usr/local|/opt|/var|/var/lib|/etc|/bin|/sbin|/lib|/lib64|/boot|/dev|/proc|/sys|/tmp|/run|/srv|/mnt|/media|/snap)
      die "refusing $1=$2 (a system directory)" ;;
    /etc/*|/proc/*|/sys/*|/dev/*|/boot/*|/run/*|/bin/*|/sbin/*|/lib/*|/lib64/*|/usr/bin/*|/usr/sbin/*|/usr/lib/*)
      die "refusing $1=$2 (inside a system directory)" ;;
  esac
}
[ -n "$INSTALL_PREFIX" ] || die "INSTALL_PREFIX is empty"
[ -n "$AGENTOS_HOME" ] || die "AGENTOS_HOME is empty"
case "$INSTALL_PREFIX$AGENTOS_HOME" in *$'\n'*) die "paths with newlines are not supported" ;; esac
INSTALL_PREFIX="$(normpath "$INSTALL_PREFIX")"
AGENTOS_HOME="$(normpath "$AGENTOS_HOME")"
PREFIX_REAL="$(physpath "$INSTALL_PREFIX")"
HOME_REAL="$(physpath "$AGENTOS_HOME")"
SRC_REAL="$(physpath "$SRC_DIR")"
refuse_system_path INSTALL_PREFIX "$INSTALL_PREFIX"
refuse_system_path AGENTOS_HOME "$AGENTOS_HOME"
refuse_system_path INSTALL_PREFIX "$PREFIX_REAL"  # a symlink that points at a system directory
refuse_system_path AGENTOS_HOME "$HOME_REAL"
[ "$PREFIX_REAL" != "$HOME_REAL" ] || die "INSTALL_PREFIX and AGENTOS_HOME must differ"
case "$PREFIX_REAL/" in "$HOME_REAL"/*) die "INSTALL_PREFIX cannot be inside AGENTOS_HOME" ;; esac
if [ "$WITH_SYSTEMD" = 1 ]; then
  [ "$(id -u)" = 0 ] || die "need root for --systemd (nothing was changed)"
  case "$INSTALL_PREFIX$AGENTOS_HOME" in
    *[%\"\\\#\&]*) die "with --systemd, INSTALL_PREFIX and AGENTOS_HOME cannot contain % \" \\ # or &" ;;
  esac
  if [ "$HARDENED" = 1 ]; then
    case "$HOME_REAL/" in
      /root/*|/home/*|/run/user/*) die "the hardened unit hides /root and /home (ProtectHome); put AGENTOS_HOME elsewhere" ;;
    esac
  fi
fi
# Options for `agentos node init`, split like a shell would (quotes and backslashes are
# honoured) but never glob-expanded or evaluated.
INIT_ARGS=()
if [ -n "$AGENTOS_INIT_ARGS" ]; then
  parsed="$(printf '%s' "$AGENTOS_INIT_ARGS" | xargs printf '%s\n')" \
    || die "AGENTOS_INIT_ARGS has unbalanced quotes: $AGENTOS_INIT_ARGS"
  while IFS= read -r arg; do INIT_ARGS+=("$arg"); done <<< "$parsed"
fi
if [ "$PREFIX_REAL" != "$SRC_REAL" ]; then
  case "$PREFIX_REAL/" in "$SRC_REAL"/*) die "INSTALL_PREFIX is inside the checkout; use the checkout itself or a separate dir" ;; esac
  case "$SRC_REAL/" in "$PREFIX_REAL"/*) die "the checkout is inside INSTALL_PREFIX; use a separate dir" ;; esac
fi
# Runtimes that other installers manage inside the prefix (the Node.js used by the
# brain CLIs). They are kept across upgrades and do not make the prefix "foreign".
KEEP_DIRS=(runtime node)
foreign_entries() {
  local entry keep skip
  for entry in "$INSTALL_PREFIX"/* "$INSTALL_PREFIX"/.[!.]*; do
    [ -e "$entry" ] || continue
    skip=0
    for keep in "${KEEP_DIRS[@]}"; do [ "$(basename "$entry")" = "$keep" ] && skip=1; done
    [ "$skip" = 1 ] || echo "$entry"
  done
}
if [ -d "$INSTALL_PREFIX" ] && [ -n "$(foreign_entries)" ] \
  && ! grep -qs '^name = "agentos"' "$INSTALL_PREFIX/pyproject.toml"; then
  die "$INSTALL_PREFIX is not empty and is not an agentos install; pick another INSTALL_PREFIX"
fi

# ---- system packages ----
if [ "$SKIP_APT" = 0 ]; then
  missing=()
  for pkg in "${APT_PACKAGES[@]}"; do
    dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" || missing+=("$pkg")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    [ "$(id -u)" = 0 ] || die "need root to install: ${missing[*]} (or pass --skip-apt)"
    say "Installing ${missing[*]}"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q
    apt-get install -y -q --no-install-recommends "${missing[@]}"
  else
    say "System packages already installed"
  fi
fi

# ---- uv ----
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
command -v uv >/dev/null 2>&1 || die "uv is not on PATH after installing it"

# ---- code ----
mkdir -p "$INSTALL_PREFIX"
if [ "$PREFIX_REAL" != "$SRC_REAL" ]; then
  say "Copying $SRC_DIR to $INSTALL_PREFIX"
  excludes=(--exclude /.venv/ --exclude .git --exclude __pycache__ --exclude '*.pyc'
            --exclude .pytest_cache --exclude .ruff_cache --exclude /poc/shots/
            --exclude /poc/profiles/ --exclude /poc/fingerprints/ --exclude /poc/secrets.json
            --exclude /web/dist/)  # the prefix builds its own web/dist: a copied one would replace the build it made
  for keep in "${KEEP_DIRS[@]}"; do excludes+=(--exclude "/$keep/"); done
  case "$HOME_REAL" in  # never delete node data that lives inside the prefix
    "$PREFIX_REAL"/*) excludes+=(--exclude "/${HOME_REAL#"$PREFIX_REAL"/}/") ;;
  esac
  # The installed code must be root's and not writable by anyone else, whoever cloned the
  # checkout and under whatever umask: agentos-runas refuses to run code another user can change.
  copy_as=(--chmod=go-w)
  [ "$(id -u)" = 0 ] && copy_as+=(--chown=root:root)
  rsync -a --delete "${copy_as[@]}" "${excludes[@]}" "$SRC_DIR/" "$INSTALL_PREFIX/"
fi

# ---- web app ----
NO_NODEJS=0
# web/ is the web app the node serves. pnpm comes through npx, so a node needs nothing but Node.js. It
# is built again whenever its sources changed since the last good build (deploy/web-build.sh), and
# the last good build is kept when a build fails.
if [ -f "$INSTALL_PREFIX/web/package.json" ]; then
  . "$INSTALL_PREFIX/deploy/web-build.sh"
  if web_is_stale "$INSTALL_PREFIX/web"; then
    [ -x "$INSTALL_PREFIX/node/bin/npm" ] && PATH="$INSTALL_PREFIX/node/bin:$PATH"
    if command -v npx >/dev/null 2>&1; then
      say "Building the web app"
      (cd "$INSTALL_PREFIX/web" && npx --yes pnpm@10.33.0 install --frozen-lockfile) \
        && web_rebuild "$INSTALL_PREFIX/web" npx --yes pnpm@10.33.0 build \
        || warn "the web app did not build; the node keeps serving its last build"
    elif [ "$PREFIX_REAL" != "$SRC_REAL" ] && ! web_is_stale "$SRC_DIR/web"; then
      # no Node.js here, but the source came with a build of these very sources (built elsewhere)
      say "Using the web app build that came with the source (web/dist)"
      rsync -a --delete "${copy_as[@]}" "$SRC_DIR/web/dist/" "$INSTALL_PREFIX/web/dist/"
    else
      NO_NODEJS=1
    fi
  fi
fi

say "Syncing the Python environment"
# node: the lanes' browser stack; studio: the review browser's image work (Pillow) for ui_check,
# ui_diff, system_check and profile_learn_brand.
sync_args=(--frozen --extra node --extra studio)
[ "$WITH_DEV" = 1 ] || sync_args+=(--no-dev)
VENV_PY="$INSTALL_PREFIX/.venv/bin/python"
if [ -n "$SERVICE_USER" ]; then
  # Where the system has no Python 3.12 (Ubuntu 22.04 and 26.04, Debian 12), uv downloads one. Its
  # default place is the installing user's home (/root/.local/share/uv), which the service user
  # cannot enter, and the service then fails to start (203/EXEC). Keep it in the prefix, in a
  # directory upgrades keep (KEEP_DIRS), and rebuild a venv that still points into a home.
  export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$INSTALL_PREFIX/runtime/python}"
  if [ -e "$VENV_PY" ]; then
    case "$(readlink -f "$VENV_PY")" in
      /root/*|/home/*) say "Rebuilding the venv: its Python is in a home directory $SERVICE_USER cannot enter"
                       rm -rf "$INSTALL_PREFIX/.venv" ;;
    esac
  fi
fi
(cd "$INSTALL_PREFIX" && uv sync "${sync_args[@]}")

# ---- browser ----
# Root keeps the browser in its own cache; a service user keeps it in AGENTOS_HOME/.cache, the
# cache dir the unit points XDG_CACHE_HOME at (it must be writable: Camoufox writes a fontconfig
# file there at launch). Backups leave .cache out.
BROWSER_ENV=()
if [ -n "$SERVICE_USER" ]; then
  BROWSER_ENV=(env "XDG_CACHE_HOME=$AGENTOS_HOME/.cache")
fi
if [ "$SKIP_BROWSER" = 0 ]; then
  if [ -n "$SERVICE_USER" ] && [ ! -e "$AGENTOS_HOME/.cache/camoufox" ] && [ -d "$HOME/.cache/camoufox" ]; then
    say "Copying the installed Camoufox browser into $AGENTOS_HOME/.cache for $SERVICE_USER"
    mkdir -p "$AGENTOS_HOME/.cache"
    cp -a "$HOME/.cache/camoufox" "$AGENTOS_HOME/.cache/camoufox"
  fi
  if (cd "$INSTALL_PREFIX" && ${BROWSER_ENV[@]+"${BROWSER_ENV[@]}"} "$VENV_PY" -c \
      "from camoufox.pkgman import camoufox_path; camoufox_path(download_if_missing=False)") >/dev/null 2>&1; then
    say "Camoufox browser already installed"
  else
    say "Fetching the Camoufox browser"
    (cd "$INSTALL_PREFIX" && ${BROWSER_ENV[@]+"${BROWSER_ENV[@]}"} uv run --no-sync python -m camoufox fetch)
  fi
  # Camoufox draws fingerprints from fpgen's model, which lives inside the venv (site-packages/
  # fpgen/data) and comes with `camoufox fetch`. A venv that was rebuilt while the browser stayed
  # in its cache has no model, and under the hardened unit the prefix is read-only, so the first
  # lane then fails to start ("Read-only file system: .../fpgen/data"). Put the pinned, checksummed
  # model in place now (a no-op when it is there). Never `import fpgen` for this: it fetches an
  # unpinned model.
  (cd "$INSTALL_PREFIX" && "$VENV_PY" -c 'from camoufox.fpgen_model import ensure_fpgen_model; ensure_fpgen_model()') \
    || warn "the fingerprint model is not installed; run \`$VENV_PY -m camoufox fetch\` as root before the first lane"
  # Studio's review browser is Playwright's Chromium (not Camoufox): ui_check, system_check, ui_diff
  # and profile_learn_brand open pages in it. Same cache rules as Camoufox. A failed fetch only
  # warns, since lanes work without it; studio_status then names the command.
  say "Fetching Chromium for studio's review browser"
  (cd "$INSTALL_PREFIX" && ${BROWSER_ENV[@]+"${BROWSER_ENV[@]}"} uv run --no-sync python -m playwright install chromium) \
    || warn "Chromium for studio is not installed; run \`$VENV_PY -m playwright install chromium\` before ui_check"
fi
if [ "$SKIP_APT" = 0 ]; then
  [ "$(id -u)" = 0 ] || die "need root for Playwright's Firefox system deps (or pass --skip-apt)"
  say "Installing Playwright's Firefox and Chromium system deps"
  (cd "$INSTALL_PREFIX" && uv run --no-sync python -m playwright install-deps firefox chromium)
fi

# ---- service user ----
if [ -n "$SERVICE_USER" ]; then
  if id -u "$SERVICE_USER" >/dev/null 2>&1; then
    say "User $SERVICE_USER already exists"
  else
    [ "$(id -u)" = 0 ] || die "need root to create the user $SERVICE_USER"
    say "Creating the system user $SERVICE_USER (home $AGENTOS_HOME, no login shell)"
    useradd --system --user-group --home-dir "$AGENTOS_HOME" --no-create-home \
      --shell /usr/sbin/nologin --comment "agentos node daemon" "$SERVICE_USER"
  fi
fi

# ---- node data ----
say "Preparing AGENTOS_HOME at $AGENTOS_HOME"
mkdir -p "$AGENTOS_HOME"
chmod 700 "$AGENTOS_HOME"
if [ -f "$AGENTOS_HOME/node.toml" ]; then
  echo "node.toml already exists; keeping its token. Show the connect lines with: agentos node init"
else
  AGENTOS_HOME="$AGENTOS_HOME" "$INSTALL_PREFIX/.venv/bin/agentos" node init ${INIT_ARGS[@]+"${INIT_ARGS[@]}"}
fi
if [ -n "$SERVICE_USER" ]; then
  [ "$(id -u)" = 0 ] || [ "$(id -un)" = "$SERVICE_USER" ] || die "need root to give $AGENTOS_HOME to $SERVICE_USER"
  say "Giving $AGENTOS_HOME to $SERVICE_USER"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$AGENTOS_HOME"
  chmod -R go-rwx "$AGENTOS_HOME"
  # The service user must reach its code and own its home; a parent directory it cannot traverse
  # (for example a dev prefix under /root) would make the service fail at start.
  reach=1
  runuser -u "$SERVICE_USER" -- test -x "$INSTALL_PREFIX/.venv/bin/agentos" 2>/dev/null || reach=0
  # the script is not enough: its interpreter is a link that can point where the user cannot go
  runuser -u "$SERVICE_USER" -- "$INSTALL_PREFIX/.venv/bin/python" -c '' 2>/dev/null || reach=0
  runuser -u "$SERVICE_USER" -- test -w "$AGENTOS_HOME" 2>/dev/null || reach=0
  if [ "$reach" = 0 ]; then
    msg="$SERVICE_USER cannot run $INSTALL_PREFIX/.venv/bin/agentos or write $AGENTOS_HOME (a parent directory is not traversable)"
    if [ "$WITH_SYSTEMD" = 1 ]; then die "$msg"; else warn "$msg"; fi
  fi
fi

# ---- systemd ----
# The unit as it will be installed: paths follow INSTALL_PREFIX and AGENTOS_HOME, and with --user the
# service runs as that user with HOME and XDG_CACHE_HOME in AGENTOS_HOME.
render_unit() {
  local template="$INSTALL_PREFIX/deploy/agentosd.service"
  [ "$HARDENED" = 1 ] && template="$INSTALL_PREFIX/deploy/agentosd-hardened.service"
  local user_lines=""
  if [ -n "$SERVICE_USER" ] && [ "$HARDENED" = 0 ]; then
    user_lines="User=$SERVICE_USER\nGroup=$SERVICE_USER\nEnvironment=\"HOME=$AGENTOS_HOME\"\nEnvironment=\"XDG_CACHE_HOME=$AGENTOS_HOME/.cache\""
  fi
  sed -e "s#/var/lib/agentos#$AGENTOS_HOME#g" -e "s#/opt/agentos#$INSTALL_PREFIX#g" \
      -e "s#^User=agentos\$#User=$SERVICE_USER#" -e "s#^Group=agentos\$#Group=$SERVICE_USER#" "$template" |
    if [ -n "$user_lines" ]; then sed -e "s#^\[Service\]\$#[Service]\n$user_lines#"; else cat; fi
}

# agentos-runas, the root helper a hardened (unprivileged) agentosd asks to start brains as the
# brain user (src/agentos/runas.py): its fixed caller and brain user come from here.
render_runas_unit() {
  sed -e "s#/opt/agentos#$INSTALL_PREFIX#g" -e "s#--caller agentos #--caller $SERVICE_USER #g" \
      -e "s#--brain-user agentos-brain\$#--brain-user $BRAIN_USER#" "$INSTALL_PREFIX/deploy/agentos-runas.service"
}

if [ -n "$RENDER_UNIT" ]; then
  say "Writing the unit to $RENDER_UNIT (systemd is not touched)"
  render_unit > "$RENDER_UNIT"
fi

if [ "$WITH_SYSTEMD" = 1 ] && [ -z "$RENDER_UNIT" ]; then
  [ "$(id -u)" = 0 ] || die "need root for --systemd"
  command -v systemctl >/dev/null 2>&1 || die "systemctl not found"
  say "Installing agentosd.service$([ "$HARDENED" = 1 ] && echo " (hardened)")$([ -n "$SERVICE_USER" ] && echo " as $SERVICE_USER")"
  render_unit > /etc/systemd/system/agentosd.service.new
  mv /etc/systemd/system/agentosd.service.new /etc/systemd/system/agentosd.service

  # Brains run as their own user. Root agentosd drops to it itself; the hardened, unprivileged one
  # asks agentos-runas. A root install removes a helper left by an earlier hardened one.
  if [ "$HARDENED" = 1 ]; then
    if id -u "$BRAIN_USER" >/dev/null 2>&1; then
      say "Brain user $BRAIN_USER already exists"
    else
      say "Creating the brain user $BRAIN_USER (home /var/lib/$BRAIN_USER, no login shell)"
      useradd --system --user-group --create-home --home-dir "/var/lib/$BRAIN_USER" \
        --shell /usr/sbin/nologin --comment "agentos brains" "$BRAIN_USER"
    fi
    brain_home="$(getent passwd "$BRAIN_USER" | cut -d: -f6)"
    [ -d "$brain_home" ] || install -d -m 700 -o "$BRAIN_USER" -g "$BRAIN_USER" "$brain_home"
    chmod 700 "$brain_home"
    say "Installing agentos-runas.service (starts brains as $BRAIN_USER for $SERVICE_USER)"
    render_runas_unit > /etc/systemd/system/agentos-runas.service.new
    mv /etc/systemd/system/agentos-runas.service.new /etc/systemd/system/agentos-runas.service
    "$INSTALL_PREFIX/.venv/bin/python" -I -S -B "$INSTALL_PREFIX/src/agentos/runas.py" check --caller "$SERVICE_USER" --brain-user "$BRAIN_USER" \
      || die "agentos-runas refuses this setup (above); agentosd.service is installed but brains would not start"
  elif [ -f /etc/systemd/system/agentos-runas.service ]; then
    say "Removing agentos-runas.service (agentosd runs as root and starts brains itself)"
    systemctl disable --now agentos-runas.service >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/agentos-runas.service
  fi

  say "Bounding logs (journald limits, logrotate for $AGENTOS_HOME/logs)"
  mkdir -p /etc/systemd/journald.conf.d
  if ! cmp -s "$INSTALL_PREFIX/deploy/journald-agentos.conf" /etc/systemd/journald.conf.d/agentos.conf; then
    install -m 644 "$INSTALL_PREFIX/deploy/journald-agentos.conf" /etc/systemd/journald.conf.d/agentos.conf
    systemctl restart systemd-journald || warn "could not restart systemd-journald; the limits apply after a reboot"
  fi
  if [ -d /etc/logrotate.d ]; then
    owner="${SERVICE_USER:-root}"
    sed -e "s#/var/lib/agentos#$AGENTOS_HOME#g" -e "s#su root root#su $owner $owner#" \
      "$INSTALL_PREFIX/deploy/agentos.logrotate" > /etc/logrotate.d/agentos
    chmod 644 /etc/logrotate.d/agentos
  fi

  if [ "$BACKUP_TIMER" = 1 ]; then
    [ -f "$AGENTOS_HOME/secrets/backup.pass" ] || die "--backup-timer needs $AGENTOS_HOME/secrets/backup.pass (chmod 600)"
    say "Installing agentos-backup.timer (daily)"
    sed -e "s#/var/lib/agentos#$AGENTOS_HOME#g" -e "s#/opt/agentos#$INSTALL_PREFIX#g" \
      "$INSTALL_PREFIX/deploy/agentos-backup.service" > /etc/systemd/system/agentos-backup.service
    install -m 644 "$INSTALL_PREFIX/deploy/agentos-backup.timer" /etc/systemd/system/agentos-backup.timer
  fi

  systemctl daemon-reload
  systemctl enable agentosd.service
  [ "$HARDENED" = 0 ] || systemctl enable agentos-runas.service
  [ "$BACKUP_TIMER" = 0 ] || systemctl enable --now agentos-backup.timer
  if [ "$NO_RESTART" = 0 ] && [ "$YES" = 0 ] && systemctl is-active --quiet agentosd.service; then
    # A restart closes every open lane, including lanes other sessions hold: name them, and only go
    # on when none has a holder (or with --yes). Exit 3 from the check means a lane has one.
    set +e
    AGENTOS_HOME="$AGENTOS_HOME" "$INSTALL_PREFIX/.venv/bin/agentos" node restart --check
    check=$?
    set -e
    [ "$check" != 3 ] || RESTART_HELD=1
  fi
  if [ "$NO_RESTART" = 1 ]; then
    echo "Not restarting agentosd (--no-restart)."
  elif [ "$RESTART_HELD" = 1 ]; then
    warn "not restarting agentosd: the lanes above are in use by other sessions, and a restart would close them."
    warn "The new code is installed and runs after the next restart: agentos node restart (it asks first),"
    warn "or run the installer again with --yes."
  else
    [ "$HARDENED" = 0 ] || systemctl restart agentos-runas.service
    systemctl restart agentosd.service
    systemctl --no-pager --lines=5 status agentosd.service || true
  fi
fi

# ---- the agentos command on the node ----
# /usr/local/bin/agentos: the node's CLI with its home, as the service user when root runs it, so
# files it writes in AGENTOS_HOME stay the service user's. Commands that need root stay root.
WRAPPER=""
if [ "$WITH_SYSTEMD" = 1 ] && [ -z "$RENDER_UNIT" ]; then
  WRAPPER=/usr/local/bin/agentos
  if [ -e "$WRAPPER" ] && ! grep -q '^# agentos-node-wrapper' "$WRAPPER" 2>/dev/null; then
    warn "$WRAPPER exists and is not this installer's; leaving it as it is"
    WRAPPER=""
  else
    say "Installing $WRAPPER (the CLI with AGENTOS_HOME=$AGENTOS_HOME${SERVICE_USER:+, as $SERVICE_USER})"
    mkdir -p /usr/local/bin
    cat > "$WRAPPER.new" <<WRAP
#!/bin/sh
# agentos-node-wrapper: written by deploy/install-node.sh (run it again to update). The node's CLI,
# with the node's home. Root runs it as the service user, except for the commands that need root.
bin=$INSTALL_PREFIX/.venv/bin/agentos
home=$AGENTOS_HOME
user=${SERVICE_USER}
export AGENTOS_HOME="\$home"
if [ -z "\$user" ] || [ "\$(id -u)" != 0 ]; then
  exec "\$bin" "\$@"
fi
case "\$1 \$2 \$3" in
  "upgrade "*|"node upgrade "*|"migrate-service-user "*|"node migrate-service-user "*|"backup restore "*|"node backup restore "*)
    exec "\$bin" "\$@" ;;
esac
exec runuser -u "\$user" -- env HOME="\$home" AGENTOS_HOME="\$home" XDG_CACHE_HOME="\$home/.cache" "\$bin" "\$@"
WRAP
    chmod 755 "$WRAPPER.new"
    mv "$WRAPPER.new" "$WRAPPER"
  fi
fi

say "Done. agentos is at $INSTALL_PREFIX/.venv/bin/agentos, node data at $AGENTOS_HOME"
if [ -n "$WRAPPER" ]; then
  if [ -n "$SERVICE_USER" ]; then
    echo "On this node, run it as root with: sudo agentos doctor (it runs as $SERVICE_USER with AGENTOS_HOME=$AGENTOS_HOME)."
  else
    echo "On this node, run it with: sudo agentos doctor (AGENTOS_HOME=$AGENTOS_HOME)."
  fi
fi
if [ ! -f "$INSTALL_PREFIX/web/dist/index.html" ]; then
  warn "this node has no web UI: the web app is not built$([ "$NO_NODEJS" = 1 ] && echo ", because Node.js is not installed here")."
  warn "MCP, the API and the CLI work; the sign-in link shows a placeholder page until the web app is built. To add one:"
  warn "  - install Node.js 22 here and run this installer again, or"
  warn "  - build on your computer, in your checkout: (cd web && pnpm install && pnpm build);"
  warn "    then copy that checkout here with its builds (scripts/sync-node.sh --web DIR, or rsync) and run this installer again"
  warn "    from that copy. \`agentos connect HOST --install\` from a checkout sends it, builds included, on a new node."
fi
if [ "$WITH_SYSTEMD" = 1 ] && [ -z "$SERVICE_USER" ] && [ -z "$RENDER_UNIT" ]; then
  say "Note: agentosd runs as root. On a new node, install with --user agentos --hardened instead (deploy/README.md)."
fi
[ "$RESTART_HELD" = 0 ] || exit 3
