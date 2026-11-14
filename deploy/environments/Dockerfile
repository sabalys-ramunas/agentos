# The machine of an environment on the docker provider (vms.toml: provider = "docker"): Ubuntu 24.04
# with sshd, set up by the same script cloud-init runs on a cloud VM. For a person without a cloud
# account, for CI, and for working on agentos itself.
#
# Build it with `agentos environments setup --build` (it puts this file, entrypoint.sh and the setup
# script in one folder and runs docker build), or by hand from the top of a checkout:
#
#   mkdir -p /tmp/agentos-env && cp deploy/environments/* src/agentos/environments/setup.sh /tmp/agentos-env/
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
COPY entrypoint.sh /usr/local/lib/agentos-env/entrypoint.sh
RUN chmod 755 /usr/local/lib/agentos-env/entrypoint.sh
# 2: the entrypoint walls the machine off (AGENTOS_ENV_WALL=1); agentos.vms.docker refuses an older image
LABEL org.agentos.environment="2"
WORKDIR /work
EXPOSE 22
ENTRYPOINT ["/usr/local/lib/agentos-env/entrypoint.sh"]
