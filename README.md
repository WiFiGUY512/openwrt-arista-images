# OpenWrt images for Arista access points

Built images of an OpenWrt port for the Arista C-230 (Qualcomm IPQ8071A)
and C-260 (IPQ8074) access points, on OpenWrt main. Source is not in this
repository (see [Source](#source)). Not affiliated with Arista Networks:
installing replaces part of the AP's firmware, at your own risk.

Each release has, for each model:

| File | What it is for |
|---|---|
| `...-initramfs-uImage.itb` | the installer: OpenWrt booted from RAM over TFTP |
| `...-squashfs-factory.ubi` | the image the installer writes to flash |
| `c230-install-SHA256SUMS`, `c260-install-SHA256SUMS` | the installer's checksums for that image |
| `...-squashfs-sysupgrade.bin` | updating an AP that already runs this port |
| `SHA256SUMS` | the checksums of every file |

- **AP still on Arista's firmware:** [First install](#first-install-c-230).
- **AP already on this port:** [Updating](#updating-an-ap-that-runs-this-port).

## No console cable: `arista-field.py`

[`install/arista-field.py`](install/README.md) does the first install over the
network, and can put the AP's own firmware back afterwards. It verifies the AP,
downloads its whole flash and checks it against the AP's own hashes, then asks
whether to install OpenWrt or restore what was there.

```sh
curl -O https://raw.githubusercontent.com/WiFiGUY512/openwrt-arista-images/main/install/arista-field.py
python arista-field.py --host 192.168.1.191
```

It needs Python 3 with `paramiko`, TCP 8069 open on your PC, UDP 69 free, and a
DHCP server on the AP's segment. The procedure below does the same thing by hand
with a console cable, and is worth reading before you run either.

## Status

- **C-230:** first install done on one unit (2026-10-22). This guide's
  exact steps, with these files, are being run on a second unit now.
- **C-260:** units that already run the port update with sysupgrade. A
  first install with these files has not been run yet; see
  [C-260](#first-install-c-260).

## What the install changes

- OpenWrt goes into NAND slot 0. **Arista's system in slot 1 is kept**: it
  is the fallback, and OpenWrt takes the radio's board data from it.
- The U-Boot environment is written once, with a boot command that tries
  OpenWrt first and falls back to Arista's own boot.
- Nothing else is written: U-Boot, the boot chain, the ART partition
  (calibration and MAC addresses) and slot 1 stay as they are.
- Whatever Arista had in slot 0 is replaced. Take the backup in step 4.

## You need

- **Console cable:** an RJ-45 rollover cable (RS-232, Cisco style) to
  the AP's console port, a USB-to-RS-232 adapter, and a terminal (PuTTY,
  screen, minicom). 115200 8N1, no flow control.
- **TFTP server on your PC**, allowed through its firewall, serving the
  model's `...-initramfs-uImage.itb`. On Windows, Tftpd64. On Linux,
  tftpd-hpa or dnsmasq. On macOS, the built-in tftpd.
- **Network:** the AP's Ethernet 1 (the PoE port) on the same network as
  the PC, untagged, with a DHCP server. Power by PoE (802.3at) or DC.
- **SSH/SCP client** on the PC (built into Windows 10/11, macOS and Linux).
- **Time:** about 20 minutes.

## First install: C-230

### 1. Stop U-Boot

Connect the console, then power the AP. Paste this string repeatedly
while it boots, until you see `IPQ807x#`:

```
s6.D5r4Gnb89ZEWSW6Kq.
```

Then press Ctrl-C once to clear what is left on the line.

Don't let Arista's firmware boot while you are at it: its boot saves the
U-Boot environment every time.

### 2. Boot the installer from RAM

At `IPQ807x#`, type these one line at a time. Long pasted lines can hang
Arista's U-Boot. Nothing here is saved.

- `ipaddr` is a free address on your network, for the AP.
- `serverip` is your PC's address.

```
setenv ipaddr 192.168.1.250
setenv serverip 192.168.1.10
setenv fdt_high 0x4A3E0000
setenv bootargs console=ttyMSM0,115200n8 rootwait panic=0
tftpboot 0x60000000 openwrt-qualcommax-ipq807x-arista_c-230-initramfs-uImage.itb
bootm 0x60000000#config@ac02
```

OpenWrt starts from RAM in about 30 s; press Enter for a shell. There is
no root password. The RAM system takes an address by DHCP on Ethernet 1;
check it with:

```
cat /tmp/sysinfo/board_name          # arista,c-230
ip -4 addr show br-lan
```

### 3. Wait for the clock

Wait a minute for the AP to set its clock over the internet. The downloads
in step 5 are HTTPS and fail with a wrong date.

### 4. Back up the flash

On the AP's console:

```
mkdir -p /tmp/backup && cd /tmp/backup
cat /proc/mtd > proc-mtd.txt
for n in $(grep -v '"rootfs' /proc/mtd | grep -o '^mtd[0-9]*'); do cat /dev/$n > $n.bin; done
gzip -c /dev/$(grep '"rootfs"' /proc/mtd | cut -d: -f1) > slot0-rootfs.bin.gz
sha256sum * > SHA256SUMS
du -sh .
```

That copies every NOR partition (ART included) and NAND slot 0, Arista's
slot that the install replaces. Then copy it to the PC:

```
scp -O -r root@<AP address>:/tmp/backup c230-backup
```

If your scp doesn't know `-O`, leave it out. Keep this backup: ART exists
nowhere else, and putting Arista's slot 0 back needs it. Then free the
AP's RAM:

```
rm -rf /tmp/backup
```

### 5. Get the image onto the AP

On the AP's console:

```
mkdir -p /tmp/c230 && cd /tmp/c230
wget -O factory.ubi https://github.com/WiFiGUY512/openwrt-arista-images/releases/download/v2026.10.17.1-41-gb4ecbba/openwrt-qualcommax-ipq807x-arista_c-230-squashfs-factory.ubi
wget -O SHA256SUMS https://github.com/WiFiGUY512/openwrt-arista-images/releases/download/v2026.10.17.1-41-gb4ecbba/c230-install-SHA256SUMS
```

Without internet, copy them from the PC instead, under those names:

```
scp -O openwrt-qualcommax-ipq807x-arista_c-230-squashfs-factory.ubi root@<AP address>:/tmp/c230/factory.ubi
scp -O c230-install-SHA256SUMS root@<AP address>:/tmp/c230/SHA256SUMS
```

Create `/tmp/c230` on the AP first.

### 6. Install

```
arista-c230-install --dry-run --no-backup --stick /tmp/c230
arista-c230-install --no-backup --stick /tmp/c230
```

- **The first command only checks:** that the unit is a C-230, the flash
  layout, Arista's system in slot 1, and the image's checksums.
- **The second installs:**
  1. It asks for the last four hex digits of the AP's MAC address (on its
     label).
  2. It writes the U-Boot environment.
  3. It writes slot 0 and reads it back.
  4. It ends with `C230-INSTALL-RESULT: OK installed`.

Then:

```
reboot
```

### First boot

- **Boot time:** about 30 s from U-Boot to the console.
- **Ports:** Ethernet 1 and 2 are both in the LAN bridge and take an
  address by DHCP. Cable only one, or turn on STP: two on the same switch
  make a loop.
- **Passwords:** there is no root password. Set one with `passwd` before
  anything else.
- **Web interface:** LuCI is at `http://<AP address>`.
- **Wi-Fi:** off until you set it up.

## First install: C-260

The same steps, with these changes:

- **Step 2:** use `openwrt-qualcommax-ipq807x-arista_c-260-initramfs-uImage.itb`
  and `bootm 0x60000000#config@hk01`. The board name is `arista,c-260`.
- **Steps 4-6:** the C-260's installer, `arista-c260-install`, always takes
  its own backup, and it needs more room than the AP's RAM has. So it needs
  a FAT32 USB stick (1 GB or more) with a `c260/` folder holding
  `factory.ubi` and `SHA256SUMS` (that is, `c260-install-SHA256SUMS`).
  Plug the stick in after step 2, then run:

  ```
  arista-c260-install --dry-run
  arista-c260-install
  ```

  It finds the stick and writes a verified backup of every partition to it.
- **Before you start:** check which slot Arista runs from, with
  `fw_printenv pri`. `pri=1` is the usual case. With `pri=0`, Arista runs
  from slot 0, which the install replaces, and slot 1 holds an older
  Arista. The installer warns, and goes on only if you confirm.
- **Not yet run:** these files have not been through a C-260 first install.
  If you are first, open an issue with how it went.

## Alternative: a USB stick for the C-230

Instead of steps 4 and 5, put `factory.ubi` and `SHA256SUMS` in a `c230/`
folder on a FAT32 USB stick with 1 GB free, and plug it in after step 2.
Then run:

```
arista-c230-install --dry-run
arista-c230-install
```

It finds the stick and backs up every partition to it, verified, before it
installs. The RAM boot in step 2 still needs TFTP.

## Back to Arista's firmware

- **Once:** stop U-Boot and type `run arista_boot`. It boots Arista from
  slot 1.
- **For good:** from OpenWrt, run `fw_setenv bootcmd 'run ubootscript'`,
  then `reboot`.

## Updating an AP that runs this port

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

## Source

Built from OpenWrt main with the port's patches; the device support is
going upstream as OpenWrt pull requests. The corresponding source for any
image here is available on request: open an issue in this repository.

