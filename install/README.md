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
- **Status.** Still to run on a real C-260. Everything the installer then does
  is the C-260's own tested script, but the round trip through this tool (env
  write, TFTP fetch, RAM installer, slot 0, first boot) has not been done on
  that model. First run should be `--backup-only`, which writes nothing.
- The fallback is the same shape as the C-230's: the netboot `bootcmd` restores
  Arista's own boot and resets if the TFTP fetch fails.
