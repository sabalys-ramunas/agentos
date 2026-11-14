# Running a node: install, harden, check, back up, upgrade

Everything here is the `agentos.ops` package and the files in `deploy/`. The browser profiles
and the vault are the crown jewels: a lost profile is a lost login. These tools exist so that a
dead disk, a bad upgrade or a dying proxy never costs one.

## Templates: one step to a green self-check

Hetzner Cloud, a home box (a USB installer) and a Mac mini controller each have a template in
`deploy/templates/`, with the variables to fill in, the create steps and the checks. See
[docs/NODE-TEMPLATES.md](../docs/NODE-TEMPLATES.md), or `agentos node templates`.

## Install

```
# production, as the unprivileged `agentos` user with the hardened unit (use this one)
sudo deploy/install-node.sh --systemd --user agentos --hardened

# production, as root: what a bare --systemd gives you, kept for nodes installed that way
sudo deploy/install-node.sh --systemd

# a dev copy: no /opt, no systemd (as root, or add --skip-apt once the packages are installed)
INSTALL_PREFIX=$PWD AGENTOS_HOME=$PWD/.agentos-home deploy/install-node.sh --dev
```

A bare `--systemd` runs agentosd **as root**, with no sandbox around the browsers. It stays the
default only so that `agentos upgrade` and a re-run of the installer never change how an existing
node runs. On a new node, always pass `--user agentos --hardened`. The installer prints a reminder
when it installs the root unit.

**The `agentos` command on the node.** With `--systemd` the installer writes `/usr/local/bin/agentos`,
a small wrapper that runs `/opt/agentos/.venv/bin/agentos` with the node's `AGENTOS_HOME`. Run as root,
it runs the command as the service user (so `sudo agentos doctor` or `sudo agentos lane list` read and
write the home as agentosd does); `upgrade`, `backup restore` and `migrate-service-user` stay root,
because they need it. The long form (`sudo -u agentos AGENTOS_HOME=/var/lib/agentos
/opt/agentos/.venv/bin/agentos ...`) still works. A file at that path the installer did not write is
left alone.

**The web UI.** The installer builds the panel and the web app when Node.js 22 is on the node. Without
Node.js it uses a build that came with the source (`panel/dist`, and a `web/dist` built from the same
sources), and with neither the node has **no web UI**: MCP, the API and the CLI work, and the panel
sign-in link shows a placeholder page. The installer says so at the end. To add one, install Node.js
22 and run the installer again, or build on your computer (`cd web && pnpm install && pnpm build`,
and `cd panel && npm ci && npm run build` in a checkout that has `panel/`), copy that checkout to the node with its builds
(`scripts/sync-node.sh --web DIR`, or rsync) and run the installer again from that copy.
`agentos connect HOST --install`, run from a checkout, sends that checkout with its builds.

`--user NAME` creates a system user (home = AGENTOS_HOME, shell `/usr/sbin/nologin`), gives it
AGENTOS_HOME (`chown -R`, `chmod -R go-rwx`), and keeps its Camoufox browser in
`AGENTOS_HOME/.cache` (copied from root's cache when there is one, else fetched). Xvfb, x11vnc
and the browsers then all run as that user. The code in the prefix stays owned by
root and read-only to the service. The installer refuses `--systemd` when the user cannot reach
its code or write its home (a prefix under `/root`, for example). To move an existing root node
over: stop agentosd, run the line above, start it; the vault key file and every profile change
owner with the home.

Other flags: `--no-restart` (install and enable the unit without starting it; `agentos upgrade`
uses it), `--yes` (restart even over lanes other sessions hold, below), `--render-unit PATH` (write
the unit as it would be installed, touch nothing in systemd; check it with `systemd-analyze verify
PATH`), `--backup-timer` (daily backups, below).

### Restarts and the lanes they close

A restart of agentosd closes every open lane: the browser stops gracefully (cookies and profile
kept, the page gone), claims end and calls in flight fail. On a node several agents share, that is
someone else's work, so each restart path names the open lanes and who is on them (claims, flow
and connector runs, a person in control, an agent driving it, calls in flight, live views) and
asks first when any lane has a holder:

- `agentos node restart` asks, then runs `systemctl restart agentosd`; `--yes` does not ask,
  `--check` only reports (exit 3 when a lane is held, for scripts).
- `agentos upgrade` and `agentos migrate-service-user` name them in their confirmation.
- `install-node.sh --systemd` on a running node does not restart over held lanes unless given
  `--yes`: it installs the code, says how to restart later and exits 3.

A plain `systemctl restart agentosd` cannot ask; agentosd then logs each lane it closes with its
holders and records a `closed_by_shutdown` lane event. A claim's holder gets `claim_lost` ("the node
restarted") on its next call, and every lane opens again from its profile on the next call that
needs it. Installing, upgrading or removing a connector needs no restart at all: agentosd serves
the new tool list within seconds and tells connected clients (`notifications/tools/list_changed`).

### The hardened unit (`agentosd-hardened.service`)

On: `User=`, `NoNewPrivileges`, no capabilities, `ProtectSystem=strict` with
`ReadWritePaths=AGENTOS_HOME`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`,
`ProtectProc=invisible`, kernel tunables, modules, logs, cgroups, clock and hostname protected,
`RestrictSUIDSGID`, `RestrictRealtime`, `LockPersonality`, `RestrictAddressFamilies` (unix, inet,
inet6, netlink), native syscalls only, `MemoryHigh=85%`, `MemoryMax=92%`, `OOMPolicy=continue`
(a browser killed for memory is relaunched by the lane manager; the service stays up).

Off on purpose: `RestrictNamespaces` (Firefox's own sandbox uses user namespaces, and so does
bubblewrap for terminal lanes), `MemoryDenyWriteExecute` (the JIT), `SystemCallFilter` (Firefox
needs more than `@system-service`), `ProcSubset=pid` (the health service reads `/proc/meminfo`).

With `PrivateTmp`, the X lock files live in the unit's own `/tmp`. The X servers' abstract
sockets and the viewer ports stay in the host's network namespace, so SSH tunnels work and
`agentos doctor` still sees which displays are taken. AGENTOS_HOME cannot live under `/home` or
`/root` with this unit.

### Logs

The daemon logs to the journal, one JSON object per line with the lane, workspace, grant, run,
request and tool ids that apply (agentos.core.logs), scrubbed of the node token, grant tokens,
proxy credentials and vault values before it is written. Read it with `agentos logs`, for
example `agentos logs --lane biz-a/instagram --since 10m --level warn` or `agentos logs -f
--run run_123 --json`. Run outside systemd, agentosd writes the same lines to
`AGENTOS_HOME/logs/agentosd.jsonl` (rotated by agentosd at 20 MiB, four kept) and a readable
line to the terminal; `agentos logs` reads those files when there is no journal.

`--systemd` installs `journald-agentos.conf` as `/etc/systemd/journald.conf.d/agentos.conf`
(at most 1 GiB, 2 GiB always free, three months) and `agentos.logrotate` as
`/etc/logrotate.d/agentos` for the display logs in `AGENTOS_HOME/logs/*.log` (weekly or at
20 MiB, four kept, compressed). Both units cap a log storm at 5000 lines per 30 seconds.

## `agentos doctor`

One read-only checklist with the exact fix for anything that is not right:

| Check | Passes when |
|---|---|
| config | node.toml exists, parses and has a token |
| os, python | Ubuntu 24.04 on x86_64 or arm64; Python 3.12+ in a venv |
| packages | xvfb, x11vnc, the fonts (bubblewrap and xclip: a warning only). The live view is served by agentosd itself, so noVNC and websockify are not needed. xclip reads what a lane copied, for the live view's copy to your computer: `agentos upgrade` installs no packages, so a node installed before it shows this warning until `sudo apt-get install -y xclip` |
| camoufox | the browser is installed; every pinned fingerprint names the same Firefox version |
| displays | enough free display and viewer slots in the configured ranges for max_warm_lanes; fails while an x11vnc of ours listens on TCP (one started before the unix socket: restart agentosd) |
| service | agentosd is active and enabled (a warning while it runs as root) |
| mcp | the MCP port answers 401 without the token and 2xx with it |
| permissions | home, secrets, lanes 700; node.toml, databases, secrets, fingerprints 600; one owner |
| vault | initialized; unsealed in the running daemon; the key file present |
| disk | AGENTOS_HOME and /tmp have 5 GiB and 10% free (fails under 1 GiB or 5%) |
| clock | NTP synchronized (TOTP and TLS need it) |
| memory | about 700 MiB per warm lane plus 512 MiB fits in RAM; little swap in use |
| proxies | each proxy accepts a connection and its exit IP is the proxy host (checked through it) |
| migrations | every database is at its latest schema |
| backups | a backup newer than 7 days exists in AGENTOS_HOME/backups |

`agentos doctor --json` is the same report for the panel and the TUI; `--offline` skips the
proxies, `--only disk,proxies` runs a subset. The exit code is 1 when anything fails. Nothing it
prints contains a secret, and it changes nothing: SQLite files are opened so that no `-wal` or
`-shm` file is left behind, and the MCP probe ends the session it opens. Run it as root or as
the service user (it reads the home).

## Backups

```
agentos backup create [--to DIR | --to s3://bucket/prefix] [--keep 7] [--passphrase-file F]
agentos backup verify ARCHIVE
agentos backup restore ARCHIVE [--to HOME] [--force] [--owner agentos]
agentos backup list [--to ...]
agentos backup prune --keep N [--to ...]
```

One backup is one file, `agentos-<node>-<UTC time>.tar.gz.age`: a gzipped tar encrypted with a
passphrase in the [age](https://age-encryption.org/v1) format (scrypt, ChaCha20-Poly1305 in
authenticated 64 KiB chunks). Without the passphrase it cannot be opened, so keep the passphrase
in a password manager. The stock tool opens it too: `age -d FILE > backup.tar.gz`.

The passphrase comes from `--passphrase-file` (or `AGENTOS_BACKUP_PASSPHRASE_FILE`), which must
be chmod 600, or from a prompt; never from the command line. At least 12 characters.

**In a backup:** node.toml (with the MCP token), `secrets/` (proxies, the vault key file,
`backup.pass`), every SQLite database (agentos.db, vault.db, every package's database, copied
with the SQLite backup API so one in use is still consistent), every lane's `profile/`
(cookies, logins, local storage, IndexedDB) and `fingerprint.json`, and every other file under
AGENTOS_HOME. A manifest with the size and SHA-256 of every file is the last member.

**Not in a backup:** `run/`, `logs/`, `backups/`, `tmp/`, top-level dot directories (`.cache`
with the service user's browser), `lanes/*/*/shots/`, browser caches inside profiles (`cache2`,
`startupCache`, `thumbnails`, `shader-cache`, `jumpListCache`, `OfflineCache`, `safebrowsing`,
`crashes`, `minidumps`, `datareporting`, `saved-telemetry-pings`; `--include-caches` keeps them),
profile lock files, symlinks, the code, the browser, apt packages and the unit (the installer
recreates those), and the passphrase of a passphrase-mode vault (never stored on the node).

**Consistency.** A running browser writes its profile, so each lane is copied while its browser
is down. A warm lane is paused through the daemon's ops socket: sent cold (cookies flushed),
copied to a private staging directory, and warmed again right away, a pause of seconds. A browser
that starts during the copy (an agent reopened the lane) makes the copy start over. With no
daemon to ask, a warm lane fails the backup unless `--allow-live` (then it is marked `live`).

**Safety.** The archive is written as `.partial-<name>` and renamed only when complete, so an
interrupted backup never looks like a backup. `verify` decrypts everything, checks every
checksum against the manifest and runs `PRAGMA integrity_check` on each database. `restore`
extracts into a staging directory beside the target and swaps it in only when every checksum
matched: a wrong passphrase, a damaged or truncated archive, or a path outside the home leaves
the target untouched. It refuses a non-empty home (`--force` moves the old one aside to
`<home>.before-restore-<time>`, it never deletes it) and a home a daemon is using. Afterwards
every directory is 700 and every file 600. They belong to `--owner` when it is given; else, run as
root, to the user that owns the home now, or to the user the installed agentosd unit runs as (so
a node with a service user gets its home back as that user); else to the restoring user. Because
it stages beside the home, it needs write access to the home's parent: the service user cannot
restore `/var/lib/agentos` itself, and is told to run it as root (`sudo agentos backup restore
ARCHIVE --force` with the node's wrapper).

**Retention.** `--keep N` (default 7) deletes this node's oldest backups beyond N in the target,
and touches only files named like a backup. Partial files older than a day are removed too.

**Off the node.** A backup in `AGENTOS_HOME/backups` does not survive a dead disk. Use
`--to /mnt/other-disk` or `--to s3://bucket/prefix` (AWS S3, Cloudflare R2, Backblaze B2, MinIO:
set `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, optionally `AWS_REGION` and
`AWS_ENDPOINT_URL`, or write `secrets/backup-s3.json` with `access_key_id`,
`secret_access_key`, `region`, `endpoint`, chmod 600). Single uploads, so up to 5 GiB per backup.

**Daily.** Put the passphrase in `AGENTOS_HOME/secrets/backup.pass` (chmod 600), then
`sudo deploy/install-node.sh --systemd --user agentos --hardened --backup-timer` installs `agentos-backup.timer` (04:00
plus up to an hour, catches up after downtime). Pass the same `--user` and `--hardened` flags the
node was installed with: a run without them installs the root unit. Edit `agentos-backup.service` to add `--to`.

### Restore onto a new node

```
git clone <the agentos repository URL> agentos && cd agentos && uv sync
sudo .venv/bin/python -m agentos.ops backup restore agentos-node-1-20261104T040000Z.tar.gz.age \
    --to /var/lib/agentos
sudo deploy/install-node.sh --systemd --user agentos --hardened
sudo -u agentos AGENTOS_HOME=/var/lib/agentos /opt/agentos/.venv/bin/agentos doctor
```

Restore first, into the empty home, then install: the installer keeps the restored node.toml and
token, gives the home to the service user and puts the browser back in `.cache` (backups leave it
out). The node keeps its name, token, proxies, vault and lanes; the lanes come up logged in with
the same pinned fingerprints. Proxies that only allow the old node's IP need the new IP allowed.
To restore over a node that already runs, stop agentosd first and pass `--force` (the old home is
moved aside), then run the installer again.

### Move one workspace to another node (or back to the laptop)

A backup is the whole node; an export is one workspace: its lanes (profiles and pinned
fingerprints, proxy labels), the workspace record and readiness, its vault entries, flows,
schedules and grant definitions, in one age-encrypted file (docs/WORKSPACES.md, "Moving a
workspace").

```
# old node: a passphrase (asked twice, or --passphrase-file / AGENTOS_EXPORT_PASSPHRASE_FILE),
# or an age key of yours: -r age1...
sudo -u agentos AGENTOS_HOME=/var/lib/agentos /opt/agentos/.venv/bin/agentos workspace export acme --to /tmp/
scp old-node:/tmp/agentos-acme-20261104T120000Z.agentos .

# new node: check it, then import it (lanes come in cold, schedules off)
agentos workspace import agentos-acme-20261104T120000Z.agentos --dry-run
agentos workspace import agentos-acme-20261104T120000Z.agentos --reissue-grants
```

- Warm lanes are paused for the copy through the daemon and warmed again, like a backup. Nothing
  on the old node is changed or deleted, so afterwards **delete or archive the lanes there** (or
  keep them cold): two warm copies of a lane look like one device in two places, which sites
  answer with checkpoints. The import prints that warning too.
- Proxy credentials stay on the old node unless `--with-proxies`. The new node needs the same
  labels (`agentos proxy add`), or `--map-proxy OLD=NEW`. A lane is never created without its
  proxy; `--allow-missing-proxy` gives it a placeholder that fails closed.
- Grant tokens never leave the node: `--reissue-grants` mints new ones from the definitions and
  prints each once. Webhook schedules get new URLs and secrets, printed once.
- The new node needs a vault (`agentos vault init`) that is not sealed. For a passphrase vault
  pass `--vault-passphrase-file`, or answer the prompt.
- From the panel: `POST /api/workspaces/{id}/export` streams the file and
  `POST /api/workspaces/import` takes it as an upload (owner only; docs/WORKSPACES.md, "API").

## Migrations

Each database has a `schema_version` table (package, version, name, applied_at). A package adds
`agentos/<package>/migrations.py` with
`MIGRATIONS = MigrationSet(package=..., db="<file>.db", migrations=(Migration(1, "name", up), ...))`;
versions run 1..n in order, each in its own transaction with its version row, so a rerun is a
no-op and a failed migration leaves its database as it was. Before anything runs, every database
that will change is copied to `AGENTOS_HOME/backups/migrations/<time>/` (the last five are kept).
A database written by a newer agentos is refused. `agentos migrate status` and
`agentos migrate run [--dry-run]`.

Lane directories have a format version too, in `lanes/<ws>/<name>/lane-format.json`
(agentos/node/lanefmt.py). Version 1 is the layout lanes had before the file existed, so the first
`agentos migrate run` only writes the file. `migrate run` handles the databases first, then every
cold lane: a lane agentosd has warm (it holds a shared lock on `.lane-format.lock`), or one a
browser runs, is skipped and left for the next run. Each step copies what it changes to
`AGENTOS_HOME/backups/lane-migrations/<time>/<ws>/<name>/` first, and the version file is
replaced after each step, so an interrupted run resumes where it stopped. agentosd does not warm a
lane whose format is newer than it knows, or older with a step pending (`lane_format`). An
upgrade that rolls back puts the lane files back too.

## Upgrade

```
cd ~/agentos && git pull
sudo /opt/agentos/.venv/bin/agentos upgrade --source ~/agentos --user agentos --hardened   # the flags the node was installed with
```

It copies the whole prefix (code and venv) to `<prefix>.rollback`, drains the lanes through the
ops socket (each closes gracefully after its current tool call), stops agentosd, runs the new
installer with `--no-restart`, runs the new code's migrations, starts agentosd and waits up to
90 seconds for it to be active with the MCP port answering the token. If any of that fails it
stops agentosd, puts the old prefix and the pre-migration databases back, starts it, checks
again, and says which state the node is in. `--no-service` upgrades a dev install.

## Health service

Runtime service `health` (`agentos.ops.service`), inside agentosd. Every minute: disk under
AGENTOS_HOME and /tmp, RAM and swap, browser version drift; every fifth round: every proxy's
reachability and exit IP, and the exit IP of every warm lane with a proxy. A warm lane whose
exit IP is not its proxy's is quarantined (or sent cold, so the self-check runs before it can
warm again). A dead proxy never quarantines anything: the browser cannot fall back to the node's
IP. Changes of level go to the `notifier` service once (and again on recovery).
`agentos health check` and `agentos health status` show it.

## Browser version drift

A pinned fingerprint keeps announcing the Firefox it was pinned with; after a Camoufox update
the UA lags the engine, which is a signal in itself. `agentos doctor` and the health service
flag it, and

```
agentos lane refresh-fingerprint personal/reddit --keep-device
```

changes only the `rv:<N>.0` and `Firefox/<N>.0` tokens of `navigator.userAgent` and
`headers.User-Agent` to the installed Camoufox's version (or `--version N`). Every other pinned
value stays byte for byte: platform, oscpu, appVersion (Firefox puts no version in it), screen,
fonts, voices, WebGL, media devices, timezone, locale. The old file is kept as
`fingerprint.json.v<old>.bak`; a warm lane picks the change up the next time it warms.
