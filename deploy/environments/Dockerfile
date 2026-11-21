# The machine of an environment on the docker provider (vms.toml: provider = "docker"): Ubuntu 24.04
# with sshd, set up by the same script cloud-init runs on a cloud VM. For a person without a cloud
# account, for CI, and for working on openajar itself.
#
# Build it with `openajar environments setup --build` (it puts this file, entrypoint.sh and the setup
# script in one folder and runs docker build), or by hand from the top of a checkout:
#
#   mkdir -p /tmp/agentos-env && cp deploy/environments/* src/openajar/environments/setup.sh \
#     src/openajar/environments/browser.sh src/openajar/environments/browser-requirements.txt \
#     src/openajar/environments/desktop.sh /tmp/agentos-env/
#   docker build -t agentos-env:latest /tmp/agentos-env
#
# The node makes each machine's SSH host key and login key and copies them in before the container
# starts; nothing secret is in the image. iptables is for the entrypoint's wall: the container
# starts with NET_ADMIN, closes its way to private networks and to this computer, then gives the
# capability up for good (entrypoint.sh).
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -q && apt-get install -y -q --no-install-recommends openssh-server ca-certificates iptables \
    && rm -rf /var/lib/apt/lists/* && rm -f /etc/ssh/ssh_host_*
COPY setup.sh /usr/local/lib/agentos-env/setup.sh
RUN sh /usr/local/lib/agentos-env/setup.sh
# The environment's browser on the machine (environments.toml browser = "vm"): Xvfb, x11vnc, ffmpeg (video
# proofs record the screen with it), fonts and Camoufox pinned to the node's versions (browser.sh). About
# 1 GB and a few minutes on top; its own layer.
COPY browser.sh browser-requirements.txt /usr/local/lib/agentos-env/
# Camoufox finds its browser through GitHub's releases API, which limits unauthenticated calls; a build may
# pass a token as a build secret (never a layer or an image env): docker build --secret id=github_token,...
RUN --mount=type=secret,id=github_token,required=false \
    GITHUB_TOKEN="$(cat /run/secrets/github_token 2>/dev/null || true)" sh /usr/local/lib/agentos-env/browser.sh
# The desktop on that screen (environments.toml desktop = true): openbox, lxterminal, pcmanfm, the dock's
# python3-xlib and python3-pil, fonts, and TigerVNC's Xvnc (desktop.sh). About 90 MB; its own layer.
COPY desktop.sh /usr/local/lib/agentos-env/
RUN sh /usr/local/lib/agentos-env/desktop.sh
COPY entrypoint.sh /usr/local/lib/agentos-env/entrypoint.sh
RUN chmod 755 /usr/local/lib/agentos-env/entrypoint.sh
# 2: the entrypoint walls the machine off (AGENTOS_ENV_WALL=1); openajar.vms.docker refuses an older image
LABEL org.agentos.environment="2"
# 1: it has the environment's browser (browser.sh)
LABEL org.agentos.browser="1"
# the desktop's version (desktop.sh VERSION). An image without it, or with an older one, still works: each
# machine installs the desktop when its screen first starts (openajar.environments.vmbrowser)
LABEL org.agentos.desktop="1"
WORKDIR /work
EXPOSE 22
ENTRYPOINT ["/usr/local/lib/agentos-env/entrypoint.sh"]
