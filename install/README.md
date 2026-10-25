# arista-field.py: an automated installer for Arista C-230 / C-260

One file, no console cable. It verifies the AP, downloads that AP's own firmware
and checks it, then either installs OpenWrt or puts the original firmware back.

## Getting it

```sh
curl -O https://raw.githubusercontent.com/WiFiGUY512/openwrt-arista-images/main/install/arista-field.py
python -m pip install paramiko
```

Needs Python 3 and `paramiko`. The images come from this repository's releases,
so nothing else has to be downloaded by hand.

```sh
python arista-field.py --host 192.168.1.191                 # menu: install or revert
python arista-field.py --host 192.168.1.191 --backup-only   # just save its firmware
```

This automates the console-and-TFTP procedure in the repository's main README:
the same RAM installer, the same `arista-c230-install`, but driven over the
network, with the AP's firmware saved first and the choice made afterwards.

---
`arista-field.py` puts OpenWrt on an Arista C-230 **or puts that AP's own
firmware back**, over the network, with no serial cable.

It is built around one rule: **an AP in the wild may be the only copy of its own
firmware.** So it downloads the AP's flash first, verifies every byte of it, and
only then offers to change anything.

Tested end to end on the bench unit (MAC `30:86:2d:73:b6:6f`) on 2026-10-25,
both directions:

| Run | Result |
|---|---|
| stock 8.9.0-63.1 → OpenWrt | installed, came up at its DHCP address, found by MAC |
| OpenWrt → its own firmware | slot 0 and environment read back **byte-identical** to the backup, booted Arista 8.9.0-63.1 |

## What it needs

- This PC on a network that can reach the AP (same subnet or routed to it).
- Python 3 with `paramiko`  (`python -m pip install paramiko`).
- **TCP 8069** open inbound (the AP posts its firmware to this PC).
- **UDP 69** free (U-Boot fetches the RAM installer over TFTP; nothing else in
  U-Boot can carry a 23 MB image).
- The AP on its factory firmware, reachable over SSH, in **online** mode (not
  `/tmp/offline`, which blocks the upgrade command the tool uses).
- DHCP on the AP's own segment. The RAM installer comes up as a DHCP client on
  LAN1; without a lease it is unreachable and nothing is written.

## Use

```sh
# 1. Look, and save the AP's firmware.  Writes nothing to the AP.
python arista-field.py --host 192.168.1.191 --backup-only

# 2. Do it, choosing at the menu
python arista-field.py --host 192.168.1.191
#      [1] install OpenWrt
#      [2] put this AP's original firmware back
#      [3] nothing

# Non-interactive forms
python arista-field.py --host 192.168.1.191 --install --yes --yes-install
python arista-field.py --host 192.168.1.191 --restore backups/c230-<mac>/<stamp> --yes

# Resume if a netboot worked but the tool was interrupted
python arista-field.py --installer-at 192.168.1.17 --yes --yes-install

# Check an AP without touching it
python arista-field.py --host 192.168.1.191 --verify-only
```

Every run writes a log and, when it downloads, a backup directory:

```
c230\field-run-YYYYmmdd-HHMMSS\run.log     screen output, verbatim
backups\c230-<mac12>\<stamp>\              the AP's firmware
    manifest.json                          model, version, MAC, per-partition sha256/md5
    SHA256SUMS
    mtdNN_<name>.bin.gz                    every physical partition
    u-boot-env.txt, proc-mtd.txt
```

`--skip-download` reuses a saved backup for that AP if it still verifies
(seconds instead of the full download). `--fast` skips the 256 MiB fallback slot.

## What it writes, exactly

1. **Two U-Boot environment variables** (`owrt_nb`, `bootcmd`) so U-Boot fetches
   the installer. `bootcmd` keeps the AP's own boot as its last step: if the
   fetch fails it restores `bootcmd`/`bootargs` and resets, so the AP always ends
   up on its own firmware rather than looping.
2. **The installer's own writes**, only after its checks pass: one environment
   write (`bootcmd`, `pri=1`), then NAND slot 0.
3. For a **revert**: NAND slot 0 (erased and written from the backup) and the
   NOR environment partition `0:APPSBLENV`. Slot 1 and every other NOR partition
   are left as found.

Each write is announced as it happens and totalled at the end.

## How it gets code in

```
stock firmware --SSH(config shell + prep_upgrade eval)--> root shell
root shell --gzip|curl POST--> this PC          backup of every partition
root shell --1 env write, reboot-->
U-Boot  --TFTP--> RAM installer (23 MB, ~30 s)
RAM installer --SSH--> factory.ubi (20 MB) + checks
RAM installer --ubiformat--> NAND slot 0
```

TFTP is used for exactly one hop, because U-Boot speaks nothing else. The
payload goes over SSH/SFTP, where TCP gives ordering, retransmission and an
integrity check; a truncated TFTP transfer has neither and would be a corrupt
install.

## If something fails

The tool prints, for every step, what it is doing, why, and what to try if it
fails, plus a progress line every few seconds so a slow step never looks dead.
The environment variables it writes are self-healing: **a failed fetch puts the
AP's own boot back and reboots into its firmware.** Running it again costs
nothing.

Recovery of last resort is the serial console (COM6 on the bench unit, see
`lab-uboot-boot.py` and `NOTES.md`), which can RAM-boot the installer by hand
without writing anything.

## Known limits

- **C-230 only.** `MODELS` in the script holds the per-model facts (FIT config,
  slot and environment partition names, installer name). The C-260 is next; its
  layout and command set must come from the C-260's own firmware, not the C-360's.
- Arista's injected shell lives about a minute before its own upgrade service
  gives up and reboots. The tool writes short commands one at a time and does not
  depend on long-lived shell state.
- The AP must be in online mode: `/tmp/offline` blocks the upgrade command.
- A wrong `--installer-at` looks like a hang; the tool asks for that address
  rather than guessing across subnets, and only scans the AP's own /24 if you
  leave it blank.

## Where the images come from

- **This PC** fetches the release from GitHub:
  `WiFiGUY512/openwrt-arista-images` (public -- verified 2026-10-25 with no
  credentials: API 200, nine assets, ~135 MiB, and the asset URLs download
  anonymously). Latest at the time of writing: `v2026.10.17.1-41-gb4ecbba`.
- **The AP never talks to GitHub, or to the internet at all.** U-Boot pulls the
  initramfs from this PC over TFTP, and `factory.ubi` goes to the RAM installer
  over SSH from this PC. A field AP can be on an isolated segment with no
  uplink, which is the normal case for an AP that is being recovered.
- Files are cached under `~/arista-openwrt-files/<tag>/` and every one is
  checked against the release's `SHA256SUMS` before use, including the
  `factory.ubi` again on the AP after transfer.
- No signal at the site? `--offline` uses the cached release and skips the
  GitHub API; without the flag the tool also falls back to the cache if GitHub
  cannot be reached. Copy `arista-openwrt-files\` to the laptop beforehand.

