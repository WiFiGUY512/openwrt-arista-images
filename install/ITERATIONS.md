# Iterations of `arista-field.py`

Every entry below is a change forced by something that actually happened on a
unit, not a tidy-up. The commit ids are in the public repo
`WiFiGUY512/openwrt-arista-images`, under `install/`.

## 2026-10-25 — first day, C-230

| # | What changed | Why |
|---|---|---|
| 1 | one script, `MODELS` per model | replaces the hand-run console procedure and the earlier `arista-net-install.py`, which needed a DHCP server of its own and served the payload over HTTP |
| 2 | backup over the network, the AP pushes | Arista's sshd accepts `exec` channels and returns nothing (rc 0, no output), so nothing can be pulled from the AP; it gzips each partition and POSTs it to this PC, and the PC compares against the AP's own `md5sum` of the same device |
| 3 | `md5sum`, not `sha256sum`, on stock | the factory image has no `sha256sum` |
| 4 | netboot as two short `fw_setenv` lines | a single ~700-character line is truncated by the AP's tty; the shell then waits for the rest of a quoted value and nothing runs |
| 5 | `sleep 5` + a second `tftpboot` | from a `bootcmd` U-Boot runs ~2 s after power-on and the AQR114C still reports the link Down; the same command typed at a prompt works |
| 6 | fallback rewrites `bootcmd`+`bootargs` and resets | a failed `tftpboot` leaves U-Boot's load address set; Arista's `ubootscript` then calls its own kernel bad, flips slots and loops (`Saving Environment to SPI Flash` every ~35 s) |
| 7 | verify the AP **by MAC** | the first verifier took the first Arista host it found and reported the C-260 at 192.168.1.168 as the C-230 |
| 8 | `ask()` refuses without a terminal | an `input()` with piped stdin blocked for ever and left a run looking alive while doing nothing |
| 9 | `--offline`, and a cache fallback | a site with no uplink failed even with the release already downloaded |
| 10 | C-260 as a `MODELS` entry | one script for both models |
| 11 | NOR decided by erase size, backup in either order | the C-230 lists NOR first and slots last, the C-260 the reverse; on a C-260 the old logic saw zero NOR partitions (refusing to run) and would have backed up the slots only — losing NOR and ART |
| 12 | tool moved to `tools\arista-field\`, releases to `firmware\releases\` | it outgrew a per-model folder; paths are discovered rather than assumed, so the same file runs from this project, from the published repo, or from a copy |
| 13 | ask the installer what it supports before calling it | the C-260's shipped script has no `--no-backup` and wants >512 MiB free for its own backup, while the RAM installer's `/tmp` reports 432 MiB: the dry run answered with a usage line. It now greps the script, passes the flag only if present, and otherwise stages on a 768M tmpfs |
| 14 | reconnect when the factory shell has expired | a 3.5 min backup plus a slow answer at the menu outlived Arista's one-minute helper, and the first `fw_setenv` hit `Socket is closed` |
| 15 | find the installer by its DHCP name before scanning | `OpenWrt` resolves through the DHCP server's lease names: 0.6 s against 60-80 s of /24 scanning. The MAC check decides, because a lease name can answer with a stale address |
| 16 | do not write the source file with an open-for-write patch script | one patch opened it with an invalid `newline` value, which truncates before raising: the file went to zero bytes and was recovered from the last push. Use an editor that replaces, never truncates |
| 17 | identify candidate hosts in parallel | measured on the bench /24: the port sweep is 0.9 s (128 threads), identifying the 19 hosts that answered was **35.5 s sequentially** and 2.4 s in parallel. nmap would not have been faster -- the sweep was never the cost, the one-at-a-time SSH probing was. `find_ap` now answers in 0.6 s via the DHCP name, or ~3 s by scan |
| 18 | C-360 support: `MODELS["c360"]` | its own numbers, not the C-260's: FIT `config@hk09` on main, **load address 0xA0000000** (the C-230/C-260 use 0x60000000), NAND slot 0 384 MiB (0x18000000, not 256), NOR 32 MiB with a 1 MiB ART, installer `arista-c360-install`, and a 1024M staging tmpfs because its backup is ~800 MiB of flash. Its way in is the same: `c360/rootfs/slot0/opt/bin/prep_upgrade.sh:487` evals the download command |
| 19 | the FIT configuration is read from the image | a model does not have one configuration: the C-360 is `config@1` in 25.12 and `config@hk09` in main, the C-260 `config@1`/`config@hk01`. The tool now scans the image for `config@...` and boots what is there, saying so when it differs from the model's default -- otherwise U-Boot answers "config not found" |
| 20 | `--files` accepts a plain folder of images | not everyone has a release to fetch; pointing at a folder that holds the four files plus SHA256SUMS now works, verified the same way |
| 21 | the release is chosen per model, not 'the latest' | releases come out one per build and not every one carries every model; without `--tag` the tool walks them newest-first by publication date and takes the first that has this AP's model. `index.json` (when present) is checked as well as `SHA256SUMS`; `--channel testing`, `--tag` pinning, `--list-releases` |
| 22 | two bugs found by that review | `download_firmware()` called `ensure_shell(run, ...)` with no `run` in scope -- any run taking a fresh backup died with NameError; and offline caching took `sorted(cands)[-1]`, i.e. tag *text*, which puts `-9-` after `-42-`. Both fixed |
| 23 | version-agnostic model detect and prompts (**tool 1.1**; `--version` added) | a C-260 on firmware 16.1.0-51.6 (not the 11.0.1-49 the tool was read on) answered `version` with "Invalid command", and its `upgrade` dialogue asks "Continue? [y]/n:" / "Confirm? [y]/n:" instead of 11.x's "([y]/n)". Fall back to `show device info` when `version` names no model; parse the colon firmware form; make the Continue/Confirm matchers accept optional parens. The upgrade script itself is unchanged across the two builds, so only the wording needed loosening |
| 24 | `upgrade` prompt order read per build, not assumed (**tool 1.2**) | 1.1's loosened wording still died on that same C-260: a tester's run showed 16.x asks for the server/host *before* "Continue?" ("Upgrade from Server/Host []"), with no build-version/vendor sub-prompts and no later "Confirm?" -- the reverse of 11.x's order (Continue? first, then the two optional sub-prompts, then the server address, then Confirm?). Left as 1.1 had it, the tool answered Continue? with the server field still blank, and the upgrade ran empty. Now waits for whichever of the two leading prompts appears first and drives the rest from that; Confirm? is answered only if it appears |
| 25 | root shell via `radartool`, not the `upgrade` eval (**tool 1.3**) | on a 16.1.0-51.6 C-260, `upgrade <url>` is rejected ("Invalid argument": the CLI's real grammar is `upgrade url <url>`, from `cli_commands.yaml`), and the "Upgrade from Server/Host []" line is an *echo* of the source, not a prompt -- a payload typed at it answers Continue? and starts an upgrade from nothing. The clean way in is `radartool params ;sh; radio 0`, which evals its arguments and drops to BusyBox ash (captured on a C-360 2026-10-05, confirmed on this C-260 2026-11-04, and documented for firmware 18 by Arista-AP-firmware). The tool now tries radartool first and keeps the upgrade wizard only as a fallback, its source passed as `upgrade url <url>` and believed only when the echo line shows our host |
| 23 | radartool first, for every model | the config CLI passes `radartool`'s arguments to a shell unquoted, so `radartool params ;sh; radio 0` is a root shell from a documented command rather than an eval in a download path. It is tried on every model, with the upgrade wizard as the fallback; of the units seen, only the C-230 (8.9.0-63.1) has no radartool and its CLI says so instantly, which is caught so the fallback costs a second rather than a timeout |
| 24 | it reads calmly now | one line per step (`3/7  Download this AP's own firmware`), the reasoning in the log and on screen only with `--verbose`; normal-but-alarming events (no sftp-server, an installer without `--no-backup`, a retried read-back, radartool absent) became hidden `note()`s; a stop prints `Couldn't continue:`, one or two lines of what to do, whether anything was written, and where the log is. Prompts are `yes` rather than `env` (a command name) or `restore` |
| 25 | ASCII only, and UTF-8 output | the step marker was a `U+25B8`, which crashed a Windows cp1252 console with UnicodeEncodeError -- the least calming thing possible. Markers are plain text, and stdout/stderr are UTF-8 with `errors="replace"`, so no byte from an AP can turn output into a traceback |
| 26 | the ways in are an ordered, named list | `ROOT_METHODS` with `MODELS[...]["root_order"]` per model: radartool first everywhere, Arista's upgrade wizard only if it fails. Each method returns True at a root shell, raises `MethodFailed` (the next is tried, after Ctrl-C gets the config shell back) or `ShellNotReady` (the AP is restarting; connect retries). The C-230 skips radartool because 8.9.0's CLI has none. `test-methods.py`: 9 checks against fake ptys, including that the wizard is never touched when radartool works |

## 2026-11-12 — C-460 (eMMC, Wi-Fi 7)

| # | What changed | Why |
|---|---|---|
| 27 | `MODELS["c460"]` and an eMMC code path | the first model whose slots are GPT partitions on eMMC, not UBI volumes in NAND, and the first that installs from a `sysupgrade.bin` rather than a `factory.ubi`. `storage: "emmc"` switches `verify_stock`, the backup set, the install and the revert onto eMMC branches; everything else (connect, the way in, the netboot, the report) is shared. Read off a real unit at firmware 18.2.0-32 on 2026-11-12, and checked by `test-c460.py` |
| 28 | layout verified by GPT, not `/proc/mtd` | eMMC partitions are read from `/sys/class/block/mmcblk0p*` (name, start LBA, blocks); `emmc_problems()` pins each slot partition's number and block count and `0:HLOS`'s start LBA `0x22` (the one the boot command reads). NOR (the env) is still `/proc/mtd`. A moved `0:HLOS` or a renumbered `rootfs` is refused before any write |
| 29 | install is `sysupgrade`, revert is `dd` | the RAM installer is our own OpenWrt initramfs, so the write is `sysupgrade -n` (kernel FIT to `0:HLOS`, squashfs to `rootfs`), preceded by one `fw_setenv` script that sets the boot command (OpenWrt from slot 0, Arista's slot 1 as the `pri=1` fallback). A revert `dd`s each saved slot-0 partition back — a block device needs no erase — and writes the NOR env back the NOR way. `sysupgrade -T` gates the image; the env is read back; the MAC is confirmed, as on the C-models |
| 30 | the backup saves firmware, not the whole disk | an eMMC backup is every NOR partition plus the two slots (`0:HLOS`/`rootfs`/`rootfs_data` and their `_1`), not `ramdump` (3.7 GiB), `panicdump` or `reserved1`, which hold no firmware and the install never touches. `--fast` still skips slot 1 |
| 31 | `netboot_env` takes the model's own boot command and fdt | the C-460's Arista `bootcmd` carries a trailing `;` (`run ubootscript;`), and its RAM boots want stock's `fdt_high` (`0x48500000`) left alone, so the clear-and-restore line and the optional `fdt_high` override are now parameters, defaulting to the C-230/C-260 behaviour |
| 32 | `--ramboot`: run from RAM, no install | boot OpenWrt into RAM over the network and stop — the netboot stage with nothing after it, so the eMMC is never touched. The only writes are the U-Boot env vars the netboot needs, written to restore Arista's own boot before the fetch, so a reboot or power-cycle returns the unit to stock on its own. `--image <build.itb>` RAM-boots a local build with no release needed; a console RAM boot (`tftpboot`+`bootm`) still writes nothing at all but needs the cable. Guards reject `--ramboot` mixed with install/restore/backup/verify before any network call; checked in `test-c460.py` |
| 33 | the C-460 netboot spans a wide, variable link window via a helper var | five RAM-boot attempts on the unit (2026-11-12) never fetched: the C-460's 10 G RTL8261N links slowly and unpredictably in U-Boot (18-129 s banner->fetch across the manual boots, median 35), `tftpboot` aborts hard ("No ethernet found") with no device up yet, and `netretry` does not help (ARP only). Fixes: leave `netretry` unset (as the stock env has it); don't single-shot. A 12-attempt inline `owrt_nb` (495 chars) overran the AP's radartool shell line and stalled `fw_setenv`, so the per-attempt `tftpboot && bootm; sleep` moved into a short `owrt_try` helper that `owrt_nb` runs N times (`use_helper`/`net_helper`), keeping every var < 400 chars. The eMMC was never touched and every attempt fell back to stock cleanly — but a **successful C-460 network RAM fetch is still not achieved**; the serial-console RAM boot (`tftpboot`+`bootm`, zero-write) remains the reliable path. The NAND models keep the proven inline form |
| 34 | the C-460's RAM installer starts by **kexec from Arista's firmware** (`ram_boot: kexec`) | U-Boot's 10 G netboot never fetched; stock's kernel loads unsigned modules, so `boot_installer_kexec` sends a kexec module + static kexec (`kexec/c460/`), builds the DTB in Python (`fdt_parse`/`fdt_build`, memory from the live DT, `no-map` for every `/proc/iomem` hole ours lacks, RPM off, SPI-QUP without DMA, eMMC without CQE) and jumps: no env or flash write. Bench: 9 jumps (c460/kexec/README.md), then `--ramboot` end to end 2026-11-13 14:28 (34 s, MAC match). `test-c460.py`: the Python DTB equals the one that booted, node for node |
| 35 | `show failsafe` at connect | Arista's failure detector (>5 warm boots in 30 min) resets the AP config and blocks the way in; the tool now says so instead of failing at radartool |
| 36 | from an installed OpenWrt the C-460's installer boots **from slot 0** (`openwrt_ram_boot: slot0`) | the revert needs a RAM installer and this U-Boot cannot netboot; the boot command already reads `0:HLOS` and `bootm`s whatever FIT is there, so `boot_installer_slot0` writes the initramfs there (layout, our bootcmd, size, sha256 before; read-back after) and reboots. No env write |
| 37 | `do_restore_emmc` stages one image at a time and refuses a live system | it staged 32 + 768 + 768 MiB in the installer's tmpfs without deleting (the third could not fit); now removed after each verified write, free space checked, read-back after `drop_caches`; refuses a system not running from RAM or a mounted target. `wait_installer` accepts only a RAM system (an installed OpenWrt answers to the same name and MAC) |
| 38 | a settings partition saved while mounted is **left blank**, not written back | first revert on the unit: slot 0 and env restored exactly and authenticated, then Arista's boot failed the ext4 journal replay of the restored `rootfs_data` and its `mkfs.ext4` stopped at `Proceed anyway? (y,N)` on the console, forever. `ext4_captured_mounted()` reads the saved superblock (INCOMPAT_RECOVER); such a copy is not written, the first 16 MiB are zeroed, and Arista formats it unprompted (proven on slot 1's blank overlay in the fallback test, then on the second revert) |

## 2026-11-15 -- one front door (tool 2.0)

The tool could do the job, but only for someone who already knew its flags, the
AP's address, which release to ask for and which of three documents to follow.
2.0 changes how it is started and what it shows, not how it installs.

| # | What changed | Why |
|---|---|---|
| 39 | run it with no options | it looks for the AP itself: a sweep of the PC's own /24 networks for SSH, then only the hosts whose MAC address carries one of these APs' prefixes are asked who they are. A default login is never tried against the rest of somebody's network. Measured on a /24 with eight APs: 6.5 s |
| 40 | the model is read at the AP's own CLI before anything else | `show device info` (or `version` on older firmware) names the model, build and MAC with no root shell, so the menu and the one question come before the shell whose helper expires after a minute. The old order asked at a menu while that clock ran, and a slow answer cost a restart of the AP |
| 41 | one question | the menu, then one yes/no that names the build and says the firmware is saved first. The three later prompts (type yes for the boot change, the installer's address, the last four of the MAC) are gone: the unit is already identified by MAC by the tool itself, and the installer is found by its MAC |
| 42 | "accepted" builds | without `--tag` it takes the newest release not marked pre-release that has the model; a model with no such release gets the newest testing one and the question says so. An AP already on OpenWrt is never offered an older build than it runs |
| 43 | the screen is one line per step | detail lines became log-only (`say`), a few became always-shown (`tell`), and slow steps show one progress line that replaces itself. A real install had printed 150 lines, 58 of them partition names. `--verbose` shows everything, with the clock. A stop still says what happened, what to do and what was written |
| 44 | it waits for the AP | finding the AP afterwards (by MAC) is the default and the last line gives its address and what to open. `--no-wait` skips it |
| 45 | `--update`, and "update" in the menu | an AP that already runs the port is updated with OpenWrt's own `sysupgrade` after its own `sysupgrade -T`, rather than reinstalled through the RAM installer. Written and checked against a fake; **not yet run on a unit** |
| 46 | everything this PC must offer is checked first | UDP 69 and TCP 8069 are tried before the AP is touched; a refused port is a sentence (on Linux, the `sudo` command) instead of a traceback after the AP's shell was already open. The release is fetched before the connect for the same reason |
| 47 | the C-460's kexec helper files come from the repository | a single downloaded file had none; they are fetched from `install/kexec/<model>/` and checked against its `SHA256SUMS`, before the AP is touched |
| 48 | files live next to the tool | `backups/`, `releases/` and `logs/` beside `arista-field.py`; a folder further up is used only if it already holds this tool's backups. The old default wrote to the parent folder and logs to wherever it was started |
| 49 | a question whose input has ended stops the run | found while testing the one-line installer: Windows calls its null device a terminal, `input()` raised EOF, the empty answer was not a valid choice and the question repeated for ever |
| 50 | a typed address with a foreign MAC is questioned first | the tool tried the factory login on whatever address it was given; now an address whose MAC is not one of these APs' prefixes gets a yes/no before any login |
| 51 | `get.ps1` and `get.sh` | one line to paste: they make a folder, a private Python environment with `paramiko`, fetch the tool and run it. The Windows one is tested; the Linux one is tested as far as a machine without `python3-venv` allows (it says what to install) |

`test-guided.py` (43 checks, no AP and no network) holds the order of all this
in place: nothing reaches an AP before the question is answered yes, the W-318
is refused before any shell is tried, a busy port is caught, piped output
carries headlines only.

### 2026-11-15, afternoon -- the first C-360, and what it found

A C-360 (Arista 20.0.0-179, running from slot 1) was installed with 2.0 on the
third attempt. The first two stopped in the backup, before any write, and each
showed a fault that the C-230 and C-260 had never met. `test-backup.py` (11
checks) holds these in place.

| # | change | why |
|---|---|---|
| 52 | a partition the running firmware writes to is saved as read | the backup hashed each partition on the AP, then read it again to send, and compared. Arista writes to the slot it runs from (its settings volume is inside it) and to LOGGING, so the two reads differed and the run stopped. `live_partitions()` finds them (the kernel's attached-UBI list, the kernel command line, `pri`); they are sent once, gzip's checksum covers the trip, and the manifest marks them `live`. Other partitions get one re-read before the run stops |
| 53 | UBI volumes are told by block size as well as name | a C-360 lists four (`ubi_rootfs`, `rootfs_data`, `rootfs_sig`, `ubi_logging`); only two names were known, and `ubi_logging` cannot be opened while Arista has it mounted. A volume's block is the slot's less two pages, never a power of two |
| 54 | a stopped run restarts the AP while the session is open | the restart was left to interpreter exit, which runs after the SSH library (imported later, so its exit hook runs first) has closed every session: nothing was sent, and the screen still said the AP was restarting. `die()` and the Ctrl-C ending now do it themselves, hold the session until the AP drops it, and say "restart the AP" when it could not be sent |
| 55 | a failed read on the AP is called that | "did not arrive (firewall?)" was printed for a partition the AP could not open, and the backup step's advice (free space, firewall) for a hash mismatch. `die(msg, hint=...)` lets a failure carry its own advice |

Seen and left alone:

- Build 42's image does not carry the build tag in its release string, so an AP
  just installed with it is offered "Update OpenWrt to build 42". It goes away
  when a later build becomes the accepted one.
- A question needs a terminal: answers piped in are refused, by design. The
  whole run without one is `--install --yes`.
- The test suites write their run logs into `logs/` beside real ones.

## What is proven, and on what

- **C-230:** install and revert, both end to end, twice (2026-10-25).
- **C-260 (11.0.1-49.8):** all three paths on hardware, 2026-10-25:
  `--backup-only` (26 partitions, ART included, 63.6 MiB); a full install
  (`pri=1`, slot 0 written, three radios up -- ath10k_pci + 2x ath11k -- board
  data and Bluetooth PS keys copied from the Arista slot); and a full revert,
  slot 0 and `0:APPSBLENV` both reading back identical, back on Arista.
- **C-260 again, 2026-10-27:** installed with release 42 (whose C-260 installer
  takes `--no-backup`), resumed with `--installer-at` after an interrupted run.
- **C-460 (18.2.0-32), 2026-11-12 and 11-13:** the way in (`config`/`config`,
  then `radartool params ;sh; radio 0`), the eMMC+NOR layout verify, the backup
  of 29 partitions (84 MiB, each matching the AP's own md5), `--ramboot` by
  kexec, the install three times (about 3 minutes each), the fallback to
  Arista's slot 1 with OpenWrt's kernel broken on purpose, and the revert twice
  (from OpenWrt and from Arista's slot 1), slot 0 and the environment reading
  back identical to the backup.
- **2.0's own changes (2026-11-15):** finding and identifying APs ran against
  eight real units (read-only); the Windows one-line installer ran end to end
  as far as the first question.
- **C-360 (20.0.0-179, running from slot 1), 2026-11-15, with 2.0:** a full
  install with `--install --yes`, release 42: the way in by `radartool`, the
  layout check, the backup (28 partitions, 135 MiB, two kept as read), the
  network boot (22 MiB by TFTP in 8 s, `config@hk09` at 0xA0000000), the
  installer's own dry run (6.5 minutes on this model) and write, and OpenWrt
  answering 21 minutes after the start: four radios, boot command with the
  Arista fallback, `pri=1`. Also `--verify-only`. The menu and the yes/no were
  seen on the unit but not answered there (no terminal to type on).
- **Not yet, with 2.0:** a restore on any unit; `--update` on a unit; an
  install on a C-230, C-260 or C-460 (their steps are 1.3's, called in the same
  order).
- **Not yet:** a unit whose
  factory password was changed (the question for it is untested on hardware);
  a full install on firmware 16.x end to end (its root shell is confirmed:
  `radartool params ;sh; radio 0` dropped a C-260 at 16.1.0-51.6 to BusyBox
  ash, 2026-11-04); and a way into Arista's firmware on a W-318, W-118 or
  C-200 (on a W-118 at 8.7.1, `radartool` refuses shell characters; its
  upgrade wizard has the same eval as the C-230's and has not been tried).

## Deliberate design points (do not "simplify" these away)

- **TFTP for the boot image only.** U-Boot speaks nothing else. Everything after
  that is SSH, where TCP gives ordering, retransmission and an end-to-end
  integrity check.
- **The AP never contacts GitHub.** It has no uplink by assumption. The PC
  fetches the release, the AP fetches from the PC.
- **Two independent reads per partition.** The AP's hash of the raw device and
  ours of the decompressed copy, compared. The exception is a partition the
  running firmware writes to (item 52): two reads of it cannot agree, so it is
  read once and marked `live` in the manifest.
- **Nothing is written before the backup verifies.** (Until 2.0 the menu came
  after the backup; it now comes first, so nobody waits at a prompt while
  Arista's shell expires. The order of the work on the AP is the same.)
- **The fallback is self-healing**, never a boot loop.
