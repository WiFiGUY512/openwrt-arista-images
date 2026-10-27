# Quickstart: one AP, no console cable

You need: a PC that can reach the AP over the network, Python 3, and the AP
powered and on its factory firmware.

## 1. Get the script

**Windows (PowerShell):**

```powershell
curl.exe -O https://raw.githubusercontent.com/WiFiGUY512/openwrt-arista-images/main/install/arista-field.py
python -m pip install paramiko
```

**macOS / Linux:**

```sh
curl -O https://raw.githubusercontent.com/WiFiGUY512/openwrt-arista-images/main/install/arista-field.py
python3 -m pip install paramiko
```

If `python` is not found on Windows, try `py` instead of `python`.

## 2. Find the AP's address

It is on your DHCP server's lease list, on the label, or in your router's client
list. Then check it answers:

```sh
ping <AP address>
```

If you are unsure which build the tool will take, ask it first — it needs no AP:

```sh
python arista-field.py --list-releases
```

## 3. Save the AP's firmware first (writes nothing)

```sh
python arista-field.py --host <AP address> --backup-only
```

This reads every partition off the AP, gzips it here, and checks each one against
the AP's own hash. It takes about four minutes and changes nothing on the AP.
**Keep the folder it prints** — that is the only copy of that AP's firmware, and
`--restore` is what puts it back.

## 4. Then choose

```sh
python arista-field.py --host <AP address>
```

```
[1] Install OpenWrt on this AP
[2] Put this AP's original firmware back
[3] Nothing -- stop here
```

Pick `1`. It writes two U-Boot variables, reboots the AP into a temporary
installer over TFTP, sends the image over SSH, and the installer writes slot 0
after its own checks pass. Arista's system in slot 1 is kept as the fallback.

When it finishes, the AP restarts on its own: look for it on your DHCP server's
lease list (hostname `OpenWrt`, and the MAC is on the label). Add `--verify` if
you would rather the tool wait for it and check.

## If something goes wrong

- **"cannot log in ... (tried root, config)"** — Arista's account is `config`;
  if its password was changed, add `--user config --password <pw>`. A locked or
  already-OpenWrt unit needs `--installer-at <address>` instead.
- **"this run needs an answer ... and there is no terminal"** — you are running
  it from something that is not a terminal. Add `--install` (and `--yes`) or run
  it in a normal terminal window.
- **It cannot find the installer** — the temporary system takes an address by
  DHCP on the AP's LAN port, so that segment needs a DHCP server.
- **It stops anywhere** — the AP keeps its own firmware: a failed fetch puts
  Arista's boot back and restarts it. Nothing is lost; send the log
  (`field-run-*/run.log`) and the last screen.

## Rule of thumb

`--backup-only` first, always. The tool refuses to write before the backup
verifies, but the backup is also what you would need if anything later goes
wrong on a unit that has no other copy of its firmware.
