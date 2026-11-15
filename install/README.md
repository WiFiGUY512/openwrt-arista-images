# Install OpenWrt on an Arista access point, over the network

One command, on a PC on the same network as the AP. No console cable, no USB
stick, nothing to open.

**Windows** (PowerShell):

```powershell
irm https://raw.githubusercontent.com/WiFiGUY512/openwrt-arista-images/main/install/get.ps1 | iex
```

**macOS or Linux:**

```sh
curl -fsSL https://raw.githubusercontent.com/WiFiGUY512/openwrt-arista-images/main/install/get.sh | sh
```

It needs Python 3.8 or newer and tells you how to get it if it is missing.
Everything it downloads or saves stays in one folder, `arista-openwrt` in your
home folder.

## What happens

```
OpenWrt for Arista access points -- network installer 2.0

Found on your network:

   1  192.168.1.50     C-260 on Arista firmware 11.0.1-49.8

Which one? [1, or type another AP's address] 1

192.168.1.50 is a C-260 on Arista firmware 11.0.1-49.8, MAC 30:86:2d:..

What would you like to do?

   1  Install OpenWrt
   2  Only save this AP's own firmware to this PC (changes nothing)
   3  Nothing; leave it as it is

Choice [1-3]: 1

Ready to install OpenWrt, build 42 (2026-10-26), on this C-260.

  - Its own firmware is saved to this PC first, so it can be put back.
  - Arista's firmware also stays in the AP's second slot as a fallback.
  - The AP restarts a few times and serves no Wi-Fi for about 10 minutes.
    Keep it powered and plugged in.

Go ahead? [y/N] y

1/6  Get the OpenWrt build
2/6  Connect to the AP and check it
3/6  Save this AP's own firmware
4/6  Start the installer
5/6  Install OpenWrt
6/6  Wait for OpenWrt to start

Done.  OpenWrt is running on this C-260, at 192.168.1.51.

  Next:  open http://192.168.1.51 in a browser (user root, no password yet --
         set one first).  Wi-Fi is off until you turn it on there.
  Kept:  this AP's original firmware, in ...\arista-openwrt\backups\c260-...
         Run this tool again to put it back.
```

It finds the AP, reads the model from the AP itself, picks the build for that
model, and asks one question. After you answer it runs to the end by itself.
From then on the AP is an ordinary OpenWrt device: update it from its own web
page or with `sysupgrade`, or run this tool again and it offers the update.

## Which models

| Model | Over the network | Notes |
|---|---|---|
| C-230 | yes | installed and put back to Arista on a unit, twice each |
| C-260 | yes | installed and put back on a unit (Arista 11.0.1); the way in is confirmed on 16.1.0 |
| C-360 | yes, not yet run on a unit | the tool says so before it asks; its numbers are the C-360's own |
| C-460 | yes | installed and put back on a unit (Arista 18.2.0), once its first build is published |
| W-318, W-118, C-200 | not yet | no confirmed way into Arista's firmware on these; install them [by hand](BY-HAND.md), with a console cable |

An AP of any of these models that already runs this OpenWrt port can be
**updated** with the same tool.

## What it needs

- The PC and the AP on the same network, with a DHCP server on it (any home
  router is one). The temporary installer takes its address that way.
- The AP on Arista's firmware, started and reachable. The factory login is
  `config` / `config`; if a previous owner's management system changed it, the
  tool asks for the password, or reset the AP to its factory defaults.
- This PC allowed to accept connections from the AP while it runs: the AP sends
  its firmware to the PC (TCP 8069) and fetches the installer from it (UDP 69;
  on the C-460, TCP 8069-8079). **On Windows, click Allow** if the firewall asks
  about Python. On Linux, opening UDP 69 needs `sudo`; the tool prints the
  exact command if it comes to that.
- About 15 minutes.

## What it changes on the AP

- OpenWrt goes into the AP's **first firmware slot**. Arista's firmware in the
  second slot is not touched and stays as the fallback: if OpenWrt cannot be
  loaded, the boot loader starts Arista's instead.
- One boot setting, so the AP tries OpenWrt first.
- Nothing else: the boot loader, the radio calibration and the MAC addresses
  stay as they are.

Before any of that, the AP's whole flash is copied to this PC and every part of
it is checked against the AP's own checksum. Nothing is written until that
copy is complete. **Keep the `backups` folder:** it is the only copy of that
AP's own firmware.

## Putting Arista's firmware back

Run the same command. For an AP whose firmware this PC saved, the menu offers
"Put Arista's firmware back". It writes the saved slot and boot settings back
and checks them byte for byte.

## If something goes wrong

The tool stops with one sentence saying what happened, what to do, and whether
anything was written. Until step 5 nothing on the AP has changed: a run that
stops before then leaves the AP on its own firmware, and running it again costs
nothing.

| It says | What to do |
|---|---|
| No Arista access point answered | Type the AP's address when asked. It is on your router's list of clients. The AP needs two or three minutes after power-on. |
| The default login was refused | The AP's password was changed. Type it when asked, or reset the AP to factory defaults. |
| Cannot open UDP 69 / TCP 8069 | Another program uses that port (a TFTP server, or a second copy of this tool); close it. On Linux, run it with `sudo` as shown. |
| A partition did not arrive / the installer never answered | The PC's firewall is blocking the AP. Allow Python through it and run again. |
| The installer never answered, and the firewall is open | The AP's network needs a DHCP server. |
| The AP cannot be installed over the network yet | That model installs [by hand](BY-HAND.md). |

Every run writes a full log; its path is on the last line. Open an issue with
that file if you are stuck.

## Running it directly

The one-line commands above download `arista-field.py` into
`arista-openwrt` and run it. You can also run it yourself:

```sh
python arista-field.py                               # find the AP and ask
python arista-field.py --host 192.168.1.50           # this AP, then ask
python arista-field.py --host 192.168.1.50 --backup-only     # save its firmware, change nothing
python arista-field.py --host 192.168.1.50 --install --yes   # no questions
python arista-field.py --host 192.168.1.51 --update          # an AP already on OpenWrt
python arista-field.py --host 192.168.1.51 --restore backups/c260-<mac>/<date>
python arista-field.py --list-releases               # what each release carries
python arista-field.py --verbose ...                 # every detail on screen
```

`python arista-field.py --help` lists the rest (a pinned release with `--tag`,
`--offline` for a site with no internet, `--no-wait`, and so on).

**Which build it takes.** The newest release that is not marked as a
pre-release (a build that has finished its soak), for that AP's model. Where a
model has no such release yet, the newest test build, and the question says
"testing". `--channel testing` always takes the newest build;
`--channel stable` never takes a test build. An AP already on a test build is
offered newer test builds, and is never offered an older build.
[STATUS.md](../STATUS.md) lists every build.

**Where things are kept**, next to `arista-field.py`:

```
backups/<model>-<mac>/<date>/    that AP's own firmware, with checksums
releases/<tag>/                  downloaded OpenWrt builds, checked
logs/field-run-<date>/run.log    everything a run did, with the time
```

## More

- [BY-HAND.md](BY-HAND.md): installing with a console cable (every model), and
  the USB stick.
- [INTERNALS.md](INTERNALS.md): how the tool gets in, what it writes, and the
  per-model details.
- [ITERATIONS.md](ITERATIONS.md): what changed in the tool and why.
