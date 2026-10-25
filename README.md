# OpenWrt images for Arista access points

Built images of an OpenWrt port for Arista C-230 and C-260 access points
(Qualcomm IPQ8074, OpenWrt main). Each release has the sysupgrade images
and a `SHA256SUMS` file. Source is not in this repository.

These are for updating an AP that **already runs this port**. An AP still
on Arista's firmware needs the first-install procedure (U-Boot, TFTP RAM
boot, installer), not these files.

## Updating from the AP's console

Check what the AP runs first:

```
cat /tmp/sysinfo/board_name; grep DISTRIB_DESCRIPTION /etc/openwrt_release
```

- `arista,c-230` or `arista,c-260` with `OpenWrt SNAPSHOT`: use the
  commands in the release notes as they are; settings are kept.
- `OpenWrt 25.12`: the older 25.12 port. Use `sysupgrade -n -v`; the port
  names changed, so old settings do not carry over.
- Neither command works: still Arista's firmware. See above.

The commands in each release download the image, refuse to flash unless
the SHA-256 matches, then flash and reboot. The AP is back in about two
minutes; if it is not back after five, power-cycle it.

To check an image without flashing, replace `sysupgrade -v` with
`sysupgrade -T`.

## Source

Built from OpenWrt main with the port's patches. The corresponding
source for any image here is available on request: open an issue in
this repository.
