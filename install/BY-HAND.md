# Installing by hand

**Most people do not need this page.** The [network installer](README.md) does
the whole install from a PC, with no cable, for every model here: the C-200,
C-230, C-260, C-360, C-460, W-118 and W-318. This page is for:

- an AP the network installer cannot reach or log in to;
- anyone who wants to see every step.

It needs a console cable and a TFTP server. The procedure is the same idea on
every model: stop the boot loader, start OpenWrt from memory over TFTP, back up
the flash, write OpenWrt to the first slot, and set the boot command once.

| Model | Its page |
|---|---|
| C-230, C-260 | below |
| C-360 | [c360/INSTALL.md](https://github.com/WiFiGUY512/openwrt-arista/blob/main/c360/INSTALL.md), section B |
| C-460 | [c460/INSTALL.md](https://github.com/WiFiGUY512/openwrt-arista/blob/main/c460/INSTALL.md) |
| C-200 | [c200/INSTALL.md](https://github.com/WiFiGUY512/openwrt-arista/blob/main/c200/INSTALL.md) |
| W-318 | [w318/INSTALL.md](https://github.com/WiFiGUY512/openwrt-arista/blob/main/w318/INSTALL.md) |
| W-118 | [w118/INSTALL.md](https://github.com/WiFiGUY512/openwrt-arista/blob/main/w118/INSTALL.md) (its console is a header inside the case) |

A halfway route: start OpenWrt from memory by hand (steps 1 and 2 below), then
let the network installer do the rest from your PC with
`python arista-field.py --installer-at <the AP's address>`. That works for the
models in the first list of the [README](README.md).

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
- **Steps 4-6:** use release 42 or later for every file (the RAM installer
  in step 2 included): from release 42 on, the C-260's installer,
  `arista-c260-install`, takes `--no-backup` like the C-230's. Steps 5
  and 6 for the C-260 are then:

  ```
  mkdir -p /tmp/c260 && cd /tmp/c260
  wget -O factory.ubi https://github.com/WiFiGUY512/openwrt-arista-images/releases/download/v2026.10.17.1-42-g4949f0e/openwrt-qualcommax-ipq807x-arista_c-260-squashfs-factory.ubi
  wget -O SHA256SUMS https://github.com/WiFiGUY512/openwrt-arista-images/releases/download/v2026.10.17.1-42-g4949f0e/c260-install-SHA256SUMS
  arista-c260-install --dry-run --no-backup --stick /tmp/c260
  arista-c260-install --no-backup --stick /tmp/c260
  ```

  Release 41's C-260 installer has no `--no-backup`. It always takes its
  own backup and needs a FAT32 USB stick (1 GB or more) with a `c260/`
  folder holding `factory.ubi` and `SHA256SUMS`.
- **Before you start:** check which slot Arista runs from, with
  `fw_printenv pri`. `pri=1` is the usual case. With `pri=0`, Arista runs
  from slot 0, which the install replaces, and slot 1 holds an older
  Arista. The installer warns, and goes on only if you confirm.
- **Tested:** C-260 first installs have run on a real unit through
  [`arista-field.py`](install/README.md), which drives these same
  installer commands over the network (2026-10-25, and with release 42 on
  2026-10-27), and so has putting Arista's firmware back. The by-hand
  console route above has not been run end to end on a C-260.

## Back to Arista's firmware

- **Once:** stop U-Boot and type `run arista_boot`. It boots Arista from
  slot 1.
- **For good:** from OpenWrt, run `fw_setenv bootcmd 'run ubootscript'`,
  then `reboot`.

## Last resort: a USB stick

A USB stick does not replace the console cable: Arista's boot loader stops only
for a key string typed on the console, so every stick method still starts
there. It is here for the case where the AP's network cannot be used at all.

Instead of steps 4 and 5, put `factory.ubi` and `SHA256SUMS` in a `c230/`
folder on a FAT32 USB stick with 1 GB free, and plug it in after step 2.
Then run:

```
arista-c230-install --dry-run
arista-c230-install
```

It finds the stick and backs up every partition to it, verified, before it
installs. The RAM boot in step 2 still needs TFTP.

The C-360 has a stick image that carries the installer itself
([c360/INSTALL.md](https://github.com/WiFiGUY512/openwrt-arista/blob/main/c360/INSTALL.md), section A). It has not been used to install a unit.
