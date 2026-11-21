# OpenWrt for Arista access points

OpenWrt for the Arista C-130, C-200, C-230, C-260, C-360, C-460, W-118 and
W-318 Wi-Fi access points: the built images, and an installer that puts them
on an AP over the network. Not affiliated with Arista Networks. Installing
replaces part of the AP's firmware, at your own risk; the installer saves the
AP's own firmware first and can put it back.

## Install

On a PC on the same network as the AP. No console cable.

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
| C-200, C-230, C-260, C-360 | yes |
| W-118, W-318 | yes |
| C-130, C-460 | yes, from its first published build |

## Update

An AP that already runs this port is an ordinary OpenWrt device. Run the same
command and choose "Update OpenWrt"; settings are kept.

Or do it on the AP, with `sysupgrade` and that model's
`...-squashfs-sysupgrade.bin` from a release (check it against the release's
`SHA256SUMS` first). The AP is back in about two minutes; if it is not back
after five, power-cycle it.

To see what an AP runs:

```
cat /tmp/sysinfo/board_name; grep DISTRIB_DESCRIPTION /etc/openwrt_release
```

An AP that says `OpenWrt 25.12` runs the older 25.12 port. Its port names
changed since, so update it without its settings (`sysupgrade -n`).

## Back to Arista's firmware

Run the same command: the menu offers it for any AP whose firmware that PC
saved.

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

You do not need to download any of them yourself: the installer does.

## If the installer cannot reach an AP

[install/BY-HAND.md](install/BY-HAND.md) points to each model's console-cable
procedure. Most installs never need it.

## Source

Built from OpenWrt main with the port's patches, which are in
[openwrt-arista](https://github.com/WiFiGUY512/openwrt-arista); the device
support is going upstream as OpenWrt pull requests. The corresponding source
for any image here is available on request: open an issue in this
repository.
