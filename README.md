# OpenWrt for Arista access points

OpenWrt for the Arista C-230, C-260, C-360, C-460, W-318, W-118 and C-200
Wi-Fi access points: the built images, and an installer that puts them on an AP
over the network. Not affiliated with Arista Networks. Installing replaces part
of the AP's firmware, at your own risk; the installer saves the AP's own
firmware first and can put it back.

## Install

On a PC on the same network as the AP. No console cable, no USB stick.

**Windows** (PowerShell):

```powershell
irm https://raw.githubusercontent.com/WiFiGUY512/openwrt-arista-images/main/install/get.ps1 | iex
```

**macOS or Linux:**

```sh
curl -fsSL https://raw.githubusercontent.com/WiFiGUY512/openwrt-arista-images/main/install/get.sh | sh
```

It finds the AP, reads its model, picks the build for that model, asks one
question, and runs to the end. **[install/README.md](install/README.md)** shows
what you will see, what it needs, and what to do if it stops.

| Model | Installs over the network |
|---|---|
| C-230, C-260, C-360 | yes |
| C-460 | yes, from its first published build |
| W-318, W-118, C-200 | not yet: [by hand](install/BY-HAND.md), with a console cable |

To go back to Arista's firmware, run the same command: the menu offers it for
any AP whose firmware that PC saved.

## Update

An AP that already runs this port is an ordinary OpenWrt device. Run the same
command and choose "Update OpenWrt", or do it on the AP with the commands in
each release's notes.

Check what the AP runs first:

```
cat /tmp/sysinfo/board_name; grep DISTRIB_DESCRIPTION /etc/openwrt_release
```

- **`arista,c-230` or `arista,c-260` with `OpenWrt SNAPSHOT`:** use the
  commands in the release notes as they are. Settings are kept.
- **`OpenWrt 25.12`:** that's the older 25.12 port. Use `sysupgrade -n -v`.
  The port names changed, so old settings do not carry over.
- **Neither command works:** it's still on Arista's firmware. See
  [First install](#first-install-c-230).

The commands in each release download the image, refuse to flash unless
the SHA-256 matches, then flash and reboot. The AP is back in about two
minutes; if it is not back after five, power-cycle it.

To check an image without flashing, replace `sysupgrade -v` with
`sysupgrade -T`.

## Builds

**[STATUS.md](STATUS.md)** lists every published build, which models it
carries, upstream progress and the known issues. New builds are published as
pre-releases (testing) and promoted once they have run for a while; the
installer takes the newest promoted build for a model, and the newest testing
build where a model has none yet.

Each release has, for each model:

| File | What it is for |
|---|---|
| `...-initramfs-uImage.itb` | the installer's temporary system, started from memory |
| `...-squashfs-factory.ubi` | what a first install writes (the C-230, C-260, C-360, C-200) |
| `...-squashfs-sysupgrade.bin` | updates, and the first install on the C-460, W-318 and W-118 |
| `<model>-install-SHA256SUMS`, `SHA256SUMS`, `index.json` | checksums the installer verifies |

## Other ways to install

[install/BY-HAND.md](install/BY-HAND.md): the console-cable procedure for every
model, and the USB stick. Use it for the W-318, W-118 and C-200, or when the
network installer cannot reach an AP.

## Source

Built from OpenWrt main with the port's patches; the device support is
going upstream as OpenWrt pull requests. The corresponding source for any
image here is available on request: open an issue in this repository.
