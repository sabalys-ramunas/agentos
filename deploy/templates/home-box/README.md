# Home box: Ubuntu 24.04 from a USB stick

This template installs Ubuntu 24.04 Server on a machine at home without touching a keyboard
after the first boot. The installer reads `autoinstall.yaml` (Ubuntu's autoinstall format,
which cloud-init also reads). On first boot the same bootstrap as the Hetzner template runs:
keys-only SSH, unattended security upgrades, openajar installed with
`deploy/install-node.sh --systemd --user agentos --hardened`, and `openajar doctor`, whose report
is `~/openajar-doctor.txt` for the admin user.

**The installer erases the largest disk in the machine.** Unplug every other disk you care about
before you boot it. Test the stick in a virtual machine first (UTM, QEMU or VirtualBox with a
blank disk). The steps below have not been run on real hardware yet.

## Before you start

- A machine with Ethernet (DHCP) and an x86_64 or arm64 processor. The doctor's memory check
  says how many browser lanes fit in its RAM.
- Your SSH public key (`cat ~/.ssh/id_ed25519.pub`).
- A second USB stick (at least 1 GB) for the seed, and a stick of at least 4 GB for the
  installer.
- The Ubuntu 24.04 Server ISO from ubuntu.com/download/server, with its SHA256SUMS checked.

## 1. Fill in the seed

Copy `autoinstall.yaml` outside the checkout and `chmod 600` the copy. Replace every `CHANGE-ME`
and `PASSWORD_HASH`:

- `identity.password`: the output of `openssl passwd -6`. The password is never used for SSH,
  but the installer needs a hash.
- `ssh.authorized-keys` and `ADMIN_SSH_KEY` (in the `bootstrap.env` block): your public key,
  one line, the same key in both places.
- `identity.username` and `ADMIN_USER`: the same name. The default is `agentos-admin`.
- `identity.hostname` and `timezone`: this machine's name and your time zone.

Then optionally fill in `CONTROLLER_URL` and `JOIN_TOKEN` (a token is valid for one hour, so
make it just before you install). Leaving the token empty and joining over SSH is safer,
because the token then never sits in a file on the stick. `docs/NODE-TEMPLATES.md` explains both.

## 2. Make the installer stick and the seed

The installer finds its seed on a volume labelled `CIDATA`. The simplest layout is two sticks:

1. **Installer stick**: write the Ubuntu ISO to it with Balena Etcher, Rufus (Windows) or `dd`
   (Linux, or macOS after `diskutil list` shows the right disk). `dd` writes straight over the
   disk you name, so check the name twice.
2. **Seed stick**: format a stick as FAT32 with the label `CIDATA`, then copy the filled-in file
   onto it twice: once as `user-data` and once as an empty `meta-data` file. The file must start
   with `#cloud-config`, which `autoinstall.yaml` already does.

Ubuntu's documentation describes a second route: put the seed in a `nocloud` folder inside a
remastered ISO and add `autoinstall ds=nocloud\;s=/cdrom/nocloud/` to the boot arguments. Try
that only in a virtual machine first.

## 3. Install

1. Put both sticks in, power on, and open the boot menu (the key depends on the machine,
   often F12, F11 or Esc). Boot the installer stick.
2. If the installer asks "Continue with autoinstall? (yes|no)", answer `yes`. If it stops at
   an interactive question instead, the seed was not found: check the `CIDATA` label and the
   file names.
3. The installation takes about 15 minutes and reboots. Remove the sticks when it asks.
4. The first boot builds openajar. Give it 15 to 30 minutes on a slow machine.

## 4. Check

Find the machine's address on your router (or with `ip addr` on its screen), then:

```
ssh agentos-admin@ADDRESS
cloud-init status --wait
cat ~/openajar-doctor.txt
```

Exit code 0 in the report means no check failed. Warnings (for example "no vault yet" and "no
backup yet") are the next things to set up, not install errors. An install error shows as
`cloud-init status: error`; the full log is `/var/log/cloud-init-output.log` (root only).

If the SSH key was wrong, you cannot sign in over SSH. Use the machine's own console to fix
`/etc/agentos/bootstrap.env` (it is root-only), then run `sudo /usr/local/sbin/agentos-first-boot`
again.

## 5. Join a controller

The bootstrap joins the controller when `CONTROLLER_URL` and `JOIN_TOKEN` are set, then blanks
the token. Otherwise join over SSH as described in `docs/NODE-TEMPLATES.md`, and restart
`agentosd` afterwards.

## Notes

- The template does not configure a firewall. Only SSH (port 22) should be reachable from your
  network. The MCP port and the controller stay on 127.0.0.1, so they are not exposed, but turn on
  `ufw` (allow OpenSSH first) or your router's firewall for the rest.
- Keep the machine powered on. Most BIOS and UEFI setups have a "restore on AC power loss"
  option; turn it on so the node comes back after a power cut.
- Back it up off the machine (`openajar backup create --to ...`) before you rely on it for
  logins. The doctor warns until a backup exists.
