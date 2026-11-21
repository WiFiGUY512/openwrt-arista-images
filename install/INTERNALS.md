# How the network installer works

This is the detail behind [README.md](README.md): how `arista-field.py` gets
into an AP, what it writes, and what differs per model. To install an AP you
do not need any of it.

`arista-field.py` puts OpenWrt on an Arista access point **or puts that AP's
own firmware back**, over the network, with no serial cable.

It is built around one rule: **an AP in the wild may be the only copy of its own
firmware.** So it downloads the AP's flash first, verifies every byte of it, and
only then changes anything. The one question it asks comes before that, so
nobody has to sit and wait for a prompt; the order of the work is unchanged.

## Tested on real units

| Model (Arista firmware) | Run | Result |
|---|---|---|
| C-230 (8.9.0-63.1) | stock → OpenWrt, twice (2026-10-25) | installed, came up at its DHCP address, found by MAC |
| C-230 (8.9.0-63.1) | OpenWrt → its own firmware, twice | slot 0 and environment read back **byte-identical** to the backup, booted Arista 8.9.0-63.1 |
| C-260 (11.0.1-49.8) | `--backup-only` (2026-10-25) | 26 partitions, ART included, each matching the AP's own hash |
| C-260 (11.0.1-49.8) | stock → OpenWrt (2026-10-25, and again with release 42 on 2026-10-27) | installed, Arista kept in slot 1; three radios up, board data and Bluetooth keys taken from Arista's slot |
| C-260 (11.0.1-49.8) | OpenWrt → its own firmware (2026-10-25) | slot 0 and environment read back **byte-identical**, booted Arista 11.0.1 |
| C-360 (20.0.0-179) | stock → OpenWrt (2026-11-15, release 42, tool 2.0) | 28 partitions saved (two in use, kept as read); installed in 22 minutes, Arista kept in slot 1; four radios, the Wi-Fi 6E firmware taken from Arista's slot. Not yet: OpenWrt → its own firmware |
| C-460 (18.2.0-32) | `--backup-only --fast` (2026-11-12); `--ramboot` by kexec, install three times, the fallback, and OpenWrt -> its own firmware twice (2026-11-13) | 29 partitions, each matching the AP's own md5; installed in about 3 minutes; U-Boot fell back to Arista in slot 1 unattended when OpenWrt's kernel was broken on purpose; slot 0 and the environment read back **byte-identical** to the backup |

## What it needs

- This PC on a network that can reach the AP (same subnet or routed to it).
  Finding the AP by itself needs the same subnet; a routed AP is named with
  `--host`.
- Python 3.8 or newer with `paramiko` (the tool offers to install it; the
  one-line installers keep a private copy).
- **TCP 8069** open inbound (the AP posts its firmware to this PC).
- **UDP 69** free (U-Boot fetches the RAM installer over TFTP; nothing else in
  U-Boot can carry a 23 MB image). Not on the C-460, which fetches over HTTP
  from a port in 8069-8079.
- The AP on its factory firmware, reachable over SSH, in **online** mode (not
  `/tmp/offline`, which blocks the upgrade command the C-230's way in uses).
- DHCP on the AP's own segment. The RAM installer comes up as a DHCP client on
  LAN1; without a lease it is unreachable and nothing is written.

Both ports are tried before the AP is touched, so a port that is taken stops
the run with nothing changed.

## The order of a run

1. **Find and identify.** With no `--host`, the PC's own /24 networks are swept
   for SSH, and only hosts whose MAC address has one of the prefixes these APs
   carry are asked who they are. An AP on Arista's firmware is asked `show
   device info` at its own CLI (`version` on older firmware); an AP on OpenWrt
   is asked for its board name. No root shell is taken and nothing changes.
2. **Ask once.** The menu, then one yes/no question that names the build.
3. **Fetch the release** and check it, before the AP is touched. For the
   C-460, its three kexec helper files too.
4. **Connect and verify**: the root shell, then the model, firmware, flash
   layout and boot variables. Anything unexpected stops the run.
5. **Save the firmware**, every partition, each checked against the AP's hash.
6. **Start the RAM installer, install, and wait** for the AP, which is found
   again by its MAC address.

Useful options beyond those in the README:

```sh
# Resume if the installer started but the tool was interrupted
python arista-field.py --installer-at 192.168.1.17

# Check an AP against what the tool expects, and write nothing
python arista-field.py --host 192.168.1.191 --verify-only

# Reuse this AP's saved firmware if it still verifies; skip the fallback slot
python arista-field.py --host 192.168.1.191 --install --skip-download --fast

# A unit whose password was changed (Arista: the account is 'config')
python arista-field.py --host 192.168.1.191 --user config --password <pw>
```

Every run writes a log and, when it saves firmware, a backup directory, both
next to `arista-field.py`:

```
logs/field-run-YYYYmmdd-HHMMSS/run.log    everything, with the time
backups/c230-<mac12>/<stamp>/             the AP's firmware
    manifest.json                          model, version, MAC, per-partition sha256/md5
    SHA256SUMS
    mtdNN_<name>.bin.gz                    every physical partition
    u-boot-env.txt, proc-mtd.txt
```

The screen shows one line per step and a progress line; `--verbose` shows what
the log has.

## What it writes, exactly

1. **Two U-Boot environment variables** (`owrt_nb`, `bootcmd`) so U-Boot fetches
   the installer. `bootcmd` keeps the AP's own boot as its last step: if the
   fetch fails it restores `bootcmd`/`bootargs` and resets, so the AP always ends
   up on its own firmware rather than looping.
2. **The installer's own writes**, only after its checks pass. On the NAND
   models (C-230/C-260/C-360): one environment write (`bootcmd`, `pri=1`), then
   NAND slot 0. On the eMMC model (C-460): one environment write (the boot
   command — OpenWrt from slot 0, falling back to Arista's slot 1 at `pri=1`),
   then `sysupgrade` writes the kernel to `0:HLOS` and the root filesystem to
   `rootfs` on eMMC. Either way Arista's second slot is left untouched.
3. For a **revert**: the saved slot 0 and the NOR environment partition
   `0:APPSBLENV`. On the NAND models slot 0 is erased and written with
   `flash_erase`/`nandwrite` (two aarch64 mtd tools fetched beside the script
   and checked against the published `SHA256SUMS`); on the C-460 each eMMC slot-0
   partition is written back with `dd` (a block device needs no erase). Slot 1
   and every other partition are left as found. The install path needs nothing
   extra.

Each write is announced as it happens and totalled at the end.

## How it gets code in

```
stock firmware --SSH(config shell + its own radartool)--> root shell
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

The log records, for every step, what it is doing, why, and what to try if it
fails; the screen shows the step and a progress line, so a slow step never
looks dead. When a run stops, the screen says what happened, what to do, and
whether anything was written.
The environment variables it writes are self-healing: **a failed fetch puts the
AP's own boot back and reboots into its firmware.** Running it again costs
nothing.

Recovery of last resort is the serial console ([BY-HAND.md](BY-HAND.md)), which
can RAM-boot the installer by hand without writing anything.

## Known limits

- **Models.** Install and revert with no console: the C-230, C-260 and C-460,
  each proven on a real unit, and the C-360 (installed on a unit; its revert
  not yet run on one), and the W-118, W-318 and C-200 (each installed twice and
  switched back to Arista once on a unit), and the C-130 (installed once on a
  unit, its fallback to Arista's second slot seen to start; the switch back
  with the tool not yet run on one, and no published build carries it yet). A model whose way into Arista's
  firmware has not been proven on a unit is marked `field_install: False`:
  it is identified, updated when it already runs OpenWrt, its images fetched
  and verified, and its first install refused with the by-hand page
  ([BY-HAND.md](BY-HAND.md)). No model is marked so today. `MODELS` in the script holds the per-model facts
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
- A wrong `--installer-at` looks like a hang. Without it the installer is found
  by its DHCP name and its MAC address, then by a scan of the AP's own /24.

## Where the images come from

- **This PC** fetches the release from GitHub:
  `WiFiGUY512/openwrt-arista-images` (public; the assets download anonymously).
  Releases come out one per build and not every one carries every model, so
  **without `--tag` the tool takes the newest accepted release that actually
  has this AP's model**, newest first by publication date. Accepted means not
  marked as a pre-release on GitHub: new builds go out as pre-releases (testing)
  and are promoted after a soak. A model with no accepted release yet gets the
  newest testing one, and the question says so. `--list-releases` shows what
  each release has; `--tag T` pins one (and stops if it lacks the model);
  `--channel testing` takes the newest build of either kind and `--channel
  stable` never takes a testing one. An AP that already runs OpenWrt is never
  offered an older build than it runs. [STATUS.md](../STATUS.md) lists every
  build. Where a release carries an `index.json`, the tool checks the files
  against it as well as against `SHA256SUMS`.
- **The AP never talks to GitHub, or to the internet at all.** U-Boot pulls the
  initramfs from this PC over TFTP, and `factory.ubi` goes to the RAM installer
  over SSH from this PC. A field AP can be on an isolated segment with no
  uplink, which is the normal case for an AP that is being recovered.
- Files are cached under `releases/<tag>/` next to the tool and every one is
  checked against the release's `SHA256SUMS` before use, including the
  `factory.ubi` again on the AP after transfer.
- No signal at the site? `--offline` uses the cached release and skips the
  GitHub API; without the flag the tool also falls back to the cache if GitHub
  cannot be reached. Run it once with a connection first, or copy `releases/`
  to the laptop beforehand.

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
- **The backup stays on the PC.** The C-260's own installer can take a backup
  to a drive on the AP; this tool passes `--no-backup` and keeps the backup
  here instead, so `/tmp` on the AP is enough.
- **The way in works on the C-260 too.** Its factory shell is a readable script,
  and its own firmware has the same chain this tool uses -- checked on
  2026-10-25 in the slot-1 image of a C-260 backup, firmware 11.0.1-49: `sensor-shell.sh:533`
  dispatches `upgrade`, its argument filter covers only the first three words of
  the typed line, `do_upgrade.sh:179` reads the server address/path unfiltered,
  `do_upgrade.sh:424` passes it to `prep_upgrade.sh` as argument 8, and
  `prep_upgrade.sh:373` evals it. That build's `do_upgrade.sh` has no
  `/tmp/offline` guard, so that C-230 failure mode does not exist on it. (The
  tool prefers the CLI's `radartool` command, which is not an eval at all; the
  wizard above is the fallback, and the only unit seen without radartool is the
  C-230 at 8.9.0.) Its
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

## C-460

The first eMMC model, and the first Wi-Fi 7 one (IPQ9574). Same command, same
file; the model is read from the AP.

What is per-model lives in `MODELS`:

| | C-460 |
|---|---|
| storage | eMMC (GPT), with the U-Boot environment in SPI NOR |
| FIT configuration | `config@1`, loaded at `0x44000000` |
| install image | `…-squashfs-sysupgrade.bin` (no `factory.ubi`) |
| slot 0 | `0:HLOS` (kernel) + `rootfs` + `rootfs_data` |
| slot 1 (fallback) | `0:HLOS_1` + `rootfs_1` + `rootfs_data_1`, left untouched |
| NOR total | 32 MiB, checked |

Notes that matter for the C-460:

- **eMMC, not NAND.** The slots are GPT partitions on `mmcblk0`, so the install
  is `sysupgrade` (kernel FIT to `0:HLOS`, squashfs to `rootfs`) rather than
  `ubiformat`, and a revert writes each slot-0 partition back with `dd` — a block
  device needs no erase. The environment still lives in NOR (`0:APPSBLENV`), so a
  revert writes it the NOR way. The real layout is checked by `test-c460.py`.
- **One boot-command write.** The install writes a single `fw_setenv` script:
  U-Boot reads `0:HLOS` from LBA `0x22` into RAM and boots it, and if that FIT is
  missing or bad it runs Arista's own boot at `pri=1` — Arista's slot 1, which
  the install never touches. So the fallback is the AP's own firmware, as on the
  other models, but through `pri` rather than a rewritten `bootcmd`.
- **`pri`.** Arista runs a C-460 from slot 0 (`pri=0`): the install replaces the
  system that runs now, and the backup taken in step 3 is what puts it back. The
  tool says so before it writes.
- **The way in is the CLI's `radartool`**, the same evaluated-argument shell as
  the other models — confirmed on a C-460 at firmware 18.2.0-32 on 2026-11-12
  (`config`/`config`, `show device info` names the model). `sftp-server` is
  absent, so the image is pushed over a plain SSH channel, as on the C-260.
- **RAM boot, no install (`--ramboot`).** The C-460 can be booted into OpenWrt
  from RAM over the network, to try a build without committing it to flash:

  ```sh
  python arista-field.py --host 192.168.1.50 --ramboot                 # a release
  python arista-field.py --host 192.168.1.50 --ramboot --image out.itb # a local build
  ```

  It connects, verifies (read-only), and starts the initramfs **from Arista's
  running firmware with kexec**: stock's kernel has no kexec but loads unsigned
  modules, so a kexec module built for it (`kexec/c460/`, from
  `c460/kexec/build.sh`) loads our image and jumps to it. The AP pulls the files
  from this PC over HTTP. **Nothing is written** — no eMMC, no U-Boot environment
  — and a plain reboot or a power-cycle returns the unit to its own firmware.
  **Confirmed 2026-11-13**: OpenWrt in RAM, found by MAC, 34 s after the jump.

  The kexec'd installer is deliberately minimal, because of what stock leaves
  behind: one CPU, no Wi-Fi, the RPM off (no cpufreq), SPI in FIFO and the eMMC in
  PIO (~16 MB/s; `0:HLOS` read back md5-exact). The tool builds its device tree
  from the image's own (memory from the unit's live DT, a `no-map` for each of
  stock's carve-outs), and refuses if the AP's kernel is not the module's
  (`5.4.213`). Details and the bench log: `c460/kexec/README.md`.

  **Arista's failsafe:** more than 5 warm reboots within 30 minutes trip stock's
  failure detector, which **resets the AP config to factory defaults** and enters
  failsafe, where there is no way in. The tool stops if it finds an AP in
  failsafe; `failsafe recover` in Arista's CLI leaves it. For a test series, run
  `no failsafe` first and `failsafe` after (each reboots the AP).

  (The C-460 never uses the U-Boot TFTP netboot the NAND models use: it did not
  fetch unattended on a unit, because the 10 G PHY links 18-129 s after reset.)
- **Back to Arista from OpenWrt.** `--host <the AP on OpenWrt> --restore <backup dir>`.
  U-Boot cannot netboot here, so the RAM installer is written into slot 0's
  kernel partition (`0:HLOS`) and booted by the boot command the install wrote;
  no network boot, no environment write. From that moment the AP boots the
  installer, until the restore (which follows at once) or a new install.
- **Arista's settings do not come back.** `rootfs_data` is Arista's ext4 settings
  partition, and the backup can only read it while Arista has it mounted, so the
  saved copy is not a filesystem that mounts (Arista's firmware has no `fsfreeze`).
  Written back as it is, Arista's boot stops at a `mkfs` prompt on the serial
  console, for good -- seen on the unit. The restore therefore leaves that
  partition blank; Arista formats it on its first boot and starts from its
  defaults (and its own `CONFIG` partition, which is never touched). The saved
  copy stays in the backup.
- **Status.** The way in, the layout read, and `--backup-only --fast` are
  confirmed on a unit (2026-11-12), and `--ramboot` by kexec (2026-11-13); the
  eMMC **install** and **revert** are both **confirmed on a unit (2026-11-13)**,
  network-only: install twice (~3 min each: backup, kexec installer, one env
  write, `sysupgrade` to slot 0), then reboot, sysupgrade and factory reset on the
  installed system, the **fallback** (OpenWrt's kernel broken on purpose: U-Boot
  fell through to Arista in slot 1 unattended), and the revert twice -- from
  OpenWrt and from Arista in slot 1 (~5 min, slot 0 and the environment read back
  byte-identical to the backup). First run on any C-460
  should be `--backup-only`, which writes nothing.
