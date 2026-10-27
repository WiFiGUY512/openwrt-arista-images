# Iterations of `arista-field.py`

Every entry below is a change forced by something that actually happened on a
unit, not a tidy-up. The commit ids are in the public repo
`WiFiGUY512/openwrt-arista-images`, under `install/`.

## 2026-10-25 â€” first day, C-230

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
| 11 | NOR decided by erase size, backup in either order | the C-230 lists NOR first and slots last, the C-260 the reverse; on a C-260 the old logic saw zero NOR partitions (refusing to run) and would have backed up the slots only â€” losing NOR and ART |
| 12 | tool moved to `tools\arista-field\`, releases to `firmware\releases\` | it outgrew a per-model folder; paths are discovered rather than assumed, so the same file runs from this project, from the published repo, or from a copy |
| 13 | ask the installer what it supports before calling it | the C-260's shipped script has no `--no-backup` and wants >512 MiB free for its own backup, while the RAM installer's `/tmp` reports 432 MiB: the dry run answered with a usage line. It now greps the script, passes the flag only if present, and otherwise stages on a 768M tmpfs |
| 14 | reconnect when the factory shell has expired | a 3.5 min backup plus a slow answer at the menu outlived Arista's one-minute helper, and the first `fw_setenv` hit `Socket is closed` |
| 15 | find the installer by its DHCP name before scanning | `OpenWrt` resolves through the DHCP server's lease names: 0.6 s against 60-80 s of /24 scanning. The MAC check decides, because a lease name can answer with a stale address |
| 16 | do not write the source file with an open-for-write patch script | one patch opened it with an invalid `newline` value, which truncates before raising: the file went to zero bytes and was recovered from the last push. Use an editor that replaces, never truncates |
| 17 | identify candidate hosts in parallel | measured on the bench /24: the port sweep is 0.9 s (128 threads), identifying the 19 hosts that answered was **35.5 s sequentially** and 2.4 s in parallel. Griff asked whether nmap would have been faster; it would not have -- the sweep was never the cost, the one-at-a-time SSH probing was. `find_ap` now answers in 0.6 s via the DHCP name, or ~3 s by scan |
| 18 | C-360 support: `MODELS["c360"]` | its own numbers, not the C-260's: FIT `config@hk09` on main, **load address 0xA0000000** (the C-230/C-260 use 0x60000000), NAND slot 0 384 MiB (0x18000000, not 256), NOR 32 MiB with a 1 MiB ART, installer `arista-c360-install`, and a 1024M staging tmpfs because its backup is ~800 MiB of flash. Its way in is the same: `c360/rootfs/slot0/opt/bin/prep_upgrade.sh:487` evals the download command |
| 19 | the FIT configuration is read from the image | a model does not have one configuration: the C-360 is `config@1` in 25.12 and `config@hk09` in main, the C-260 `config@1`/`config@hk01`. The tool now scans the image for `config@...` and boots what is there, saying so when it differs from the model's default -- otherwise U-Boot answers "config not found" |
| 20 | `--files` accepts a plain folder of images | not everyone has a release to fetch; pointing at a folder that holds the four files plus SHA256SUMS now works, verified the same way |
| 21 | the release is chosen per model, not 'the latest' | releases come out one per build and not every one carries every model; without `--tag` the tool walks them newest-first by publication date and takes the first that has this AP's model. `index.json` (when present) is checked as well as `SHA256SUMS`; `--channel testing`, `--tag` pinning, `--list-releases` |
| 22 | two bugs found by that review | `download_firmware()` called `ensure_shell(run, ...)` with no `run` in scope -- any run taking a fresh backup died with NameError; and offline caching took `sorted(cands)[-1]`, i.e. tag *text*, which puts `-9-` after `-42-`. Both fixed |

## What is proven, and on what

- **C-230:** install and revert, both end to end, twice (2026-10-25).
- **C-260 (11.0.1-49.8):** all three paths on hardware, 2026-10-25:
  `--backup-only` (26 partitions, ART included, 63.6 MiB); a full install at
  19:05Z (`pri=1`, slot 0 written, OpenWrt SNAPSHOT r0-bcb16b63 on 6.18.55,
  three radios up -- ath10k_pci + 2x ath11k -- board data and Bluetooth PS keys
  copied from the Arista slot); and a full revert at 19:13Z, slot 0 and
  `0:APPSBLENV` both reading back identical, back on Arista at 192.168.1.25.
- **Not yet:** a revert on a C-260 (the path is the C-230's, already exercised),
  and a unit whose factory password was changed (the `--user/--password` route
  is untested on hardware).

## Deliberate design points (do not "simplify" these away)

- **TFTP for the boot image only.** U-Boot speaks nothing else. Everything after
  that is SSH, where TCP gives ordering, retransmission and an end-to-end
  integrity check.
- **The AP never contacts GitHub.** It has no uplink by assumption. The PC
  fetches the release, the AP fetches from the PC.
- **Two independent reads per partition.** The AP's hash of the raw device and
  ours of the decompressed copy, compared.
- **Nothing is written before the backup verifies**, and the menu comes after it.
- **The fallback is self-healing**, never a boot loop.
