# arista-field.py: an automated installer for Arista C-230 / C-260

One file, no console cable, one script for every model. It verifies the AP,
downloads that AP's own firmware and checks it, then either installs OpenWrt or
puts the original firmware back.

## Getting it

Published here: <https://github.com/WiFiGUY512/openwrt-arista-images/tree/main/install>
(script: <https://raw.githubusercontent.com/WiFiGUY512/openwrt-arista-images/main/install/arista-field.py>).

```sh
curl -O https://raw.githubusercontent.com/WiFiGUY512/openwrt-arista-images/main/install/arista-field.py
python -m pip install paramiko
```

Needs Python 3 and `paramiko`. The images come from this repository's releases,
so nothing else has to be downloaded by hand.

If it has to run without a terminal (a script, a CI job), say what to do on the
command line: `--install`, `--restore DIR`, `--backup-only`, `--yes`,
`--yes-install`. A prompt with no terminal to ask on is an error, not a wait.

---

## Tested on real units

| Model (Arista firmware) | Run | Result |
|---|---|---|
| C-230 (8.9.0-63.1) | stock → OpenWrt, twice (2026-10-25) | installed, came up at its DHCP address, found by MAC |
| C-230 (8.9.0-63.1) | OpenWrt → its own firmware, twice | slot 0 and environment read back **byte-identical** to the backup, booted Arista 8.9.0-63.1 |
| C-260 (11.0.1-49.8) | `--backup-only` (2026-10-25) | 26 partitions, ART included, each matching the AP's own hash |
| C-260 (11.0.1-49.8) | stock → OpenWrt (2026-10-25, and again with release 42 on 2026-10-27) | installed, Arista kept in slot 1; three radios up, board data and Bluetooth keys taken from Arista's slot |
| C-260 (11.0.1-49.8) | OpenWrt → its own firmware (2026-10-25) | slot 0 and environment read back **byte-identical**, booted Arista 11.0.1 |
| C-360 | (none yet) | in the tool, not yet run on a unit |

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

# Wait for the AP afterwards and check it, instead of leaving that to you
python arista-field.py --host 192.168.1.191 --install --verify

# What each release carries, and which one this tool would take (no AP needed)
python arista-field.py --list-releases
python arista-field.py --host 192.168.1.191 --channel testing   # include pre-releases

# Check an AP without touching it
python arista-field.py --host 192.168.1.191 --verify-only

# A unit whose password was changed (Arista: the account is 'config')
python arista-field.py --host 192.168.1.191 --user config --password <pw>
```

Every run writes a log and, when it downloads, a backup directory:

```
field-run-YYYYmmdd-HHMMSS\run.log         screen output, verbatim
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
   are left as found. This path needs two aarch64 mtd tools; if they are not
   beside the script, it fetches them from this repository and checks them
   against the published `SHA256SUMS`. The install path needs nothing extra.

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

- **Models.** Install and revert with no console: C-230 and C-260, both proven
  on real units, and the C-360 (in the tool, not yet run on a unit). The W-318,
  W-118 and C-200 are listed and their images fetched and verified, but their
  first install is by console. `MODELS` in the script holds the per-model facts
  (FIT config, slot and environment partition names, installer name), each taken
  from that model's own firmware.
- **Arista firmware versions.** The way in was read in, and run on, C-230
  8.9.0-63.1 and C-260 11.0.1-49.8. Another Arista version may differ:
  `--backup-only` shows whether the tool gets in, and writes nothing.
- **Used units.** The factory account is `config`, password `config`. A unit
  that was centrally managed before may have had it changed: give it with
  `--user config --password <pw>`, or reset the AP to factory defaults first.
- Arista's injected shell lives about a minute before its own upgrade service
  gives up and reboots. The tool writes short commands one at a time and does not
  depend on long-lived shell state.
- The AP must be in online mode: `/tmp/offline` blocks the upgrade command.
- A wrong `--installer-at` looks like a hang; the tool asks for that address
  rather than guessing across subnets, and only scans the AP's own /24 if you
  leave it blank.

## Where the images come from

- **This PC** fetches the release from GitHub:
  `WiFiGUY512/openwrt-arista-images` (public; the assets download anonymously).
  Releases come out one per build and not every one carries every model, so
  **without `--tag` the tool takes the newest release that actually has this
  AP's model**, newest first by publication date. `--list-releases` shows what
  each one has; `--tag T` pins one (and stops if it lacks the model);
  `--channel testing` also accepts pre-release builds. New builds go out as
  testing first and become stable after a soak, so the newest build is usually
  on the testing channel; [STATUS.md](../STATUS.md) lists both. Where a release carries
  an `index.json`, the tool checks the files against it as well as against
  `SHA256SUMS`.
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

## C-260

Same command, same file; the model is read from the AP.

```sh
python arista-field.py --host 192.168.1.191 --backup-only   # save it first
python arista-field.py --host 192.168.1.191                 # then choose
```

What is per-model lives in `MODELS` and nowhere else:

| | C-230 | C-260 |
|---|---|---|
| FIT configuration | `config@ac02` | `config@hk01` |
| slot 0 name | `rootfs` | `ubi` in 25.12's tree, `rootfs` in main's (both accepted) |
| installer | `arista-c230-install` | `arista-c260-install` |
| NOR total | 32 MiB, checked | 32 MiB, checked (measured from a C-260's /proc/mtd) |

Notes that matter for the C-260:

- **The partition tables differ in order.** The C-230 lists its NOR first and
  its NAND slots last; the C-260 lists slot 0, slot 1, then the NOR. Nothing may
  depend on position: the tool tells NOR from NAND by erase size (64 KiB against
  128 KiB) and skips the UBI volume pseudo-devices a running system exposes.
  Both models' real tables are checked by `test-layout.py` (next to this file).
- **`pri`.** Arista normally runs a C-260 from slot 1 (`pri=1`), so slot 0 is
  genuinely spare and the install replaces the spare. With `pri=0` the running
  Arista is in slot 0: the installer warns, and the backup taken in step 3 is
  what puts it back.
- **No USB stick needed.** The C-260's installer wants a stick when it takes its
  own backup; this tool passes `--no-backup` and keeps the backup here instead,
  so `/tmp` on the AP is enough.
- **The way in works on the C-260 too.** Its factory shell is a readable script,
  and its own firmware has the same chain this tool uses -- checked on
  2026-10-25 in the slot-1 image of a C-260 backup, firmware 11.0.1-49: `sensor-shell.sh:533`
  dispatches `upgrade`, its argument filter covers only the first three words of
  the typed line, `do_upgrade.sh:179` reads the server address/path unfiltered,
  `do_upgrade.sh:424` passes it to `prep_upgrade.sh` as argument 8, and
  `prep_upgrade.sh:373` evals it. That build's `do_upgrade.sh` has no
  `/tmp/offline` guard, so that C-230 failure mode does not exist on it. Its
  `version` command prints what this tool parses (`The device model is C-260.`,
  `Device Build: [...]`), so the model is read, not asked for.
- **Status.** Proven on a C-260 (Arista 11.0.1-49.8): backup, install and
  revert end to end on 2026-10-25, and installed again with release 42 on
  2026-10-27. From release 42 on, the C-260's installer takes `--no-backup`
  like the C-230's; for an older installer the tool stages on a 768 MiB tmpfs
  instead, which also works but takes longer. First run on any new unit should
  still be `--backup-only`, which writes nothing.
- The fallback is the same shape as the C-230's: the netboot `bootcmd` restores
  Arista's own boot and resets if the TFTP fetch fails.
