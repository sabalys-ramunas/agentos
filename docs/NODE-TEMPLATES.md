# Node templates: from nothing to a green self-check

A template takes a machine you have, or a server you rent, to a running openajar node with
`openajar doctor` passing and, if you want, joined to a controller. Three ship in `deploy/templates/`:

| Template | Where it runs | Browser lanes | How you start it |
|---|---|---|---|
| `hetzner` | a Hetzner Cloud server | yes | `create.sh` (hcloud) |
| `home-box` | a machine at home | yes | a USB installer with an autoinstall seed |
| `mac-mini` | a Mac mini | no, Linux-only for now | `setup.sh` (a controller, not a node) |

List them, or see one's files and variables, with the CLI:

```
openajar node templates           # the list
openajar node templates hetzner   # path, files, and the variables to fill in
```

Status: the templates are tested as files (YAML and the keys the first boot needs, shell syntax,
`create.sh` against a fake `hcloud`, `cloud-init schema`). Nothing here has created a real server,
an account or a token. The first real server, home box and Mac mini are still to be run.

## Choose

- **A server you do not own**: `hetzner`. Lanes run there. It needs a Hetzner Cloud account.
- **A machine you keep at home**: `home-box`. The installer erases its largest disk.
- **A Mac mini as the hub**: `mac-mini` runs the controller, the one MCP URL your agents use and
  the place nodes join. Browser lanes do not run on macOS yet, so a Mac mini usually sits beside
  a Linux node (a `hetzner` or `home-box` one), which joins it.

## Fill in the variables

The Linux templates take the same values in one `bootstrap.env` block:

| Variable | Required | What it is |
|---|---|---|
| `ADMIN_SSH_KEY` | yes | your public key, one line: `ssh-ed25519 AAAA... you@laptop` |
| `ADMIN_USER` | no | the admin user that signs in over SSH (default `agentos-admin`) |
| `NODE_NAME` | no | the node's name in openajar (default: the hostname) |
| `AGENTOS_GIT_URL` | yes | the repository to install from: the public openajar repository or your fork |
| `AGENTOS_GIT_REF` | no | a branch, tag or commit to install (default `main`) |
| `CONTROLLER_URL` | no | the controller to join over TLS, `wss://...`. Empty: join over SSH after the first boot (see "Join a controller") |
| `JOIN_TOKEN` | no | a single-use join token, valid one hour |

Each value is the text after the first `=` on its line: no quotes, no shell. Keep the filled-in
file private (`chmod 600`), outside the checkout, and never commit it.

`openajar node templates NAME` also marks which variables are required and which have defaults.

## Hetzner Cloud

1. Create an API token in the Hetzner Cloud console and make an hcloud context with it:
   `hcloud context create openajar`. The token stays in hcloud's own config. None of the scripts
   read, print or store it.
2. Add your public key to the project, under a name you choose:
   `hcloud ssh-key create --name laptop --public-key-from-file ~/.ssh/id_ed25519.pub`.
3. Pick a server type, a location and an image: `hcloud server-type list`, `hcloud location list`.
   The image is `ubuntu-24.04`. Nothing has a default, because prices and regions change.
4. Copy the template and fill it in:

   ```
   mkdir -p ~/openajar-nodes && cp deploy/templates/hetzner/cloud-init.yaml ~/openajar-nodes/hetzner-1.yaml
   chmod 600 ~/openajar-nodes/hetzner-1.yaml
   # set ADMIN_SSH_KEY (and NODE_NAME, AGENTOS_GIT_REF, CONTROLLER_URL if you need them)
   ```

5. Create the server. Every option is required:

   ```
   deploy/templates/hetzner/create.sh --name hetzner-1 --type TYPE --location LOCATION \
     --image ubuntu-24.04 --ssh-key laptop --user-data ~/openajar-nodes/hetzner-1.yaml
   ```

   The script refuses a file that is readable by others, still has `CHANGE-ME` in it, has no
   `#cloud-config` first line, or has no admin key. It passes the file to hcloud by path and
   writes nothing.
6. Wait for the first boot, then read the report. The first boot builds openajar, so allow 10 to
   20 minutes:

   ```
   ssh agentos-admin@$(hcloud server ip hetzner-1)
   cloud-init status --wait
   cat ~/openajar-doctor.txt
   ```

7. A doctor exit code of 0 means no check failed. Warnings are the next things to set up, such as
   no vault (`openajar vault init`) or no backup (`openajar backup create --to ...`). Run the doctor
   again any time with `sudo -u agentos OPENAJAR_HOME=/var/lib/agentos /opt/agentos/.venv/bin/openajar doctor`.

## Home box

Read `deploy/templates/home-box/README.md`. It covers the USB installer stick, the seed, the
install and the checks, step by step. In short: copy `autoinstall.yaml`, replace the `CHANGE-ME`
values (the password hash and the admin key), write the installer and the seed, boot the machine
from the installer, then `ssh agentos-admin@ADDRESS` and read `~/openajar-doctor.txt`.

## Mac mini (the controller)

1. Put the checkout on the Mac and `cd` into it.
2. Run `deploy/templates/mac-mini/setup.sh`. It sets up uv, the Python environment, the controller
   settings in `~/.openajar`, the LaunchAgent, and a self-check.
3. Run `deploy/templates/mac-mini/power.sh` once, so the Mac stays awake and starts after a power cut.
4. `OPENAJAR_HOME=~/.openajar .venv/bin/openajar controller agent-token` prints the line for Claude
   Code. Keep the token private.

`deploy/templates/mac-mini/README.md` says what runs on macOS today and what does not.

## Join a controller

A node joins a controller once. Make a join token on the controller, which prints the line to run:

```
openajar controller join-token --name hetzner-1
```

A token is single use and valid for one hour. Two ways to get the node to the controller:

- **Over SSH (keeps the token out of the server's user data).** The node cannot reach the
  controller's `localhost` port directly, so forward it. From your laptop, with the controller box
  as `controller-box`:

  ```
  ssh -N -L 7401:localhost:7401 controller-box     # terminal 1
  ssh -R 7401:localhost:7401 agentos-admin@NODE    # terminal 2: the node's localhost:7401 is your laptop's
  ```

  Then on the node, join and restart:

  ```
  sudo -u agentos OPENAJAR_HOME=/var/lib/agentos /opt/agentos/.venv/bin/openajar node join --self-hosted ws://localhost:7401 TOKEN
  sudo systemctl restart agentosd
  ```

- **TLS, set before the first boot.** If the controller sits behind a TLS reverse proxy
  (`public_url` and `behind_tls_proxy` in `controller.toml`, see docs/CONTROLLER.md), put
  `CONTROLLER_URL=wss://...` and a fresh `JOIN_TOKEN` into the file and create the node within an
  hour. The first boot joins and then blanks the token. The provider keeps user data, so prefer
  the SSH way when you can. A node joined this way keeps the hosted controller ceiling (posting
  up to commit, live views to watch only); for your own controller, set the `controller_*`
  settings in node.toml afterwards (docs/CONTROLLER.md, "The controller ceiling").

## What the first boot does, in order

`deploy/templates/common/bootstrap.sh`, run from a checkout of `AGENTOS_GIT_REF`:

1. Checks the variables. Nothing changes until they pass.
2. Creates the admin user with its key, locks its password, gives it sudo with no password (the
   key is the factor), and turns SSH to keys only with no root login. `sshd -t` runs before the
   reload, and a bad config is removed.
3. Turns on unattended security upgrades.
4. Runs `deploy/install-node.sh --systemd --user agentos --hardened` in the checkout.
5. Joins the controller if `CONTROLLER_URL` and `JOIN_TOKEN` are set, then blanks the token.
6. Runs `openajar doctor` as the `agentos` user and writes `~/openajar-doctor.txt` for the admin.

Its exit code is the doctor's. A failed install step stops the bootstrap where it failed, and no report is written. The full log on a Hetzner server is `/var/log/cloud-init-output.log`.
Run the first-boot script again after you change `/etc/agentos/bootstrap.env`:
`sudo /usr/local/sbin/agentos-first-boot`.

## Not in the templates

- A firewall. Only SSH should be reachable from outside. Turn on `ufw` (allow OpenSSH first) or
  use the provider's firewall.
- Backups off the node, a vault, proxies, phone access, and a TLS certificate for a controller.
  The doctor says which of these are missing.
- The hosted one-click flow for people who do not want to run any of this. That belongs to the
  paid edition, not this repository.

## Changing a template

```
uv run pytest tests/unit/test_node_templates.py tests/unit/test_ops_templates.py
cloud-init schema --config-file deploy/templates/hetzner/cloud-init.yaml   # when cloud-init is installed
```

The tests check the YAML, the variables against each manifest, the shell syntax, `create.sh`
against a fake hcloud, and that no em dash reaches a template or this page. CI checks the shell
syntax of every script under `deploy/templates`.
